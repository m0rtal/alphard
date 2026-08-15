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
