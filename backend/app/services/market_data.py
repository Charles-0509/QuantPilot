"""Route market data independently of the execution broker.

IBKR free accounts lack US real-time entitlement, while an Alpaca account
(free) streams IEX in real time and serves consolidated SIP history. The router
keeps every trading call on the broker and only redirects bar/quote reads.
"""

from __future__ import annotations

from typing import Any

DATA_METHODS = {"get_bars", "recent_bars", "get_latest_quotes"}


class MarketDataRouter:
    def __init__(self, broker: Any, data: Any):
        object.__setattr__(self, "broker", broker)
        object.__setattr__(self, "data", data)

    def __getattr__(self, name: str) -> Any:
        target = self.data if name in DATA_METHODS else self.broker
        return getattr(target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.broker, name, value)

    @property
    def feed(self) -> str:
        return self.data.feed

    @property
    def history_adjustment(self) -> str:
        return getattr(self.data, "history_adjustment", "all")

    @property
    def market_data_source(self) -> str:
        return "alpaca"

    def connection_status(self) -> dict[str, Any]:
        status = dict(self.broker.connection_status())
        data = self.data.connection_status()
        status["market_data"] = {"source": "alpaca", "feed": data.get("feed"), "state": data.get("state"),
                                 "message": data.get("message")}
        status["feed"] = data.get("feed")
        return status


def market_data_info(client: Any) -> dict[str, Any]:
    """What the UI should say about the current quote and bar source."""
    source = getattr(client, "market_data_source", None) or getattr(client, "broker_name", "alpaca")
    if source == "alpaca":
        return {"source": "alpaca", "label": "Alpaca 免费行情",
                "live": "IEX 实时（单一交易所，成交量偏小）",
                "history": "SIP 全市场（15 分钟前及更早）"}
    return {"source": "ibkr", "label": "IBKR 行情", "live": "取决于 IBKR 账户行情订阅", "history": "IBKR 历史数据"}
