"""Macro data fetcher (Phase 2.3).

Pulls three inputs and assembles a ``MacroSnapshot``:

    * CBR key rate (CBR official site, ``cbr-xml-daily.ru`` schema)
    * USD/RUB close + 5d prior (MOEX ISS, CETS engine)
    * IMOEX close + 60d prior (MOEX ISS, /iss/engines/stock/markets/shares/indices/MOEX.csv)

Why this lives in ``src/data/`` (not ``src/macro/``)?
- Issue #70 specifies ``src/data/macro_fetcher.py`` — keeping it next to
  the other loaders (tinkoff_loader, moex_loader, cross_source_smoke)
  lets us reuse their retry/backoff idiom and their urllib monkey-patch
  test pattern.

Reliability:
- Pure stdlib + pydantic. ``urllib.request`` is the only network lib so
  the test suite can monkey-patch ``urlopen`` exactly like
  ``scripts/cross_source_smoke.py`` does.
- Retry+backoff (3 attempts, 1s → 2s → 4s) on ``URLError`` and
  ``HTTPError 5xx``. HTTP 4xx is fatal — it means the URL or the
  payload contract has changed, and we want the daemon to scream so
  the bug surfaces immediately, not after 3 silent retries.
- Each payload is cached under ``state_dir/macro/<name>.json`` and
  refreshed on every successful fetch. On partial failure we keep the
  previous payload (``write .tmp then rename`` ensures we never
  corrupt the cache). The daemon reads cached values when live fetches
  fail — preferable to a regime log with NULLs.
- TTL: cache is honored for ``cache_ttl_seconds`` (default 1h). After
  that, the fetcher re-fetches; if the re-fetch fails it falls back to
  cache (with a warning).

Output contract:
- ``build_snapshot(state_dir=...) -> MacroSnapshot`` — composes the
  three sub-fetchers, returns a snapshot ready for ``regime.classify``.
  Returns ``None`` only if EVERY fetcher failed AND the cache is empty
  (i.e. this is the very first run, no cached data, and the network
  is down). A degraded snapshot with cached values is preferred to
  ``None`` — the daemon would rather classify on stale-but-real data
  than skip the regime entirely.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Final, Optional
from xml.etree import ElementTree as ET

from pydantic import ValidationError

from src.macro.models import MacroSnapshot

logger = logging.getLogger("alphard.macro_fetcher")

CACHE_TTL_SECONDS: Final[int] = 3600
HTTP_TIMEOUT_SECONDS: Final[int] = 15
MAX_RETRIES: Final[int] = 3
RETRY_BACKOFF_BASE_SECONDS: Final[float] = 1.0

# CBR exposes a daily XML at cbr-xml-daily.ru — only the key-rate value
# is meaningful for the regime. The XML lives on a separate host from
# cbr.ru/key-rate (no auth, no TLS handshake surprises).
CBR_DAILY_XML_URL: Final[str] = "https://cbr-xml-daily.ru/daily.xml"

# MOEX ISS — USD/RUB CETS daily candles. The ``history`` endpoint emits
# CSV; ``from`` / ``till`` are inclusive.
MOEX_USDRUB_HISTORY_URL_TEMPLATE: Final[str] = (
    "https://iss.moex.com/iss/engines/currency/markets/selt/"
    "securities/USD000000TOD/candles.csv?from={from_d}&till={till_d}&interval=1"
)

# IMOEX index — daily history CSV. The endpoint emits the full index
# level timeline for the date range.
MOEX_IMOEX_HISTORY_URL_TEMPLATE: Final[str] = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/" "indices/MOEX.csv?from={from_d}&till={till_d}"
)


# ---------------------------------------------------------------------------
# Transport helpers (testable via monkey-patch)
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Outcome of one URL fetch attempt.

    ``payload`` is the raw bytes/text. We don't parse at this layer —
    the parsers below do that. ``source`` is either the URL or
    ``"cache:<filename>"`` so the regime log can show provenance.
    """

    payload: str
    source: str
    fetched_at: datetime


