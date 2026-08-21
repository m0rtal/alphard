"""Tests for ``src/data/delist_source.py`` — MOEX ISS listed_from/listed_till.

Issue #101: the previous ``min(... key=lambda d: d or date.max)`` trick in
``fetch_delist_dates`` was unidiomatic and brittle — it only worked because
``min(x, x) == x`` when both args coerce to ``date.max``. The new explicit
conditional update accumulator is straightforward and has well-defined
behaviour for the common bond case where every board row omits
``listed_from`` and ``listed_till``.
"""

from __future__ import annotations

import urllib.error
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from src.data.delist_source import fetch_delist_dates


def _xml_two_boards(
    board_rows: list[dict[str, str]],
) -> bytes:
    """Build a minimal /iss/securities/{secid}.xml payload with one
    ``<data id="boards">`` block containing the given row dicts.
    """
    rows_xml = "\n".join("<row " + " ".join(f'{k}="{v}"' for k, v in row.items()) + "/>" for row in board_rows)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<document><data id="boards"><rows>{rows_xml}</rows></data></document>'
    ).encode("utf-8")


def _fake_urlopen(xml: bytes):
    """Return a context manager that yields a fake ``resp.read()`` body."""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

        def read(self) -> bytes:
            return xml

    return _Resp()


class TestFetchDelistDates:
    """Issue #101: lock in the (None, None) and conditional-update semantics."""

    def test_single_row_full_dates(self) -> None:
        xml = _xml_two_boards(
            [
                {
                    "listed_from": "2018-01-15",
                    "listed_till": "",
                    "board": "TQBR",
                }
            ]
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen(xml),
        ):
            result = fetch_delist_dates(["SBER"])
        assert result == {"SBER": (date(2018, 1, 15), None)}

    def test_two_rows_takes_earliest_from_and_latest_till(self) -> None:
        """Multi-board case: boards= TQBR + TQNE.
        TQBR listed_from=2018-01-15 listed_till=2024-12-01.
        TQNE listed_from=2020-06-01 listed_till=2025-03-10.
        Expected: (2018-01-15, 2025-03-10) — earliest from, latest till.
        """
        xml = _xml_two_boards(
            [
                {
                    "listed_from": "2018-01-15",
                    "listed_till": "2024-12-01",
                    "board": "TQBR",
                },
                {
                    "listed_from": "2020-06-01",
                    "listed_till": "2025-03-10",
                    "board": "TQNE",
                },
            ]
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen(xml),
        ):
            result = fetch_delist_dates(["SBER"])
        assert result == {"SBER": (date(2018, 1, 15), date(2025, 3, 10))}

    def test_bond_no_dates_returns_none_none(self) -> None:
        """Issue #101: bonds which mature without delisting have empty
        ``listed_from`` and ``listed_till``. Pre-fix, the
        ``min(...key=...)`` trick made this case work only by accident.
        Post-fix, it must deterministically return (None, None).
        """
        xml = _xml_two_boards(
            [
                {"listed_from": "", "listed_till": "", "board": "TQCB"},
            ]
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen(xml),
        ):
            result = fetch_delist_dates(["OFZ26230"])
        assert result == {"OFZ26230": (None, None)}

    def test_mixed_one_valid_one_empty_takes_valid(self) -> None:
        """Two rows for the same ticker, one with dates and one empty.
        Pre-fix the conditional+min logic only had listed_from half;
        listed_till was already conditional. Now both halves use the
        same idiom.
        """
        xml = _xml_two_boards(
            [
                {"listed_from": "2019-04-01", "listed_till": "2023-08-15"},
                {"listed_from": "", "listed_till": ""},
            ]
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen(xml),
        ):
            result = fetch_delist_dates(["GAZP"])
        assert result == {"GAZP": (date(2019, 4, 1), date(2023, 8, 15))}

    def test_network_error_returns_none_none(self) -> None:
        """Network failure path: documented fallback to (None, None)."""
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns failure"),
        ):
            result = fetch_delist_dates(["SBER"])
        assert result == {"SBER": (None, None)}

    def test_malformed_xml_returns_none_none(self) -> None:
        """Parse error path: also (None, None)."""
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen(b"<not-xml"),
        ):
            result = fetch_delist_dates(["SBER"])
        assert result == {"SBER": (None, None)}

    def test_no_boards_data_block_returns_none_none(self) -> None:
        """A well-formed XML with no <data id="boards"> block.
        The accumulator loop runs zero iterations → (None, None).
        """
        xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<document><data id="other"><rows><row x="1"/></rows></data></document>'
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen(xml),
        ):
            result = fetch_delist_dates(["SBER"])
        assert result == {"SBER": (None, None)}

    def test_multiple_tickers_independent(self) -> None:
        """Two tickers in one call, each independently handled.

        Tinkoff-style test: queue two different XMLs for two URL
        requests, assert both rows are populated independently.
        """
        xml_sber = _xml_two_boards([{"listed_from": "2018-01-15", "listed_till": ""}])
        xml_gazp = _xml_two_boards([{"listed_from": "", "listed_till": ""}])
        # The urlopen side_effect dispatches based on the URL — SBER
        # vs GAZP differ in the secid segment of the path.
        url_to_xml = {
            "https://iss.moex.com/iss/securities/SBER.xml": xml_sber,
            "https://iss.moex.com/iss/securities/GAZP.xml": xml_gazp,
        }

        def _dispatch(url: str, *_a: Any, **_kw: Any):
            return _fake_urlopen(url_to_xml[url])

        with patch("urllib.request.urlopen", side_effect=_dispatch):
            result = fetch_delist_dates(["SBER", "GAZP"])
        assert result == {
            "SBER": (date(2018, 1, 15), None),
            "GAZP": (None, None),
        }

    def test_partial_dates_only_till(self) -> None:
        """Only listed_till set, listed_from empty — common for
        recently-delisted tickers where the listing date is unknown.
        """
        xml = _xml_two_boards([{"listed_from": "", "listed_till": "2024-12-30"}])
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen(xml),
        ):
            result = fetch_delist_dates(["VSMO"])
        assert result == {"VSMO": (None, date(2024, 12, 30))}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
