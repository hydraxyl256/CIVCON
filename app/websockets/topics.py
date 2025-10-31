from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import List, Dict, Any
import json
import asyncio

router = APIRouter()

# List of connected clients
connected_clients: List[WebSocket] = []

@router.websocket("/ws/topics")
async def websocket_topics(websocket: WebSocket):
    """Handle real-time topic updates."""
    await websocket.accept()
    connected_clients.append(websocket)
    print("🟢 Client connected to /ws/topics")

    try:
        while True:
            # Keep the connection open — optional pings or messages can go here.
            data = await websocket.receive_text()
            print(f" Client sent: {data}")
            # Optionally echo or ignore
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        print(" Client disconnected from /ws/topics")
    except Exception as e:
        print(f" WebSocket error: {e}")
        if websocket in connected_clients:
            connected_clients.remove(websocket)



async def broadcast_new_topic(topic: Dict[str, Any]):
    """Send a new topic to all connected clients."""
    message = {
        "event": "new_topic",
        "topic": topic
    }

    disconnected_clients = []

    for ws in connected_clients:
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            disconnected_clients.append(ws)

    # Clean up broken connections
    for ws in disconnected_clients:
        connected_clients.remove(ws)
