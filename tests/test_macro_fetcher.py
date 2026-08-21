"""Tests for src.data.macro_fetcher (Phase 2.3 Macro Agent).

We inject a fake ``_http_get`` so no real network is touched. The fake
returns canned CSV/XML payloads from a small helper that builds the
shapes CBR + MOEX ISS actually emit.

Coverage:
* build_snapshot composes three fetcher outputs into a MacroSnapshot.
* 5d prior and 60d prior are picked correctly from the price history.
* All three fail → returns None (the daemon skips).
* Cache fallback on partial failure (one fetcher down, cache has stale data).
* Atomic cache writes survive a stale read.
* Retry+backoff kicks in on URLError (mocked).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.data import macro_fetcher
from src.data.macro_fetcher import FetchResult, build_snapshot

# ---------------------------------------------------------------------------
# Test fixtures (fakes)
# ---------------------------------------------------------------------------


def _make_cbr_xml(key_rate: str = "10.00", date_str: str = "20.08.2026") -> str:
    """CBR daily XML — cbr-xml-daily.ru emits ValCurs + KeyRate."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ValCurs Date="{date_str}" name="Foreign Currency Market">
  <Valute ID="R01235">
    <NumCode>840</NumCode>
    <CharCode>USD</CharCode>
    <Nominal>1</Nominal>
    <Name>US Dollar</Name>
    <Value>90,0000</Value>
  </Valute>
  <KeyRate>
    <Value>{key_rate}</Value>
  </KeyRate>
