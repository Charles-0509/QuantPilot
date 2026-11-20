from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.services.ibkr_service import IBKRService, IBKRError
from app.services.alpaca_service import AlpacaAmbiguousOrderError


@pytest.fixture
def client(monkeypatch, tmp_path):
    service = IBKRService(
        Settings(_env_file=None, investor_db_path=str(tmp_path / "unused.db")), account_id="DU123"
    )
    state = {
        "paper": True,
        "selected": "DU123",
        "orders": [],
        "post_reply": [{"order_id": "101", "order_status": "Submitted"}],
        "calls": [],
    }

    def request(method, path, **kwargs):
        state["calls"].append((method, path, kwargs))
        if path == "/iserver/auth/status":
            return {"authenticated": True, "connected": True, "competing": False}
        if path == "/iserver/accounts":
            return {
                "isPaper": state["paper"],
                "selectedAccount": state["selected"],
                "accounts": ["DU123", "U123"],
            }
        if path == "/portfolio/accounts":
            return [
                {"accountId": "DU123", "currency": "USD"},
                {"accountId": "U123", "currency": "USD"},
            ]
        if path == "/iserver/account/orders":
            return {"orders": state["orders"]}
        if path == "/iserver/account/DU123/orders":
            return state["post_reply"]
        if path.startswith("/iserver/reply/"):
            return {"message": "declined"}
        if path == "/iserver/secdef/search":
            return [{"symbol": "AAPL", "conid": 265598, "sections": [{"secType": "STK"}]}]
        if path == "/iserver/contract/265598/info":
            return {"symbol": "AAPL", "con_id": 265598, "currency": "USD", "instrument_type": "STK"}
        if path == "/iserver/marketdata/snapshot":
            return [
                {
                    "conid": 265598,
                    "84": "100.01",
                    "86": "100.03",
                    "6509": state.get("md", "RpB"),
                    "_updated": 1792674000000,
                }
            ]
        if path.endswith("/summary"):
            return {
                "netliquidation": {"amount": 100000, "currency": "USD"},
                "buyingpower": {"amount": 100000},
                "totalcashvalue": {"amount": 100000},
            }
        raise AssertionError((method, path, kwargs))

    monkeypatch.setattr(service, "_request", request)
    return service, state


def submit(service, cid="qp-test", **kwargs):
    return service.submit_entry_order(
        symbol="AAPL",
        qty=1,
        notional=None,
        order_type="market",
        time_in_force="day",
        client_order_id=cid,
        **kwargs,
    )


def test_environment_uses_broker_flag_not_du_prefix(client):
    service, state = client
    state["paper"] = False
    with pytest.raises(IBKRError, match="环境不匹配"):
        submit(service)
    assert not any(m == "POST" and p.endswith("/orders") for m, p, _ in state["calls"])


def test_gateway_contract_info_schema_selects_unique_usd_stock(client, monkeypatch):
    service, state = client
    original = service._request
    currencies = {265598: "USD", 532640894: "CAD", 38708077: "MXN", 273982664: "CHF"}

    def request(method, path, **kwargs):
        if path == "/iserver/secdef/search":
            return [
                {"symbol": "AAPL", "conid": str(cid), "sections": [{"secType": "STK"}]}
                for cid in currencies
            ]
        if path.startswith("/iserver/contract/"):
            cid = int(path.split("/")[-2])
            return {
                "con_id": cid, "symbol": "AAPL", "currency": currencies[cid],
                "instrument_type": "STK", "exchange": "SMART",
            }
        return original(method, path, **kwargs)

    monkeypatch.setattr(service, "_request", request)
    asset = service.get_asset("AAPL")
    assert asset["id"] == "265598" and asset["currency"] == "USD"
    assert service.get_latest_quotes(["AAPL"])["AAPL"]["bid_price"] == 100.01
    assert not any(m in {"POST", "DELETE"} and p.endswith("/orders") for m, p, _ in state["calls"])


@pytest.mark.parametrize("instrument_type", [None, "CFD", "OPT"])
def test_contract_info_requires_explicit_stock_type(client, monkeypatch, instrument_type):
    service, state = client
    original = service._request

    def request(method, path, **kwargs):
        if path == "/iserver/contract/265598/info":
            return {"con_id": 265598, "currency": "USD", "instrument_type": instrument_type}
        return original(method, path, **kwargs)

    monkeypatch.setattr(service, "_request", request)
    with pytest.raises(IBKRError, match="合约缺失或不唯一"):
        service.get_asset("AAPL")
    assert not any(m == "POST" and p.endswith("/orders") for m, p, _ in state["calls"])


