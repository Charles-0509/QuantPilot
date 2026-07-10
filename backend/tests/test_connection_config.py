from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import update_connection_config
from app.config import Settings
from app.database import Base
from app.models import ConnectionConfig
from app.schemas import ConnectionConfigUpdate
from app.services.alpaca_service import AlpacaService
from app.services.credentials import credential_key_path, decrypt_credential, encrypt_credential


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
    index = pd.date_range("2025-01-01", periods=3, freq="D", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": [100, 101, 102],
            "high": [101, 102, 103],
            "low": [99, 100, 101],
            "close": [100, 101, 102],
            "volume": [1000, 1000, 1000],
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
    end = datetime(2025, 1, 4, tzinfo=timezone.utc)

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
            self.pauses: list[tuple[str, bool]] = []
            self.reset_called = False

        async def pause(self, reason: str, cancel_orders: bool) -> None:
            self.pauses.append((reason, cancel_orders))

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
    )

    saved = session.get(ConnectionConfig, 1)
    assert saved is not None
    assert secret not in saved.api_secret_cipher
    assert decrypt_credential(saved.api_secret_cipher, settings) == secret
    assert result["api_key_hint"] == "...ABCD"
    assert fake_engine.pauses[0][1] is False
    assert fake_engine.reset_called is True
    session.close()
