"""Account-scoped live consent. Paper remains the default for every provider."""

import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

LIVE_CONFIRMATION_TEXT = "我同意并确认"
DISCLAIMER_VERSION = "ibkr-live-2026-10-22-v1"
LIVE_DISCLAIMER = (
    "您正在启用 IBKR 真实交易账户。启用后，策略、手动操作及风控设置可能产生真实资金交易，"
    "可能导致本金损失、滑点、手续费及超出预期的成交；网络故障、软件缺陷或行情延迟可能导致订单失败、延迟或重复风险。"
    "历史回测和模拟盘表现不保证真实交易结果。本平台仅提供交易工具，不提供投资建议，也不承诺收益。"
    "请自行核对当前账户、交易权限、策略和仓位限额，并承担账户交易责任。"
    "切换到其他模拟账户或券商后，再次启用真实账户必须重新确认。"
)


def normalize_target(target):
    values = target.model_dump(exclude={"confirmation_text", "challenge_token"})
    raw = values["ibkr_gateway_url"].strip()
    url = urlsplit(raw)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or len(raw) > 500
    ):
        raise ValueError("网关地址必须是无用户名、密码和查询参数的 HTTP/HTTPS 地址")
    try:
        url.port
    except ValueError as exc:
        raise ValueError("网关端口无效") from exc
    path = url.path.rstrip("/")
    if not path:
        path = "/v1/api"
    if path != "/v1/api":
        raise ValueError("网关 API 路径必须是 /v1/api")
    values["ibkr_gateway_url"] = urlunsplit((url.scheme, url.netloc, path, "", ""))
    account = values["ibkr_account_id"].strip().upper()
    if account and not re.fullmatch(r"[A-Z][A-Z0-9]{1,31}", account):
        raise ValueError("IBKR 账户ID格式无效")
    values["ibkr_account_id"] = account
    if values["broker"] == "alpaca" and values["trading_mode"] != "paper":
        raise ValueError("Alpaca 接入仅支持 Paper Trading")
    return values


def context_hash(values):
    fields = [
        "broker",
        "trading_mode",
        "ibkr_gateway_url",
        "ibkr_account_id",
        "ibkr_verify_tls",
        "revision",
    ]
    return hashlib.sha256(
        json.dumps({**{key: values[key] for key in fields}, "disclaimer_version": DISCLAIMER_VERSION}, sort_keys=True).encode()
    ).hexdigest()


def current_config(row, *, default_gateway_url="https://127.0.0.1:5000/v1/api"):
    return {
        "broker": row.broker if row else "alpaca",
        "trading_mode": row.trading_mode if row else "paper",
        "ibkr_gateway_url": row.ibkr_gateway_url if row else default_gateway_url,
        "ibkr_account_id": row.ibkr_account_id if row else "",
        "ibkr_verify_tls": row.ibkr_verify_tls if row else True,
        "revision": row.revision if row else 0,
        "updated_at": row.updated_at if row else None,
    }


def live_context(row):
    # Consent records are created against the new revision, and revoked by any switch.
    return context_hash(current_config(row))
