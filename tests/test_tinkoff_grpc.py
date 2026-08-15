"""Tests for the new gRPC TinkoffDataLoader.

Replaces the old REST-based TinkoffDataLoader tests in test_data_loader.py.
The new loader uses t-tech-investments (gRPC SDK) instead of REST HTTPS.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.data import LoaderAuthError, TickerMeta, TinkoffDataLoader


class TestTinkoffDataLoader:
    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TINKOFF_SANDBOX_TOKEN", raising=False)
        monkeypatch.delenv("TINKOFF_REAL_TOKEN", raising=False)
        with pytest.raises(LoaderAuthError):
            TinkoffDataLoader()

    def test_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token")
        loader = TinkoffDataLoader()
        assert loader._token == "fake-token"

    def test_explicit_token_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "env-token")
        loader = TinkoffDataLoader(token="explicit-token")
        assert loader._token == "explicit-token"

    def test_get_candles_returns_ohlcv_rows(self) -> None:
        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )

        class Money:
            def __init__(self, v: int) -> None:
                self.units = v
                self.nano = 0

        candle = MagicMock()
        candle.time = datetime(2026, 8, 10, tzinfo=timezone.utc)
        candle.open = Money(311)
        candle.high = Money(312)
        candle.low = Money(308)
        candle.close = Money(311)
        candle.volume = 38000000

        mock_response = MagicMock()
        mock_response.candles = [candle]

        mock_client = MagicMock()
        mock_client.market_data.get_candles.return_value = mock_response

        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            with patch.object(TinkoffDataLoader, "get_ticker", return_value=meta):
                loader = TinkoffDataLoader(token="t")
                bars = loader.fetch_ohlcv(
                    "SBER",
                    datetime(2026, 8, 1).date(),
                    datetime(2026, 8, 14).date(),
                )
        assert len(bars) == 1
        assert bars[0].ticker == "SBER"
        assert bars[0].close == Decimal("311")
        assert bars[0].source == "tkf"

    def test_iter_corporate_actions_yields_nothing_phase_1(self) -> None:
        loader = TinkoffDataLoader(token="t")
        actions = list(loader.iter_corporate_actions("SBER", date(2026, 1, 1), date(2026, 12, 31)))
        assert actions == []

    def test_list_tickers_filters_unavailable(self) -> None:
        def make_share(ticker: str, figi: str | None, status: int, api_trade: bool) -> MagicMock:
            s = MagicMock()
            s.ticker = ticker
            s.figi = figi
            s.name = ticker
            s.lot = 1
            s.isin = f"RU{ticker}"
            s.class_code = "TQBR"
            s.trading_status = status
            s.api_trade_available_flag = api_trade
            return s

        shares = [
            make_share("SBER", "BBG004730N88", 14, True),  # keep
            make_share("DELISTED", None, 14, False),  # api_trade=False -> skip
            make_share("SUSPENDED", "BBG001", 99, True),  # status 99 -> skip
            make_share("GAZP", "BBG004", 14, True),  # keep
        ]
        mock_response = MagicMock()
        mock_response.instruments = shares
        mock_client = MagicMock()
        mock_client.instruments.shares.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            loader = TinkoffDataLoader(token="t")
            tickers = loader.list_tickers()
        assert {t.ticker for t in tickers} == {"SBER", "GAZP"}

    def test_list_bonds_filters_class_and_status(self) -> None:
        """TQOB OFZ + TQCB corp kept; wrong class_code and bad status filtered."""

        def make_bond(
            ticker: str,
            figi: str,
            class_code: str,
            status: int,
            api_trade: bool,
        ) -> MagicMock:
            b = MagicMock()
            b.ticker = ticker
            b.figi = figi
            b.name = f"Bond-{ticker}"
            b.lot = 1
            b.isin = f"RU{ticker}"
            b.class_code = class_code
            b.trading_status = status
            b.api_trade_available_flag = api_trade
            b.currency = "RUB"
            return b

        bonds = [
            make_bond("OFZ26207", "BBG002PD3452", "TQOB", 14, True),  # OFZ — keep
            make_bond("CORP01", "BBGCORP01", "TQCB", 14, True),  # corp — keep
            make_bond("WRONG", "BBGWRONG", "TQIE", 14, True),  # wrong class -> skip
            make_bond("NOPRADE", "BBGNPR", "TQOB", 14, False),  # api_trade=False -> skip
            make_bond("DELIST", "BBGDEL", "TQOB", 99, True),  # bad status -> skip
        ]
        mock_response = MagicMock()
        mock_response.instruments = bonds
        mock_client = MagicMock()
        mock_client.instruments.bonds.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            loader = TinkoffDataLoader(token="t")
            result = loader.list_bonds()
        assert {t.ticker for t in result} == {"OFZ26207", "CORP01"}
        for meta in result:
            assert meta.source == "tkf"
            assert meta.currency == "RUB"

    def test_list_bonds_caches(self) -> None:
        """Second call must not re-hit the gRPC client."""
        bond = MagicMock()
        bond.ticker = "OFZ26207"
        bond.figi = "BBG002PD3452"
        bond.name = "OFZ 26207"
        bond.lot = 1
        bond.isin = "RU000A0JS4M1"
        bond.class_code = "TQOB"
        bond.trading_status = 14
        bond.api_trade_available_flag = True
        bond.currency = "RUB"

        mock_response = MagicMock()
        mock_response.instruments = [bond]
        mock_client = MagicMock()
        mock_client.instruments.bonds.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            loader = TinkoffDataLoader(token="t")
            loader.list_bonds()
            loader.list_bonds()
            loader.list_bonds()
        assert mock_client.instruments.bonds.call_count == 1

    def test_list_etfs_filters_class(self) -> None:
        """TQTE kept; wrong class_code filtered."""

        def make_etf(ticker: str, figi: str, class_code: str, status: int) -> MagicMock:
            e = MagicMock()
            e.ticker = ticker
            e.figi = figi
            e.name = f"ETF-{ticker}"
            e.lot = 1
            e.isin = f"RU{ticker}"
            e.class_code = class_code
            e.trading_status = status
            e.api_trade_available_flag = True
            e.currency = "RUB"
            return e

        etfs = [
            make_etf("TMOS", "BBGTMOS01", "TQTE", 14),  # keep
            make_etf("FXUS", "BBGFXUS01", "TQTE", 14),  # keep
            make_etf("WRONG", "BBGWRONG", "TQTD", 14),  # wrong class -> skip
            make_etf("SUSP", "BBGSUSP", "TQTE", 99),  # bad status -> skip
        ]
        mock_response = MagicMock()
        mock_response.instruments = etfs
        mock_client = MagicMock()
        mock_client.instruments.etfs.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            loader = TinkoffDataLoader(token="t")
            result = loader.list_etfs()
        assert {t.ticker for t in result} == {"TMOS", "FXUS"}
        for meta in result:
            assert meta.source == "tkf"

    def test_list_etfs_caches(self) -> None:
        etf = MagicMock()
        etf.ticker = "TMOS"
        etf.figi = "BBGTMOS01"
        etf.name = "T-Капитал Индекс Мосбиржи"
        etf.lot = 1
        etf.isin = "RU000A101X68"
        etf.class_code = "TQTE"
        etf.trading_status = 14
        etf.api_trade_available_flag = True
        etf.currency = "RUB"

        mock_response = MagicMock()
        mock_response.instruments = [etf]
        mock_client = MagicMock()
        mock_client.instruments.etfs.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            loader = TinkoffDataLoader(token="t")
            loader.list_etfs()
            loader.list_etfs()
        assert mock_client.instruments.etfs.call_count == 1
