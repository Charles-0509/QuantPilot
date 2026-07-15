from app.api import (
    collect_dashboard_snapshot,
    engine_connection_state,
    operational_engine_status,
)


class PartialAlpaca:
    configured = True

    def get_account(self):
        return {"equity": "100000"}

    def get_positions(self):
        raise ConnectionError("positions unavailable")

    def get_orders(self, _status):
        return [{"id": "open-1"}]

    def get_clock(self):
        raise TimeoutError("clock unavailable")


def test_dashboard_snapshot_preserves_independent_successes() -> None:
    snapshot = collect_dashboard_snapshot(PartialAlpaca())

    assert snapshot["account"] == {"equity": "100000"}
    assert snapshot["orders"] == []
    assert snapshot["positions"] == []
    assert snapshot["clock"] == {}
    assert snapshot["availability"] == {
        "account": "fresh",
        "positions": "unavailable",
        "orders": "unavailable",
        "clock": "unavailable",
    }
    assert snapshot["data_errors"] == {
        "positions": "暂时无法确认当前持仓",
        "orders": "暂时无法确认开放订单",
        "clock": "市场开闭状态暂时不可用",
    }


def test_operational_engine_status_never_accepts_orders_while_degraded() -> None:
    assert operational_engine_status("paused", {"state": "connected"})[:2] == (
        "paused",
        False,
    )
    assert operational_engine_status("running", {"state": "connected"})[:2] == (
        "active",
        True,
    )
    assert operational_engine_status("running", {"state": "degraded"})[:2] == (
        "degraded",
        False,
    )
    assert operational_engine_status("running", {"state": "circuit_open"})[:2] == (
        "circuit_open",
        False,
    )


def test_engine_gate_ignores_research_failures_but_blocks_execution_dependencies() -> None:
    auxiliary = {
        "state": "degraded",
        "health": {
            "failed_operations": [
                {"channel": "data", "operation": "historical_research"}
            ]
        },
    }

    assert engine_connection_state(auxiliary) == "connected"
    assert operational_engine_status("running", auxiliary)[:2] == ("active", True)
    for channel, operation in [
        ("trading", "orders:open"),
        ("trading", "asset"),
        ("data", "latest_quotes"),
    ]:
        critical = {
            "state": "degraded",
            "health": {
                "failed_operations": [
                    {"channel": channel, "operation": operation}
                ]
            },
        }
        assert engine_connection_state(critical) == "degraded"
        assert operational_engine_status("running", critical)[:2] == (
            "degraded",
            False,
        )
