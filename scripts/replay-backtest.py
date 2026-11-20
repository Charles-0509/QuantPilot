#!/usr/bin/env python3
"""Replay a downloaded QuantPilot experiment without credentials or network calls."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.services.backtest_artifacts import replay_experiment  # noqa: E402

if len(sys.argv) != 2:
    raise SystemExit('用法: python scripts/replay-backtest.py backtest-<id>.json.gz')
result = replay_experiment(Path(sys.argv[1]).read_bytes())
print(json.dumps({'metrics': result.metrics, 'trades': result.trades}, ensure_ascii=False, allow_nan=False, indent=2))
