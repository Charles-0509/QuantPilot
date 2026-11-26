"""Portable experiment inputs, without credentials or account identifiers."""

from __future__ import annotations

import gzip
import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pandas as pd

from app.schemas import BacktestRequest, RuleDefinition
from .backtest import run_backtest
from .instruments import is_option_symbol
from .option_backtest import ReplayOptionHistory, run_option_backtest


def engine_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    files = [
        "schemas.py",
        "services/backtest.py",
        "services/rules.py",
        "services/indicators.py",
        "services/sizing.py",
        "services/backtest_artifacts.py",
        "services/option_backtest.py",
        "services/instruments.py",
    ]
    digest = hashlib.sha256()
    for name in files:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return {
        "model": "bar-close-v2",
        "source_sha256": digest.hexdigest(),
        "python": platform.python_version(),
        "packages": {name: version(name) for name in ("pandas", "numpy", "pydantic")},
    }


def pack_experiment(definition, payload, frames, risk_settings, *, data_feed='iex', adjustment='all',
                    option_chains=None):
    data = {}
    coverage = {}
    for symbol, frame in frames.items():
        data[symbol] = {
            "frame": json.loads(
                frame.to_json(orient="split", date_format="iso", double_precision=15)
            ),
            "attrs": frame.attrs,
        }
        coverage[symbol] = {
            "bars": len(frame),
            "start": frame.index[0].isoformat() if len(frame) else None,
            "end": frame.index[-1].isoformat() if len(frame) else None,
        }
    raw_data = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    metadata = {
        "engine": engine_identity(),
        "dataset_sha256": hashlib.sha256(raw_data).hexdigest(),
        **({"option_chains_sha256": hashlib.sha256(json.dumps(
            option_chains, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "option_contracts": sorted(symbol for symbol in frames if is_option_symbol(symbol))}
           if option_chains is not None else {}),
        "data_feed": data_feed,
        "adjustment": adjustment,
        "coverage": coverage,
    }
    bundle = {
        "format": 1,
        "strategy_definition": definition,
        "request": payload,
        "risk_settings": risk_settings,
        **metadata,
        "data": data,
        **({"option_chains": option_chains} if option_chains is not None else {}),
    }
    raw = json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return gzip.compress(raw, mtime=0), metadata


def replay_experiment(content: bytes):
    bundle = json.loads(gzip.decompress(content))
    if bundle["engine"] != engine_identity():
        raise ValueError("回测引擎或依赖版本已变化，请使用快照记录的环境复现")
    raw_data = json.dumps(
        bundle["data"], sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if hashlib.sha256(raw_data).hexdigest() != bundle["dataset_sha256"]:
        raise ValueError("回测数据校验失败")
    frames = {}
    for symbol, item in bundle["data"].items():
        split = item["frame"]
        frame = pd.DataFrame(
            split["data"], columns=split["columns"], index=pd.to_datetime(split["index"], utc=True)
        )
        frame.attrs = item["attrs"]
        frames[symbol] = frame
    definition = RuleDefinition.model_validate(bundle["strategy_definition"])
    payload = BacktestRequest.model_validate(bundle["request"])
    if definition.is_option:
        chains = bundle.get("option_chains") or {}
        digest = hashlib.sha256(json.dumps(chains, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        if digest.hexdigest() != bundle.get("option_chains_sha256"):
            raise ValueError("回测期权合约快照校验失败")
        return run_option_backtest(
            definition, {s: frames[s] for s in definition.symbols}, ReplayOptionHistory(chains, frames),
            initial_cash=payload.initial_cash, slippage_bps=payload.slippage_bps, commission=payload.commission,
            benchmark=frames[payload.benchmark].loc[frames[payload.benchmark].index >= payload.start],
            start=payload.start, end=payload.end, risk_settings=bundle["risk_settings"],
        )
    return run_backtest(
        definition,
        {s: frames[s] for s in definition.symbols},
        initial_cash=payload.initial_cash,
        slippage_bps=payload.slippage_bps,
        commission=payload.commission,
        benchmark=frames[payload.benchmark].loc[frames[payload.benchmark].index >= payload.start],
        start=payload.start,
        risk_settings=bundle["risk_settings"],
    )
