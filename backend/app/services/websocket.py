from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


WEBSOCKET_SEND_TIMEOUT_SECONDS = 1.0


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
        async def send(websocket: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(
                    websocket.send_json(payload),
                    timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                )
            except Exception:
                return websocket
            return None

        if connections:
            stale = [
                websocket
                for websocket in await asyncio.gather(
                    *(send(websocket) for websocket in connections)
                )
                if websocket is not None
            ]
        if stale:
            async with self._lock:
                for websocket in stale:
                    self.connections.pop(websocket, None)


websocket_manager = WebSocketManager()