def test_unknown_environment_and_wrong_selected_account_fail_closed(client):
    service, state = client
    state["paper"] = None
    with pytest.raises(IBKRError, match="isPaper"):
        service.probe_accounts(fresh=True)
    state["paper"] = True
    state["selected"] = "U123"
    with pytest.raises(IBKRError, match="选中账户"):
        submit(service)


def test_live_mode_without_context_consent_cannot_submit(client):
    service, state = client
    service.paper = False
    service.account_id = "U123"
    state["paper"] = False
    state["selected"] = "U123"
    with pytest.raises(IBKRError, match="风险确认"):
        submit(service)
    service._guard = lambda: False
    with pytest.raises(IBKRError, match="配置已变化"):
        submit(service)


def test_bracket_body_and_namespaced_parent_ids(client):
    service, state = client
    state["post_reply"] = [
        {"order_id": "101", "order_status": "Submitted"},
        {"order_id": "102", "order_status": "PreSubmitted"},
        {"order_id": "103", "order_status": "PreSubmitted"},
    ]
    result = submit(service, stop_price=98, take_price=104)
    body = next(k["payload"] for m, p, k in state["calls"] if m == "POST" and p.endswith("/orders"))
    parent, *legs = body["orders"]
    assert parent["cOID"] == "qp-test" and parent["acctId"] == "DU123"
    assert all(x["parentId"] == "qp-test" and "cOID" not in x for x in legs)
    assert result["id"] == "ibkr:DU123:101"
    assert all(x["parent_order_id"] == result["id"] for x in result["legs"])


def test_warning_is_never_automatically_confirmed(client):
    service, state = client
    state["post_reply"] = [
        {"id": "warning-id", "message": ["Market data warning"], "messageIds": ["o163"]}
    ]
    with pytest.raises(IBKRError, match="警告已拒绝"):
        submit(service)
    replies = [k["payload"] for m, p, k in state["calls"] if p.startswith("/iserver/reply/")]
    assert replies == [{"confirmed": False}]


def test_missing_order_after_prior_attempt_is_not_a_confirmed_404(client):
    service, _ = client
    service._submission_attempted = lambda cid: True
    with pytest.raises(IBKRError, match="无法证明"):
        service.get_order_by_client_id("qp-test")


def test_submit_timeout_is_not_replayed(client, monkeypatch):
    service, state = client
    original = service._request

    def request(method, path, **kwargs):
        if method == "POST" and path.endswith("/orders"):
            state["calls"].append((method, path, kwargs))
            raise IBKRError("timeout", uncertain=True)
        return original(method, path, **kwargs)

    monkeypatch.setattr(service, "_request", request)
    with pytest.raises(AlpacaAmbiguousOrderError):
        submit(service)
    with pytest.raises(AlpacaAmbiguousOrderError):
        submit(service)
    assert sum(m == "POST" and p.endswith("/orders") for m, p, _ in state["calls"]) == 1


def test_orders_are_filtered_by_account_and_cannot_cancel_other_account(client):
    service, state = client
    state["orders"] = [
        {
            "acct": "DU123",
            "orderId": 101,
            "order_ref": "qp-test",
            "ticker": "AAPL",
            "side": "BUY",
            "status": "Filled",
            "filledQuantity": 1,
            "totalSize": 1,
        },
        {
            "acct": "U123",
            "orderId": 101,
            "order_ref": "other",
            "ticker": "AAPL",
            "side": "BUY",
            "status": "Filled",
            "filledQuantity": 2,
            "totalSize": 2,
        },
    ]
    orders = service.get_orders()
    assert len(orders) == 1 and orders[0]["filled_qty"] == 1 and orders[0]["id"] == "ibkr:DU123:101"
    with pytest.raises(IBKRError, match="账户不匹配"):
        service.cancel_order("ibkr:U123:101")


def test_delayed_snapshot_is_not_treated_as_live_quote(client):
    service, state = client
    quotes = service.get_latest_quotes(["AAPL"])
    assert quotes["AAPL"]["bid_price"] == 100.01 and quotes["AAPL"]["ask_price"] == 100.03
    state["md"] = "DpB"
    with pytest.raises(IBKRError, match="实时行情"):
        service.get_latest_quotes(["AAPL"])


def test_derived_clock_handles_early_close_and_holiday(client):
    service, _ = client
    service._now = lambda: datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc)
    assert service.get_clock()["is_open"]
    assert service.get_clock()["next_close"].startswith("2026-11-27T18:00")
    service._now = lambda: datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
    assert not service.get_clock()["is_open"]
    assert service.get_clock()["next_open"].startswith("2026-11-27T14:30")


