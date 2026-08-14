"""Tests for the unified WebSocket connection manager.

Covers:
- Envelope shape (`id`, `type`, `ts`, `data`).
- `connect` / `disconnect` / `send_message` / `broadcast` /
  `send_room_message` lifecycle.
- Heartbeat: a stale socket is closed by the heartbeat loop.
- Auth helper: missing/invalid token raises the right
  `WebSocketAuthError` and the helper exposes the documented
  close codes.
- Slow-consumer policy: the per-socket queue drops the oldest
  frame when full instead of blocking the broadcast loop.
- Multi-socket-per-user: a single `user_id` may have several
  active sockets; `send_message` reaches all of them.

We intentionally do NOT test the Redis cross-worker path here —
that requires a live Redis and is exercised manually under
``local — backend multi-worker`` in the plan's Verification
section. The local fanout paths (room broadcast, direct send,
global broadcast) ARE covered.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from app.core import realtime
from app.core.realtime import (
    ConnectionManager,
    DistributedConnectionManager,
    get_connection_manager,
    reset_connection_manager,
    wrap,
)
from app.core.ws_auth import (
    WS_CLOSE_CODES,
    WebSocketAuthError,
    authenticate_ws,
    close_ws_with_auth_error,
)

# ─────────────────────────────────────────────────────────────────
# Envelope
# ─────────────────────────────────────────────────────────────────


def test_envelope_shape_round_trip():
    """Every outbound frame carries id/type/ts/data."""
    msg = wrap({"hello": "world"}, type="greeting")
    d = msg.to_dict()
    assert {"id", "type", "ts", "data"}.issubset(d.keys())
    assert d["type"] == "greeting"
    assert d["data"] == {"hello": "world"}
    assert json.loads(msg.to_json()) == d


def test_envelope_id_is_unique():
    """Two envelopes built back-to-back have different IDs."""
    a = wrap({"x": 1})
    b = wrap({"x": 1})
    assert a.id != b.id


def test_wrap_default_type():
    """`wrap` defaults the envelope `type` to ``"envelope"``."""
    msg = wrap({"foo": 1})
    assert msg.type == "envelope"


# ─────────────────────────────────────────────────────────────────
# Manager lifecycle
# ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def fresh_manager():
    """A clean ConnectionManager per test (no heartbeat loop)."""
    m = ConnectionManager()
    yield m
    # Tear down any sockets created during the test.
    for uid in list(m.all_active_users()):
        await m.disconnect(uid)
    await m.stop()


class _FakeWebSocket:
    """Minimal WebSocket double used by manager unit tests.

    The real `WebSocket` requires a server-side upgrade which is
    expensive to set up in a unit test. The manager only touches
    `accept()`, `send_text()`, `close()`, and `client_state`, so
    we can model just those.
    """

    STATE_CONNECTING = 0
    STATE_OPEN = 1
    STATE_CLOSING = 2
    STATE_DISCONNECTED = 3

    def __init__(self):
        self.accepted = False
        self.sent: list[str] = []
        self.closed = False
        self.client_state = self.STATE_CONNECTING
        self.close_code: int | None = None

    async def accept(self):
        self.accepted = True
        self.client_state = self.STATE_OPEN

    async def send_text(self, text: str):
        if self.closed:
            raise RuntimeError("send after close")
        self.sent.append(text)

    async def close(self, code: int = 1000):
        self.closed = True
        self.client_state = self.STATE_DISCONNECTED
        self.close_code = code


@pytest.mark.asyncio
async def test_connect_marks_socket_accepted_and_registers(fresh_manager):
    ws = _FakeWebSocket()
    rec = await fresh_manager.connect(user_id=1, websocket=ws, route="/test")
    assert ws.accepted
    assert fresh_manager.is_connected(1)
    assert fresh_manager.active_user_count() == 1
    assert rec.route == "/test"
    assert rec.user_id == 1


@pytest.mark.asyncio
async def test_disconnect_removes_user(fresh_manager):
    ws = _FakeWebSocket()
    await fresh_manager.connect(1, ws, route="/test")
    await fresh_manager.disconnect(1, ws)
    assert not fresh_manager.is_connected(1)
    assert ws.closed


@pytest.mark.asyncio
async def test_multi_socket_per_user(fresh_manager):
    """A user may have several sockets; send_message hits all."""
    a = _FakeWebSocket()
    b = _FakeWebSocket()
    await fresh_manager.connect(7, a, route="/r")
    await fresh_manager.connect(7, b, route="/r")
    assert fresh_manager.active_user_count() == 1  # one user
    assert len(fresh_manager.active_connections[7]) == 2

    await fresh_manager.send_message(7, wrap({"hi": True}, type="x"))
    # Give sender tasks a moment to drain.
    await asyncio.sleep(0.05)
    assert len(a.sent) == 1
    assert len(b.sent) == 1
    assert "hi" in a.sent[0]


@pytest.mark.asyncio
async def test_send_message_envelopes_legacy_dict(fresh_manager):
    """A bare dict becomes an envelope with the dict as `data`."""
    ws = _FakeWebSocket()
    await fresh_manager.connect(2, ws, route="/t")
    await fresh_manager.send_message(2, {"type": "notification", "text": "hi"})
    await asyncio.sleep(0.05)
    payload = json.loads(ws.sent[0])
    assert payload["type"] == "notification"
    assert payload["data"]["text"] == "hi"
    assert "id" in payload and "ts" in payload


@pytest.mark.asyncio
async def test_send_message_to_unknown_user_is_noop(fresh_manager):
    """No exception, just silent skip."""
    await fresh_manager.send_message(999, wrap({"x": 1}))


@pytest.mark.asyncio
async def test_broadcast_reaches_every_user(fresh_manager):
    a = _FakeWebSocket()
    b = _FakeWebSocket()
    await fresh_manager.connect(1, a, route="/t")
    await fresh_manager.connect(2, b, route="/t")
    await fresh_manager.broadcast(wrap({"announcement": "ping"}))
    await asyncio.sleep(0.05)
    assert len(a.sent) == 1
    assert len(b.sent) == 1


@pytest.mark.asyncio
async def test_broadcast_excludes_user(fresh_manager):
    a = _FakeWebSocket()
    b = _FakeWebSocket()
    await fresh_manager.connect(1, a, route="/t")
    await fresh_manager.connect(2, b, route="/t")
    await fresh_manager.broadcast(wrap({"x": 1}), exclude_user_id=1)
    await asyncio.sleep(0.05)
    assert a.sent == []
    assert len(b.sent) == 1


# ─────────────────────────────────────────────────────────────────
# Rooms
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_room_broadcast_targets_only_members(fresh_manager):
    a = _FakeWebSocket()
    b = _FakeWebSocket()
    await fresh_manager.connect(1, a, route="/t")
    await fresh_manager.connect(2, b, route="/t")
    await fresh_manager.join_room("chat:42", 1)
    await fresh_manager.send_room_message("chat:42", wrap({"hi": 1}))
    await asyncio.sleep(0.05)
    assert len(a.sent) == 1
    assert b.sent == []


@pytest.mark.asyncio
async def test_room_leave_removes_membership(fresh_manager):
    a = _FakeWebSocket()
    await fresh_manager.connect(1, a, route="/t")
    await fresh_manager.join_room("chat:42", 1)
    assert fresh_manager.room_user_count("chat:42") == 1
    await fresh_manager.leave_room("chat:42", 1)
    assert fresh_manager.room_user_count("chat:42") == 0


@pytest.mark.asyncio
async def test_room_excludes_sender(fresh_manager):
    a = _FakeWebSocket()
    b = _FakeWebSocket()
    await fresh_manager.connect(1, a, route="/t")
    await fresh_manager.connect(2, b, route="/t")
    await fresh_manager.join_room("chat:42", 1)
    await fresh_manager.join_room("chat:42", 2)
    await fresh_manager.send_room_message(
        "chat:42", wrap({"hi": 1}), exclude_user_id=1,
    )
    await asyncio.sleep(0.05)
    assert a.sent == []
    assert len(b.sent) == 1


# ─────────────────────────────────────────────────────────────────
# Backwards-compat aliases
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_json_and_send_personal_message_are_aliases(fresh_manager):
    ws = _FakeWebSocket()
    await fresh_manager.connect(3, ws, route="/t")
    await fresh_manager.send_json(3, {"type": "n", "x": 1})
    # Legacy `send_personal_message(message, user_id)` signature.
    await fresh_manager.send_personal_message({"type": "n", "x": 2}, 3)
    await asyncio.sleep(0.05)
    assert len(ws.sent) == 2


# ─────────────────────────────────────────────────────────────────
# Slow-consumer / queue policy
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slow_consumer_drops_oldest_frame(fresh_manager):
    """A full outgoing queue drops the oldest frame instead of
    blocking the broadcaster. With maxsize=2 and 3 messages sent
    back-to-back, the first message is dropped and the last two
    land on the socket.
    """
    ws = _FakeWebSocket()
    # Tiny queue so we can saturate it deterministically.
    await fresh_manager.connect(5, ws, route="/t", queue_size=2)

    # Pause the sender task by NOT awaiting anything — just
    # pile up the queue.
    await fresh_manager.send_message(5, wrap({"n": 1}))
    await fresh_manager.send_message(5, wrap({"n": 2}))
    # Third send triggers QueueFull → drop oldest.
    await fresh_manager.send_message(5, wrap({"n": 3}))

    rec = fresh_manager.active_connections[5][0]
    # Queue should hold the two newest frames (n=2 and n=3).
    queued = []
    while not rec.queue.empty():
        queued.append(rec.queue.get_nowait().data)
    assert [q["n"] for q in queued] == [2, 3]


# ─────────────────────────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_closes_dead_socket(monkeypatch):
    """A socket that never pongs gets closed by the heartbeat loop.

    We patch `HEARTBEAT_INTERVAL_S` and `HEARTBEAT_TIMEOUT_S` to
    tiny values so the test runs in well under a second.
    """
    monkeypatch.setattr(realtime, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(realtime, "HEARTBEAT_TIMEOUT_S", 0.1)

    m = ConnectionManager()
    ws = _FakeWebSocket()
    await m.connect(42, ws, route="/hb")
    # Force the socket to look stale: rewind `last_pong_at` by 1s.
    rec = m.active_connections[42][0]
    rec.last_pong_at = asyncio.get_event_loop().time() - 1.0

    await m.start()
    # Wait long enough for the heartbeat loop to sweep.
    await asyncio.sleep(0.3)
    await m.stop()

    assert ws.closed
    assert not m.is_connected(42)


@pytest.mark.asyncio
async def test_heartbeat_keeps_fresh_socket_open(monkeypatch):
    """A recently-active socket is NOT closed."""
    monkeypatch.setattr(realtime, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(realtime, "HEARTBEAT_TIMEOUT_S", 0.5)

    m = ConnectionManager()
    ws = _FakeWebSocket()
    await m.connect(8, ws, route="/hb")
    await m.start()
    await asyncio.sleep(0.15)
    await m.stop()
    # Fresh socket never went stale; should still be open.
    assert not ws.closed
    assert m.is_connected(8)


# ─────────────────────────────────────────────────────────────────
# Distributed manager (no Redis → local-only fallback)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_distributed_manager_without_redis_url_starts_in_local_mode():
    """No REDIS_URL → no Redis subscriber, but still works locally."""
    m = DistributedConnectionManager(redis_url=None)
    await m.start()
    ws = _FakeWebSocket()
    await m.connect(1, ws, route="/t")
    await m.send_message(1, wrap({"hi": 1}))
    await asyncio.sleep(0.05)
    assert len(ws.sent) == 1
    await m.stop()
    assert not m.is_connected(1)


# ─────────────────────────────────────────────────────────────────
# Singleton factory
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_connection_manager_returns_singleton(monkeypatch):
    """First call wins; subsequent args ignored."""
    reset_connection_manager()
    a = get_connection_manager()
    b = get_connection_manager()
    assert a is b
    # Second call with redis_url should still return the local one.
    c = get_connection_manager(redis_url="redis://x")
    assert c is a
    reset_connection_manager()


# ─────────────────────────────────────────────────────────────────
# Auth helper
# ─────────────────────────────────────────────────────────────────


def test_ws_auth_close_codes_are_documented():
    """All four expected close codes exist on the helper."""
    assert WS_CLOSE_CODES["missing_token"] == 4401
    assert WS_CLOSE_CODES["invalid_token"] == 4401
    assert WS_CLOSE_CODES["forbidden"] == 4403
    assert WS_CLOSE_CODES["internal_error"] == 1011


@pytest.mark.asyncio
async def test_authenticate_ws_rejects_missing_token():
    """No token → WebSocketAuthError(missing_token)."""
    with pytest.raises(WebSocketAuthError) as exc:
        await authenticate_ws(None, db=None)  # type: ignore[arg-type]
    assert exc.value.code == "missing_token"


@pytest.mark.asyncio
async def test_authenticate_ws_rejects_invalid_token():
    """Garbage token → WebSocketAuthError(invalid_token)."""
    with pytest.raises(WebSocketAuthError) as exc:
        await authenticate_ws("not.a.real.token", db=None)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_token"


@pytest.mark.asyncio
async def test_close_ws_with_auth_error_uses_documented_code():
    ws = _FakeWebSocket()
    await close_ws_with_auth_error(
        ws, WebSocketAuthError("nope", code="forbidden"),
    )
    assert ws.closed
    assert ws.close_code == WS_CLOSE_CODES["forbidden"]


@pytest.mark.asyncio
async def test_close_ws_with_auth_error_falls_back_to_1011_on_unknown_code():
    ws = _FakeWebSocket()
    await close_ws_with_auth_error(
        ws, WebSocketAuthError("oops", code="something_weird"),
    )
    assert ws.close_code == 1011
