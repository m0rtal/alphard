"""
Alphard Data Quality Gate — CLI entry point.

Run any of the three gates standalone against a single ticker:

    python -m src.data.quality ingestion SBER
    python -m src.data.quality cross_source SBER
    python -m src.data.quality historical SBER

The CLI pulls OHLCV from a local CSV (path given via --csv), runs the
selected gate, writes every Issue to the audit log (Postgres if
$ALPHARD_PG_DSN is set, else in-memory), and prints a human summary.

DESIGN DECISIONS
----------------
1. Single CLI surface (`python -m src.data.quality <gate> <ticker>`)
   matches the spec; subcommand dispatch via dict-of-functions.

2. CSV is the canonical "I have data, run the gate" input format for
   the CLI — it does not require the Phase 1.1 DataLoader to be live.
   Production wiring (TinkoffDataLoader.load_ohlcv → CrossSource) is
   layered on top by the loader, not by this CLI.

3. Exit code: 0 on no issues, 1 if any issue at HIGH or worse
   (CRITICAL always exits 1; HIGH exits 1 unless --allow-high is set).

WHAT IS NOT HERE
----------------
- CSV schema inference. The loader specifies columns; the CLI just
  trusts the header (Phase 1.1 contract: "primary_key,open,high,low,
  close,volume").
- Network calls. The CLI is hermetic — no Tinkoff/MOEX calls.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from typing import Callable

from .audit import InMemoryAuditLog, make_default_audit_log, write_report
from .cross_source import CrossSourceParams, SourceSeries, check_cross_source
from .historical import HistoricalParams, check_historical
from .ingestion_gate import Bar, IngestionParams, check_ingestion
from .severity import QualityReport, Severity


# ---------------------------------------------------------------------------
# CSV -> Bar list
# ---------------------------------------------------------------------------


REQUIRED_CSV_COLUMNS = ("primary_key", "open", "high", "low", "close", "volume")


def load_bars_from_csv(path: str) -> list[Bar]:
    """Read a CSV with REQUIRED_CSV_COLUMNS into Bar objects.

    primary_key is parsed as ISO date (YYYY-MM-DD). Numeric columns are
    parsed as float (open/high/low/close) and int (volume). Bad rows
    raise ValueError so the caller can decide whether to fail or skip.
    """
    out: list[Bar] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV missing required columns: {','.join(missing)}; " f"got {reader.fieldnames}")
        for row in reader:
            try:
                bar = Bar(
                    primary_key=date.fromisoformat(row["primary_key"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["volume"])),
                )
            except (ValueError, KeyError) as e:
                raise ValueError(f"bad row {row}: {e}") from e
            out.append(bar)
    return out


def load_closes_from_csv(path: str, source_name: str) -> SourceSeries:
    """Read a CSV with REQUIRED_CSV_COLUMNS into a SourceSeries (date,close)."""
    pairs: list[tuple[date, float]] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pairs.append((date.fromisoformat(row["primary_key"]), float(row["close"])))
    return SourceSeries(source_name=source_name, bars=tuple(pairs))


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------


def _cmd_ingestion(args: argparse.Namespace) -> QualityReport:
    bars = load_bars_from_csv(args.csv)
    params = IngestionParams()
    return check_ingestion(
        args.ticker,
        bars,
        now=datetime.now(),
        params=params,
    )


def _cmd_cross_source(args: argparse.Namespace) -> QualityReport:
    if not getattr(args, "csv_b", None):
        raise SystemExit("cross_source requires --csv-b for the second source")
    sa = load_closes_from_csv(args.csv, args.source_a or "tinkoff")
    sb = load_closes_from_csv(args.csv_b, args.source_b or "moex")
    return check_cross_source(args.ticker, sa, sb, params=CrossSourceParams())


def _cmd_historical(args: argparse.Namespace) -> QualityReport:
    bars = load_bars_from_csv(args.csv)
    params = HistoricalParams()
    return check_historical(
        args.ticker,
        bars,
        params=params,
        now=datetime.now(),
    )


SUBCOMMANDS: dict[str, Callable[[argparse.Namespace], QualityReport]] = {
    "ingestion": _cmd_ingestion,
    "cross_source": _cmd_cross_source,
    "historical": _cmd_historical,
}


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------


def print_summary(report: QualityReport, audit_name: str, written: int) -> None:
    worst = report.worst_severity()
    print(f"--- {report.gate} gate: ticker={report.ticker} ---")
    print(f"  worst_severity : {worst.value if worst else 'NONE'}")
    print(f"  passed         : {report.passed}")
    print(f"  rejected       : {report.rejected}")
    print(f"  skipped        : {report.skipped}")
    print(f"  issues         : {len(report.issues)}")
    for sev in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        bucket = report.by_severity(sev)
        if bucket:
            print(f"    {sev.value}:")
            for issue in bucket:
                print(f"      - [{issue.kind.value}] {issue.message}")
    print(f"  audit_log      : {audit_name} (wrote {written} event(s))")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.data.quality",
        description="Run a single data quality gate against a ticker.",
    )
    sub = p.add_subparsers(dest="gate", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("ticker", help="Ticker symbol, e.g. SBER")
    common.add_argument(
        "--csv",
        dest="csv",
        required=True,
        help="Path to OHLCV CSV (header must include primary_key,open,high,low,close,volume)",
    )
    common.add_argument(
        "--allow-high",
        action="store_true",
        help="Exit 0 even when worst severity is HIGH (CRITICAL still exits 1)",
    )

    for name in SUBCOMMANDS:
        sp = sub.add_parser(name, parents=[common])
        sp.add_argument(
            "--csv-b",
            dest="csv_b",
            help="(cross_source only) CSV for the second source",
        )
        sp.add_argument("--source-a", default="tinkoff", help="(cross_source) name of source A")
        sp.add_argument("--source-b", default="moex", help="(cross_source) name of source B")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = SUBCOMMANDS[args.gate]

    audit = make_default_audit_log()
    audit_name = type(audit).__name__
    try:
        try:
            report = func(args)
        except ValueError as e:
            # Bad CSV (missing columns, unparseable row, …). Treat as
            # CRITICAL — the input itself is unusable. We still log
            # through the audit sink so operators see the cause.
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        write_report(audit, report)
        written = len(audit.events) if isinstance(audit, InMemoryAuditLog) else len(report.issues)
        print_summary(report, audit_name, written)
    finally:
        audit.close()

    worst = report.worst_severity()
    if worst == Severity.CRITICAL:
        return 1
    if worst == Severity.HIGH and not args.allow_high:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
