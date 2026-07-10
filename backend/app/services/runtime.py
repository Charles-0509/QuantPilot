from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import AuthUser, ConnectionConfig
from app.services.alpaca_service import AlpacaService
from app.services.credentials import CredentialDecryptionError, decrypt_credential
from app.services.engine import TradingEngine


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UserRuntime:
    alpaca: AlpacaService
    engine: TradingEngine


class UserRuntimeManager:
    """Owns one Paper Trading client and engine per active QuantPilot user."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._runtimes: dict[int, UserRuntime] = {}
        self._lock = asyncio.Lock()

    def _build(self, user_id: int) -> UserRuntime:
        # Environment credentials are retained as a backwards-compatible fallback
        # for the original administrator only. Web credentials always take priority.
        alpaca = AlpacaService(self.settings, use_env=user_id == 1)
        with SessionLocal() as db:
            saved = db.scalar(
                select(ConnectionConfig).where(ConnectionConfig.user_id == user_id)
            )
            if saved is not None:
                try:
                    alpaca.configure(
                        decrypt_credential(saved.api_key_cipher, self.settings),
                        decrypt_credential(saved.api_secret_cipher, self.settings),
                        feed=saved.data_feed,
                        source="web",
                        updated_at=saved.updated_at,
                    )
                except (CredentialDecryptionError, ValueError):
                    logger.warning("Stored Alpaca credentials for user %s could not be decrypted", user_id)
        return UserRuntime(alpaca=alpaca, engine=TradingEngine(self.settings, alpaca, user_id=user_id))

    async def ensure(self, user_id: int) -> UserRuntime:
        runtime = self._runtimes.get(user_id)
        if runtime is not None:
            return runtime
        async with self._lock:
            runtime = self._runtimes.get(user_id)
            if runtime is None:
                runtime = self._build(user_id)
                self._runtimes[user_id] = runtime
                await runtime.engine.start()
        return runtime

    async def start(self) -> None:
        with SessionLocal() as db:
            user_ids = db.scalars(select(AuthUser.id).where(AuthUser.is_active.is_(True))).all()
        for user_id in user_ids:
            await self.ensure(user_id)

    async def disable(self, user_id: int) -> None:
        async with self._lock:
            runtime = self._runtimes.pop(user_id, None)
        if runtime is not None:
            await runtime.engine.pause("账户已被管理员停用", cancel_orders=True)
            await runtime.engine.shutdown()

    async def shutdown(self) -> None:
        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            await runtime.engine.shutdown()
