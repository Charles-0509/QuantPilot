from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self.connections: dict[WebSocket, int] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: int) -> None:
        await websocket.accept()
        async with self._lock:
            self.connections[websocket] = user_id

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.connections.pop(websocket, None)

    async def broadcast(self, user_id: int, event: str, data: Any) -> None:
        payload = {"event": event, "data": data}
        stale: list[WebSocket] = []
        async with self._lock:
            connections = [
                websocket
                for websocket, connection_user_id in self.connections.items()
                if connection_user_id == user_id
            ]
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        if stale:
            async with self._lock:
                for websocket in stale:
                    self.connections.pop(websocket, None)


websocket_manager = WebSocketManager()
