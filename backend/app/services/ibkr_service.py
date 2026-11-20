"""IBKR Client Portal Gateway adapter for USD US equities/ETFs.

Gateway login/2FA remain in IBKR's browser. HTTP mutations are never replayed.
Unknown broker warnings are declined, never automatically accepted.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from app.schemas import BrokerConfigTarget
from .broker_config import normalize_target
from .alpaca_service import AlpacaServiceError, AlpacaAmbiguousOrderError


class IBKRError(AlpacaServiceError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class IBKRService:
    broker_name = "ibkr"
    feed = "ibkr"
    supports_trailing_stop = False
    supports_streaming = False
    supports_cash_sweep = False
    history_adjustment = "broker_default"

    def __init__(
        self,
        settings,
        *,
        gateway_url="https://127.0.0.1:5000/v1/api",
        account_id="",
        trading_mode="paper",
        verify_tls=True,
        authorization_guard=None,
        client_id_resolver=None,
        submission_attempted=None,
    ):
        target = normalize_target(
            BrokerConfigTarget(
                broker="ibkr",
                trading_mode=trading_mode,
                ibkr_gateway_url=gateway_url,
                ibkr_account_id=account_id,
                ibkr_verify_tls=verify_tls,
            )
        )
        self.settings = settings
        self.gateway_url = target["ibkr_gateway_url"]
        self.account_id = target["ibkr_account_id"]
        self.paper = trading_mode == "paper"
        self.verify_tls = verify_tls
        self.configured = bool(self.account_id)
        self.source = "web"
        self._guard = authorization_guard
        self._resolver = client_id_resolver or (lambda reference: reference)
        self._submission_attempted = submission_attempted or (lambda reference: False)
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.headers.update({"User-Agent": "QuantPilot/1.6", "Accept": "application/json"})
        self._lock = threading.RLock()
        self._orders_lock = threading.RLock()
        self._cache = {}
        self._contracts = {}
        self._client_ids = {}
        self._attempted = set()
        self._last_orders_at = 0.0
        self._last_requests = {}
        self._last_success = None
        self._last_failure = None
        self._last_error = None
        self._failed = {}
        self._failure_count = 0
        self._open_until = 0.0
        self._poll_task = None
        self._poll_generation = 0
        self._now = lambda: datetime.now(timezone.utc)

    def connection_config(self):
        return {
            "configured": self.configured,
            "paper": self.paper,
            "broker": "ibkr",
            "source": "web",
            "feed": self.feed,
            "api_key_hint": None,
            "updated_at": None,
        }

    def connection_status(self):
        state = (
            "unconfigured"
            if not self.configured
            else "circuit_open"
            if time.monotonic() < self._open_until
            else "degraded"
            if self._failed
            else "connected"
            if self._last_success
            else "unknown"
        )
        return {
            "configured": self.configured,
            "connected": state == "connected",
            "state": state,
            "broker": "ibkr",
            "paper": self.paper,
            "feed": self.feed,
            "source": "web",
            "account_id": self.account_id,
            "transport": "REST polling",
            "message": self._last_error
            or (
                "IBKR 已连接"
                if state == "connected"
                else "请启动并登录 IBKR Client Portal Gateway，然后选择账户"
            ),
            "last_success_at": self._last_success,
            "last_failure_at": self._last_failure,
            "consecutive_failures": self._failure_count,
            "retry_at": (
                self._now() + timedelta(seconds=max(0, self._open_until - time.monotonic()))
            ).isoformat()
            if state == "circuit_open"
            else None,
            "health": {
                "failed_operations": [{"channel": k[0], "operation": k[1]} for k in self._failed]
            },
        }

    def _failure(self, operation, message, channel="trading"):
        self._last_failure = self._now().isoformat()
        self._last_error = message
        self._failed[(channel, operation)] = message
        self._failure_count += 1
        if self._failure_count >= 3:
            self._open_until = time.monotonic() + 30

    def _request(
        self, method, path, *, params=None, payload=None, operation="account", channel="trading"
    ):
        if time.monotonic() < self._open_until:
            raise IBKRError("IBKR 连接保护中，请稍后重试")
        # Use only endpoint paths constructed by this adapter. Do not follow redirects to login pages.
        with self._lock:
            for attempt in range(3 if method == "GET" else 1):
                try:
                    minimum = (
                        5
                        if path in {"/portfolio/accounts", "/iserver/account/orders"}
                        else 1.3
                        if path == "/iserver/marketdata/history"
                        else 0.12
                    )
                    remaining = minimum - (time.monotonic() - self._last_requests.get(path, 0))
                    if remaining > 0:
                        time.sleep(remaining)
                    self._last_requests[path] = time.monotonic()
                    response = self._session.request(
                        method,
                        self.gateway_url + path,
                        params=params,
                        json=payload,
                        verify=self.verify_tls,
                        timeout=(5, 15 if channel == "trading" else 45),
                        allow_redirects=False,
                    )
                    if response.status_code in {401, 403, 302}:
                        raise IBKRError("IBKR 网关会话未登录或已失效，请在网关完成登录和双重认证")
                    if response.status_code >= 400:
                        if (
                            method == "GET"
                            and response.status_code in {429, 500, 502, 503, 504}
                            and attempt < 2
                        ):
                            time.sleep(min(2**attempt, 4))
                            continue
                        raise IBKRError(
                            f"IBKR 请求失败（HTTP {response.status_code}）",
                            uncertain=method != "GET" and response.status_code >= 500,
                        )
                    result = response.json()
                    if isinstance(result, dict) and result.get("error"):
                        raise IBKRError("IBKR 拒绝请求，请核对账户权限、订单和行情订阅")
                    self._last_success = self._now().isoformat()
                    self._failed.pop((channel, operation), None)
                    self._failure_count = 0
                    self._open_until = 0
                    if not self._failed:
                        self._last_error = None
                    return result
                except (requests.RequestException, ValueError) as exc:
                    if method == "GET" and attempt < 2:
                        time.sleep(min(2**attempt, 4))
                        continue
                    message = "IBKR 网关网络或TLS连接失败，请核对地址、证书和登录状态"
                    self._failure(operation, message, channel)
                    raise IBKRError(message, uncertain=method != "GET") from exc
                except IBKRError as exc:
                    self._failure(operation, str(exc), channel)
                    raise

    def _cached(self, key, callback, ttl=5, fresh=False):
        with self._lock:
            item = self._cache.get(key)
            if not fresh and item and time.monotonic() - item[0] < ttl:
                return copy.deepcopy(item[1])
            result = callback()
            self._cache[key] = (time.monotonic(), result)
            return copy.deepcopy(result)

    def probe_accounts(self, *, fresh=False):
        def read():
            status = self._request("POST", "/iserver/auth/status", operation="account")
            if (
                not status.get("authenticated")
                or not status.get("connected")
                or status.get("competing")
            ):
                raise IBKRError("IBKR 交易会话未就绪，请在网关完成登录并检查会话冲突")
            brokerage = self._request("GET", "/iserver/accounts", operation="account")
            paper = brokerage.get("isPaper")
            if type(paper) is not bool:
                raise IBKRError(
                    "IBKR未明确返回isPaper环境标识，不能仅凭账户前缀或端口决定模拟/实盘"
                )
            accounts = self._request("GET", "/portfolio/accounts", operation="account")
            if not isinstance(accounts, list):
                raise IBKRError("IBKR 返回的账户列表无效")
            return [
                {
                    "account_id": str(x.get("accountId") or x.get("id") or ""),
                    "paper": paper,
                    "selected": str(x.get("accountId") or x.get("id") or "")
                    == brokerage.get("selectedAccount"),
                    "currency": x.get("currency"),
                    "type": x.get("type"),
                }
                for x in accounts
            ]

        return self._cached("accounts", read, fresh=fresh)

    def _verify_account(self, *, fresh=False):
        if not self.configured:
            raise IBKRError("请先选择 IBKR 账户")
        accounts = self.probe_accounts(fresh=fresh)
        account = next((a for a in accounts if a["account_id"] == self.account_id), None)
        if not account:
            raise IBKRError("所选 IBKR 账户不在当前网关登录的账户列表中")
        if self.paper != account["paper"]:
            raise IBKRError("账户环境不匹配：真实账户不能按模拟账户接入，请重新选择并确认")
        if not account["selected"]:
            raise IBKRError("网关当前选中账户与配置不同，请重新验证并保存连接")
        return account

    def select_account(self):
        accounts = self.probe_accounts(fresh=True)
        account = next((a for a in accounts if a["account_id"] == self.account_id), None)
        if not account or account["paper"] != self.paper:
            raise IBKRError("所选账户与当前网关环境不匹配")
        if not account["selected"]:
            self._request(
                "POST", "/iserver/account", payload={"acctId": self.account_id}, operation="account"
            )
            self._cache.pop("accounts", None)
        return self._verify_account(fresh=True)

    def _ensure_writable(self):
        self._verify_account(fresh=True)
        if self._guard is not None:
            if not self._guard():
                raise IBKRError("券商或账户配置已变化，请重新连接；真实账户需重新确认")
        elif not self.paper:
            raise IBKRError("真实账户尚未获得当前配置的风险确认")

    @staticmethod
    def _number(value, default=None):
        try:
            result = float(str(value).replace(",", ""))
            return result if math.isfinite(result) else default
        except (ValueError, TypeError):
            return default

    def get_account(self):
        account = self._verify_account()
        summary = self._cached(
            "summary",
            lambda: self._request(
                "GET", f"/portfolio/{self.account_id}/summary", operation="account"
            ),
        )

        def amount(key):
            value = summary.get(key, {})
            if isinstance(value, dict) and value.get('isNull'):
                return None
            return self._number(value.get("amount") if isinstance(value, dict) else value)

        currency = (summary.get("netliquidation") or {}).get("currency") or account.get("currency")
        if currency not in {"USD", "BASE"}:
            raise IBKRError("当前美股执行接入要求USD基础币种账户，不能混用非美元金额")
        if currency == "BASE" and account.get("currency") != "USD":
            raise IBKRError("无法确认账户基础币种为USD")
        equity = amount("netliquidation")
        buying = amount("buyingpower")
        if equity is None or buying is None or amount('totalcashvalue') is None:
            raise IBKRError("IBKR 账户净值或购买力缺失，暂停下单")
        return {
            "id": self.account_id,
            "status": "ACTIVE",
            "equity": equity,
            "portfolio_value": equity,
            "cash": amount("totalcashvalue"),
            "buying_power": buying,
            "last_equity": None,
            "trading_blocked": False,
            "currency": "USD",
            "paper": self.paper,
            "broker": "ibkr",
        }

    def _positions(self, fresh=False):
        self._verify_account(fresh=fresh)

        def read():
            result = []
            for page in range(100):
                values = self._request(
                    "GET", f"/portfolio/{self.account_id}/positions/{page}", operation="positions"
                )
                if not isinstance(values, list):
                    raise IBKRError("IBKR 持仓响应无效")
                for x in values:
                    qty = self._number(x.get("position"))
                    if qty is None:
                        raise IBKRError("IBKR 持仓数量缺失")
                    if not qty:
                        continue
                    result.append(
                        {
                            "symbol": x.get("ticker") or x.get("contractDesc"),
                            "qty": qty,
                            "side": "long" if qty > 0 else "short",
                            "market_value": self._number(x.get("mktValue")),
                            "current_price": self._number(x.get("mktPrice")),
                            "avg_entry_price": self._number(x.get("avgCost")),
                            "unrealized_pl": self._number(x.get("unrealizedPnl")),
                            "conid": x.get("conid"),
                            "currency": x.get("currency"),
                        }
                    )
                if len(values) < 100:
                    return result
            raise IBKRError("IBKR 持仓页数超过限制，无法确认完整快照")

        return self._cached("positions", read, fresh=fresh)

    def get_positions(self):
        return self._positions()

    def get_positions_fresh(self):
        return self._positions(True)

    def _contract(self, symbol):
        symbol = symbol.strip().upper()
        if symbol in self._contracts:
            return self._contracts[symbol]
        results = self._request(
            "GET",
            "/iserver/secdef/search",
            params={"symbol": symbol, "secType": "STK"},
            operation="asset",
        )
        matches = []
        for item in results if isinstance(results, list) else []:
            if str(item.get("symbol", "")).upper() != symbol:
                continue
            if not any(s.get("secType") == "STK" for s in item.get("sections", [])):
                continue
            detail = self._request(
                "GET", f"/iserver/contract/{int(item['conid'])}/info", operation="asset"
            )
            if detail.get("currency") == "USD" and detail.get("instrument_type") == "STK":
                matches.append({**detail, "conid": int(item["conid"]), "symbol": symbol})
        if len(matches) != 1:
            raise IBKRError(f"{symbol} 的美元股票合约缺失或不唯一，请在IBKR核对合约")
        self._contracts[symbol] = matches[0]
        return matches[0]

    def get_asset(self, symbol):
        contract = self._contract(symbol)
        return {
            "symbol": contract["symbol"],
            "id": str(contract["conid"]),
            "status": "active",
            "tradable": True,
            "fractionable": False,
            "exchange": contract.get("exchange"),
            "currency": "USD",
        }

    def get_latest_quotes(self, symbols):
        self._verify_account()
        ids = {str(self._contract(s)["conid"]): s for s in symbols}
        params = {"conids": ",".join(ids), "fields": "31,84,86,85,88,6509"}
        result = []
        for _ in range(2):
            result = self._request(
                "GET",
                "/iserver/marketdata/snapshot",
                params=params,
                operation="latest_quotes",
                channel="data",
            )
            if (
                isinstance(result, list)
                and all("86" in x and "84" in x for x in result)
                and len(result) == len(ids)
            ):
                break
        quotes = {}
        for x in result if isinstance(result, list) else []:
            symbol = ids.get(str(x.get("conid") or x.get("conidEx", "").split("@")[0]))
            if not symbol:
                continue
            if not str(x.get("6509", "")).startswith("R"):
                message = "IBKR 未返回实时行情权限，不能用延迟行情确认自动订单"
                self._failure("latest_quotes", message, "data")
                raise IBKRError(message)
            timestamp = x.get("_updated")
            quotes[symbol] = {
                "bid_price": self._number(x.get("84")),
                "ask_price": self._number(x.get("86")),
                "timestamp": pd.to_datetime(timestamp, unit="ms", utc=True).isoformat()
                if timestamp
                else None,
            }
        if set(quotes) != set(symbols):
            raise IBKRError("IBKR 报价不完整，请检查行情权限与合约")
        return quotes

    def get_clock(self):
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XNYS")
        now = pd.Timestamp(self._now())
        date = now.tz_convert("America/New_York").date()
        session = calendar.date_to_session(str(date), direction="next")
        opening = calendar.session_open(session)
        closing = calendar.session_close(session)
        is_open = opening <= now < closing
        upcoming = calendar.next_session(session) if now >= opening else session
        next_close = (
            closing if now < closing else calendar.session_close(calendar.next_session(session))
        )
        return {
            "is_open": bool(is_open),
            "timestamp": now.isoformat(),
            "next_open": calendar.session_open(upcoming).isoformat(),
            "next_close": next_close.isoformat(),
            "calendar": "XNYS regular session",
        }

    def get_bars(self, symbols, timeframe, start, end, limit=None, use_cache=False):
        self._verify_account()
        bars = {"5Min": "5min", "15Min": "15min", "30Min": "30min", "1Hour": "1h", "1Day": "1d"}
        result = {}
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        start = start.tz_localize('UTC') if start.tzinfo is None else start.tz_convert('UTC')
        end = end.tz_localize('UTC') if end.tzinfo is None else end.tz_convert('UTC')
        for symbol in symbols:
            conid = self._contract(symbol)["conid"]
            parts = []
            cursor = end
            # Backward chunks stay below the documented per-response bar bound.
            days = 365 if timeframe == "1Day" else 1 if timeframe == "5Min" else 7
            for _ in range(600):
                if cursor < start:
                    break
                payload = self._request(
                    "GET",
                    "/iserver/marketdata/history",
                    params={
                        "conid": conid,
                        "period": f"{days}d",
                        "bar": bars[timeframe],
                        "startTime": cursor.strftime("%Y%m%d-%H:%M:%S"),
                        "direction": "-1",
                        "outsideRth": "false",
                        "source": "Last",
                    },
                    operation="stock_bars",
                    channel="data",
                )
                data = payload.get("data", [])
                if not data:
                    break
                frame = pd.DataFrame(data).rename(
                    columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
                )
                frame.index = pd.to_datetime(frame.pop("t"), unit="ms", utc=True)
                parts.append(frame[["open", "high", "low", "close", "volume"]])
                earliest = frame.index.min()
                if earliest >= cursor:
                    raise IBKRError("IBKR 历史数据游标未前进，已停止而非返回截断结果")
                cursor = earliest - pd.Timedelta(milliseconds=1)
            else:
                raise IBKRError("IBKR 历史请求范围过大，请缩小回测区间")
            if parts:
                frame = pd.concat(parts).sort_index()
                frame = frame.loc[~frame.index.duplicated(keep="last")]
                frame = frame.loc[(frame.index >= start) & (frame.index <= end)]
                frame = self._completed_regular_bars(frame, timeframe, end)
                frame.attrs['provider'] = 'ibkr'
                frame.attrs['adjustment'] = 'broker_default'
                frame.attrs['volume_factor'] = payload.get('volumeFactor')
            else:
                frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            result[symbol] = frame.tail(limit) if limit else frame
        return result

    @staticmethod
    def _completed_regular_bars(frame, timeframe, end):
        import exchange_calendars as xcals

        calendar = xcals.get_calendar('XNYS')
        keep, indices, closes = [], [], {}
        minutes = {'5Min': 5, '15Min': 15, '30Min': 30, '1Hour': 60}
        for stamp in frame.index:
            # Some daily responses use UTC midnight as a date label, rather than a session time.
            day = stamp.date() if timeframe == '1Day' and stamp == stamp.normalize() else stamp.tz_convert('America/New_York').date()
            if not calendar.is_session(str(day)):
                keep.append(False)
                indices.append(stamp)
                continue
            opening = calendar.session_open(str(day))
            closing = calendar.session_close(str(day))
            closes[day.isoformat()] = closing.isoformat()
            bar_end = closing if timeframe == '1Day' else min(stamp + pd.Timedelta(minutes=minutes[timeframe]), closing)
            keep.append(bool(bar_end <= end and (timeframe == '1Day' or opening <= stamp < closing)))
            indices.append(opening if timeframe == '1Day' else stamp)
        frame = frame.copy()
        frame.index = pd.DatetimeIndex(indices)
        frame = frame.loc[keep]
        frame.attrs['session_closes'] = closes
        return frame

    def recent_bars(self, symbols, timeframe, bars=300):
        end = self._now()
        per_day = {"5Min": 78, "15Min": 26, "30Min": 13, "1Hour": 7, "1Day": 1}[timeframe]
        days = max(14, math.ceil(bars / per_day * 7 / 5 * 1.3) + 10)
        return self._cached(
            ("bars", tuple(symbols), timeframe, bars),
            lambda: self.get_bars(symbols, timeframe, end - timedelta(days=days), end, limit=bars),
            ttl=10,
        )

    def _reference(self, client_order_id):
        value = (
            client_order_id
            if len(client_order_id) <= 64
            else "qp-" + hashlib.sha256(client_order_id.encode()).hexdigest()[:60]
        )
        self._client_ids[value] = client_order_id
        return value

    def _order_id(self, value):
        return f"ibkr:{self.account_id}:{value}"

    def _normalize_order(self, x):
        raw_id = x.get("orderId") or x.get("order_id")
        reference = str(x.get("order_ref") or x.get("cOID") or "")
        client = self._client_ids.get(reference) or self._resolver(reference) or reference
        raw_status = (
            str(x.get("status") or x.get("order_status") or "")
            .lower()
            .replace(" ", "")
            .replace("_", "")
        )
        status = {
            "presubmitted": "accepted",
            "submitted": "new",
            "pendingsubmit": "accepted",
            "filled": "filled",
            "cancelled": "canceled",
            "canceled": "canceled",
            "pendingcancel": "pending_cancel",
            "inactive": "rejected",
            "pending": "accepted",
        }.get(raw_status, raw_status or "unknown")
        filled = self._number(x.get("filledQuantity"), 0)
        qty = self._number(x.get("totalSize"), None)
        if qty is None:
            qty = filled + self._number(x.get("remainingQuantity"), 0)
        if filled > 0 and qty > filled and status not in {"canceled", "rejected", "expired"}:
            status = "partially_filled"
        side = str(x.get("side", "")).lower()
        result = {
            "id": self._order_id(raw_id),
            "broker_order_id": str(raw_id),
            "client_order_id": client,
            "symbol": x.get("ticker") or x.get("description1"),
            "side": side,
            "type": str(x.get("orderType") or "").lower(),
            "status": status,
            "qty": qty,
            "filled_qty": filled,
            "filled_avg_price": self._number(x.get("avgPrice")),
            "account_id": self.account_id,
            "broker": "ibkr",
            "raw": x,
        }
        if x.get("parentId"):
            result["parent_order_id"] = self._order_id(x["parentId"])
        return result

    def _orders(self, fresh=False):
        self._verify_account(fresh=fresh)

        def read():
            with self._orders_lock:
                remaining = 5 - (time.monotonic() - self._last_orders_at)
                if remaining > 0:
                    time.sleep(remaining)
                payload = self._request("GET", "/iserver/account/orders", operation="orders:open")
                self._last_orders_at = time.monotonic()
                if not isinstance(payload, dict) or not isinstance(payload.get('orders'), list):
                    raise IBKRError('IBKR订单快照无效，无法确认当前开放订单')
                values = payload['orders']
                if len(values) >= 1000:
                    raise IBKRError('IBKR近期订单达到响应上限，无法确认完整订单快照')
                orders = [
                    self._normalize_order(x)
                    for x in values
                    if str(x.get("acct") or x.get("account") or "") == self.account_id
                ]
                self._link_bracket_orders(orders)
                return orders

        return self._cached("orders", read, fresh=fresh)

    @staticmethod
    def _link_bracket_orders(orders):
        by_id = {o['id']: o for o in orders}
        for child in orders:
            parent = by_id.get(child.get('parent_order_id'))
            if not parent or parent.get('side') != 'buy':
                continue
            parent['order_class'] = 'bracket'
            child['order_class'] = 'bracket'
            if parent.get('status') not in {'filled', 'canceled', 'rejected', 'expired'} and parent.get('filled_qty', 0) < parent.get('qty', 0):
                child['status'] = 'held'

    def get_orders(self, status="all"):
        values = self._orders()
        return (
            [
                o
                for o in values
                if o["status"] not in {"filled", "canceled", "rejected", "expired", "replaced"}
            ]
            if status == "open"
            else values
        )

    def get_open_orders_fresh(self):
        return [
            o
            for o in self._orders(True)
            if o["status"] not in {"filled", "canceled", "rejected", "expired", "replaced"}
        ]

    def get_order_by_client_id(self, client_order_id):
        reference = self._reference(client_order_id)
        found = next(
            (o for o in self._orders(True) if o["client_order_id"] in {client_order_id, reference}),
            None,
        )
        if found is None and self._submission_attempted(client_order_id):
            raise IBKRError(
                "IBKR近期订单列表未找到原单，但无法证明从未提交；保持对账隔离，禁止重发"
            )
        return found

    def _submit(self, tickets, client_order_id):
        self._ensure_writable()
        reference = self._reference(client_order_id)
        existing = next(
            (o for o in self._orders(True) if o["client_order_id"] in {client_order_id, reference}),
            None,
        )
        if existing:
            return existing
        if client_order_id in self._attempted:
            raise AlpacaAmbiguousOrderError(client_order_id)
        self._attempted.add(client_order_id)
        try:
            reply = self._request(
                "POST",
                f"/iserver/account/{self.account_id}/orders",
                payload={"orders": tickets},
                operation="submit_order",
            )
        except IBKRError as exc:
            if not exc.uncertain:
                raise
            ambiguous = AlpacaAmbiguousOrderError(client_order_id)
            ambiguous.args = ("IBKR 提交结果尚未确认，将按cOID对账，禁止盲目重发",)
            raise ambiguous from exc
        items = reply if isinstance(reply, list) else [reply]
        if any(x.get('order_id') for x in items) and (
            len(items) != len(tickets) or any(not x.get('order_id') for x in items)
        ):
            # At least one leg may be live. A partial acknowledgement cannot be
            # classified as a rejected/no-order result or replayed.
            self._cache.pop('orders', None)
            raise AlpacaAmbiguousOrderError(client_order_id)
        if any(x.get("message") and x.get("id") and not x.get("order_id") for x in items):
            for x in items:
                if x.get("id") and x.get("message"):
                    self._request(
                        "POST",
                        f"/iserver/reply/{x['id']}",
                        payload={"confirmed": False},
                        operation="submit_order",
                    )
            raise IBKRError("IBKR 要求额外订单确认，本次警告已拒绝；请在网关核查订单设置")
        if not items or any(x.get("error") for x in items):
            raise IBKRError("IBKR 订单被拒绝，请检查权限与订单参数")
        first = items[0]
        if not first.get("order_id"):
            raise AlpacaAmbiguousOrderError(client_order_id)
        self._cache.pop("orders", None)
        self._cache.pop("positions", None)
        self._cache.pop("summary", None)
        ticket = tickets[0]
        normalized = self._normalize_order(
            {
                "orderId": first["order_id"],
                "order_ref": ticket["cOID"],
                "ticker": ticket.get("ticker"),
                "side": ticket["side"],
                "orderType": ticket["orderType"],
                "status": first.get("order_status", "Submitted"),
                "totalSize": ticket["quantity"],
            }
        )
        if len(items) > 1:
            normalized["legs"] = [
                self._normalize_order(
                    {
                        "orderId": response["order_id"],
                        "order_ref": child.get("cOID"),
                        "ticker": ticket["ticker"],
                        "side": "SELL",
                        "orderType": child["orderType"],
                        "status": response.get("order_status", "PreSubmitted"),
                        "totalSize": ticket["quantity"],
                        "parentId": first["order_id"],
                    }
                )
                for child, response in zip(tickets[1:], items[1:])
                if response.get("order_id")
            ]
            self._link_bracket_orders([normalized, *normalized['legs']])
        return normalized

    def submit_entry_order(
        self,
        *,
        symbol,
        qty,
        notional,
        order_type,
        time_in_force,
        client_order_id,
        limit_price=None,
        stop_price=None,
        take_price=None,
    ):
        if qty is None or qty <= 0:
            raise IBKRError("IBKR 接入需要有效股数")
        contract = self._contract(symbol)
        reference = self._reference(client_order_id)
        ticket = {
            "acctId": self.account_id,
            "conid": contract["conid"],
            "ticker": symbol,
            "cOID": reference,
            "orderType": "LMT" if order_type == "limit" else "MKT",
            "side": "BUY",
            "tif": time_in_force.upper(),
            "quantity": qty,
            "outsideRTH": False,
            "listingExchange": "SMART",
        }
        if order_type == "limit":
            ticket["price"] = round(float(limit_price), 2)
        tickets = [ticket]
        if stop_price is not None and take_price is not None:
            if not float(qty).is_integer():
                raise IBKRError("保护单当前仅支持整股")
            for suffix, kind, price in [("sl", "STP", stop_price), ("tp", "LMT", take_price)]:
                child = {
                    **ticket,
                    "side": "SELL",
                    "parentId": reference,
                    "orderType": kind,
                    "price": round(float(price), 2),
                }
                child.pop("cOID", None)
                tickets.append(child)
        return self._submit(tickets, client_order_id)

    def submit_exit_order(self, symbol, qty, client_order_id):
        positions = self.get_positions_fresh()
        actual = next((self._number(p["qty"], 0) for p in positions if p["symbol"] == symbol), 0)
        if qty <= 0 or qty > actual + 1e-9:
            raise IBKRError("卖出数量超过当前账户多头仓位")
        ticket = {
            "acctId": self.account_id,
            "conid": self._contract(symbol)["conid"],
            "ticker": symbol,
            "cOID": self._reference(client_order_id),
            "side": "SELL",
            "orderType": "MKT",
            "quantity": qty,
            "tif": "DAY",
            "outsideRTH": False,
        }
        return self._submit([ticket], client_order_id)

    def submit_trailing_stop(self, *args, **kwargs):
        raise IBKRError("当前IBKR适配尚未验证移动止损，请使用规则离场或Bracket保护单")

    def cancel_order(self, order_id):
        self._ensure_writable()
        prefix = f"ibkr:{self.account_id}:"
        if not str(order_id).startswith(prefix):
            raise IBKRError("订单账户不匹配，拒绝撤单")
        raw = str(order_id)[len(prefix) :]
        if not raw.isdigit():
            raise IBKRError("订单ID无效")
        result = self._request(
            "DELETE", f"/iserver/account/{self.account_id}/order/{raw}", operation="cancel_order"
        )
        self._cache.pop("orders", None)
        return result

    def cancel_all_orders(self):
        return [self.cancel_order(o["id"]) for o in self.get_open_orders_fresh()]

    def close_all_positions(self):
        # Explicit emergency action only; normal strategy exits use owned quantities in the engine.
        self.cancel_all_orders()
        if self.get_open_orders_fresh():
            raise IBKRError("撤单尚未确认，拒绝创建可能导致超卖的紧急平仓单")
        positions = self.get_positions_fresh()
        if any(float(p['qty']) < 0 for p in positions):
            raise IBKRError('当前接入仅支持多头平仓，账户存在空头，请在IBKR处理')
        return [
            self.submit_exit_order(
                p["symbol"],
                float(p["qty"]),
                "qp-emergency-"
                + hashlib.sha256(f"{p['symbol']}:{self._now()}".encode()).hexdigest()[:16],
            )
            for p in positions
            if float(p["qty"]) > 0
        ]

    async def start_streams(self, symbols, bar_callback, trade_callback):
        self.stop_streams()
        generation = self._poll_generation

        async def poll():
            seen = {}
            last_keepalive = 0
            while generation == self._poll_generation:
                try:
                    if time.monotonic() - last_keepalive >= 60:
                        await asyncio.to_thread(
                            self._request, "POST", "/tickle", operation="account"
                        )
                        last_keepalive = time.monotonic()
                    orders = await asyncio.to_thread(self.get_orders, "all")
                    if generation != self._poll_generation:
                        return
                    for order in orders:
                        key = (order["status"], order["filled_qty"])
                        if seen.get(order["id"]) == key:
                            continue
                        seen[order["id"]] = key
                        result = trade_callback(
                            {
                                "event": "fill" if order["status"] == "filled" else "update",
                                "order": order,
                            }
                        )
                        if inspect.isawaitable(result):
                            await result
                except Exception:
                    pass  # Health snapshot and the engine reconciliation path retain the failure.
                await asyncio.sleep(5)

        self._poll_task = asyncio.create_task(poll(), name="ibkr-order-poll")

    def stop_streams(self):
        # Called from a worker thread too: notify the event loop instead of canceling cross-thread.
        self._poll_generation += 1
        task = self._poll_task
        if task and not task.done():
            task.get_loop().call_soon_threadsafe(task.cancel)
        self._poll_task = None

    def streams_healthy(self):
        return bool(self._poll_task and not self._poll_task.done())
