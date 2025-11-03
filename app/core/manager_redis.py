"""
Redis-backed ConnectionManager for distributed WebSocket handling.

Features:
- Multi-socket per user
- Broadcast via Redis pub/sub so messages reach sockets connected to any instance
- Room support (join/leave/send)
- Graceful fallback to single-instance/local mode when Redis is unavailable

"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Dict, List, Set, Optional, Callable
from starlette.websockets import WebSocket, WebSocketDisconnect
from app.config import settings

try:
    import aioredis
except Exception as e:
    aioredis = None  

logger = logging.getLogger(__name__)

REDIS_URL = settings.redis_url 
DEFAULT_PREFIX = "civcon_ws"


def _serialize(msg: dict) -> str:
    return json.dumps(msg, default=str)


def _deserialize(s: str) -> dict:
    return json.loads(s)


class DistributedConnectionManager:
    """
    Redis-backed distributed Connection Manager.
    Supports multiple sockets per user, broadcast, direct messaging, and rooms.
    Falls back to local-only mode if Redis is not configured or unavailable.
    """

    def __init__(self, redis_url: Optional[str] = None, prefix: str = DEFAULT_PREFIX):
        self.active_connections: Dict[int, List[WebSocket]] = {}
        self.rooms: Dict[str, Set[int]] = {}
        self._lock = asyncio.Lock()
        self._prefix = prefix or DEFAULT_PREFIX
        self._redis_url = redis_url
        # self._pub: Optional["aioredis.Redis"] = None
        # self._sub: Optional["aioredis.Redis"] = None
        self._sub_task: Optional[asyncio.Task] = None
        self._running = False

        # channels names
        self._global_channel = f"{self._prefix}:global"
        self._direct_channel_fmt = f"{self._prefix}:user:{{user_id}}"
        self._room_channel_fmt = f"{self._prefix}:room:{{room_name}}"

        # optional hook that runs when a pub/sub message arrives (for custom handling)
        self.on_redis_message: Optional[Callable[[dict], None]] = None

    async def start(self):
        """Initialize Redis and start subscriber loop. Safe to call multiple times."""
        if self._running:
            return

        self._running = True
        if not self._redis_url or aioredis is None:
            logger.warning("Redis URL not provided or aioredis not installed — running in single-instance mode.")
            return

        try:
            # Use aioredis.from_url (Redis v4)
            self._pub = aioredis.from_url(self._redis_url, encoding="utf-8", decode_responses=True)
            self._sub = aioredis.from_url(self._redis_url, encoding="utf-8", decode_responses=True)
            # create subscriber
            self._sub_task = asyncio.create_task(self._subscriber_loop())
            logger.info("🔌 Redis connection manager started.")
        except Exception as e:
            logger.exception("Failed to start Redis pub/sub — falling back to local-only mode.")
            self._pub = None
            self._sub = None

    async def stop(self):
        """Stop background tasks and close redis connections."""
        self._running = False
        if self._sub_task:
            self._sub_task.cancel()
            self._sub_task = None
        try:
            if self._pub:
                await self._pub.close()
                self._pub = None
            if self._sub:
                await self._sub.close()
                self._sub = None
        except Exception:
            pass

 
    # Connection management
    async def connect(self, user_id: int, websocket: WebSocket):
        """Accept and register a websocket for user_id (multi-socket supported)."""
        await websocket.accept()
        async with self._lock:
            if user_id not in self.active_connections:
                self.active_connections[user_id] = []
            self.active_connections[user_id].append(websocket)
        logger.info(f"User {user_id} connected (sessions={len(self.active_connections[user_id])}).")

    async def disconnect(self, user_id: int, websocket: Optional[WebSocket] = None):
        """Remove a specific websocket or all websockets for a user."""
        async with self._lock:
            conns = self.active_connections.get(user_id)
            if not conns:
                return
            if websocket:
                try:
                    conns.remove(websocket)
                except ValueError:
                    pass
            else:
                # remove all and close them
                for ws in list(conns):
                    try:
                        await ws.close()
                    except Exception:
                        pass
                conns = []

            if not conns:
                self.active_connections.pop(user_id, None)
                logger.info(f"User {user_id} has no more active sessions and was removed.")

    def is_connected(self, user_id: int) -> bool:
        return user_id in self.active_connections and len(self.active_connections[user_id]) > 0

    
    # Local send helpers
    async def _send_local(self, user_id: int, message: dict):
        """Send a message to all sockets for a user on THIS instance only."""
        conns = self.active_connections.get(user_id, [])
        if not conns:
            return
        text = _serialize(message)
        for ws in list(conns):
            try:
                await ws.send_text(text)
            except WebSocketDisconnect:
                await self.disconnect(user_id, ws)
            except Exception as e:
                logger.warning(f"Failed to send to local ws for user {user_id}: {e}")

    async def _send_room_local(self, room_name: str, message: dict, exclude_user_id: Optional[int] = None):
        members = self.rooms.get(room_name, set())
        for uid in list(members):
            if exclude_user_id and uid == exclude_user_id:
                continue
            await self._send_local(uid, message)

    
    # Public API (send / broadcast)
    async def send_message(self, user_id: int, message: dict):
        """
        Send directly to user across the cluster.
        - Publishes to Redis direct channel if available
        - Also attempts local delivery
        """
        # local send
        await self._send_local(user_id, message)

        # publish over redis for other instances
        if self._pub:
            channel = self._direct_channel_fmt.format(user_id=user_id)
            try:
                await self._pub.publish(channel, _serialize(message))
            except Exception:
                logger.exception("Failed to publish direct message to redis; continuing (local delivery attempted)")

    async def broadcast(self, message: dict, exclude_user_id: Optional[int] = None):
        """
        Broadcast message to every connected user across cluster.
        """
        # local first
        text = _serialize(message)
        for user_id in list(self.active_connections.keys()):
            if exclude_user_id and user_id == exclude_user_id:
                continue
            await self._send_local(user_id, message)

        # publish to global channel for other instances
        if self._pub:
            try:
                await self._pub.publish(self._global_channel, text)
            except Exception:
                logger.exception("Failed to publish global broadcast to redis")

    
    # Rooms
    async def join_room(self, room_name: str, user_id: int):
        """Add user to a named room (local and optionally publish)"""
        async with self._lock:
            if room_name not in self.rooms:
                self.rooms[room_name] = set()
            self.rooms[room_name].add(user_id)

        # notify cluster (so other instances can maintain presence if desired)
        if self._pub:
            payload = {"action": "join_room", "room": room_name, "user_id": user_id}
            try:
                await self._pub.publish(self._global_channel, _serialize(payload))
            except Exception:
                logger.debug("Failed to publish join_room event")

    async def leave_room(self, room_name: str, user_id: int):
        async with self._lock:
            if room_name in self.rooms:
                self.rooms[room_name].discard(user_id)
                if not self.rooms[room_name]:
                    self.rooms.pop(room_name, None)

        if self._pub:
            payload = {"action": "leave_room", "room": room_name, "user_id": user_id}
            try:
                await self._pub.publish(self._global_channel, _serialize(payload))
            except Exception:
                logger.debug("Failed to publish leave_room event")

    async def send_room_message(self, room_name: str, message: dict, exclude_user_id: Optional[int] = None):
        # local delivery
        await self._send_room_local(room_name, message, exclude_user_id=exclude_user_id)

        # publish to redis channel for that room
        if self._pub:
            channel = self._room_channel_fmt.format(room_name=room_name)
            try:
                await self._pub.publish(channel, _serialize({"payload": message, "exclude": exclude_user_id}))
            except Exception:
                logger.exception("Failed to publish room message")


    # Redis subscriber loop
    async def _subscriber_loop(self):
        """Listen for messages on global / user / room channels and handle them."""
        if not self._sub:
            return

        # assemble subscribe patterns
        try:
            pubsub = self._sub.pubsub()
            # subscribe global + user pattern + room pattern
            await pubsub.psubscribe(f"{self._prefix}:*")
            logger.info("Subscribed to redis pattern for distributed WS manager.")
        except Exception as e:
            logger.exception("Failed to subscribe to redis pubsub.")
            return

        try:
            async for raw in pubsub.listen():
                # raw example: {'type': 'pmessage', 'pattern': 'civcon_ws:*', 'channel': 'civcon_ws:user:123', 'data': '...'}
                try:
                    if raw is None:
                        continue
                    mtype = raw.get("type")
                    if mtype not in ("message", "pmessage"):
                        continue
                    channel = raw.get("channel") or raw.get("pattern")
                    data = raw.get("data")
                    if not data:
                        continue
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")

                    # parse JSON
                    try:
                        obj = _deserialize(data)
                    except Exception:
                        # in some cases message might be a plain payload (we'll wrap)
                        obj = {"raw": data}

                    # basic routing: if channel contains ":user:" -> direct; ":room:" -> room; global -> broadcast/raw action
                    if ":user:" in channel:
                        try:
                            # channel like civcon_ws:user:123
                            uid = int(channel.split(":")[-1])
                            # deliver locally
                            await self._send_local(uid, obj)
                        except Exception:
                            logger.exception("Failed to handle direct redis message")
                    elif ":room:" in channel:
                        # channel like civcon_ws:room:roomname
                        room_name = channel.split(":")[-1]
                        payload = obj.get("payload") if isinstance(obj, dict) and "payload" in obj else obj
                        exclude = obj.get("exclude") if isinstance(obj, dict) else None
                        await self._send_room_local(room_name, payload, exclude_user_id=exclude)
                    else:
                        # global: could be actions such as join_room events or generic broadcasts
                        if isinstance(obj, dict) and obj.get("action") in ("join_room", "leave_room"):
                            # optionally update presence maps; we do no-op (rooms are local by default)
                            # but you could implement cross-instance presence if needed
                            logger.debug("Received room presence action via redis: %s", obj)
                        else:
                            # treat as broadcast message
                            await self._broadcast_local_from_redis(obj)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Error while processing redis pubsub message")
        finally:
            try:
                await pubsub.close()
            except Exception:
                pass

    async def _broadcast_local_from_redis(self, payload: dict):
        """Deliver a payload to all local connections (called from subscriber loop)."""
        for uid in list(self.active_connections.keys()):
            await self._send_local(uid, payload)


# Single global instance (import this)
_default_manager: Optional[DistributedConnectionManager] = None


def get_manager(redis_url: Optional[str] = None, prefix: str = DEFAULT_PREFIX) -> DistributedConnectionManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = DistributedConnectionManager(redis_url=redis_url, prefix=prefix)
    return _default_manager