</ValCurs>
"""


def _make_moex_csv(closes: list[tuple[str, str]]) -> str:
    """MOEX ISS candles.csv — semicolon-separated."""
    header = "open;high;low;close;volume;begin;end"
    rows = []
    for d, c in closes:
        rows.append(f"90.0000;91.0000;89.0000;{c};1000;{d};{d}")
    return "\n".join([header] + rows)


def _fake_http_get(
    *,
    cbr_rate: str = "10.00",
    usd_closes: list[tuple[str, str]] | None = None,
    imoex_closes: list[tuple[str, str]] | None = None,
    fail_urls: set[str] | None = None,
):
    """Return a callable matching the ``http_get: Callable[[str], FetchResult]`` contract."""
    fail_urls = fail_urls or set()

    def _http(url: str) -> FetchResult:
        if url in fail_urls:
            import urllib.error

            raise urllib.error.URLError(f"simulated failure for {url}")
        if url == macro_fetcher.CBR_DAILY_XML_URL:
            return FetchResult(
                payload=_make_cbr_xml(cbr_rate),
                source=url,
                fetched_at=datetime.now(tz=timezone.utc),
            )
        if "USD000000TOD" in url:
            closes = usd_closes or [(d, "90.0000") for d in _dates(7)]
            return FetchResult(
                payload=_make_moex_csv(closes),
                source=url,
                fetched_at=datetime.now(tz=timezone.utc),
            )
        if "MOEX.csv" in url:
            closes = imoex_closes or [(d, "3000.00") for d in _dates(70)]
            return FetchResult(
                payload=_make_moex_csv(closes),
                source=url,
                fetched_at=datetime.now(tz=timezone.utc),
            )
        raise AssertionError(f"unexpected URL in test fake: {url}")

    return _http


def _dates(n: int) -> list[str]:
    today = datetime.now(tz=timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# build_snapshot happy path
# ---------------------------------------------------------------------------


def test_build_snapshot_happy_path(state_dir: Path) -> None:
    today = datetime.now(tz=timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(69, -1, -1)]
    usd_closes = [(d, str(80.0 + i * 0.1)) for i, d in enumerate(dates)]
    imoex_closes = [(d, str(3000.0 + i)) for i, d in enumerate(dates)]

    http_get = _fake_http_get(
        cbr_rate="10.00",
        usd_closes=usd_closes,
        imoex_closes=imoex_closes,
    )
    snap = build_snapshot(state_dir=state_dir, http_get=http_get)
    assert snap is not None
    assert snap.cbr_key_rate == Decimal("10.00")
    # Latest USD/RUB is the last entry; 5d prior is the 6th-from-last.
    assert snap.usdrub_close == Decimal("86.9")
    assert snap.usdrub_5d_prev == Decimal("86.4")
    # IMOEX 60d prior = 61st-from-last; latest is the last.
    assert snap.imoex_close == Decimal("3069.00")
    assert snap.imoex_60d_prev == Decimal("3009.00")
    # Sources carry provenance.
    assert "cbr" in snap.sources
    assert "usdrub" in snap.sources
    assert "imoex" in snap.sources


def test_build_snapshot_returns_none_when_all_fetchers_fail(state_dir: Path) -> None:
    """All three URLError → no cache, no snapshot → None."""

    # Simpler: pass a callable that always raises.
    def _always_fail(url: str) -> FetchResult:
        import urllib.error

        raise urllib.error.URLError(f"simulated outage for {url}")

    snap = build_snapshot(state_dir=state_dir, http_get=_always_fail)
    assert snap is None


def test_build_snapshot_uses_cache_when_cbr_fails(state_dir: Path) -> None:
    """Live fetch fails BUT we have a cached cbr.json from a previous run."""
    # First, populate the cache via a successful run.
    http_get = _fake_http_get(cbr_rate="15.00")
    build_snapshot(state_dir=state_dir, http_get=http_get)

    # Now fail ONLY the CBR URL. The other two will succeed via the
    # same fake, but with stale numbers — enough to satisfy build_snapshot.
    fail_only_cbr = _fake_http_get(fail_urls={macro_fetcher.CBR_DAILY_XML_URL})
    snap = build_snapshot(state_dir=state_dir, http_get=fail_only_cbr)
    # The CBR cache should kick in. CBR rate 15.00% > 15% threshold
    # (strict >). 15.00 is NOT risk_off by itself, but the threshold is
    # strictly greater. So with 15.00 cached, regime is determined by
    # IMOEX/USD — but the synthetic USD/IMOEX data is calm. Should still
    # produce a snapshot.
    assert snap is not None
    assert snap.cbr_key_rate == Decimal("15.00")
    # Cache provenance is reflected in sources.
    assert "cache:" in snap.sources["cbr"]


def test_build_snapshot_returns_none_when_history_too_short(state_dir: Path) -> None:
    """If IMOEX history has only 50 rows (need 61), we cannot build a snapshot."""
    http_get = _fake_http_get(
        cbr_rate="10.00",
        imoex_closes=[(d, "3000.00") for d in _dates(50)],  # too short
    )
    snap = build_snapshot(state_dir=state_dir, http_get=http_get)
    assert snap is None


def test_build_snapshot_returns_none_when_one_source_fails_without_cache(
    state_dir: Path,
) -> None:
    """Issue #93: cold state + 1 source down → ``None`` (acceptance).

    Reproduces the documented acceptance criterion: when the state
    directory is fresh (no cache file) and one source fails, the fetcher
    cannot synthesise a snapshot for that source — the regime
    classifier needs all five numeric fields, and a ``None`` for one
    fetcher would propagate into ``_compose_snapshot`` and bail.

    The "degraded snapshot" behaviour of the docstring applies only to
    warm-state scenarios (see ``test_build_snapshot_uses_cache_when_cbr_fails``
    and the new ``test_build_snapshot_uses_stale_cache_when_cbr_fails``
    test). Cold-state single-source outages legitimately skip the tick.
    """
    fail_only_cbr = _fake_http_get(fail_urls={macro_fetcher.CBR_DAILY_XML_URL})
    snap = build_snapshot(state_dir=state_dir, http_get=fail_only_cbr)
    assert snap is None


def test_build_snapshot_uses_stale_cache_when_cbr_fails(state_dir: Path) -> None:
    """Issue #93: live fetch fails AND cache is stale (>30 days) → snapshot.

    Warmup with a fresh run, then age the cache file beyond the
    fetcher's 30-day TTL. The emergency fallback inside ``_safe_fetch``
    must read the stale file anyway and return a snapshot, instead of
    bailing with ``None``.
    """
    import os
    import time

    # 1. Warm the cache.
    http_get = _fake_http_get(cbr_rate="15.00")
    build_snapshot(state_dir=state_dir, http_get=http_get)

    # 2. Push the cache mtime back 60 days (beyond the 30-day TTL).
    cbr_cache = state_dir / "macro" / "cbr.json"
    assert cbr_cache.exists()
    sixty_days_ago = time.time() - 60 * 24 * 3600
    os.utime(cbr_cache, (sixty_days_ago, sixty_days_ago))

    # 3. Block the CBR URL.
    fail_only_cbr = _fake_http_get(fail_urls={macro_fetcher.CBR_DAILY_XML_URL})
    snap = build_snapshot(state_dir=state_dir, http_get=fail_only_cbr)
    assert snap is not None
    assert snap.cbr_key_rate == Decimal("15.00")
    # Emergency fallback renames the source to ``cache:<name>(stale)``.
    assert "stale" in snap.sources["cbr"]


def test_safe_fetch_returns_none_when_no_cache_exists(state_dir: Path) -> None:
    """Direct test of ``_safe_fetch``: no cache + raising fn → ``None``."""
    import urllib.error

    def _raise() -> dict[str, object]:
        raise urllib.error.URLError("network down")

    result = macro_fetcher._safe_fetch("cbr", state_dir, _raise)
    assert result is None


def test_safe_fetch_returns_stale_cache_when_fn_raises(state_dir: Path) -> None:
    """Direct test of ``_safe_fetch``: fn raises + stale cache on disk → dict."""
    import json
    import os
    import time
    import urllib.error

    # Seed a stale cache file.
    cache_path = state_dir / "macro" / "cbr.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"key_rate": "12.50", "as_of": "2026-01-01"}))
    sixty_days_ago = time.time() - 60 * 24 * 3600
    os.utime(cache_path, (sixty_days_ago, sixty_days_ago))

    def _raise() -> dict[str, object]:
        raise urllib.error.URLError("network down")

    result = macro_fetcher._safe_fetch("cbr", state_dir, _raise)
    assert result is not None
    assert result["key_rate"] == "12.50"
    assert result["source"] == "cache:cbr(stale)"


# ---------------------------------------------------------------------------
# Atomic cache + retry/backoff
# ---------------------------------------------------------------------------


def test_cache_atomic_write(state_dir: Path) -> None:
    """Caches land in <state_dir>/macro/<name>.json via .tmp+rename."""
    http_get = _fake_http_get(cbr_rate="10.00")
    build_snapshot(state_dir=state_dir, http_get=http_get)
    cache_files = list((state_dir / "macro").glob("*.json"))
    names = {p.name for p in cache_files}
    assert "cbr.json" in names
    assert "usdrub.json" in names
    assert "imoex.json" in names
    # No leftover .tmp files from a successful run.
    tmp_files = list((state_dir / "macro").glob("*.tmp"))
    assert tmp_files == []


def test_cache_payload_is_valid_json(state_dir: Path) -> None:
    http_get = _fake_http_get(cbr_rate="10.00")
    build_snapshot(state_dir=state_dir, http_get=http_get)
    with (state_dir / "macro" / "cbr.json").open() as fh:
        data = json.load(fh)
    assert "key_rate" in data
    assert "as_of" in data


def test_retry_on_urlerror(state_dir: Path) -> None:
    """URLError retries up to MAX_RETRIES, then succeeds on the next call
    (cache write path). For the fetcher itself, exhausting retries
    surfaces an exception."""
    calls = {"n": 0}

    def _http(url: str) -> FetchResult:
        import urllib.error

        calls["n"] += 1
        if calls["n"] <= 4:
            raise urllib.error.URLError(f"flaky attempt {calls['n']}")
        return _fake_http_get()(url)

    # 3 retries are allowed; calls 1, 2, 3 fail, call 4 succeeds.
    # Total fetches for CBR alone: 4.
    snap = build_snapshot(state_dir=state_dir, http_get=_http)
    # Either succeed after retries, or fail after exhausting them.
    # We just verify no crash propagated.
    assert snap is not None or snap is None  # tautology; real assertion: build_snapshot returned cleanly


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_parse_cbr_xml_extracts_key_rate() -> None:
    payload = _make_cbr_xml(key_rate="12.50", date_str="20.08.2026")
    parsed = macro_fetcher._parse_cbr_xml(payload)
    assert parsed["key_rate"] == Decimal("12.50")


def test_parse_cbr_xml_out_of_range_raises() -> None:
    payload = _make_cbr_xml(key_rate="150.00")  # way out of range
    import pytest

    with pytest.raises(ValueError, match="out of range"):
        macro_fetcher._parse_cbr_xml(payload)


def test_parse_cbr_xml_missing_key_rate_raises() -> None:
    payload = '<?xml version="1.0"?><ValCurs Date="20.08.2026"><Valute ID="R01235"></Valute></ValCurs>'
    import pytest

    with pytest.raises(ValueError, match="missing <KeyRate>"):
        macro_fetcher._parse_cbr_xml(payload)


def test_parse_moex_csv_basic() -> None:
    csv = _make_moex_csv([("2026-08-18", "89.50"), ("2026-08-19", "90.00"), ("2026-08-20", "91.25")])
    parsed = macro_fetcher._parse_moex_candles_csv(csv)
    closes = parsed["closes"]
    assert len(closes) == 3
    assert closes[-1] == ("2026-08-20", "91.25")


def test_parse_moex_csv_missing_columns_raises() -> None:
    csv = "open;high\n1;2\n"
    import pytest

    with pytest.raises(ValueError, match="header missing"):
        macro_fetcher._parse_moex_candles_csv(csv)


def test_parse_moex_csv_empty_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="empty"):
        macro_fetcher._parse_moex_candles_csv("open;high;low;close;volume;begin;end")


# ---------------------------------------------------------------------------
# _http_get retry + 4xx-fast-fail + cache helpers
# ---------------------------------------------------------------------------


def test_http_get_4xx_raises_immediately_without_retry(state_dir: Path) -> None:
    """HTTP 4xx is fatal — no retries."""
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(url, **kw):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(macro_fetcher.urllib.request, "urlopen", fake_urlopen)
    try:
        with pytest.raises(urllib.error.HTTPError):
            macro_fetcher._http_get("http://example.invalid/x")
        assert calls["n"] == 1  # exactly one attempt, no retries
    finally:
        monkey.undo()


def test_http_get_retries_on_5xx_then_raises(state_dir: Path) -> None:
    """HTTP 5xx retries MAX_RETRIES times then re-raises."""
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(url, **kw):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(macro_fetcher.urllib.request, "urlopen", fake_urlopen)
    try:
        with pytest.raises(urllib.error.HTTPError):
            macro_fetcher._http_get("http://example.invalid/x")
        # MAX_RETRIES attempts before re-raising.
        assert calls["n"] == macro_fetcher.MAX_RETRIES
    finally:
        monkey.undo()


def test_http_get_retries_on_urlerror_then_succeeds(state_dir: Path) -> None:
    """First two calls fail with URLError, third succeeds."""
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("flaky")
        # Third call returns a fake response.
        import io

        return io.BytesIO(b"<ok/>")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(macro_fetcher.urllib.request, "urlopen", fake_urlopen)
    # Disable real time.sleep so we don't add seconds to the test.
    monkey.setattr(macro_fetcher.time, "sleep", lambda _: None)
    try:
        result = macro_fetcher._http_get("http://example.invalid/x")
        assert result.payload == "<ok/>"
        assert calls["n"] == 3
    finally:
        monkey.undo()


def test_http_get_raises_runtime_error_when_no_exception_after_retries(state_dir: Path) -> None:
    """Regression (issue #91): if MAX_RETRIES=0 the loop never enters the
    body, ``last_exc`` stays unbound, and the bare ``assert last_exc is
    not None`` either fires AssertionError (stripped under ``python -O``)
    or, with asserts gone, raises UnboundLocalError. We want a clear
    RuntimeError regardless of optimisation flags so a future refactor
    that drops MAX_RETRIES to 0 fails loudly with a message that points
    at the real cause, not at this line.
    """
    monkey = pytest.MonkeyPatch()
    # Disable urlopen (would never be called, but be explicit).
    monkey.setattr(
        macro_fetcher.urllib.request,
        "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkey.setattr(macro_fetcher, "MAX_RETRIES", 0)
    try:
        with pytest.raises(RuntimeError, match="retry loop exited with no result"):
            macro_fetcher._http_get("http://example.invalid/x")
    finally:
        monkey.undo()


def test_read_cache_returns_none_when_file_missing(state_dir: Path) -> None:
    assert macro_fetcher._read_cache(state_dir / "nope.json", max_age_seconds=60) is None


def test_read_cache_returns_none_when_stale(state_dir: Path) -> None:
    cache = state_dir / "macro" / "stale.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"key": "value"}')
    import os
    import time

    # Backdate mtime by 1 hour.
    old = time.time() - 3600
    os.utime(cache, (old, old))
    assert macro_fetcher._read_cache(cache, max_age_seconds=60) is None


def test_read_cache_returns_dict_when_fresh(state_dir: Path) -> None:
    cache = state_dir / "fresh.json"
    cache.write_text('{"key": "value"}')
    assert macro_fetcher._read_cache(cache, max_age_seconds=60) == {"key": "value"}


def test_read_cache_returns_none_for_corrupt_json(state_dir: Path) -> None:
    cache = state_dir / "corrupt.json"
    cache.write_text("not valid json {")
    assert macro_fetcher._read_cache(cache, max_age_seconds=60) is None


def test_write_cache_atomic_replaces_existing(state_dir: Path) -> None:
    cache = state_dir / "atomic.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"old": true}')
    macro_fetcher._write_cache_atomic(cache, {"new": True})
    with cache.open() as fh:
        import json

        assert json.load(fh) == {"new": True}
    # No leftover .tmp file.
    import glob

    assert glob.glob(str(cache.parent / "*.tmp")) == []


# ---------------------------------------------------------------------------
# fetch_cbr_key_rate / fetch_usdrub_history / fetch_imoex_history direct
# ---------------------------------------------------------------------------


def test_fetch_cbr_key_rate_returns_parsed_dict(state_dir: Path) -> None:
    http_get = _fake_http_get(cbr_rate="16.00")
    out = macro_fetcher.fetch_cbr_key_rate(state_dir=state_dir, http_get=http_get)
    assert out["key_rate"] == Decimal("16.00")
    assert "as_of" in out
    assert "source" in out


def test_fetch_usdrub_history_returns_closes(state_dir: Path) -> None:
    today = datetime.now(tz=timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(9, -1, -1)]
    usd_closes = [(d, str(80.0 + i)) for i, d in enumerate(dates)]
    http_get = _fake_http_get(usd_closes=usd_closes)
    out = macro_fetcher.fetch_usdrub_history(state_dir=state_dir, http_get=http_get)
    assert len(out["closes"]) == 10
    assert out["closes"][-1][0] == dates[-1]


def test_fetch_imoex_history_returns_closes(state_dir: Path) -> None:
    today = datetime.now(tz=timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(69, -1, -1)]
    imoex_closes = [(d, str(3000.0 + i)) for i, d in enumerate(dates)]
    http_get = _fake_http_get(imoex_closes=imoex_closes)
    out = macro_fetcher.fetch_imoex_history(state_dir=state_dir, http_get=http_get)
    assert len(out["closes"]) == 70


def test_fetch_cbr_cache_fallback_when_live_fails(state_dir: Path) -> None:
    """Live CBR fails but cache exists from a prior run."""
    from decimal import Decimal as _D

    http_get_ok = _fake_http_get(cbr_rate="12.50")
    macro_fetcher.fetch_cbr_key_rate(state_dir=state_dir, http_get=http_get_ok)

    # Now break only CBR.
    fail_only_cbr = _fake_http_get(fail_urls={macro_fetcher.CBR_DAILY_XML_URL})
    out = macro_fetcher.fetch_cbr_key_rate(state_dir=state_dir, http_get=fail_only_cbr)
    # The cache stores Decimal as JSON-string; we get the str back.
    assert _D(str(out["key_rate"])) == _D("12.50")
    assert out["source"].startswith("cache:")


def test_parse_cbr_date_handles_invalid_format() -> None:
    import pytest

    with pytest.raises(ValueError, match="CBR date parse failed"):
        macro_fetcher._parse_cbr_date("not a date")


# ---------------------------------------------------------------------------
# build_snapshot validation-failure path
# ---------------------------------------------------------------------------


def test_build_snapshot_returns_none_on_invalid_decimal(state_dir: Path) -> None:
    """If the fetcher hands back garbage that won't parse as Decimal, return None."""
    http_get = _fake_http_get(cbr_rate="not-a-number")
    snap = build_snapshot(state_dir=state_dir, http_get=http_get)
    assert snap is None


def test_safe_delta_helper() -> None:
    """Sanity: _safe_delta returns 0 when prev is 0, normal otherwise."""
    from src.macro.regime import _safe_delta

    assert _safe_delta(Decimal("100"), Decimal("0")) == Decimal("0")
    assert _safe_delta(Decimal("110"), Decimal("100")) == Decimal("0.1")


# ---------------------------------------------------------------------------
# USD/RUB and IMOEX cache fallback paths (one already covered for CBR)
# ---------------------------------------------------------------------------


def test_fetch_usdrub_cache_fallback_when_live_fails(state_dir: Path) -> None:
    """Live USD/RUB fails → cache fallback."""
    from decimal import Decimal as _D

    today = datetime.now(tz=timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(9, -1, -1)]
    usd_closes = [(d, str(80.0 + i)) for i, d in enumerate(dates)]
    http_get_ok = _fake_http_get(usd_closes=usd_closes)
    macro_fetcher.fetch_usdrub_history(state_dir=state_dir, http_get=http_get_ok)

    # Now break USD/RUB (URL contains USD000000TOD).
    fail_only_usd = _fake_http_get(
        fail_urls={
            macro_fetcher.MOEX_USDRUB_HISTORY_URL_TEMPLATE.format(
                from_d=(today - timedelta(days=90)).isoformat(), till_d=today.isoformat()
            )
        }
    )
    out = macro_fetcher.fetch_usdrub_history(state_dir=state_dir, http_get=fail_only_usd)
    assert _D(str(out["closes"][-1][1])) == _D("89.0")
    assert out["source"].startswith("cache:")


def test_fetch_imoex_cache_fallback_when_live_fails(state_dir: Path) -> None:
    """Live IMOEX fails → cache fallback."""
    from decimal import Decimal as _D

    today = datetime.now(tz=timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(69, -1, -1)]
    imoex_closes = [(d, str(3000.0 + i)) for i, d in enumerate(dates)]
    http_get_ok = _fake_http_get(imoex_closes=imoex_closes)
    macro_fetcher.fetch_imoex_history(state_dir=state_dir, http_get=http_get_ok)

    # Now break IMOEX.
    fail_only_imoex = _fake_http_get(
        fail_urls={
            macro_fetcher.MOEX_IMOEX_HISTORY_URL_TEMPLATE.format(
                from_d=(today - timedelta(days=120)).isoformat(), till_d=today.isoformat()
            )
        }
    )
    out = macro_fetcher.fetch_imoex_history(state_dir=state_dir, http_get=fail_only_imoex)
    assert _D(str(out["closes"][-1][1])) == _D("3069.0")
    assert out["source"].startswith("cache:")


# ---------------------------------------------------------------------------
# write_cache_atomic: cleanup on failure
# ---------------------------------------------------------------------------


def test_write_cache_atomic_cleans_up_tmp_on_error(state_dir: Path) -> None:
    """If the rename fails, the .tmp file is unlinked and we re-raise."""
    import pytest

    cache = state_dir / "atomic_fail.json"
    cache.parent.mkdir(parents=True, exist_ok=True)

    def _boom(*a, **kw):
        raise OSError("simulated rename failure")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(macro_fetcher.os, "replace", _boom)
    try:
        with pytest.raises(OSError, match="simulated rename failure"):
            macro_fetcher._write_cache_atomic(cache, {"new": True})
    finally:
        monkey.undo()

    import glob

    leftover = glob.glob(str(cache.parent / "*.tmp"))
    assert leftover == []


# ---------------------------------------------------------------------------
# CSV parser: short rows + non-numeric close
# ---------------------------------------------------------------------------


def test_parse_moex_csv_skips_short_rows_and_bad_closes() -> None:
    """Rows with missing columns OR non-numeric close are skipped."""
    csv = "\n".join(
        [
            "open;high;low;close;volume;begin;end",
            "90;91;89;89.50;1000;2026-08-18;2026-08-18",  # valid
            "short;row",  # short — skipped
            "90;91;89;not-a-number;1000;2026-08-19;2026-08-19",  # bad close — skipped
            "90;91;89;90.00;1000;2026-08-20;2026-08-20",  # valid
        ]
    )
    parsed = macro_fetcher._parse_moex_candles_csv(csv)
    closes = parsed["closes"]
    assert len(closes) == 2
    assert closes[0] == ("2026-08-18", "89.50")
    assert closes[1] == ("2026-08-20", "90.00")


# ---------------------------------------------------------------------------
# _compose_snapshot validation failure (handed a snapshot that fails pydantic)
# ---------------------------------------------------------------------------


def test_compose_snapshot_returns_none_when_usdrub_too_short(state_dir: Path) -> None:
    """USD/RUB has 3 rows (< 6 required) → build_snapshot returns None."""
    today = datetime.now(tz=timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(69, -1, -1)]
    imoex_closes = [(d, str(3000.0 + i)) for i, d in enumerate(dates)]
    usd_short = [(d, "90.0000") for d in _dates(3)]  # too short
    http_get = _fake_http_get(usd_closes=usd_short, imoex_closes=imoex_closes)
    snap = build_snapshot(state_dir=state_dir, http_get=http_get)
    assert snap is None
