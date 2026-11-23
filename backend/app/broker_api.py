"""User-scoped broker selection and one-use live-account confirmation."""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .api import current_user_id, get_runtime
from .database import get_db
from .config import get_settings
from .models import BrokerSettings, BrokerLiveChallenge, EventLog, RiskSettings
from .schemas import BrokerConfigTarget, BrokerConfigUpdate, MarketDataSourceUpdate
from .services.broker_config import (
    normalize_target,
    context_hash,
    current_config,
    LIVE_CONFIRMATION_TEXT,
    LIVE_DISCLAIMER,
    DISCLAIMER_VERSION,
)
from .services.ibkr_service import IBKRService, IBKRError
from .services.runtime import UserRuntime, UserRuntimeManager, build_alpaca, build_ibkr, with_market_data
from .services.market_data import market_data_info
from .services.engine import ConnectionReconfigurationBlockedError

router = APIRouter(prefix="/api/brokers")
_configuration_lock = asyncio.Lock()


def target_values(payload):
    try:
        return normalize_target(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/market-data")
def market_data_status(runtime: UserRuntime = Depends(get_runtime)):
    return market_data_info(runtime.alpaca)


@router.put("/market-data")
async def set_market_data_source(
    payload: MarketDataSourceUpdate,
    db: Session = Depends(get_db),
    runtime: UserRuntime = Depends(get_runtime),
    user_id: int = Depends(current_user_id),
):
    """Only bar/quote reads move; orders, positions and account stay on the broker."""
    async with _configuration_lock:
        current = db.get(BrokerSettings, user_id)
        if current is None:
            current = BrokerSettings(user_id=user_id)
            db.add(current)
        if payload.source == 'alpaca' and current.broker == 'ibkr' and not build_alpaca(runtime.alpaca.settings, user_id).configured:
            raise HTTPException(status_code=422, detail='请先在设置页填写 Alpaca 密钥（免费账户即可），才能使用 Alpaca 行情')
        current.market_data_source = payload.source
        db.add(EventLog(user_id=user_id, category='broker', level='info',
                        message=f"行情来源改为 {'Alpaca 免费行情' if payload.source == 'alpaca' else 'IBKR 行情'}",
                        details={'source': payload.source}))
        db.commit()
        db.refresh(current)
        if current.broker == 'ibkr':
            client = with_market_data(runtime.alpaca.settings, user_id, current,
                                      build_ibkr(runtime.alpaca.settings, current))
            await runtime.replace_broker(client)
    return {**market_data_info(runtime.alpaca), "configured_source": current.market_data_source}


@router.get("/config")
def broker_config(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)):
    return {
        **current_config(
            db.get(BrokerSettings, user_id),
            default_gateway_url=get_settings().ibkr_gateway_url,
        ),
        "disclaimer_version": DISCLAIMER_VERSION,
        "ibkr_capabilities": {
            "market": True,
            "limit": True,
            "bracket": True,
            "trailing_stop": False,
            "cash_sweep": False,
        },
    }


@router.post("/ibkr/probe")
async def probe_broker(payload: BrokerConfigTarget, runtime: UserRuntime = Depends(get_runtime)):
    values = target_values(payload)
    client = IBKRService(
        runtime.alpaca.settings,
        gateway_url=values["ibkr_gateway_url"],
        verify_tls=values["ibkr_verify_tls"],
    )
    try:
        return {"accounts": await asyncio.to_thread(client.probe_accounts, fresh=True)}
    except IBKRError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/ibkr/live-challenge")
def live_challenge(
    payload: BrokerConfigTarget,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
):
    values = target_values(payload)
    if (
        values["broker"] != "ibkr"
        or values["trading_mode"] != "live"
        or not values["ibkr_account_id"]
    ):
        raise HTTPException(status_code=422, detail="请先选择 IBKR 真实账户")
    current = db.get(BrokerSettings, user_id)
    revision = current.revision if current else 0
    if values["revision"] != revision:
        raise HTTPException(status_code=409, detail="连接配置已变化，请刷新后重新确认")
    values["revision"] += 1
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.add(
        BrokerLiveChallenge(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            user_id=user_id,
            context_hash=context_hash(values),
            revision=revision,
            expires_at=expires,
        )
    )
    db.commit()
    return {
        "challenge_token": token,
        "expires_at": expires,
        "disclaimer": LIVE_DISCLAIMER,
        "disclaimer_version": DISCLAIMER_VERSION,
        "required_text": LIVE_CONFIRMATION_TEXT,
        "account_id": values["ibkr_account_id"],
    }