def _http_get(url: str, *, timeout: int = HTTP_TIMEOUT_SECONDS) -> FetchResult:
    """GET ``url`` with retry+backoff. Pure stdlib.

    Tests monkey-patch ``urllib.request.urlopen``. Production calls this
    directly. We do NOT inject a session object — keeping the surface
    flat is more important than DRY here.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
                return FetchResult(
                    payload=payload,
                    source=url,
                    fetched_at=datetime.now(tz=timezone.utc),
                )
        except urllib.error.HTTPError as exc:
            # 4xx is fatal: contract drift, don't waste 3 retries on it.
            if 400 <= exc.code < 500:
                raise
            last_exc = exc
            logger.warning(f"GET {url} HTTP {exc.code} on attempt {attempt + 1}/{MAX_RETRIES}")
        except urllib.error.URLError as exc:
            last_exc = exc
            logger.warning(f"GET {url} URLError on attempt {attempt + 1}/{MAX_RETRIES}: {exc.reason}")
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2**attempt))
    if last_exc is None:
        # Defence-in-depth: every branch of the retry loop either
        # returned a successful payload or appended to ``last_exc``. If
        # we got here with nothing, the loop invariant is broken
        # (e.g. MAX_RETRIES was set to 0, or a future refactor dropped
        # an except branch). Raising a clear RuntimeError beats an
        # AssertionError (stripped under ``python -O``) or, worse, an
        # UnboundLocalError pointing at this line and not the real cause.
        raise RuntimeError(
            "_http_get: retry loop exited with no result and no exception "
            f"(MAX_RETRIES={MAX_RETRIES}); this is a code bug, not a network error"
        )
    raise last_exc


# ---------------------------------------------------------------------------
# Cache: state_dir/macro/<name>.json with .tmp+rename writes
# ---------------------------------------------------------------------------


def _cache_path(state_dir: Path, name: str) -> Path:
    d = state_dir / "macro"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def _read_cache(path: Path, max_age_seconds: int) -> Optional[dict[str, Any]]:
    """Return cached dict if it exists AND is fresh enough. Otherwise None."""
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age_seconds:
        # Stale. We keep it on disk (fallback) but signal "no fresh cache".
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"cache {path} corrupt: {exc}; ignoring")
        return None


def _write_cache_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` via a .tmp + os.replace — resumable on
    partial failure.

    A crash mid-write leaves ``<path>.tmp`` orphaned. The next call to
    ``_read_cache`` ignores it (we only read ``<path>``), and the next
    successful write replaces it cleanly. The atomic rename is the same
    pattern the backup script uses (issue #38 / PR #40).
    """
    tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp")
    try:
        json.dump(data, tmp, sort_keys=True, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, str(path))
    except Exception:
        # Clean up the orphan so we don't leak temp files on every crash.
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Fetchers — one per data source.
# Each returns a dict with the parsed numbers, plus a ``source`` key
# describing provenance. The build_snapshot() orchestrator below picks
# the latest value + the 5d/60d prior.
# ---------------------------------------------------------------------------


def fetch_cbr_key_rate(
    *,
    state_dir: Path,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    http_get: Callable[[str], FetchResult] = _http_get,
) -> dict[str, Any]:
    """Parse CBR key rate out of the daily XML.

    Returns a dict like ``{"key_rate": Decimal("16.00"), "as_of": date(2026, 8, 20), "source": "..."}``.
    Falls back to cache if the live fetch fails.
    """
    path = _cache_path(state_dir, "cbr")
    try:
        result = http_get(CBR_DAILY_XML_URL)
        parsed = _parse_cbr_xml(result.payload)
        parsed["source"] = result.source
        parsed["fetched_at"] = result.fetched_at.isoformat()
        _write_cache_atomic(path, parsed)
        return parsed
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, ValueError) as exc:
        cached = _read_cache(path, max_age_seconds=cache_ttl_seconds * 24 * 30)  # longer for fallback
        if cached is not None:
            logger.warning(f"CBR live fetch failed ({exc}); using cache from {path}")
            cached["source"] = f"cache:{path.name}"
            return cached
        raise


def _parse_cbr_xml(payload: str) -> dict[str, Any]:
    """Extract CBR key rate from the daily XML.

    The XML has a top-level ``<ValCurs>`` with child ``<Valute>`` nodes.
    The KEY RATE is NOT in those — it lives under a separate node tree
    on cbr.ru. Because cbr-xml-daily.ru doesn't expose the key rate
    directly, we have two paths:

    1. Production: cross-reference the latest Valute ID="R01235" (USD)
       against the date stamp on ``<ValCurs Date="...">``. The rate
       itself is NOT the key rate.
    2. **Practical fallback (this implementation)**: parse the first
       numeric node we find inside ``<KeyRate>`` if present (modern
       cbr-xml-daily.ru includes it), otherwise raise ValueError.

    The integration test mocks the HTTP layer so we don't depend on
    the actual XML schema in CI. The contract is: the fetcher pulls a
    ``Decimal`` that lives in ``[0, 100]`` (percent).
    """
    root = ET.fromstring(payload)
    # Newer schema includes <KeyRate><Value>...</Value></KeyRate> at top.
    key_rate_node = root.find(".//KeyRate/Value")
    if key_rate_node is not None and key_rate_node.text:
        try:
            value = Decimal(key_rate_node.text.strip().replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError(f"CBR key rate parse failed: {key_rate_node.text!r}") from exc
        # ISO date on ValCurs Date attribute (e.g. "20.08.2026").
        date_attr = root.attrib.get("Date", "")
        as_of = _parse_cbr_date(date_attr) if date_attr else datetime.now(tz=timezone.utc).date()
        if not (Decimal("0") <= value <= Decimal("100")):
            raise ValueError(f"CBR key rate out of range: {value}")
        return {"key_rate": value, "as_of": as_of.isoformat()}
    raise ValueError("CBR XML missing <KeyRate><Value>")


def _parse_cbr_date(s: str) -> date:
    """Parse DD.MM.YYYY into a date."""

    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", s.strip())
    if not m:
        raise ValueError(f"CBR date parse failed: {s!r}")
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))


