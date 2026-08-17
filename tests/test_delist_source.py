from __future__ import annotations

"""Tests for src/data/delist_source.fetch_delist_dates."""

from datetime import date
from unittest.mock import patch


from src.data.delist_source import fetch_delist_dates, _parse_date


# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------


def test_parse_date_iso() -> None:
    assert _parse_date("2024-03-15") == date(2024, 3, 15)


def test_parse_date_empty_string() -> None:
    assert _parse_date("") is None


def test_parse_date_none() -> None:
    assert _parse_date(None) is None


def test_parse_date_garbage() -> None:
    assert _parse_date("not-a-date") is None
    assert _parse_date("2024") is None  # too short
    assert _parse_date("2024-13-99") is None  # invalid month/day


# ---------------------------------------------------------------------------
# fetch_delist_dates — mocked URL responses
# ---------------------------------------------------------------------------


_DELISTED_AMEZ_XML = """<?xml version="1.0" encoding="UTF-8"?>
<document>
  <data id="boards">
    <rows>
      <row listed_from="2004-07-19" listed_till="2018-04-02" />
      <row listed_from="2010-05-15" listed_till="2020-12-30" />
    </rows>
  </data>
</document>"""


_ACTIVE_SBER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<document>
  <data id="boards">
    <rows>
      <row listed_from="2007-07-20" listed_till="" />
    </rows>
  </data>
</document>"""


_EMPTY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<document>
</document>"""


_EMPTY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<document>
</document>"""


def _mock_urlopen(xml_by_ticker: dict[str, str]):
    """Build a context-manager that serves the given XML for each ticker URL."""
    from contextlib import contextmanager

    @contextmanager
    def fake_urlopen(url: str, timeout: float = 5.0):
        # url looks like https://iss.moex.com/iss/securities/SBER.xml
        ticker = url.rstrip("/").split("/")[-1].replace(".xml", "")
        body = xml_by_ticker.get(ticker, _EMPTY_XML).encode()

        class Resp:
            def __enter__(self_inner) -> Resp:
                return self_inner

            def __exit__(self_inner, *a: object) -> bool:
                return False

            def read(self_inner) -> bytes:
                return body

        yield Resp()

    return fake_urlopen


def test_fetch_parses_listed_from_and_till() -> None:
    """AMEZ had two board listings; we take min(from), max(till)."""
    from src.data import delist_source

    fake = _mock_urlopen({"AMEZ": _DELISTED_AMEZ_XML})
    with patch.object(delist_source.urllib.request, "urlopen", fake):
        result = fetch_delist_dates(["AMEZ"])
    listed_from, listed_till = result["AMEZ"]
    # min of (2004-07-19, 2010-05-15) = 2004-07-19
    assert listed_from == date(2004, 7, 19)
    # max of (2018-04-02, 2020-12-30) = 2020-12-30
    assert listed_till == date(2020, 12, 30)


def test_fetch_active_ticker_has_none_delisted_at() -> None:
    """SBER has empty listed_till — delisted_at is None, not date.max."""
    from src.data import delist_source

    fake = _mock_urlopen({"SBER": _ACTIVE_SBER_XML})
    with patch.object(delist_source.urllib.request, "urlopen", fake):
        result = fetch_delist_dates(["SBER"])
    listed_from, listed_till = result["SBER"]
    assert listed_from == date(2007, 7, 20)
    assert listed_till is None  # active ticker


def test_fetch_network_failure_yields_none() -> None:
    """Network error → conservative (None, None), don't crash."""
    import urllib.error
    from src.data import delist_source

    def boom(url: str, timeout: float = 5.0):
        raise urllib.error.URLError("network down")

    with patch.object(delist_source.urllib.request, "urlopen", boom):
        result = fetch_delist_dates(["XYZ"])
    assert result["XYZ"] == (None, None)


def test_fetch_empty_xml_yields_none() -> None:
    """ISS sometimes returns an empty document for unknown ticker."""
    from src.data import delist_source

    fake = _mock_urlopen({})
    with patch.object(delist_source.urllib.request, "urlopen", fake):
        result = fetch_delist_dates(["UNKNOWN"])
    assert result["UNKNOWN"] == (None, None)


def test_fetch_multiple_tickers() -> None:
    """Multiple tickers in one call — each gets its own lookup."""
    from src.data import delist_source

    fake = _mock_urlopen(
        {
            "AMEZ": _DELISTED_AMEZ_XML,
            "SBER": _ACTIVE_SBER_XML,
        }
    )
    with patch.object(delist_source.urllib.request, "urlopen", fake):
        result = fetch_delist_dates(["AMEZ", "SBER"])
    assert result["AMEZ"][1] == date(2020, 12, 30)
    assert result["SBER"][1] is None


def test_fetch_malformed_xml_yields_none() -> None:
    """ISS occasionally returns malformed XML — don't crash the sync."""
    from src.data import delist_source

    fake = _mock_urlopen({"BAD": "<not-xml"})
    with patch.object(delist_source.urllib.request, "urlopen", fake):
        result = fetch_delist_dates(["BAD"])
    assert result["BAD"] == (None, None)