@router.put("/config")
async def change_broker(
    payload: BrokerConfigUpdate,
    db: Session = Depends(get_db),
    runtime: UserRuntime = Depends(get_runtime),
    user_id: int = Depends(current_user_id),
):
    values = target_values(payload)
    current = db.get(BrokerSettings, user_id)
    revision = current.revision if current else 0
    if values["revision"] != revision:
        raise HTTPException(status_code=409, detail="连接配置已变化，请刷新后重新操作")
    values["revision"] += 1
    live = values["broker"] == "ibkr" and values["trading_mode"] == "live"
    now = datetime.now(timezone.utc)
    token_hash = hashlib.sha256(payload.challenge_token.encode()).hexdigest()
    if live:
        if payload.confirmation_text != LIVE_CONFIRMATION_TEXT:
            raise HTTPException(status_code=422, detail="启用真实账户前必须准确输入“我同意并确认”")
        challenge = db.get(BrokerLiveChallenge, token_hash)
        if (
            challenge is None
            or challenge.user_id != user_id
            or challenge.used_at is not None
            or challenge.revision != revision
            or challenge.context_hash != context_hash(values)
            or challenge.expires_at.replace(tzinfo=timezone.utc) <= now
        ):
            raise HTTPException(
                status_code=409, detail="真实账户确认已失效或不属于当前配置，请重新打开风险确认弹窗"
            )
    if values["broker"] == "ibkr" and values["ibkr_account_id"]:
        other = db.scalar(
            select(BrokerSettings.user_id).where(
                BrokerSettings.user_id != user_id,
                BrokerSettings.broker == "ibkr",
                (BrokerSettings.ibkr_gateway_url == values["ibkr_gateway_url"])
                | (BrokerSettings.ibkr_account_id == values["ibkr_account_id"]),
            )
        )
        if other is not None:
            raise HTTPException(
                status_code=409, detail="此网关或账户已由其他用户使用，请使用独立IBKR网关和账户"
            )
    try:
        async with _configuration_lock, runtime.engine.connection_reconfiguration(
            "券商/账户配置已更新，请核对后手动启动引擎"
        ):
            db.expire_all()
            current = db.get(BrokerSettings, user_id)
            if (current.revision if current else 0) != revision:
                raise HTTPException(status_code=409, detail="连接配置已变化，请重新确认")
            if values["broker"] == "ibkr" and values["ibkr_account_id"]:
                if db.scalar(select(BrokerSettings.user_id).where(
                    BrokerSettings.user_id != user_id, BrokerSettings.broker == 'ibkr',
                    (BrokerSettings.ibkr_gateway_url == values['ibkr_gateway_url']) |
                    (BrokerSettings.ibkr_account_id == values['ibkr_account_id']),
                )) is not None:
                    raise HTTPException(status_code=409, detail='此网关或账户已由其他用户使用')
                probe = IBKRService(
                    runtime.alpaca.settings,
                    gateway_url=values["ibkr_gateway_url"],
                    account_id=values["ibkr_account_id"],
                    trading_mode=values["trading_mode"],
                    verify_tls=values["ibkr_verify_tls"],
                )
                await asyncio.to_thread(probe.select_account)
                await asyncio.to_thread(probe.get_account)
            if live:
                now = datetime.now(timezone.utc)
                consumed = db.execute(
                    update(BrokerLiveChallenge)
                    .where(
                        BrokerLiveChallenge.token_hash == token_hash,
                        BrokerLiveChallenge.user_id == user_id,
                        BrokerLiveChallenge.used_at.is_(None),
                        BrokerLiveChallenge.expires_at > now,
                    )
                    .values(used_at=now)
                )
                if consumed.rowcount != 1:
                    raise HTTPException(status_code=409, detail="确认已使用，请重新确认")
            if current is None:
                current = BrokerSettings(user_id=user_id)
                db.add(current)
            for key, value in values.items():
                setattr(current, key, value)
            current.live_consent_context = context_hash(values) if live else None
            current.live_consent_at = now if live else None
            risk = db.scalar(select(RiskSettings).where(RiskSettings.user_id == user_id))
            if risk is not None:
                risk.cash_sweep_enabled = False
            db.add(
                EventLog(
                    user_id=user_id,
                    category="broker",
                    level="warning" if live else "info",
                    message="用户确认启用IBKR真实账户"
                    if live
                    else "切换券商/模拟账户，旧实盘确认已撤销",
                    details={
                        "broker": values["broker"],
                        "mode": values["trading_mode"],
                        "account_id": values["ibkr_account_id"],
                        "revision": values["revision"],
                        "disclaimer_version": DISCLAIMER_VERSION if live else None,
                    },
                )
            )
            db.commit()
            db.refresh(current)
            if values["broker"] == "ibkr":
                client = with_market_data(runtime.alpaca.settings, user_id, current,
                                          build_ibkr(runtime.alpaca.settings, current))
            else:
                # Current config already points to Alpaca; the factory loads only this user's keys.
                client = UserRuntimeManager(runtime.alpaca.settings)._build(user_id).alpaca
            await runtime.replace_broker(client)
    except ConnectionReconfigurationBlockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IBKRError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail='此IBKR网关或账户已被使用，请刷新后使用独立连接') from exc
    return broker_config(db=db, user_id=user_id)
