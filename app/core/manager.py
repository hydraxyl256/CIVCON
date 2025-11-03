from typing import Dict, List, Set
from starlette.websockets import WebSocket, WebSocketDisconnect
import json
import logging
import asyncio

logger = logging.getLogger(__name__)

class ConnectionManager:
    """
    Production-grade connection manager supporting:
       multiple concurrent connections per user
       broadcast messaging
       private DM messaging
       group/channel broadcasting
    """

    def __init__(self):
        # user_id -> list of WebSocket connections
        self.active_connections: Dict[int, List[WebSocket]] = {}

        # room_name -> set of user_ids
        self.rooms: Dict[str, Set[int]] = {}

        # lock to prevent race conditions on connection management
        self._lock = asyncio.Lock()

    
    # User Connection Management
    async def connect(self, user_id: int, websocket: WebSocket):
        """
        Connect a new WebSocket for a given user.
        Supports multiple active sockets per user.
        """
        await websocket.accept()
        async with self._lock:
            if user_id not in self.active_connections:
                self.active_connections[user_id] = []
            self.active_connections[user_id].append(websocket)
        logger.info(f"🟢 User {user_id} connected ({len(self.active_connections[user_id])} sessions active).")

    async def disconnect(self, user_id: int, websocket: WebSocket | None = None):
        """
        Disconnects either a specific socket or all sockets for a user.
        """
        async with self._lock:
            if user_id not in self.active_connections:
                return
            if websocket:
                try:
                    self.active_connections[user_id].remove(websocket)
                    logger.info(f"🔴 WebSocket for user {user_id} disconnected (remaining: {len(self.active_connections[user_id])})")
                except ValueError:
                    pass
            else:
                # remove all sockets
                for ws in self.active_connections[user_id]:
                    await ws.close()
                self.active_connections.pop(user_id, None)
                logger.info(f"🔴 User {user_id} fully disconnected.")
            
            # cleanup if empty
            if not self.active_connections.get(user_id):
                self.active_connections.pop(user_id, None)

    def is_connected(self, user_id: int) -> bool:
        return user_id in self.active_connections and len(self.active_connections[user_id]) > 0


    # Messaging Functions
    async def send_message(self, user_id: int, message: dict):
        """
        Send a JSON message to a specific user (to all their active sockets).
        """
        connections = self.active_connections.get(user_id)
        if not connections:
            logger.debug(f"⚪ No active WebSocket for user {user_id}")
            return

        data = json.dumps(message)
        for ws in list(connections):
            try:
                await ws.send_text(data)
            except WebSocketDisconnect:
                await self.disconnect(user_id, ws)
            except Exception as e:
                logger.warning(f"⚠️ Failed to send WS message to user {user_id}: {e}")

    async def broadcast(self, message: dict, exclude_user_id: int | None = None):
        """
        Send a message to all connected users (optionally excluding one user).
        """
        data = json.dumps(message)
        for user_id, connections in list(self.active_connections.items()):
            if exclude_user_id and user_id == exclude_user_id:
                continue
            for ws in list(connections):
                try:
                    await ws.send_text(data)
                except WebSocketDisconnect:
                    await self.disconnect(user_id, ws)

    
    # Room / Group Management
    async def join_room(self, room_name: str, user_id: int):
        """
        Add a user to a room (for group discussions, events, etc.)
        """
        if room_name not in self.rooms:
            self.rooms[room_name] = set()
        self.rooms[room_name].add(user_id)
        logger.info(f"➕ User {user_id} joined room '{room_name}'")

    async def leave_room(self, room_name: str, user_id: int):
        """
        Remove a user from a room.
        """
        if room_name in self.rooms:
            self.rooms[room_name].discard(user_id)
            if not self.rooms[room_name]:
                del self.rooms[room_name]
            logger.info(f"➖ User {user_id} left room '{room_name}'")

    async def send_room_message(self, room_name: str, message: dict, exclude_user_id: int | None = None):
        """
        Send a message to everyone in a room.
        """
        if room_name not in self.rooms:
            return

        members = self.rooms[room_name]
        data = json.dumps(message)

        for user_id in list(members):
            if exclude_user_id and user_id == exclude_user_id:
                continue
            connections = self.active_connections.get(user_id)
            if not connections:
                continue
            for ws in list(connections):
                try:
                    await ws.send_text(data)
                except WebSocketDisconnect:
                    await self.disconnect(user_id, ws)

  
    # Debug / Status Utilities
    def active_user_count(self) -> int:
        return len(self.active_connections)

    def room_user_count(self, room_name: str) -> int:
        return len(self.rooms.get(room_name, []))

    def all_active_users(self) -> List[int]:
        return list(self.active_connections.keys())


# Global singleton instance
manager = ConnectionManager()
