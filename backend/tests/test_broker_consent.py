from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import broker_api
from app.config import Settings, get_settings
from app.database import Base
from app.models import BrokerSettings, BrokerLiveChallenge, EngineState, RiskSettings
from app.schemas import BrokerConfigTarget, BrokerConfigUpdate
from app.services import engine as engine_module, runtime as runtime_module
from app.services.engine import TradingEngine
from app.services.runtime import UserRuntime
from app.services.ibkr_service import IBKRService
from app.services.broker_config import LIVE_CONFIRMATION_TEXT


@pytest.fixture
def context(tmp_path, monkeypatch):
    db_engine = create_engine(f"sqlite:///{tmp_path / 'brokers.db'}")
    Base.metadata.create_all(db_engine)
    sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    monkeypatch.setattr(runtime_module, "SessionLocal", sessions)
    monkeypatch.setattr(
        IBKRService,
        "select_account",
        lambda self: {"account_id": self.account_id, "paper": self.paper},
    )
    monkeypatch.setattr(
        IBKRService, "get_account", lambda self: {"equity": 100000, "buying_power": 100000}
    )
    settings = Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db"))

    class FakeAlpaca:
        broker_name = "alpaca"
        paper = True
        configured = False

        def __init__(self):
            self.settings = settings

        def stop_streams(self):
            pass

    with sessions() as db:
        db.add(EngineState(user_id=7, status="paused"))
        db.add(RiskSettings(user_id=7))
        db.commit()
    client = FakeAlpaca()
    runtime = UserRuntime(client, TradingEngine(settings, client, user_id=7))
    db = sessions()
    yield db, runtime
    db.close()
    db_engine.dispose()


def target(revision=0, account="U12345", mode="live", broker="ibkr"):
    return {
        "broker": broker,
        "trading_mode": mode,
        "ibkr_gateway_url": "https://127.0.0.1:5000/v1/api",
        "ibkr_account_id": account,
        "revision": revision,
    }


def grant(db, payload, user=7):
    return broker_api.live_challenge(BrokerConfigTarget(**payload), db=db, user_id=user)[
        "challenge_token"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["", "我同意", "我同意并确认 "])
async def test_direct_live_request_requires_exact_phrase(context, phrase):
    db, runtime = context
    with pytest.raises(HTTPException) as exc:
        await broker_api.change_broker(
            BrokerConfigUpdate(**target(), confirmation_text=phrase),
            db=db,
            runtime=runtime,
            user_id=7,
        )
    assert exc.value.status_code == 422
    assert db.get(BrokerSettings, 7) is None


@pytest.mark.asyncio
async def test_correct_phrase_without_modal_challenge_is_rejected(context):
    db, runtime = context
    with pytest.raises(HTTPException) as exc:
        await broker_api.change_broker(
            BrokerConfigUpdate(**target(), confirmation_text=LIVE_CONFIRMATION_TEXT),
            db=db,
            runtime=runtime,
            user_id=7,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_live_consent_is_one_use_and_revoked_by_paper_or_other_broker(context):
    db, runtime = context
    token = grant(db, target())
    saved = await broker_api.change_broker(
        BrokerConfigUpdate(
            **target(), confirmation_text=LIVE_CONFIRMATION_TEXT, challenge_token=token
        ),
        db=db,
        runtime=runtime,
        user_id=7,
    )
    assert saved["trading_mode"] == "live"
    previous = runtime.alpaca
    assert previous._guard()
    with pytest.raises(HTTPException):
        await broker_api.change_broker(
            BrokerConfigUpdate(
                **target(revision=1),
                confirmation_text=LIVE_CONFIRMATION_TEXT,
                challenge_token=token,
            ),
            db=db,
            runtime=runtime,
            user_id=7,
        )
    saved = await broker_api.change_broker(
        BrokerConfigUpdate(**target(revision=1, mode="paper", account="DU12345")),
        db=db,
        runtime=runtime,
        user_id=7,
    )
    assert saved["trading_mode"] == "paper" and not previous._guard()
    assert db.get(BrokerSettings, 7).live_consent_at is None
    with pytest.raises(HTTPException):
        await broker_api.change_broker(
            BrokerConfigUpdate(
                **target(revision=2),
                confirmation_text=LIVE_CONFIRMATION_TEXT,
                challenge_token=token,
            ),
            db=db,
            runtime=runtime,
            user_id=7,
        )
    token2 = grant(db, target(revision=2))
    await broker_api.change_broker(
        BrokerConfigUpdate(
            **target(revision=2), confirmation_text=LIVE_CONFIRMATION_TEXT, challenge_token=token2
        ),
        db=db,
        runtime=runtime,
        user_id=7,
    )
    await broker_api.change_broker(
        BrokerConfigUpdate(**target(revision=3, broker="alpaca", mode="paper")),
        db=db,
        runtime=runtime,
        user_id=7,
    )
    assert db.get(BrokerSettings, 7).live_consent_context is None
    assert runtime.alpaca.paper is True
    assert db.scalar(select(EngineState)).status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["account", "gateway", "user", "expired"])
async def test_confirmation_cannot_cross_context(context, kind):
    db, runtime = context
    payload = target()
    token = grant(db, payload, user=8 if kind == "user" else 7)
    if kind == "account":
        payload["ibkr_account_id"] = "U99999"
    if kind == "gateway":
        payload["ibkr_gateway_url"] = "https://127.0.0.1:5001/v1/api"
    if kind == "expired":
        challenge = db.scalar(select(BrokerLiveChallenge))
        challenge.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    with pytest.raises(HTTPException) as exc:
        await broker_api.change_broker(
            BrokerConfigUpdate(
                **payload, confirmation_text=LIVE_CONFIRMATION_TEXT, challenge_token=token
            ),
            db=db,
            runtime=runtime,
            user_id=7,
        )
    assert exc.value.status_code == 409
    assert db.get(BrokerSettings, 7) is None


@pytest.mark.asyncio
async def test_alpaca_cannot_be_changed_to_live(context):
    db, runtime = context
    with pytest.raises(HTTPException) as exc:
        await broker_api.change_broker(
            BrokerConfigUpdate(**target(broker="alpaca")), db=db, runtime=runtime, user_id=7
        )
    assert exc.value.status_code == 422


def test_default_is_alpaca_paper(context):
    db, _ = context
    result = broker_api.broker_config(db=db, user_id=7)
    assert result["broker"] == "alpaca" and result["trading_mode"] == "paper"


@pytest.mark.parametrize("saved_url", [None, "https://localhost:5001/v1/api"])
def test_gateway_default_uses_server_config_without_overwriting_saved_url(context, monkeypatch, saved_url):
    db, _ = context
    monkeypatch.setenv("IBKR_GATEWAY_URL", "https://localhost:5050/v1/api")
    get_settings.cache_clear()
    try:
        if saved_url:
            db.add(BrokerSettings(user_id=7, ibkr_gateway_url=saved_url))
            db.commit()
        result = broker_api.broker_config(db=db, user_id=7)
        assert result["ibkr_gateway_url"] == (saved_url or "https://localhost:5050/v1/api")
        assert result["broker"] == "alpaca" and result["trading_mode"] == "paper"
        assert result["ibkr_verify_tls"] is True
    finally:
        get_settings.cache_clear()
