#!/usr/bin/env python3
"""Replay a single sizing decision from the audit log.

Usage:
    scripts/replay_sizing.py <audit_log.jsonl> <ts>
    scripts/replay_sizing.py <audit_log.jsonl> --ticker SBER
    scripts/replay_sizing.py <audit_log.jsonl> --all

What it does
------------
Reads the audit log (JSONL — one line per ``compute_position_size`` call),
finds the record(s) matching ``ts`` (exact ISO match), ``--ticker`` (latest
matching), or ``--all`` (every record), and re-runs the sizing formula using
the stored inputs. Prints the rebuilt scalars + final size alongside the
stored record so an operator can spot divergence.

This tool is the rollback companion to ``src/broker/sizing.py``. It does NOT
need the original Quote / PortfolioState / MarketData — it reconstructs them
from the audit row's JSONB blobs. The recomputation uses the formula version
recorded on the row, so v1 rows replay under v1 forever (task body §3
"Rollback").

Exit codes
----------
0 - every record reproduced identically (within Decimal quantization)
1 - at least one record diverged (output: divergence report)
2 - bad invocation / file missing
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

# Make src/ importable when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from src.broker.sizing import (  # noqa: E402  (path tweak above)
    Bar,
    MarketData,
    PortfolioState,
    Quote,
    compute_position_size,
    compute_position_size_v1,
)
from src.macro.models import MacroRegime, RegimeLabel  # noqa: E402


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load all JSONL records. Each line MUST be a complete JSON object."""
    if not path.exists():
        raise SystemExit(f"audit log not found: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for ln_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"line {ln_no}: invalid JSON: {exc}") from exc
            rec["_line"] = ln_no
            records.append(rec)
    return records


def select_records(
    records: list[dict[str, Any]],
    ts: str | None,
    ticker: str | None,
    all_records: bool,
) -> list[dict[str, Any]]:
    if all_records:
        return records
    if ts is not None:
        matched = [r for r in records if r.get("ts") == ts]
        if not matched:
            raise SystemExit(f"no record with ts={ts!r}")
        return matched
    if ticker is not None:
        ticker = ticker.upper()
        matched = [r for r in records if r.get("ticker", "").upper() == ticker]
        if not matched:
            raise SystemExit(f"no record for ticker={ticker!r}")
        return matched[-1:]  # latest
    raise SystemExit("specify --ts, --ticker, or --all")


def _build_quote(rec: dict[str, Any]) -> Quote:
    ts_raw = rec["ts"]
    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    inputs = rec.get("inputs", {})
    return Quote(
        ticker=rec["ticker"],
        side=rec["side"],
        confidence=Decimal(inputs.get("confidence", "1.0")),
        timestamp=ts,
        reference_price=Decimal(rec["output"].get("price", "1")),
    )


def _build_portfolio(rec: dict[str, Any]) -> PortfolioState:
    inputs = rec.get("inputs", {})
    return PortfolioState(
        cash=Decimal(inputs["cash"]),
        peak_equity=Decimal(inputs["peak_equity"]),
        total_equity=Decimal(inputs["total_equity"]),
    )


def _build_market_data(rec: dict[str, Any], n_bars: int) -> MarketData:
    """Reconstruct synthetic bars.

    We don't have raw bars in the audit row (that would bloat the log);
    we approximate ATR by reconstructing bars with constant range. This is
    lossy by design — replay reproduces the *scalars* from the stored
    fields, not the bars themselves. The divergence between reconstructed
    and stored scalars is expected when the original bars were noisy; the
    operator inspects both columns side-by-side in the report.
    """
    inputs = rec.get("inputs", {})
    if n_bars <= 0:
        return MarketData(ticker=rec["ticker"], bars=())
    # Approximate bars: H-L = atr_frac * price (using stored ATR if any).
    price = Decimal(rec["output"].get("price", "100"))
    # Reconstruct close ≈ price; range ≈ stored atr_frac * price.
    atr_frac = Decimal(inputs.get("atr_frac", "0.02"))
    low = price * (Decimal("1") - atr_frac)
    high = low + price * atr_frac
    bars = tuple(Bar(high=high, low=low, close=price) for _ in range(max(n_bars, 20)))
    return MarketData(ticker=rec["ticker"], bars=bars)


def _build_regime(rec: dict[str, Any]) -> MacroRegime:
    inputs = rec.get("inputs", {})
    label_str = inputs.get("regime", "neutral")
    label: RegimeLabel
    if label_str == "risk_off":
        label = "risk_off"
    elif label_str == "risk_on_reduced":
        label = "risk_on_reduced"
    else:
        label = "neutral"
    return MacroRegime(
        regime=label,
        multiplier=Decimal(inputs.get("regime_multiplier", "1.0")),
        reason="replay",
        snapshot=None,
    )


def replay_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Replay a single record. Returns a divergence report dict."""
    inputs = rec.get("inputs", {})
    n_bars = int(inputs.get("n_bars", 20))
    quote = _build_quote(rec)
    portfolio = _build_portfolio(rec)
    market = _build_market_data(rec, n_bars)
    regime = _build_regime(rec)
    fn = compute_position_size_v1 if rec.get("formula_version") == "v1" else compute_position_size
    spec = fn(quote, portfolio, market, regime)
    stored_qty = Decimal(rec["output"].get("final_size", "0"))
    stored_skip = bool(rec["output"].get("skip", False))
    return {
        "line": rec.get("_line"),
        "ts": rec.get("ts"),
        "ticker": rec.get("ticker"),
        "formula_version": rec.get("formula_version"),
        "stored_quantity": str(stored_qty),
        "replayed_quantity": str(spec.quantity),
        "stored_skip": stored_skip,
        "replayed_skip": spec.skip,
        "skip_reason": spec.skip_reason,
        "scalars_replayed": {
            "vol": spec.meta.get("vol_scalar"),
            "liq": spec.meta.get("liq_scalar"),
            "dd": spec.meta.get("dd_scalar"),
            "regime": spec.meta.get("regime_scalar"),
        },
        "scalars_stored": rec.get("scalars", {}),
        "diverged": stored_qty != spec.quantity or stored_skip != spec.skip,
    }


def render(reports: Iterable[dict[str, Any]]) -> int:
    rc = 0
    for r in reports:
        flag = "DIVERGED" if r["diverged"] else "OK"
        print(
            f"[{flag}] line={r['line']} ts={r['ts']} ticker={r['ticker']} "
            f"v={r['formula_version']} stored_qty={r['stored_quantity']} "
            f"replayed_qty={r['replayed_quantity']}"
        )
        if r["diverged"]:
            print(f"    skip stored={r['stored_skip']} replayed={r['replayed_skip']}")
            print(f"    skip_reason={r['skip_reason']}")
            print(f"    scalars_stored={r['scalars_stored']}")
            print(f"    scalars_replayed={r['scalars_replayed']}")
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay sizing decisions from a JSONL audit log.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("audit_log", type=Path, help="Path to sizing_audit_*.jsonl")
    parser.add_argument("ts", nargs="?", help="Exact ISO ts to replay (e.g. 2026-08-22T09:30:00+00:00)")
    parser.add_argument("--ticker", help="Replay latest row for ticker")
    parser.add_argument("--all", action="store_true", help="Replay every row")
    args = parser.parse_args(argv)

    if args.ts is None and args.ticker is None and not args.all:
        parser.error("provide ts positional arg, or --ticker / --all")

    records = load_records(args.audit_log)
    selected = select_records(records, args.ts, args.ticker, args.all)
    reports = (replay_record(r) for r in selected)
    return render(reports)


if __name__ == "__main__":
    sys.exit(main())
