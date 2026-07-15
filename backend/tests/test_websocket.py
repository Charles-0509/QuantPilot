import asyncio

import pytest

from app.services import websocket as websocket_module
from app.services.websocket import WebSocketManager


class FakeSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.messages: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


@pytest.mark.asyncio
async def test_websocket_events_are_sent_only_to_the_owning_user() -> None:
    manager = WebSocketManager()
    first = FakeSocket()
    second = FakeSocket()
    await manager.connect(first, user_id=1)
    await manager.connect(second, user_id=2)

    await manager.broadcast(2, "signal", {"symbol": "GOOGL"})

    assert first.messages == []
    assert second.messages == [{"event": "signal", "data": {"symbol": "GOOGL"}}]


@pytest.mark.asyncio
async def test_slow_websocket_does_not_block_other_clients(monkeypatch) -> None:
    manager = WebSocketManager()
    fast = FakeSocket()

    class SlowSocket(FakeSocket):
        async def send_json(self, payload: dict) -> None:
            await asyncio.Event().wait()

    slow = SlowSocket()
    monkeypatch.setattr(websocket_module, "WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.01)
    await manager.connect(slow, user_id=1)
    await manager.connect(fast, user_id=1)

    await manager.broadcast(1, "engine", {"status": "paused"})

    assert fast.messages == [{"event": "engine", "data": {"status": "paused"}}]
    assert slow not in manager.connections
