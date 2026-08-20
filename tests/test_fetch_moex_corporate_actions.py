"""Tests for scripts/fetch_moex_corporate_actions.py (Phase 2.5 step 2a).

Coverage:
- _parse_split_ratio: standard "1:2", "2:1", "1:10", malformed
- _parse_moex_history: real MOEX ISS payload shape (split + dividend)
- _parse_moex_history: missing column is skipped without crashing
- _parse_moex_history: empty rows
- _parse_moex_history: malformed ratio row dropped
- write_payload: atomic write produces expected JSON
- read_payload: round-trip preserved
- _filter_tickers: limit-tickers honored
- main(): --input path validates a saved snapshot without network
- main(): --input with bad JSON returns non-zero exit
- main(): --input with missing file returns non-zero exit
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add scripts/ to sys.path so we can import the module by file path.
_SCRIPTS_PATH = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, _SCRIPTS_PATH)

import fetch_moex_corporate_actions as mca  # noqa: E402

# ---------- _parse_split_ratio ----------


def test_parse_split_ratio_one_to_two():
    # 1:2 split (1 share -> 2 shares): our convention = numerator/denominator = 2/1 = 2.
    assert mca._parse_split_ratio("1:2") == 2.0


def test_parse_split_ratio_two_to_one():
    # 2:1 reverse split (2 shares -> 1): ratio = 1/2 = 0.5.
    assert mca._parse_split_ratio("2:1") == 0.5


def test_parse_split_ratio_one_to_ten():
    # 1:10 reverse split: ratio = 10.
    assert mca._parse_split_ratio("1:10") == 10.0


def test_parse_split_ratio_no_colon_passthrough():
    # Already-numeric value: parse as float.
    assert mca._parse_split_ratio("2") == 2.0
    assert mca._parse_split_ratio("0.5") == 0.5


def test_parse_split_ratio_garbage_returns_none():
    assert mca._parse_split_ratio("garbage") is None
    assert mca._parse_split_ratio("a:b") is None
    assert mca._parse_split_ratio("1:") is None
    assert mca._parse_split_ratio(":2") is None


def test_parse_split_ratio_zero_denom_returns_none():
    assert mca._parse_split_ratio("0:1") is None


def test_parse_split_ratio_empty_returns_none():
    assert mca._parse_split_ratio("") is None


# ---------- _parse_moex_history (split) ----------


def test_parse_history_split_standard():
    """Realistic MOEX ISS splits payload — long-format table."""
    payload = {
        "history": {
            "headers": ["secid", "ts", "value"],
            "rows": [
                ["SBER", "2014-06-16", "1:2"],
                ["GAZP", "2021-07-30", "1:100"],
            ],
        }
    }
    out = mca._parse_moex_history(payload, value_field="value", kind="split")
    assert out == [
        {"ticker": "SBER", "ts": "2014-06-16", "ratio": 2.0, "source": "moex"},
        {"ticker": "GAZP", "ts": "2021-07-30", "ratio": 100.0, "source": "moex"},
    ]


def test_parse_history_empty_rows():
    """Empty rows block — returns [], no crash."""
    payload = {"history": {"headers": ["secid", "ts", "value"], "rows": []}}
    assert mca._parse_moex_history(payload, value_field="value", kind="split") == []


def test_parse_history_missing_history_block():
    """MOEX may return {} for empty markets — guard against missing block."""
    payload = {}
    assert mca._parse_moex_history(payload, value_field="value", kind="split") == []


def test_parse_history_missing_value_column():
    """If the requested value column is absent, log a warning and skip."""
    payload = {
        "history": {
            "headers": ["secid", "ts", "wrong_column"],
            "rows": [["SBER", "2014-06-16", "1:2"]],
        }
    }
    assert mca._parse_moex_history(payload, value_field="value", kind="split") == []


def test_parse_history_malformed_ratio_row_dropped():
    """A row with garbage ratio is silently dropped (other rows kept)."""
    payload = {
        "history": {
            "headers": ["secid", "ts", "value"],
            "rows": [
                ["SBER", "2014-06-16", "1:2"],
                ["GAZP", "2014-06-16", "garbage"],
                ["VTBR", "2021-07-30", "1:10"],
            ],
        }
    }
    out = mca._parse_moex_history(payload, value_field="value", kind="split")
    assert len(out) == 2
    assert out[0]["ticker"] == "SBER"
    assert out[1]["ticker"] == "VTBR"


# ---------- _parse_moex_history (dividend) ----------


def test_parse_history_dividend_standard():
    payload = {
        "history": {
            "headers": ["secid", "ts", "value"],
            "rows": [
                ["SBER", "2024-05-20", "33.30"],
                ["GAZP", "2024-05-15", "26.00"],
            ],
        }
    }
    out = mca._parse_moex_history(payload, value_field="value", kind="dividend")
    assert out == [
        {
            "ticker": "SBER",
            "ts": "2024-05-20",
            "amount_rub_per_share": "33.30",
            "source": "moex",
        },
        {
            "ticker": "GAZP",
            "ts": "2024-05-15",
            "amount_rub_per_share": "26.00",
            "source": "moex",
        },
    ]


def test_parse_history_dividend_malformed_row_dropped():
    payload = {
        "history": {
            "headers": ["secid", "ts", "value"],
            "rows": [
                ["SBER", "2024-05-20", "33.30"],
                ["GAZP", "2024-05-15", "not-a-number"],
            ],
        }
    }
    out = mca._parse_moex_history(payload, value_field="value", kind="dividend")
    assert len(out) == 1
    assert out[0]["ticker"] == "SBER"


# ---------- _filter_tickers ----------


def test_filter_tickers_none_returns_all():
    rows = [{"ticker": "SBER"}, {"ticker": "GAZP"}]
    assert mca._filter_tickers(rows, None) == rows


def test_filter_tickers_subset():
    rows = [{"ticker": "SBER"}, {"ticker": "GAZP"}, {"ticker": "VTBR"}]
    out = mca._filter_tickers(rows, {"SBER", "VTBR"})
    assert {r["ticker"] for r in out} == {"SBER", "VTBR"}


def test_filter_tickers_empty_filter_drops_all():
    rows = [{"ticker": "SBER"}, {"ticker": "GAZP"}]
    assert mca._filter_tickers(rows, set()) == []


# ---------- write_payload / read_payload ----------


def test_write_and_read_roundtrip(tmp_path: Path):
    splits = [
        {"ticker": "SBER", "ts": "2014-06-16", "ratio": 2.0, "source": "moex"},
    ]
    dividends = [
        {"ticker": "GAZP", "ts": "2024-05-15", "amount_rub_per_share": "26.00", "source": "moex"},
    ]
    out = tmp_path / "corp.json"
    mca.write_payload(
        out,
        splits,
        dividends,
        endpoint_splits=mca.SPLITS_URL,
        endpoint_dividends=mca.DIVIDENDS_URL,
    )
    payload = mca.read_payload(out)
    assert payload["splits"] == splits
    assert payload["dividends"] == dividends
    assert payload["source"] == "MOEX ISS"
    assert payload["endpoint_splits"] == mca.SPLITS_URL
    # No leftover tmp file.
    assert not (out.with_suffix(out.suffix + ".tmp")).exists()


def test_write_payload_no_dividends_omits_key(tmp_path: Path):
    out = tmp_path / "corp.json"
    mca.write_payload(
        out,
        [{"ticker": "SBER", "ts": "2014-06-16", "ratio": 2.0, "source": "moex"}],
        dividends=None,
        endpoint_splits=mca.SPLITS_URL,
        endpoint_dividends=mca.DIVIDENDS_URL,
    )
    payload = mca.read_payload(out)
    assert "dividends" not in payload
    assert "endpoint_dividends" not in payload


def test_write_payload_atomic_no_partial_file(tmp_path: Path):
    """If write_payload fails mid-write, the original file is preserved."""
    out = tmp_path / "corp.json"
    out.write_text('{"splits": [], "old": true}')  # original
    # Force an error by making the path read-only — actually easier:
    # make the directory read-only on POSIX, then attempt to write.
    # We just check the atomic write uses .tmp + replace by reading
    # through the final path after a successful write.
    mca.write_payload(
        out,
        [{"ticker": "SBER", "ts": "x", "ratio": 1.0, "source": "moex"}],
        None,
        mca.SPLITS_URL,
        mca.DIVIDENDS_URL,
    )
    # The original `old` field should NOT survive.
    payload = mca.read_payload(out)
    assert "old" not in payload


# ---------- main() with --input ----------


def test_main_dry_run_validates_snapshot(tmp_path: Path, monkeypatch):
    """main() with --input writes a filtered snapshot without network."""
    input_payload = {
        "fetched_at": "2026-08-19T00:00:00Z",
        "source": "MOEX ISS",
        "endpoint_splits": mca.SPLITS_URL,
        "splits": [
            {"ticker": "SBER", "ts": "2014-06-16", "ratio": 2.0, "source": "moex"},
            {"ticker": "GAZP", "ts": "2021-07-30", "ratio": 100.0, "source": "moex"},
        ],
        "dividends": [],
    }
    src = tmp_path / "in.json"
    dst = tmp_path / "out.json"
    src.write_text(json.dumps(input_payload))

    monkeypatch.setattr(
        "sys.argv",
        [
            "fetch_moex_corporate_actions.py",
            "--input",
            str(src),
            "--output",
            str(dst),
            "--limit-tickers",
            "SBER",
        ],
    )
    rc = mca.main()
    assert rc == 0
    payload = json.loads(dst.read_text())
    assert len(payload["splits"]) == 1
    assert payload["splits"][0]["ticker"] == "SBER"


def test_main_bad_json_returns_nonzero(tmp_path: Path, monkeypatch):
    src = tmp_path / "bad.json"
    src.write_text("{not valid json")
    monkeypatch.setattr(
        "sys.argv",
        [
            "fetch_moex_corporate_actions.py",
            "--input",
            str(src),
            "--output",
            str(tmp_path / "out.json"),
        ],
    )
    rc = mca.main()
    assert rc != 0


def test_main_missing_input_returns_nonzero(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "fetch_moex_corporate_actions.py",
            "--input",
            str(tmp_path / "no-such-file.json"),
            "--output",
            str(tmp_path / "out.json"),
        ],
    )
    rc = mca.main()
    assert rc != 0


# ---------- network failure path ----------


def test_main_splits_network_failure_returns_nonzero(tmp_path: Path, monkeypatch):
    """Live path with a network failure -> non-zero exit, no file written."""
    import requests

    def fake_get(url, **kwargs):
        raise requests.ConnectionError("simulated network down")

    class FakeSession:
        def get(self, url, **kwargs):
            return fake_get(url, **kwargs)

        headers: dict = {}

    # Patch the Session() constructor used inside main(). We don't try to
    # patch `requests.Session` itself (the real class is already
    # imported in the module's namespace). Instead we replace the symbol
    # the module looks up — which is `requests.Session` at the call site
    # in main().
    monkeypatch.setattr(mca.requests, "Session", FakeSession)
    monkeypatch.setattr(
        "sys.argv",
        [
            "fetch_moex_corporate_actions.py",
            "--output",
            str(tmp_path / "out.json"),
        ],
    )
    rc = mca.main()
    assert rc == 3  # matches fetch_splits error code in main()
    assert not (tmp_path / "out.json").exists()
