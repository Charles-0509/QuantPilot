from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.connections.discard(websocket)

    async def broadcast(self, event: str, data: Any) -> None:
        payload = {"event": event, "data": data}
        stale: list[WebSocket] = []
        async with self._lock:
            connections = list(self.connections)
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        if stale:
            async with self._lock:
                for websocket in stale:
                    self.connections.discard(websocket)


websocket_manager = WebSocketManager()
