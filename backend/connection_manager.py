import json
from typing import Dict, List
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.active_rooms: Dict[str, List[WebSocket]] = {}

    async def connect(self, room_id: str, websocket: WebSocket, max_cap: int = 6):
        await websocket.accept()
        if room_id not in self.active_rooms:
            self.active_rooms[room_id] = []
        if len(self.active_rooms[room_id]) >= max_cap:
            await websocket.send_text(json.dumps({"event_type": "system_error", "message": "Room full."}))
            await websocket.close(code=4003)
            return False
        self.active_rooms[room_id].append(websocket)
        return True

    async def disconnect(self, room_id: str, websocket: WebSocket):
        if room_id in self.active_rooms and websocket in self.active_rooms[room_id]:
            self.active_rooms[room_id].remove(websocket)
            if not self.active_rooms[room_id]:
                del self.active_rooms[room_id]

    async def broadcast_to_room(self, room_id: str, message: dict):
        if room_id in self.active_rooms:
            json_payload = json.dumps(message)
            for connection in self.active_rooms[room_id]:
                try:
                    await connection.send_text(json_payload)
                except Exception:
                    pass
