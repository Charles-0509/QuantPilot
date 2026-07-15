from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import update_connection_config
from app.config import Settings
from app.database import Base
from app.models import ConnectionConfig, EngineState, Strategy, StrategyPosition
from app.schemas import ConnectionConfigUpdate
from app.services import engine as engine_module
from app.services.alpaca_service import AlpacaService
from app.services.credentials import credential_key_path, decrypt_credential, encrypt_credential
from app.services.engine import TradingEngine


def local_settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, investor_db_path=str(tmp_path / "investor.db"))


def test_credentials_are_encrypted_with_a_local_sidecar_key(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    secret = "paper-secret-not-in-database"
    cipher = encrypt_credential(secret, settings)

    assert cipher != secret
    assert secret not in cipher
    assert decrypt_credential(cipher, settings) == secret
    assert credential_key_path(settings).exists()


def test_service_runtime_configuration_is_always_paper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeHistorical:
        def __init__(self, key: str, secret: str):
            self.key = key
            self.secret = secret

    class FakeTrading:
        def __init__(self, key: str, secret: str, *, paper: bool):
            self.key = key
            self.secret = secret
            self.paper = paper

    monkeypatch.setattr("app.services.alpaca_service.StockHistoricalDataClient", FakeHistorical)
    monkeypatch.setattr("app.services.alpaca_service.TradingClient", FakeTrading)
    service = AlpacaService(local_settings(tmp_path))
    service.configure("PK-ABCD", "paper-secret", source="web")

    assert service.configured is True
    assert service.source == "web"
    assert service.trading.paper is True
    assert service.connection_config()["api_key_hint"] == "...ABCD"


def test_historical_backtest_bars_use_short_lived_cache(tmp_path: Path) -> None:
    index = pd.DatetimeIndex(
        [
            "2025-01-01 09:30:00-05:00",
            "2025-01-01 10:00:00-05:00",
            "2025-01-01 15:30:00-05:00",
            "2025-01-02 09:30:00-05:00",
            "2025-01-02 10:00:00-05:00",
            "2025-01-02 15:30:00-05:00",
            "2025-01-03 09:30:00-05:00",
            "2025-01-03 10:00:00-05:00",
            "2025-01-03 15:30:00-05:00",
        ]
    ).tz_convert("UTC")
    frame = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104, 105, 106, 107, 108],
            "high": [101, 102, 103, 104, 105, 106, 107, 108, 109],
            "low": [99, 100, 101, 102, 103, 104, 105, 106, 107],
            "close": [100, 101, 102, 103, 104, 105, 106, 107, 108],
            "volume": [500] * 9,
        },
        index=index,
    )

    class Response:
        df = frame

    class FakeHistorical:
        def __init__(self) -> None:
            self.calls = 0

        def get_stock_bars(self, _request):
            self.calls += 1
            return Response()

    service = AlpacaService(local_settings(tmp_path))
    historical = FakeHistorical()
    service.configured = True
    service.historical = historical
    service.trading = object()
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 5, tzinfo=timezone.utc)

    first = service.get_bars(["SPY"], "1Day", start, end, use_cache=True)
    first["SPY"].iloc[0, 0] = -1
    second = service.get_bars(["SPY"], "1Day", start, end, use_cache=True)

    assert historical.calls == 1
    assert second["SPY"].iloc[0]["open"] == 100


@pytest.mark.asyncio
async def test_web_config_endpoint_persists_ciphertext_and_pauses_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = local_settings(tmp_path)
    database_engine = create_engine(f"sqlite:///{tmp_path / 'endpoint.db'}")
    Base.metadata.create_all(database_engine)
    session = sessionmaker(bind=database_engine, expire_on_commit=False)()

    class FakeAlpaca:
        def __init__(self) -> None:
            self.settings = settings
            self.configured = False
            self._config: dict = {"configured": False, "paper": True, "source": "none", "api_key_hint": None, "feed": "iex", "updated_at": None}

        def configure(self, key: str, _secret: str, **kwargs) -> None:
            self.configured = True
            self._config = {
                "configured": True,
                "paper": True,
                "source": kwargs["source"],
                "api_key_hint": f"...{key[-4:]}",
                "feed": kwargs["feed"],
                "updated_at": kwargs["updated_at"],
            }

        def connection_config(self) -> dict:
            return self._config

    class FakeEngine:
        def __init__(self) -> None:
            self.reconfiguration_reasons: list[str] = []
            self.reset_called = False

        @asynccontextmanager
        async def connection_reconfiguration(self, reason: str):
            self.reconfiguration_reasons.append(reason)
            yield

        def reset_connection(self) -> None:
            self.reset_called = True

    monkeypatch.setattr(AlpacaService, "validate_credentials", staticmethod(lambda _key, _secret: None))
    fake_alpaca = FakeAlpaca()
    fake_engine = FakeEngine()
    secret = "paper-secret-not-in-database"
    result = await update_connection_config(
        ConnectionConfigUpdate(api_key_id="PK-ABCD", api_secret_key=secret),
        db=session,
        alpaca=fake_alpaca,
        engine=fake_engine,
        user_id=1,
    )

    saved = session.get(ConnectionConfig, 1)
    assert saved is not None
    assert secret not in saved.api_secret_cipher
    assert decrypt_credential(saved.api_secret_cipher, settings) == secret
    assert result["api_key_hint"] == "...ABCD"
    assert fake_engine.reconfiguration_reasons == [
        "Alpaca 连接配置已更新，请检查后重新启动引擎"
    ]
    assert fake_engine.reset_called is True
    session.close()


@pytest.mark.asyncio
async def test_web_config_change_is_blocked_while_strategy_owns_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = local_settings(tmp_path)
    database_engine = create_engine(f"sqlite:///{tmp_path / 'blocked-endpoint.db'}")
    Base.metadata.create_all(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    with sessions() as db:
        db.add_all(
            [
                Strategy(
                    id="strategy-with-position",
                    owner_user_id=1,
                    name="Position owner",
                    description="",
                    is_template=False,
                    enabled=True,
                    definition={},
                ),
                StrategyPosition(
                    user_id=1,
                    strategy_id="strategy-with-position",
                    symbol="SPY",
                    qty=1,
                ),
                EngineState(user_id=1, status="running", reason="test"),
            ]
        )
        db.commit()

    class FakeAlpaca:
        configured = True

        def __init__(self) -> None:
            self.settings = settings
            self.configure_calls = 0

        def configure(self, *_args, **_kwargs) -> None:
            self.configure_calls += 1

        def connection_config(self) -> dict:
            return {
                "configured": True,
                "paper": True,
                "source": "web",
                "api_key_hint": "...OLD1",
                "feed": "iex",
                "updated_at": None,
            }

        def stop_streams(self) -> None:
            return None

    monkeypatch.setattr(
        AlpacaService,
        "validate_credentials",
        staticmethod(lambda _key, _secret: None),
    )
    fake_alpaca = FakeAlpaca()
    engine = TradingEngine(settings, fake_alpaca, user_id=1)
    with sessions() as db:
        with pytest.raises(HTTPException) as captured:
            await update_connection_config(
                ConnectionConfigUpdate(
                    api_key_id="PK-NEW1",
                    api_secret_key="new-paper-secret",
                ),
                db=db,
                alpaca=fake_alpaca,
                engine=engine,
                user_id=1,
            )
        assert captured.value.status_code == 409
        assert db.get(ConnectionConfig, 1) is None
        state = db.query(EngineState).filter_by(user_id=1).one()
        assert state.status == "running"
    assert fake_alpaca.configure_calls == 0
    database_engine.dispose()
