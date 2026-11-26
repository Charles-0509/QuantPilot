"""Instrument helpers shared by execution, backtests and performance.

Option contracts use the OCC symbol Alpaca reports, e.g. ``AAPL240119C00190000``:
root, expiry (YYMMDD), C/P and strike x 1000. One contract controls 100 shares.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime

OPTION_MULTIPLIER = 100
OCC_PATTERN = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass(frozen=True, slots=True)
class OptionSymbol:
    symbol: str
    root: str
    expiration: date
    right: str          # "call" | "put"
    strike: float


def parse_option_symbol(symbol: str | None) -> OptionSymbol | None:
    match = OCC_PATTERN.match(str(symbol or "").strip().upper())
    if not match:
        return None
    root, yy, mm, dd, right, strike = match.groups()
    try:
        expiration = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return OptionSymbol(match.group(0), root, expiration, "call" if right == "C" else "put", int(strike) / 1000)


def is_option_symbol(symbol: str | None) -> bool:
    return parse_option_symbol(symbol) is not None


def option_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    return f"{root.upper()}{expiration:%y%m%d}{'C' if right == 'call' else 'P'}{round(strike * 1000):08d}"


def underlying_of(symbol: str) -> str:
    parsed = parse_option_symbol(symbol)
    return parsed.root if parsed else str(symbol or "").upper()


def multiplier(symbol: str) -> int:
    return OPTION_MULTIPLIER if is_option_symbol(symbol) else 1


def round_option_price(price: float, side: str) -> float:
    """Round to a $0.05 increment, valid for both penny and nickel option classes.

    Buys round up and sells round down so a marketable limit stays marketable.
    """
    if not math.isfinite(price) or price <= 0:
        return 0.0
    ticks = price / 0.05
    ticks = math.ceil(ticks - 1e-9) if side == "buy" else math.floor(ticks + 1e-9)
    return round(max(ticks, 1) * 0.05, 2)


def days_to_expiry(expiration: date, today: date | datetime) -> int:
    current = today.date() if isinstance(today, datetime) else today
    return (expiration - current).days


def target_strike(underlying_price: float, right: str, mode: str, offset_pct: float) -> float:
    """Strike target from moneyness: OTM calls sit above spot, OTM puts below."""
    if mode == "atm" or not offset_pct:
        return underlying_price
    direction = 1 if (mode == "otm_pct") == (right == "call") else -1
    return underlying_price * (1 + direction * offset_pct / 100)


def choose_contract(contracts: list[dict], underlying_price: float, config, today: date) -> dict | None:
    """Pick the listed contract closest to the configured expiry window and strike.

    The expiration nearest the middle of [min_dte, max_dte] wins; ties prefer the
    earlier date. Within it, the strike closest to the target wins, preferring the
    more out-of-the-money strike on ties (it costs less).
    """
    eligible = []
    for contract in contracts:
        parsed = parse_option_symbol(contract.get("symbol"))
        if parsed is None or parsed.right != config.right:
            continue
        if str(contract.get("type") or parsed.right).lower().split(".")[-1] != config.right:
            continue
        if contract.get("tradable") is False:
            continue
        if str(contract.get("style") or "american").lower().split(".")[-1] != "american":
            continue
        dte = days_to_expiry(parsed.expiration, today)
        if dte < config.min_dte or dte > config.max_dte:
            continue
        open_interest = contract.get("open_interest")
        if config.min_open_interest and (open_interest is None or float(open_interest) < config.min_open_interest):
            continue
        eligible.append((parsed, contract, dte))
    if not eligible:
        return None
    middle = (config.min_dte + config.max_dte) / 2
    best_dte = min({dte for _parsed, _contract, dte in eligible}, key=lambda dte: (abs(dte - middle), dte))
    same_expiry = [(parsed, contract) for parsed, contract, dte in eligible if dte == best_dte]
    target = target_strike(underlying_price, config.right, config.strike_mode, config.strike_offset_pct)
    otm_sign = 1 if config.right == "call" else -1
    parsed, contract = min(same_expiry, key=lambda item: (abs(item[0].strike - target), -otm_sign * item[0].strike))
    return contract
