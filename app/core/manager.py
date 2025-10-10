

from starlette.websockets import WebSocket

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}

    async def connect(self, user_id: int, websocket):
        self.active_connections[user_id] = websocket

    async def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)

    async def send_message(self, user_id: int, message: dict):
        if user_id in self.active_connections:
            websocket = self.active_connections[user_id]
            await websocket.send_json(message)

manager = ConnectionManager()
