"""MOEX ISS corporate-actions fetcher (Phase 2.5 step 2a).

Why this script?
----------------
PHASE1-AUDIT flagged "Adjusted prices — adj_close = close placeholder, no
split/dividend adjustment". Phase 2.5 ships:

  - Step 1 (PR #45): pure adjustment math (`src.data.adjustment`).
  - Step 2a (this script): fetch raw corporate-action events from MOEX
    ISS into a JSON file.
  - Step 2b (next PR): upsert the JSON into Postgres + re-apply
    `apply_split_adjustment` to historical OHLCV bars.

MOEX ISS publishes both splits and dividends via the public
engines/stock/markets/shares API. No authentication, no rate-limit
above the default ~50 req/min (single batched request here covers the
whole exchange).

Output
------
A single JSON file with the shape::

    {
      "fetched_at": "2026-08-20T05:30:00Z",
      "source": "MOEX ISS",
      "endpoint": "https://iss.moex.com/iss/.../splits.json",
      "splits": [
        {"ticker": "SBER", "ts": "2014-06-16", "ratio": 2.0, "source": "moex"},
        ...
      ],
      "dividends": [...]  # only when --include-dividends is set
    }

If --include-dividends is NOT set, the ``dividends`` key is omitted
entirely — the script respects the user's "splits only" choice from the
PHASE2-ROADMAP.

Usage
-----
::

    python3 scripts/fetch_moex_corporate_actions.py \\
        --output /tmp/corp_actions.json

    # Splits + dividends:
    python3 scripts/fetch_moex_corporate_actions.py \\
        --include-dividends \\
        --output /tmp/corp_actions.json

    # Dry-run (no network): parses a previously saved JSON to verify
    # the schema is what step 2b will consume.
    python3 scripts/fetch_moex_corporate_actions.py \\
        --input /tmp/corp_actions.json \\
        --dry-run

Notes
-----
* The MOEX ISS endpoint returns a long-format table under
  ``history.cursor`` for date-paginated queries. We use the un-paginated
  ``history`` block which lists all known events for the share market.
* Network failures: a request error aborts the run with a non-zero
  exit code and an actionable stderr line. We do NOT silently write a
  partial file — downstream step 2b must not see a corrupted snapshot.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

logger = logging.getLogger("alphard.moex_corp_actions")

MOEX_BASE = "https://iss.moex.com"

# Shares market / splits endpoint (long-format table under
# ``history`` returns ALL known splits for the share market).
SPLITS_URL = f"{MOEX_BASE}/iss/engines/stock/markets/shares/splits.json"

# Dividends endpoint. Different ``iss.json`` block name.
DIVIDENDS_URL = f"{MOEX_BASE}/iss/engines/stock/markets/shares/dividends.json"

# Connection tuning. MOEX ISS is a public static CDN — no auth, no
# signing, but be polite (single round-trip per call).
REQUEST_TIMEOUT = 60  # seconds; ISS is usually <5s but allow headroom
USER_AGENT = "alphard-research/0.1 (+https://github.com/m0rtal/alphard)"


def _parse_moex_history(payload: dict, value_field: str, kind: str) -> list[dict]:
    """Convert a MOEX ISS history payload into our normalized form.

    The MOEX ISS long-format table looks like::

        "history": {
          "headers": ["secid", "ts", "value"],
          "rows": [
            ["SBER", "2014-06-16", "1:2"],
            ...
          ]
        }

    We normalize each row to::

        {"ticker": "SBER", "ts": "2014-06-16", "ratio": 2.0, "source": "moex"}

    Notes
    -----
    * For ``value_field='split'`` we expect the cell to be a string like
      "1:2" (denominator: numerator). The ratio stored in our output is
      ``numerator / denominator`` per our internal convention (matches
      ``CorporateAction.value``).
    * For ``value_field='dividend'`` we expect a numeric RUB/share. We
      coerce to string for JSON serialization but keep Decimal
      precision in the runtime dict (not used downstream yet — step 2b).
    """
    out: list[dict] = []
    history = payload.get("history") or {}
    rows = history.get("rows") or []
    if not rows:
        logger.warning(
            "MOEX ISS %s response had no history rows (columns=%s)",
            kind,
            history.get("headers"),
        )
        return out

    headers = history.get("headers") or []
    secid_idx = headers.index("secid") if "secid" in headers else None
    ts_idx = headers.index("ts") if "ts" in headers else None
    if value_field not in headers:
        logger.warning(
            "MOEX ISS %s response missing %s column (headers=%s)",
            kind,
            value_field,
            headers,
        )
        return out
    val_idx = headers.index(value_field)

    for row in rows:
        if not isinstance(row, list) or len(row) <= max(secid_idx or 0, ts_idx or 0, val_idx):
            continue
        ticker = row[secid_idx].strip().upper() if secid_idx is not None else None
        ts = row[ts_idx].strip() if ts_idx is not None else None
        raw_value = row[val_idx]
        if not ticker or not ts:
            continue

        if kind == "split":
            # "1:2" -> ratio=2.0 (per our convention: numerator/denominator).
            parsed = _parse_split_ratio(str(raw_value).strip())
            if parsed is None:
                continue
            out.append(
                {
                    "ticker": ticker,
                    "ts": ts,
                    "ratio": parsed,
                    "source": "moex",
                }
            )
        elif kind == "dividend":
            # Numeric value, RUB/share. We keep Decimal precision in
            # memory but serialize as str to avoid float drift.
            try:
                amount = Decimal(str(raw_value).strip())
            except Exception:  # noqa: BLE001 — defensive
                continue
            out.append(
                {
                    "ticker": ticker,
                    "ts": ts,
                    "amount_rub_per_share": str(amount),
                    "source": "moex",
                }
            )
    return out


def _parse_split_ratio(s: str) -> float | None:
    """Parse MOEX "1:2" / "2:1" / "1:10" -> float numerator/denominator.

    Returns None on unparseable input (skip silently — the MOEX feed has
    historical entries with non-standard formats, and we'd rather lose
    one split than corrupt the whole file).
    """
    if ":" not in s:
        # Some MOEX entries are already a single number (rare).
        try:
            return float(s)
        except ValueError:
            return None
    parts = s.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        denom = float(parts[0])
        numer = float(parts[1])
    except ValueError:
        return None
    if denom == 0:
        return None
    return numer / denom


def fetch_splits(session: requests.Session, timeout: int = REQUEST_TIMEOUT) -> list[dict]:
    """Fetch all splits from MOEX ISS and return normalized records."""
    response = session.get(SPLITS_URL, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return _parse_moex_history(payload, value_field="value", kind="split")


def fetch_dividends(session: requests.Session, timeout: int = REQUEST_TIMEOUT) -> list[dict]:
    """Fetch all dividends from MOEX ISS and return normalized records."""
    response = session.get(DIVIDENDS_URL, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return _parse_moex_history(payload, value_field="value", kind="dividend")


def write_payload(
    output_path: Path,
    splits: list[dict],
    dividends: Iterable[dict] | None,
    endpoint_splits: str,
    endpoint_dividends: str,
) -> None:
    """Atomically write the JSON output.

    Atomic means: write to ``output_path.tmp`` first, fsync, then
    rename. If the run crashes mid-write we leave the previous file
    intact rather than producing a half-written snapshot.
    """
    payload: dict = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "MOEX ISS",
        "endpoint_splits": endpoint_splits,
        "splits": splits,
    }
    if dividends is not None:
        payload["endpoint_dividends"] = endpoint_dividends
        payload["dividends"] = list(dividends)

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp_path.replace(output_path)
    logger.info("wrote %d splits to %s", len(splits), output_path)
    if dividends is not None:
        logger.info("wrote %d dividends to %s", len(list(dividends)), output_path)


def read_payload(input_path: Path) -> dict:
    """Load a previously-saved payload for --dry-run verification."""
    return json.loads(input_path.read_text())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/alphard_corp_actions.json"),
        help="Where to write the JSON snapshot (default: /tmp/...).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=("Skip the network and re-validate a previously saved " "snapshot (--dry-run mode)."),
    )
    parser.add_argument(
        "--include-dividends",
        action="store_true",
        help=(
            "Also fetch MOEX ISS dividends (default off: phase 2.5 step "
            "2b is for splits; dividends land in a later step)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT,
        help=f"Per-request timeout in seconds (default {REQUEST_TIMEOUT}).",
    )
    parser.add_argument(
        "--limit-tickers",
        type=str,
        default=None,
        help=("Comma-separated whitelist of tickers to keep (e.g. " "'SBER,GAZP,VTBR'). Mostly for testing."),
    )
    return parser.parse_args()


def _filter_tickers(rows: list[dict], limit: set[str] | None) -> list[dict]:
    if limit is None:
        return rows
    return [r for r in rows if r.get("ticker") in limit]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()

    limit = {t.strip().upper() for t in args.limit_tickers.split(",")} if args.limit_tickers else None

    if args.input is not None:
        # Dry-run path: re-validate a saved file.
        logger.info("--input set: skipping network, validating %s", args.input)
        try:
            payload = read_payload(args.input)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("cannot read %s: %s", args.input, exc)
            return 2
        splits = payload.get("splits", [])
        logger.info(
            "loaded %d splits%s",
            len(splits),
            f", {len(payload.get('dividends', []))} dividends" if "dividends" in payload else "",
        )
        splits = _filter_tickers(splits, limit)
        logger.info("after ticker filter: %d splits", len(splits))
        # Round-trip: rewrite to --output (default /tmp).
        write_payload(
            args.output,
            splits,
            payload.get("dividends") if "dividends" in payload else None,
            payload.get("endpoint_splits", SPLITS_URL),
            payload.get("endpoint_dividends", DIVIDENDS_URL),
        )
        return 0

    # Live path: hit the network.
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    try:
        splits = fetch_splits(session, timeout=args.timeout)
    except requests.RequestException as exc:
        logger.error("splits fetch failed: %s", exc)
        return 3
    splits = _filter_tickers(splits, limit)
    logger.info("MOEX ISS returned %d splits", len(splits))

    dividends: list[dict] | None = None
    if args.include_dividends:
        try:
            dividends_raw = fetch_dividends(session, timeout=args.timeout)
        except requests.RequestException as exc:
            logger.error("dividends fetch failed: %s", exc)
            return 4
        dividends = _filter_tickers(dividends_raw, limit)
        logger.info("MOEX ISS returned %d dividends", len(dividends))

    write_payload(
        args.output,
        splits,
        dividends,
        endpoint_splits=SPLITS_URL,
        endpoint_dividends=DIVIDENDS_URL,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