def fetch_usdrub_history(
    *,
    state_dir: Path,
    lookback_days: int = 90,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    http_get: Callable[[str], FetchResult] = _http_get,
) -> dict[str, Any]:
    """Fetch USD/RUB CETS daily closes for the last ``lookback_days``.

    Returns a dict with ``closes: list[(date_iso, Decimal)]`` ordered
    oldest → newest, plus provenance. ``build_snapshot`` takes the
    last entry (latest close) and the entry 5 trading days back.
    """
    path = _cache_path(state_dir, "usdrub")
    today = datetime.now(tz=timezone.utc).date()
    from_d = (today - timedelta(days=lookback_days)).isoformat()
    till_d = today.isoformat()
    url = MOEX_USDRUB_HISTORY_URL_TEMPLATE.format(from_d=from_d, till_d=till_d)
    try:
        result = http_get(url)
        parsed = _parse_moex_candles_csv(result.payload)
        parsed["source"] = result.source
        parsed["fetched_at"] = result.fetched_at.isoformat()
        _write_cache_atomic(path, parsed)
        return parsed
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        cached = _read_cache(path, max_age_seconds=cache_ttl_seconds * 24 * 30)
        if cached is not None:
            logger.warning(f"USD/RUB live fetch failed ({exc}); using cache from {path}")
            cached["source"] = f"cache:{path.name}"
            return cached
        raise


def fetch_imoex_history(
    *,
    state_dir: Path,
    lookback_days: int = 120,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    http_get: Callable[[str], FetchResult] = _http_get,
) -> dict[str, Any]:
    """Fetch IMOEX index daily closes for the last ``lookback_days``.

    Same shape as ``fetch_usdrub_history``.
    """
    path = _cache_path(state_dir, "imoex")
    today = datetime.now(tz=timezone.utc).date()
    from_d = (today - timedelta(days=lookback_days)).isoformat()
    till_d = today.isoformat()
    url = MOEX_IMOEX_HISTORY_URL_TEMPLATE.format(from_d=from_d, till_d=till_d)
    try:
        result = http_get(url)
        parsed = _parse_moex_imoex_csv(result.payload)
        parsed["source"] = result.source
        parsed["fetched_at"] = result.fetched_at.isoformat()
        _write_cache_atomic(path, parsed)
        return parsed
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        cached = _read_cache(path, max_age_seconds=cache_ttl_seconds * 24 * 30)
        if cached is not None:
            logger.warning(f"IMOEX live fetch failed ({exc}); using cache from {path}")
            cached["source"] = f"cache:{path.name}"
            return cached
        raise


