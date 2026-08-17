"""Sync delisted_at for the ticker universe via MOEX ISS reference data.

Why
---
``ticker_universe.delisted_at`` is the boundary date for the backfill
age-aware completion formula: ``expected_bars =
trading_days(listed_at, today|delisted_at) * (1 - halts_pct)``. Without
a real delisted_at the formula can't tell a 2018-2020 delisted ticker
from a 2024-present live ticker.

Tinkoff's gRPC shares endpoint surfaces ``trading_status`` (a
SecurityTradingStatus enum) but not the actual delisting date. MOEX
ISS exposes ``listed_till`` and ``history_till`` per (board, secid)
in ``/iss/securities/{secid}.xml``. We use those.

What this module does
--------------------
* ``fetch_delist_dates(universe)`` — given a list of tickers, returns
  a dict ``ticker -> listed_at | None, delisted_at | None``.
* One ISS request per ticker (cheap, public, no rate limit).
* Network failures fall back to ``None`` (conservative — the
  age-aware formula will use ``MIN_YEAR`` as the floor, which is
  safe but slightly overestimates the expected count).

No PG writes here — the caller (``pg_store.sync_universe_delisted``)
takes the dict and updates rows.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date

logger = logging.getLogger("alphard.delist_source")


def _parse_date(s: str | None) -> date | None:
    if not s or len(s) < 10:
        return None
    try:
        return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, IndexError):
        return None


def fetch_delist_dates(
    tickers: list[str],
    *,
    timeout: float = 5.0,
) -> dict[str, tuple[date | None, date | None]]:
    """Look up ``listed_from`` / ``listed_till`` for each ticker via ISS.

    Returns dict mapping ticker -> (listed_at, delisted_at). Either
    side can be None when ISS doesn't expose it (e.g. bonds which
    mature without delisting). Network errors are logged and produce
    an empty entry — caller treats None as "use fallback".
    """
    out: dict[str, tuple[date | None, date | None]] = {}
    for ticker in tickers:
        url = f"https://iss.moex.com/iss/securities/{ticker}.xml"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                xml_bytes = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.debug(f"ISS lookup failed for {ticker}: {exc}")
            out[ticker] = (None, None)
            continue
        listed_from: date | None = None
        listed_till: date | None = None
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            logger.debug(f"ISS parse error for {ticker}: {exc}")
            out[ticker] = (None, None)
            continue
        # /iss/securities/{secid}.xml has a <data id="boards"> block
        # with one row per board the secid trades on. We take the
        # earliest listed_from and the latest listed_till across
        # boards — represents the security's true lifetime at MOEX.
        for data_block in root.findall(".//data[@id='boards']"):
            for row in data_block.findall("rows/row"):
                listed_from = (
                    min(
                        (listed_from or _parse_date(row.get("listed_from"))),
                        _parse_date(row.get("listed_from")),
                        key=lambda d: d or date.max,
                    )
                    or listed_from
                )
                listed_till_candidate = _parse_date(row.get("listed_till"))
                if listed_till_candidate is not None:
                    if listed_till is None or listed_till_candidate > listed_till:
                        listed_till = listed_till_candidate
        out[ticker] = (listed_from, listed_till)
    return out