def test_http_200_error_is_rejected_not_counted_as_success(client, monkeypatch):
    service, _ = client

    class Response:
        status_code = 200

        def json(self):
            return {"error": "insufficient funds"}

    monkeypatch.setattr(service._session, "request", lambda *a, **k: Response())
    with pytest.raises(IBKRError, match="拒绝请求"):
        IBKRService._request(
            service,
            "POST",
            "/iserver/account/DU123/orders",
            payload={"orders": []},
            operation="submit_order",
        )


def test_history_uses_backward_chunks_and_completed_session_boundaries(client, monkeypatch):
    import pandas as pd

    service, state = client
    original = service._request
    responses = [
        {'volumeFactor': 100, 'data': [
            {'t': int(pd.Timestamp(t).timestamp() * 1000), 'o': 100, 'h': 102, 'l': 99, 'c': 101, 'v': 123}
            for t in ['2026-11-27T17:30Z', '2026-11-27T17:35Z']
        ]},
        {'data': []},
    ]
    def request(method, path, **kwargs):
        if path == '/iserver/marketdata/history':
            state['calls'].append((method, path, kwargs))
            return responses.pop(0)
        return original(method, path, **kwargs)
    monkeypatch.setattr(service, '_request', request)
    frame = service.get_bars(['AAPL'], '5Min', datetime(2026, 11, 27, 15, tzinfo=timezone.utc), datetime(2026, 11, 27, 17, 37, tzinfo=timezone.utc))['AAPL']
    assert len(frame) == 1 and frame.iloc[0]['close'] == 101 and frame.iloc[0]['volume'] == 123
    assert frame.attrs['session_closes']['2026-11-27'].startswith('2026-11-27T18:00')
    assert all(k['params']['direction'] == '-1' for m, p, k in state['calls'] if p.endswith('/history'))


def test_daily_and_short_final_hour_are_complete_only_at_early_close():
    import pandas as pd
    daily = pd.DataFrame({'close': [100]}, index=pd.to_datetime(['2026-11-27T00:00Z']))
    before = pd.Timestamp('2026-11-27T17:59Z')
    close = pd.Timestamp('2026-11-27T18:00Z')
    assert IBKRService._completed_regular_bars(daily, '1Day', before).empty
    ready = IBKRService._completed_regular_bars(daily, '1Day', close)
    assert len(ready) == 1 and ready.index[0] == pd.Timestamp('2026-11-27T14:30Z')
    hour = pd.DataFrame({'close': [100]}, index=pd.to_datetime(['2026-11-27T17:30Z']))
    assert IBKRService._completed_regular_bars(hour, '1Hour', before).empty
    assert len(IBKRService._completed_regular_bars(hour, '1Hour', close)) == 1


def test_null_summary_and_invalid_order_snapshot_are_not_zero_or_empty(client, monkeypatch):
    service, _ = client
    original = service._request
    def request(method, path, **kwargs):
        if path.endswith('/summary'):
            return {'netliquidation': {'amount': 0, 'currency': 'USD', 'isNull': True}, 'buyingpower': {'amount': 100}, 'totalcashvalue': {'amount': 50}}
        if path.endswith('/account/orders'):
            return {'message': 'unavailable'}
        return original(method, path, **kwargs)
    monkeypatch.setattr(service, '_request', request)
    with pytest.raises(IBKRError, match='缺失'):
        service.get_account()
    with pytest.raises(IBKRError, match='快照无效'):
        service.get_open_orders_fresh()


def test_partial_bracket_acknowledgement_is_ambiguous_not_rejected(client):
    service, state = client
    state['post_reply'] = [{'order_id': '101', 'order_status': 'Submitted'}, {'error': 'child refused'}]
    with pytest.raises(AlpacaAmbiguousOrderError):
        submit(service, stop_price=98, take_price=104)


def test_unfilled_ibkr_bracket_legs_do_not_reserve_nonexistent_position(client):
    from app.services.engine import TradingEngine
    service, state = client
    state['post_reply'] = [{'order_id': str(i), 'order_status': 'PreSubmitted'} for i in [101, 102, 103]]
    result = submit(service, stop_price=98, take_price=104)
    assert all(o['status'] == 'held' for o in result['legs'])
    assert TradingEngine._reserved_sell_qty([result], 'AAPL') == 0
    result['status'] = 'filled'
    result['filled_qty'] = 1
    for leg in result['legs']:
        leg['status'] = 'new'
    assert TradingEngine._reserved_sell_qty([result], 'AAPL') == 1
