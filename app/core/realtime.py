"""
Unified WebSocket connection manager for the CIV-CON backend.

Replaces the previous pair (`app.core.manager.ConnectionManager` +
`app.core.manager_redis.DistributedConnectionManager`) with a
single class hierarchy:

- `ConnectionManager` — in-memory dict, broadcast + room support.
- `DistributedConnectionManager(ConnectionManager)` — same API,
  with Redis pub/sub on `civcon_ws:user:{id}`, `civcon_ws:room:{name}`,
  and `civcon_ws:global` channels so messages reach sockets
  connected to any worker.

Features preserved from the old implementations:
- Multi-socket per user.
- `send_message`, `broadcast`, `send_room_message`, `join_room`,
  `leave_room`, `is_connected`, `active_user_count`.
- Graceful fallback to local-only mode if Redis is unavailable.

New behaviour:
- Backwards-compat aliases `send_json` and `send_personal_message`
  so legacy call sites in `app/services/notifications.py` and
  `app/routers/admin_communication.py` resolve. New code should
  use `send_message` directly.
- Per-socket envelope wrapper (`{id, type, ts, data}`) — the
  legacy payload lives in `data`, so consumers that read the
  inner shape keep working. Consumers can opt in to dedupe-by-id.
- Per-socket heartbeat tracked via `last_pong_at`. A single
  background task (`heartbeat_loop`) iterates every connected
  socket and closes any socket that has not ponged within the
  configured timeout.
- Per-socket outgoing `asyncio.Queue` with a `maxsize` so a slow
  consumer cannot stall the broadcast loop.

Usage
-----
A single process-wide instance is created via
`get_connection_manager()`. All routers should call this at
request time (never cache the result in a module-level constant):
    from app.core.realtime import get_connection_manager
    manager = get_connection_manager()
    await manager.send_message(user_id, payload)

Startup wiring (`app/main.py` lifespan):
    manager = get_connection_manager()
    await manager.start()  # no-op if no REDIS_URL

Shutdown wiring:
    await manager.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from app.observability.metrics import (
    ws_connections,
    ws_connections_total,
    ws_messages_total,
    ws_reconnect_total,
)

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")  # Fallback; main.py passes via get_connection_manager
DEFAULT_PREFIX = "civcon_ws"
DEFAULT_QUEUE_SIZE = 100
HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_TIMEOUT_S = 60.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ─────────────────────────────────────────────────────────────────
# Envelope
# ─────────────────────────────────────────────────────────────────


@dataclass
class WSMessage:
    """Server-to-client envelope.

    `data` carries the legacy payload so existing consumers that
    read the inner shape (`{"type": "...", "message": "..."}`)
    keep working unchanged. `id` is a per-frame UUID4 used by the
    client to dedupe replays after reconnect; consumers that
    don't care about dedupe can ignore it.
    """
    type: str
    data: Any
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "ts": self.ts, "data": self.data}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


def wrap(data: Any, type: str = "envelope") -> WSMessage:
    """Build an envelope around an arbitrary `data` payload.

    The default `type` is the envelope marker; callers building a
    domain message (notification, message, ping) can override.
    """
    return WSMessage(type=type, data=data)


# ─────────────────────────────────────────────────────────────────
# Per-socket record
# ─────────────────────────────────────────────────────────────────


@dataclass
class _SocketRecord:
    """Per-socket bookkeeping (heartbeat, queue, sender task)."""
    websocket: WebSocket
    user_id: int
    route: str
    queue: asyncio.Queue
    last_pong_at: float
    sender_task: asyncio.Task | None = None

    def __init__(
        self,
        websocket: WebSocket,
        user_id: int,
        route: str,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ):
        self.websocket = websocket
        self.user_id = user_id
        self.route = route
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.last_pong_at = asyncio.get_event_loop().time()


# ─────────────────────────────────────────────────────────────────
# ConnectionManager (in-memory)
# ─────────────────────────────────────────────────────────────────


class ConnectionManager:
    """In-memory WebSocket connection manager.

    Direct subclass for single-instance deployments. Use
    `DistributedConnectionManager` for multi-worker setups; that
    class extends this one with Redis pub/sub.
    """

    def __init__(self) -> None:
        # user_id -> list of records (multi-socket per user)
        self.active_connections: dict[int, list[_SocketRecord]] = {}
        # room_name -> set of user_ids
        self.rooms: dict[str, set[int]] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_stop = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background heartbeat task. Idempotent."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="ws-heartbeat",
        )

    async def stop(self) -> None:
        """Stop the background heartbeat task and close all sockets."""
        self._heartbeat_stop.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
        # Close all sockets cleanly.
        for user_id in list(self.active_connections.keys()):
            await self.disconnect(user_id)

    # ── connect / disconnect ────────────────────────────────────

    async def connect(
        self,
        user_id: int,
        websocket: WebSocket,
        route: str = "unknown",
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> _SocketRecord:
        """Accept the WebSocket and register it for the user.

        Starts a per-socket sender task that drains the outgoing
        queue, so broadcast loops don't block on a slow consumer.
        """
        await websocket.accept()
        record = _SocketRecord(
            websocket=websocket,
            user_id=user_id,
            route=route,
            queue_size=queue_size,
        )
        async with self._lock:
            self.active_connections.setdefault(user_id, []).append(record)
        record.sender_task = asyncio.create_task(
            self._sender_loop(record),
            name=f"ws-sender-{user_id}",
        )
        # Observability.
        try:
            ws_connections.labels(route=route).inc()
            ws_connections_total.labels(route=route, event="connect").inc()
        except Exception:
            pass
        logger.info(
            "User %s connected to %s (sessions=%d).",
            user_id, route, len(self.active_connections[user_id]),
        )
        return record

    async def disconnect(
        self,
        user_id: int,
        websocket: WebSocket | None = None,
    ) -> None:
        """Disconnect a specific socket (or every socket for the user)."""
        async with self._lock:
            conns = self.active_connections.get(user_id)
            if not conns:
                return
            if websocket is None:
                targets = list(conns)
            else:
                targets = [r for r in conns if r.websocket is websocket]
                if not targets:
                    return
            for record in targets:
                # Cancel sender task
                if record.sender_task and not record.sender_task.done():
                    record.sender_task.cancel()
                # Close socket
                try:
                    if record.websocket.client_state != WebSocketState.DISCONNECTED:
                        await record.websocket.close()
                except Exception:
                    pass
                conns.remove(record)
                # Observability.
                try:
                    ws_connections.labels(route=record.route).dec()
                    ws_connections_total.labels(
                        route=record.route, event="disconnect",
                    ).inc()
                except Exception:
                    pass
            if not conns:
                self.active_connections.pop(user_id, None)

    def is_connected(self, user_id: int) -> bool:
        return bool(self.active_connections.get(user_id))

    def active_user_count(self) -> int:
        return len(self.active_connections)

    def all_active_users(self) -> list[int]:
        return list(self.active_connections.keys())

    # ── rooms ────────────────────────────────────────────────────

    async def join_room(self, room_name: str, user_id: int) -> None:
        async with self._lock:
            self.rooms.setdefault(room_name, set()).add(user_id)
        logger.debug("User %s joined room %s.", user_id, room_name)

    async def leave_room(self, room_name: str, user_id: int) -> None:
        async with self._lock:
            members = self.rooms.get(room_name)
            if not members:
                return
            members.discard(user_id)
            if not members:
                self.rooms.pop(room_name, None)
        logger.debug("User %s left room %s.", user_id, room_name)

    def room_user_count(self, room_name: str) -> int:
        return len(self.rooms.get(room_name, set()))

    # ── send ─────────────────────────────────────────────────────

    async def send_message(self, user_id: int, message: Any) -> None:
        """Send a payload to every socket belonging to the user.

        `message` may be:
          - a `WSMessage` (already an envelope)
          - a dict (auto-wrapped via `wrap()`)
          - a JSON string (sent verbatim)
        """
        conns = list(self.active_connections.get(user_id, []))
        if not conns:
            return
        for record in conns:
            await self._enqueue(record, message)

    async def send_json(
        self, user_id: int, message: Any, type: str = "envelope"
    ) -> None:
        """Backwards-compat alias for `send_message`.

        Pre-existing call sites in `app/services/notifications.py`
        and `app/routers/admin_communication.py` were calling
        `send_json` / `send_personal_message`, neither of which
        existed on the old `ConnectionManager`. This alias keeps
        them working through the migration.
        """
        await self.send_message(user_id, message)

    async def send_personal_message(
        self, message: Any, user_id: int
    ) -> None:
        """Backwards-compat alias. The legacy signature was
        `send_personal_message(message, user_id)` (note the order).
        Kept verbatim so admin_communication.py works unchanged.
        """
        await self.send_message(user_id, message)

    async def broadcast(
        self,
        message: Any,
        exclude_user_id: int | None = None,
    ) -> None:
        """Send a payload to every connected user."""
        for user_id in list(self.active_connections.keys()):
            if exclude_user_id is not None and user_id == exclude_user_id:
                continue
            await self.send_message(user_id, message)

    async def send_room_message(
        self,
        room_name: str,
        message: Any,
        exclude_user_id: int | None = None,
    ) -> None:
        """Send a payload to every member of a room."""
        members = list(self.rooms.get(room_name, set()))
        for user_id in members:
            if exclude_user_id is not None and user_id == exclude_user_id:
                continue
            await self.send_message(user_id, message)

    # ── heartbeat ────────────────────────────────────────────────

    def mark_pong(self, user_id: int) -> None:
        """Update the `last_pong_at` for every socket of the user."""
        now = asyncio.get_event_loop().time()
        for record in self.active_connections.get(user_id, []):
            record.last_pong_at = now

    async def _heartbeat_loop(self) -> None:
        """Single background task that sweeps all sockets.

        Sends a `ping` envelope every `HEARTBEAT_INTERVAL_S`
        seconds and closes any socket whose `last_pong_at` is
        older than `HEARTBEAT_TIMEOUT_S` seconds. A single task
        (rather than one per socket) keeps the per-connection
        overhead constant regardless of fanout.
        """
        while not self._heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._heartbeat_stop.wait(),
                    timeout=HEARTBEAT_INTERVAL_S,
                )
                # Stop event fired.
                break
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            # Sweep.
            now = asyncio.get_event_loop().time()
            stale: list[tuple[int, _SocketRecord]] = []
            for uid, conns in list(self.active_connections.items()):
                for record in list(conns):
                    age = now - record.last_pong_at
                    if age > HEARTBEAT_TIMEOUT_S:
                        stale.append((uid, record))
                    else:
                        # Send a ping (envelope).
                        try:
                            await self._enqueue(
                                record,
                                WSMessage(type="ping", data={}),
                            )
                        except Exception:
                            logger.debug(
                                "Failed to enqueue ping for user %s.", uid,
                            )
            for uid, record in stale:
                logger.warning(
                    "Closing dead WS for user %s on route %s "
                    "(no pong for %.0fs)",
                    uid, record.route, now - record.last_pong_at,
                )
                try:
                    ws_reconnect_total.labels(
                        route=record.route, reason="heartbeat_timeout",
                    ).inc()
                except Exception:
                    pass
                await self.disconnect(uid, record.websocket)

    # ── internals ─────────────────────────────────────────────────

    async def _enqueue(self, record: _SocketRecord, message: Any) -> None:
        """Push a message onto a socket's outgoing queue.

        If the queue is full (slow consumer), drop the oldest
        queued message and log a warning. This prevents a stuck
        client from blocking the broadcast loop indefinitely.
        """
        envelope = self._coerce(message)
        try:
            record.queue.put_nowait(envelope)
        except asyncio.QueueFull:
            # Drop the oldest, then enqueue.
            try:
                _ = record.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            logger.warning(
                "Slow consumer on user %s route %s — dropped oldest frame.",
                record.user_id, record.route,
            )
            try:
                record.queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # Still full after drop; give up.
                logger.error(
                    "Dropping frame for user %s route %s — queue still full.",
                    record.user_id, record.route,
                )

    @staticmethod
    def _coerce(message: Any) -> WSMessage:
        if isinstance(message, WSMessage):
            return message
        if isinstance(message, dict):
            # If the dict already has the envelope shape, pass through.
            if {"id", "type", "ts", "data"}.issubset(message.keys()):
                return WSMessage(
                    id=message["id"],
                    type=message["type"],
                    ts=message.get("ts") or _now_iso(),
                    data=message["data"],
                )
            # Legacy payload: if it has a `type` field, keep it
            # as the envelope `type` so consumers reading
            # `envelope.type` see the same value as before.
            t = str(message.get("type", "envelope"))
            return WSMessage(type=t, data=message)
        if isinstance(message, str):
            return WSMessage(type="raw", data=message)
        return WSMessage(type="envelope", data=message)

    async def _sender_loop(self, record: _SocketRecord) -> None:
        """Drain a socket's outgoing queue.

        Reads envelopes from the queue and sends them as JSON
        text frames. Sleeps on empty queues.
        """
        try:
            while True:
                envelope = await record.queue.get()
                try:
                    await record.websocket.send_text(envelope.to_json())
                except WebSocketDisconnect:
                    return
                except Exception as exc:
                    logger.warning(
                        "Send failed for user %s route %s: %s",
                        record.user_id, record.route, exc,
                    )
                    return
                # Observability: count outbound frames.
                try:
                    ws_messages_total.labels(
                        route=record.route, direction="out",
                    ).inc()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass


# ─────────────────────────────────────────────────────────────────
# DistributedConnectionManager (Redis pub/sub)
# ─────────────────────────────────────────────────────────────────


class DistributedConnectionManager(ConnectionManager):
    """Multi-worker fanout via Redis pub/sub.

    On `send_message`/`broadcast`/`send_room_message`, publishes
    the envelope to a Redis channel (`civcon_ws:user:{id}`,
    `civcon_ws:room:{name}`, or `civcon_ws:global`). A subscriber
    loop on each worker picks up the message and delivers it to
    any locally-connected sockets.

    Falls back silently to local-only mode if Redis is unavailable.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        prefix: str = DEFAULT_PREFIX,
    ) -> None:
        super().__init__()
        self._prefix = prefix or DEFAULT_PREFIX
        self._redis_url = redis_url
        self._pub = None
        self._sub = None
        self._sub_task: asyncio.Task | None = None

        self._global_channel = f"{self._prefix}:global"
        self._direct_channel_fmt = f"{self._prefix}:user:{{user_id}}"
        self._room_channel_fmt = f"{self._prefix}:room:{{room_name}}"

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start heartbeat and Redis subscriber. Safe to call twice."""
        await super().start()
        if self._sub_task and not self._sub_task.done():
            return
        if not self._redis_url:
            logger.info(
                "DistributedConnectionManager: no REDIS_URL — local-only mode.",
            )
            return
        try:
            import redis.asyncio as redis_asyncio  # type: ignore
        except Exception as exc:
            logger.warning(
                "redis.asyncio unavailable (%s) — local-only mode.", exc,
            )
            return
        try:
            self._pub = redis_asyncio.from_url(
                self._redis_url, encoding="utf-8", decode_responses=True,
            )
            self._sub = redis_asyncio.from_url(
                self._redis_url, encoding="utf-8", decode_responses=True,
            )
            self._sub_task = asyncio.create_task(
                self._subscriber_loop(), name="ws-redis-sub",
            )
            logger.info("DistributedConnectionManager: Redis pub/sub started.")
        except Exception:
            logger.exception(
                "Failed to start Redis pub/sub — falling back to local-only.",
            )
            self._pub = None
            self._sub = None

    async def stop(self) -> None:
        if self._sub_task:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sub_task = None
        for client in (self._pub, self._sub):
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception:
                pass
        self._pub = None
        self._sub = None
        await super().stop()

    # ── overrides ────────────────────────────────────────────────

    async def send_message(self, user_id: int, message: Any) -> None:
        envelope = self._coerce(message)
        # Local delivery.
        await super().send_message(user_id, envelope)
        # Cross-worker fanout.
        if self._pub is not None:
            channel = self._direct_channel_fmt.format(user_id=user_id)
            try:
                await self._pub.publish(channel, envelope.to_json())
            except Exception:
                logger.exception(
                    "Failed to publish direct message to Redis.",
                )

    async def broadcast(
        self,
        message: Any,
        exclude_user_id: int | None = None,
    ) -> None:
        envelope = self._coerce(message)
        # Local delivery: deliver each user's view (no `exclude`
        # needed locally because we iterate the user table directly).
        for uid in list(self.active_connections.keys()):
            if exclude_user_id is not None and uid == exclude_user_id:
                continue
            await super().send_message(uid, envelope)
        if self._pub is not None:
            try:
                payload = envelope.to_json()
                # Cross-worker fanout encodes the exclude list inline.
                if exclude_user_id is not None:
                    payload_obj = json.loads(payload)
                    payload_obj.setdefault("meta", {})["exclude_user_id"] = exclude_user_id
                    payload = json.dumps(payload_obj, default=str)
                await self._pub.publish(self._global_channel, payload)
            except Exception:
                logger.exception("Failed to publish broadcast to Redis.")

    async def send_room_message(
        self,
        room_name: str,
        message: Any,
        exclude_user_id: int | None = None,
    ) -> None:
        envelope = self._coerce(message)
        await super().send_room_message(room_name, envelope, exclude_user_id)
        if self._pub is not None:
            channel = self._room_channel_fmt.format(room_name=room_name)
            try:
                # Wrap the envelope in a transport envelope so
                # subscriber knows to deliver via the room API.
                transport = {
                    "envelope": envelope.to_dict(),
                    "exclude": exclude_user_id,
                }
                await self._pub.publish(channel, json.dumps(transport, default=str))
            except Exception:
                logger.exception("Failed to publish room message to Redis.")

    # ── subscriber loop ──────────────────────────────────────────

    async def _subscriber_loop(self) -> None:
        if self._sub is None:
            return
        try:
            pubsub = self._sub.pubsub()
            await pubsub.psubscribe(f"{self._prefix}:*")
        except Exception:
            logger.exception("Failed to subscribe to Redis pubsub.")
            return

        try:
            async for raw in pubsub.listen():
                try:
                    if not raw:
                        continue
                    mtype = raw.get("type")
                    if mtype not in ("message", "pmessage"):
                        continue
                    channel = raw.get("channel") or raw.get("pattern") or ""
                    data = raw.get("data")
                    if not data:
                        continue
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    await self._dispatch(channel, data)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Error while processing redis message.")
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                pass

    async def _dispatch(self, channel: str, data: str) -> None:
        """Route an incoming Redis message to the local workers."""
        try:
            obj = json.loads(data)
        except Exception:
            return
        # Direct user channel.
        if ":user:" in channel:
            try:
                uid = int(channel.split(":")[-1])
                env = obj if isinstance(obj, dict) and "id" in obj else wrap(obj)
                await super().send_message(uid, env)
            except Exception:
                logger.exception("Failed to handle direct redis message.")
            return
        # Room channel.
        if ":room:" in channel:
            room_name = channel.split(":")[-1]
            try:
                # `send_room_message` wraps in `{envelope, exclude}`.
                env_obj = obj.get("envelope") if isinstance(obj, dict) else obj
                exclude = obj.get("exclude") if isinstance(obj, dict) else None
                env = (
                    WSMessage(**env_obj) if isinstance(env_obj, dict) and "type" in env_obj
                    else wrap(env_obj)
                )
                await super().send_room_message(room_name, env, exclude_user_id=exclude)
            except Exception:
                logger.exception("Failed to handle room redis message.")
            return
        # Global channel — broadcast (or apply per-message exclude).
        try:
            exclude = None
            if isinstance(obj, dict):
                meta = obj.get("meta") or {}
                exclude = meta.get("exclude_user_id")
                env = (
                    WSMessage(**{k: v for k, v in obj.items() if k != "meta"})
                    if {"id", "type", "ts", "data"}.issubset(obj.keys())
                    else wrap(obj)
                )
            else:
                env = wrap(obj)
            await super().broadcast(env, exclude_user_id=exclude)
        except Exception:
            logger.exception("Failed to handle global redis message.")


# ─────────────────────────────────────────────────────────────────
# Process-wide singleton
# ─────────────────────────────────────────────────────────────────


_default_manager: ConnectionManager | None = None


def get_connection_manager(
    redis_url: str | None = None,
    prefix: str = DEFAULT_PREFIX,
) -> ConnectionManager:
    """Return the process-wide connection manager.

    The first call constructs a `DistributedConnectionManager` if
    `redis_url` is provided, otherwise a plain `ConnectionManager`.
    Subsequent calls return the cached instance (the `redis_url`
    and `prefix` parameters are ignored after the first call —
    this is intentional, so misconfiguration late in startup can't
    split the manager identity across routers).
    """
    global _default_manager
    if _default_manager is None:
        if redis_url:
            _default_manager = DistributedConnectionManager(
                redis_url=redis_url, prefix=prefix,
            )
        else:
            _default_manager = ConnectionManager()
    return _default_manager


def reset_connection_manager() -> None:
    """Reset the cached singleton. Test-only helper."""
    global _default_manager
    _default_manager = None