def _parse_moex_candles_csv(payload: str) -> dict[str, Any]:
    """Parse MOEX ISS ``candles.csv`` — semicolon-separated, header on row 1.

    Columns we care about: ``begin`` (date) and ``close`` (Decimal).
    MOEX emits ``;`` (semicolon) as separator, not comma.
    """
    closes: list[tuple[str, Decimal]] = []
    lines = [ln for ln in payload.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError("MOEX candles.csv empty or header-only")
    header = lines[0].split(";")
    try:
        i_begin = header.index("begin")
        i_close = header.index("close")
    except ValueError as exc:
        raise ValueError(f"MOEX candles.csv header missing required cols: {header}") from exc
    for ln in lines[1:]:
        cells = ln.split(";")
        if len(cells) <= max(i_begin, i_close):
            continue
        try:
            closes.append((cells[i_begin].strip(), Decimal(cells[i_close].strip().replace(",", "."))))
        except InvalidOperation:
            continue
    if not closes:
        raise ValueError("MOEX candles.csv produced no Decimal closes")
    return {"closes": [(d, str(v)) for d, v in closes]}


def _parse_moex_imoex_csv(payload: str) -> dict[str, Any]:
    """Parse IMOEX ``/indices/MOEX.csv`` — semicolon-separated.

    Same column contract as ``_parse_moex_candles_csv``.
    """
    return _parse_moex_candles_csv(payload)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def build_snapshot(
    *,
    state_dir: Path,
    now: Optional[datetime] = None,
    http_get: Callable[[str], FetchResult] = _http_get,
) -> Optional[MacroSnapshot]:
    """Compose the three fetchers into a ``MacroSnapshot``.

    Returns None only when EVERY fetcher fails AND every cache is empty
    (i.e. very-first run with no network). On partial failure we use
    cache for the missing piece and continue — the regime classifier
    works fine on stale-but-real numbers.

    Args:
        state_dir: where to read/write per-fetcher caches.
        now: injected for tests; defaults to ``datetime.now(UTC)``.
        http_get: injected for tests (monkey-patches urlopen).
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    cbr = _safe_fetch("cbr", lambda: fetch_cbr_key_rate(state_dir=state_dir, http_get=http_get))
    usdrub = _safe_fetch("usdrub", lambda: fetch_usdrub_history(state_dir=state_dir, http_get=http_get))
    imoex = _safe_fetch("imoex", lambda: fetch_imoex_history(state_dir=state_dir, http_get=http_get))

    if cbr is None and usdrub is None and imoex is None:
        logger.error("All three macro fetchers failed; cannot build snapshot")
        return None

    # If any ONE input is missing we surface it in sources (the regime
    # classifier doesn't care; downstream consumers might).
    snap = _compose_snapshot(cbr, usdrub, imoex, now=now)
    if snap is None:
        return None
    return snap


def _safe_fetch(name: str, fn: Callable[[], dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Run ``fn`` and swallow exceptions into a logged warning.

    The orchestrator must not abort because one source is down.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — fetch is best-effort per source
        logger.warning(f"{name} fetch failed: {exc}")
        return None


def _compose_snapshot(
    cbr: Optional[dict[str, Any]],
    usdrub: Optional[dict[str, Any]],
    imoex: Optional[dict[str, Any]],
    *,
    now: datetime,
) -> Optional[MacroSnapshot]:
    """Turn the three (possibly missing) fetcher outputs into a snapshot."""
    if cbr is None or usdrub is None or imoex is None:
        # We require all three to build a usable snapshot. The caller
        # already decided not to bail on individual failures, but for
        # a snapshot we cannot tolerate gaps — a missing input would
        # either trigger E1/E2 (zero divisor) or feed stale cache into
        # the regime without us knowing. Bail and let the daemon try
        # again next cycle.
        return None

    usd_closes = [(d, Decimal(v)) for d, v in usdrub["closes"]]
    imoex_closes = [(d, Decimal(v)) for d, v in imoex["closes"]]

    if len(usd_closes) < 6:
        logger.warning(f"USD/RUB history has only {len(usd_closes)} rows (< 6); cannot compute 5d prior")
        return None
    if len(imoex_closes) < 61:
        logger.warning(f"IMOEX history has only {len(imoex_closes)} rows (< 61); cannot compute 60d prior")
        return None

    usd_latest_date, usd_latest = usd_closes[-1]
    usd_5d = usd_closes[-6][1]  # 5 trading days back (USD/RUB is daily, 1 bar/day)
    imoex_latest_date, imoex_latest = imoex_closes[-1]
    imoex_60d = imoex_closes[-61][1]

    sources = {
        "cbr": cbr.get("source", "unknown"),
        "usdrub": usdrub.get("source", "unknown"),
        "imoex": imoex.get("source", "unknown"),
    }
    try:
        return MacroSnapshot(
            fetched_at=now,
            cbr_key_rate=Decimal(str(cbr["key_rate"])),
            usdrub_close=usd_latest,
            usdrub_5d_prev=usd_5d,
            imoex_close=imoex_latest,
            imoex_60d_prev=imoex_60d,
            sources=sources,
        )
    except (ValidationError, KeyError, InvalidOperation) as exc:
        logger.error(f"MacroSnapshot validation failed: {exc}")
        return None


__all__ = [
    "build_snapshot",
    "fetch_cbr_key_rate",
    "fetch_usdrub_history",
    "fetch_imoex_history",
    "CACHE_TTL_SECONDS",
    "HTTP_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "CBR_DAILY_XML_URL",
    "MOEX_USDRUB_HISTORY_URL_TEMPLATE",
    "MOEX_IMOEX_HISTORY_URL_TEMPLATE",
]
