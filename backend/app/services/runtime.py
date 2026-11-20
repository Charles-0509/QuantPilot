from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import AuthUser, BrokerSettings, ConnectionConfig, Signal
from app.services.broker_service import BrokerService
from app.services.alpaca_service import AlpacaService
from app.services.credentials import CredentialDecryptionError, decrypt_credential
from app.services.engine import TradingEngine
from app.services.ibkr_service import IBKRService
from app.services.broker_config import live_context


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UserRuntime:
    alpaca: BrokerService  # Compatibility name for the active broker adapter.
    engine: TradingEngine

    @property
    def broker(self):
        return self.alpaca

    async def replace_broker(self, client):
        was_started = self.engine._task is not None
        try:
            await self.engine.shutdown()
        finally:
            self.alpaca = client
            self.engine.alpaca = client
            self.engine.reset_connection()
            if was_started:
                await self.engine.start()


def build_ibkr(settings, saved):
    user_id = saved.user_id
    revision = saved.revision
    account_id = saved.ibkr_account_id
    expected_mode = saved.trading_mode
    def authorized():
        with SessionLocal() as db:
            current = db.get(BrokerSettings, user_id)
            return bool(current and current.broker == 'ibkr' and current.revision == revision
                and current.ibkr_account_id == account_id and current.trading_mode == expected_mode
                and (expected_mode == 'paper' or (current.live_consent_at and current.live_consent_context == live_context(current))))
    def resolve(reference):
        with SessionLocal() as db:
            payloads = db.scalars(select(Signal.payload).where(Signal.user_id == user_id)).all()
        return next((p['client_order_id'] for p in payloads if p and p.get('client_order_id') == reference), reference)
    def attempted(reference):
        with SessionLocal() as db:
            payloads = db.scalars(select(Signal.payload).where(Signal.user_id == user_id)).all()
        return any(p and p.get('client_order_id') == reference and p.get('ibkr_submission_started_at') for p in payloads)
    return IBKRService(settings, gateway_url=saved.ibkr_gateway_url, account_id=account_id,
        trading_mode=expected_mode, verify_tls=saved.ibkr_verify_tls, authorization_guard=authorized,
        client_id_resolver=resolve, submission_attempted=attempted)


class UserRuntimeManager:
    """Owns one selected broker client and engine per active QuantPilot user."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._runtimes: dict[int, UserRuntime] = {}
        self._lock = asyncio.Lock()

    def _build(self, user_id: int) -> UserRuntime:
        with SessionLocal() as db:
            broker_settings = db.get(BrokerSettings, user_id)
        if broker_settings is not None and broker_settings.broker == 'ibkr':
            client = build_ibkr(self.settings, broker_settings)
            return UserRuntime(alpaca=client, engine=TradingEngine(self.settings, client, user_id=user_id))
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
