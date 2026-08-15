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
        assert bars[0].primary_source == "tkf"

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


class TestTinkoffLoaderCoverage:
    """Additional tests to push coverage of tinkoff_loader.py above 95%."""

    # ---------------------------------------------------------- helpers

    @staticmethod
    def _money(units: int, nano: int = 0) -> MagicMock:
        m = MagicMock()
        m.units = units
        m.nano = nano
        return m

    @staticmethod
    def _share(
        ticker: str,
        figi: str,
        class_code: str = "TQBR",
        status: int = 14,
        api_trade: bool = True,
        name: str | None = None,
        isin: str | None = None,
    ) -> MagicMock:
        s = MagicMock()
        s.ticker = ticker
        s.figi = figi
        s.name = name or ticker
        s.lot = 1
        s.isin = isin or f"RU{ticker}"
        s.class_code = class_code
        s.trading_status = status
        s.api_trade_available_flag = api_trade
        return s

    @staticmethod
    def _bond(
        ticker: str,
        figi: str,
        class_code: str = "TQOB",
        status: int = 14,
        api_trade: bool = True,
        currency: str = "RUB",
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
        b.currency = currency
        return b

    @staticmethod
    def _etf(
        ticker: str,
        figi: str,
        class_code: str = "TQTE",
        status: int = 14,
        api_trade: bool = True,
        currency: str = "RUB",
    ) -> MagicMock:
        e = MagicMock()
        e.ticker = ticker
        e.figi = figi
        e.name = f"ETF-{ticker}"
        e.lot = 1
        e.isin = f"RU{ticker}"
        e.class_code = class_code
        e.trading_status = status
        e.api_trade_available_flag = api_trade
        e.currency = currency
        return e

    @staticmethod
    def _mock_client_for(instruments: list[MagicMock], method_name: str) -> tuple[MagicMock, MagicMock]:
        """Return (mock_client_class, mock_client) wired for instruments.<method_name>()()."""
        mock_response = MagicMock()
        mock_response.instruments = instruments
        mock_client = MagicMock()
        getattr(mock_client.instruments, method_name).return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        return mock_client_class, mock_client

    # ---------------------------------------------------------- _money_to_decimal / _candle_to_row

    def test_money_to_decimal_handles_subunit(self) -> None:
        from src.data.tinkoff_loader import _money_to_decimal

        money = self._money(units=311, nano=500_000_000)
        assert _money_to_decimal(money) == Decimal("311.5")

    def test_money_to_decimal_missing_attrs(self) -> None:
        from src.data.tinkoff_loader import _money_to_decimal

        # gettatr default 0 path
        assert _money_to_decimal(MagicMock(spec=[])) == Decimal("0")

    def test_candle_to_row_handles_date_only(self) -> None:
        """If candle.time is a date (no .date() attr), use it directly."""
        from src.data.tinkoff_loader import _candle_to_row

        candle = MagicMock()
        candle.time = date(2026, 8, 10)  # plain date, no .date()
        candle.open = self._money(100)
        candle.high = self._money(105)
        candle.low = self._money(99)
        candle.close = self._money(102)
        candle.volume = 1000
        row = _candle_to_row("SBER", candle)
        assert row.ts == date(2026, 8, 10)
        assert row.close == Decimal("102")
        assert row.primary_source == "tkf"

    # ---------------------------------------------------------- token bucket exhaustion

    def test_fetch_ohlcv_calls_bucket_acquire(self) -> None:
        """fetch_ohlcv must acquire a token for each chunk."""
        from src.data import TickerMeta

        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )

        mock_response = MagicMock()
        mock_response.candles = []
        mock_client = MagicMock()
        mock_client.market_data.get_candles.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        # capacity=1 with rate_per_min=1.0 — first chunk's acquire consumes the
        # only token. Single-fetch path exercises bucket.acquire() exactly once.
        loader = TinkoffDataLoader(token="t", rate_per_min=1.0)
        with patch("t_tech.invest.Client", mock_client_class):
            with patch.object(TinkoffDataLoader, "get_ticker", return_value=meta):
                bars = loader.fetch_ohlcv("SBER", datetime(2026, 8, 1).date(), datetime(2026, 8, 14).date())
        assert bars == []

    # ---------------------------------------------------------- list_tickers cache hit

    def test_list_tickers_cache_hit_skips_grpc(self) -> None:
        """Second list_tickers() call must return from cache without hitting the client."""
        cls, client = self._mock_client_for([self._share("SBER", "BBG004730N88")], "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            loader.list_tickers()
            loader.list_tickers()
        assert client.instruments.shares.call_count == 1

    # ---------------------------------------------------------- list_shares_all (lines 163-201)

    def test_list_shares_all_returns_full_universe_including_delisted(self) -> None:
        """list_shares_all does NOT filter on trading_status — returns live + delisted."""
        shares = [
            self._share("SBER", "BBG004730N88", status=14),  # normal
            self._share("VSMO", "BBGVSMO001", status=1),  # NOT_AVAILABLE_FOR_TRADING -> delisted
            self._share("DELISTED", "BBGDEL01", status=99),  # unknown enum -> UNKNOWN branch
        ]
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader.list_shares_all()
        by_ticker = {m.ticker: m for m in result}
        assert set(by_ticker) == {"SBER", "VSMO", "DELISTED"}
        assert by_ticker["SBER"].delisted is False
        assert by_ticker["VSMO"].delisted is True  # NOT_AVAILABLE_FOR_TRADING
        # status 99 -> SecurityTradingStatus(99) raises -> status_name="UNKNOWN" path
        # "UNKNOWN" doesn't contain NOT_AVAILABLE/DELISTED/EXCLUDED -> delisted=False
        assert by_ticker["DELISTED"].delisted is False

    def test_list_shares_all_skips_wrong_class_code(self) -> None:
        shares = [
            self._share("SBER", "BBG1", class_code="TQBR", status=14),
            self._share("OTHER", "BBG2", class_code="TQCB", status=14),
        ]
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader.list_shares_all()
        assert {m.ticker for m in result} == {"SBER"}

    def test_list_shares_all_excluded_status_sets_delisted(self) -> None:
        """Status containing 'EXCLUDED' sets delisted=True via list_shares_all branch."""
        shares = [
            self._share("EXCL", "BBGEXCL", status=2),  # OPENING_PERIOD (no special words)
            # status=1 -> NOT_AVAILABLE_FOR_TRADING -> contains NOT_AVAILABLE_FOR_TRADING
            self._share("DELIST", "BBGDELIST", status=1),
        ]
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader.list_shares_all()
        by_t = {m.ticker: m for m in result}
        assert by_t["DELIST"].delisted is True

    def test_list_shares_all_handles_unknown_enum_status(self) -> None:
        """Status integer outside SecurityTradingStatus enum -> UNKNOWN branch."""
        shares = [
            self._share("STRANGE", "BBGSTRANGE"),
        ]
        shares[0].trading_status = 9999  # int OK, but SecurityTradingStatus(9999) raises
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader.list_shares_all()
        assert len(result) == 1
        assert result[0].delisted is False  # UNKNOWN branch -> delisted=False

    def test_list_shares_all_handles_missing_figi_and_isin(self) -> None:
        """getattr defaults must coerce None cleanly."""
        shares = [self._share("NOFIGI", "BBGNF")]
        shares[0].figi = None
        shares[0].isin = None
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader.list_shares_all()
        assert result[0].figi is None
        assert result[0].isin is None

    def test_list_shares_all_caches(self) -> None:
        """Second call to list_shares_all() uses the cache."""
        shares = [self._share("SBER", "BBG1")]
        cls, client = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            loader.list_shares_all()
            loader.list_shares_all()
        assert client.instruments.shares.call_count == 1

    # ---------------------------------------------------------- get_ticker (lines 232-247)

    def test_get_ticker_finds_in_universe_first(self) -> None:
        from src.data import TickerMeta

        # Pre-populate _universe_cache with one entry.
        loader = TinkoffDataLoader(token="t")
        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )
        loader._universe_cache = {"SBER": meta}
        found = loader.get_ticker("SBER")
        assert found.ticker == "SBER"
        assert found.figi == "BBG004730N88"

    def test_get_ticker_falls_back_to_bonds(self) -> None:
        from src.data import TickerMeta

        # Pre-populate bonds cache only.
        loader = TinkoffDataLoader(token="t")
        bond_meta = TickerMeta(
            ticker="OFZ26207",
            figi="BBG002PD3452",
            name="OFZ",
            lot=1,
            isin="RU000A0JS4M1",
            currency="RUB",
            source="tkf",
            class_code="TQOB",
        )
        loader._universe_cache = {}
        loader._bonds_cache = {"OFZ26207": bond_meta}
        loader._etfs_cache = {}
        found = loader.get_ticker("OFZ26207")
        assert found.ticker == "OFZ26207"
        assert found.class_code == "TQOB"

    def test_get_ticker_falls_back_to_etfs(self) -> None:
        from src.data import TickerMeta

        loader = TinkoffDataLoader(token="t")
        etf_meta = TickerMeta(
            ticker="TMOS",
            figi="BBGTMOS01",
            name="T-Капитал",
            lot=1,
            isin="RU000A101X68",
            currency="RUB",
            source="tkf",
            class_code="TQTE",
        )
        loader._universe_cache = {}
        loader._bonds_cache = {}
        loader._etfs_cache = {"TMOS": etf_meta}
        found = loader.get_ticker("TMOS")
        assert found.ticker == "TMOS"

    def test_get_ticker_raises_when_missing(self) -> None:
        from src.data import LoaderNotFoundError

        # Mock all three universe endpoints to return empty lists so cache getters
        # don't try a real gRPC connection.
        empty_response = MagicMock()
        empty_response.instruments = []
        mock_client = MagicMock()
        mock_client.instruments.shares.return_value = empty_response
        mock_client.instruments.bonds.return_value = empty_response
        mock_client.instruments.etfs.return_value = empty_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            loader = TinkoffDataLoader(token="t")
            with pytest.raises(LoaderNotFoundError):
                loader.get_ticker("NOPE")

    def test_get_ticker_finds_in_list_shares_all_cache(self) -> None:
        """get_ticker() iterates _shares_all_TQBR cache before live universe."""
        from src.data import TickerMeta

        loader = TinkoffDataLoader(token="t")
        meta = TickerMeta(
            ticker="VSMO",
            figi="BBGVSMO001",
            name="VSMO",
            lot=1,
            isin="RU000VSMO",
            currency="RUB",
            source="tkf",
            delisted=True,
        )
        # Pre-populate the list_shares_all cache (covers line 237-240).
        loader._shares_all_TQBR = [meta]  # type: ignore[attr-defined]
        # And the live caches are empty.
        loader._universe_cache = {}
        loader._bonds_cache = {}
        loader._etfs_cache = {}
        found = loader.get_ticker("VSMO")
        assert found.ticker == "VSMO"
        assert found.delisted is True

    def test_get_ticker_uppercases_input(self) -> None:
        from src.data import TickerMeta

        loader = TinkoffDataLoader(token="t")
        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )
        loader._universe_cache = {"SBER": meta}
        # Lowercase input -> uppercased internally.
        assert loader.get_ticker("sber").ticker == "SBER"

    # ---------------------------------------------------------- fetch_ohlcv edge cases

    def test_fetch_ohlcv_raises_when_no_figi(self) -> None:
        """If the resolved TickerMeta has figi=None, fetch_ohlcv raises LoaderError (line 266)."""
        from src.data import LoaderError, TickerMeta

        meta_no_figi = TickerMeta(
            ticker="NOFIGI",
            figi=None,
            name="NoFigi",
            lot=1,
            isin="RU000NOFIGI",
            currency="RUB",
            source="tkf",
        )
        loader = TinkoffDataLoader(token="t")
        with patch.object(loader, "get_ticker", return_value=meta_no_figi):
            with pytest.raises(LoaderError):
                loader.fetch_ohlcv("NOFIGI", datetime(2026, 8, 1).date(), datetime(2026, 8, 14).date())

    def test_fetch_ohlcv_accepts_naive_datetime_start(self) -> None:
        """start as naive datetime — loader must attach UTC tzinfo (line 270)."""
        from src.data import TickerMeta

        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )

        mock_response = MagicMock()
        mock_response.candles = []
        mock_client = MagicMock()
        mock_client.market_data.get_candles.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        # Use datetime for BOTH ends so _validate_range (date vs datetime cmp) doesn't trip.
        naive_start = datetime(2026, 8, 1, 0, 0, 0)  # tzinfo=None
        naive_end = datetime(2026, 8, 14, 0, 0, 0)  # tzinfo=None
        with patch("t_tech.invest.Client", mock_client_class):
            with patch.object(TinkoffDataLoader, "get_ticker", return_value=meta):
                loader = TinkoffDataLoader(token="t")
                bars = loader.fetch_ohlcv("SBER", naive_start, naive_end)
        assert bars == []
        # Verify the call used an aware datetime for from_.
        call_kwargs = mock_client.market_data.get_candles.call_args.kwargs
        assert call_kwargs["from_"].tzinfo is not None

    def test_fetch_ohlcv_accepts_naive_datetime_end(self) -> None:
        """end as naive datetime — loader must attach UTC tzinfo (line 274)."""
        from src.data import TickerMeta

        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )

        mock_response = MagicMock()
        mock_response.candles = []
        mock_client = MagicMock()
        mock_client.market_data.get_candles.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        # Both naive so _validate_range (aware vs naive cmp) doesn't trip.
        naive_start = datetime(2026, 8, 1, 0, 0, 0)
        naive_end = datetime(2026, 8, 14, 0, 0, 0)  # tzinfo=None
        with patch("t_tech.invest.Client", mock_client_class):
            with patch.object(TinkoffDataLoader, "get_ticker", return_value=meta):
                loader = TinkoffDataLoader(token="t")
                bars = loader.fetch_ohlcv("SBER", naive_start, naive_end)
        assert bars == []
        call_kwargs = mock_client.market_data.get_candles.call_args.kwargs
        assert call_kwargs["to"].tzinfo is not None

    def test_fetch_ohlcv_validates_inverted_range(self) -> None:
        """start > end must raise LoaderError before any I/O."""
        from src.data import LoaderError, TickerMeta

        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )
        loader = TinkoffDataLoader(token="t")
        with patch.object(loader, "get_ticker", return_value=meta):
            with pytest.raises(LoaderError):
                loader.fetch_ohlcv("SBER", datetime(2026, 8, 14).date(), datetime(2026, 8, 1).date())

    def test_fetch_ohlcv_chunks_long_range(self) -> None:
        """A range > 365d should produce multiple chunks and thus multiple gRPC calls."""
        from src.data import TickerMeta

        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )

        candle = MagicMock()
        candle.time = datetime(2026, 8, 10, tzinfo=timezone.utc)
        candle.open = self._money(311)
        candle.high = self._money(312)
        candle.low = self._money(308)
        candle.close = self._money(311)
        candle.volume = 1000

        mock_response = MagicMock()
        mock_response.candles = [candle]
        mock_client = MagicMock()
        mock_client.market_data.get_candles.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            with patch.object(TinkoffDataLoader, "get_ticker", return_value=meta):
                loader = TinkoffDataLoader(token="t", rate_per_min=1000.0)
                # 800-day range > 365d -> must produce 2 chunks.
                bars = loader.fetch_ohlcv("SBER", date(2024, 1, 1), date(2026, 3, 1))
        assert mock_client.market_data.get_candles.call_count >= 2
        assert len(bars) >= 2

    def test_fetch_ohlcv_empty_response(self) -> None:
        from src.data import TickerMeta

        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )

        mock_response = MagicMock()
        mock_response.candles = []  # no candles returned
        mock_client = MagicMock()
        mock_client.market_data.get_candles.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            with patch.object(TinkoffDataLoader, "get_ticker", return_value=meta):
                loader = TinkoffDataLoader(token="t")
                bars = loader.fetch_ohlcv("SBER", datetime(2026, 8, 1).date(), datetime(2026, 8, 14).date())
        assert bars == []

    def test_fetch_ohlcv_decimal_precision(self) -> None:
        """OHLCV values must use 8-decimal precision from nano field."""
        from src.data import TickerMeta

        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )

        candle = MagicMock()
        candle.time = datetime(2026, 8, 10, tzinfo=timezone.utc)
        candle.open = self._money(311, nano=123_456_789)
        candle.high = self._money(312, nano=999_999_999)
        candle.low = self._money(308, nano=1)
        candle.close = self._money(311, nano=500_000_000)
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
                bars = loader.fetch_ohlcv("SBER", datetime(2026, 8, 1).date(), datetime(2026, 8, 14).date())
        assert len(bars) == 1
        # 311 + 0.500000000 = 311.500000000
        assert bars[0].close == Decimal("311.500000000")
        # 311 + 0.123456789 = 311.123456789
        assert bars[0].open == Decimal("311.123456789")

    # ---------------------------------------------------------- iter_ohlcv (line 309)

    def test_iter_ohlcv_yields_same_as_fetch(self) -> None:
        from src.data import TickerMeta

        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )

        candle = MagicMock()
        candle.time = datetime(2026, 8, 10, tzinfo=timezone.utc)
        candle.open = self._money(100)
        candle.high = self._money(101)
        candle.low = self._money(99)
        candle.close = self._money(100)
        candle.volume = 1000

        mock_response = MagicMock()
        mock_response.candles = [candle]
        mock_client = MagicMock()
        mock_client.market_data.get_candles.return_value = mock_response
        mock_client_class = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        with patch("t_tech.invest.Client", mock_client_class):
            with patch.object(TinkoffDataLoader, "get_ticker", return_value=meta):
                loader = TinkoffDataLoader(token="t")
                rows = list(loader.iter_ohlcv("SBER", datetime(2026, 8, 1).date(), datetime(2026, 8, 14).date()))
        assert len(rows) == 1
        assert rows[0].ticker == "SBER"

    # ---------------------------------------------------------- _ensure_universe (lines 326, 335, 342-343)

    def test_ensure_universe_cache_hit(self) -> None:
        """Pre-populating _universe_cache must bypass the gRPC call (line 326)."""
        from src.data import TickerMeta

        loader = TinkoffDataLoader(token="t")
        meta = TickerMeta(
            ticker="SBER",
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            isin="RU0009029540",
            currency="RUB",
            source="tkf",
        )
        loader._universe_cache = {"SBER": meta}
        result = loader._ensure_universe()
        assert result == {"SBER": meta}

    def test_ensure_universe_skips_wrong_class_code(self) -> None:
        """Class code != TQBR must be filtered (line 335)."""
        shares = [
            self._share("SBER", "BBG1", class_code="TQBR", status=14),
            self._share("WRONG", "BBG2", class_code="TQCB", status=14),
        ]
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_universe()
        assert set(result) == {"SBER"}

    def test_ensure_universe_skips_bad_status(self) -> None:
        """Status not in (5,14,15) must be filtered."""
        shares = [
            self._share("GOOD", "BBG1", status=14),
            self._share("BAD", "BBG2", status=99),  # not 5/14/15
        ]
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_universe()
        assert set(result) == {"GOOD"}

    def test_ensure_universe_skips_no_api_trade(self) -> None:
        """api_trade_available_flag=False must be filtered."""
        shares = [
            self._share("YES", "BBG1", api_trade=True),
            self._share("NO", "BBG2", api_trade=False),
        ]
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_universe()
        assert set(result) == {"YES"}

    def test_ensure_universe_non_integer_status_keeps_instrument(self) -> None:
        """ValueError on int() must NOT skip the instrument — `pass` (lines 342-343)."""
        shares = [self._share("SBER", "BBG1", status=14)]
        shares[0].trading_status = "weird"  # int() will raise
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_universe()
        # The except branch falls through `pass` — the instrument is included.
        assert "SBER" in result

    def test_ensure_universe_handles_missing_figi(self) -> None:
        """Missing figi attribute defaults to None (defensive getattr)."""
        shares = [self._share("SBER", "BBG1")]
        shares[0].figi = None
        cls, _ = self._mock_client_for(shares, "shares")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_universe()
        assert result["SBER"].figi is None

    # ---------------------------------------------------------- _ensure_bonds (lines 363, 380-381)

    def test_ensure_bonds_cache_hit(self) -> None:
        from src.data import TickerMeta

        loader = TinkoffDataLoader(token="t")
        meta = TickerMeta(
            ticker="OFZ26207",
            figi="BBG002PD3452",
            name="OFZ",
            lot=1,
            isin="RU000A0JS4M1",
            currency="RUB",
            source="tkf",
            class_code="TQOB",
        )
        loader._bonds_cache = {"OFZ26207": meta}
        result = loader._ensure_bonds()
        assert result == {"OFZ26207": meta}

    def test_ensure_bonds_skips_wrong_class_code(self) -> None:
        """Class codes outside TQOB/TQCB must be dropped."""
        bonds = [
            self._bond("OFZ", "BBG1", class_code="TQOB"),
            self._bond("CORP", "BBG2", class_code="TQCB"),
            self._bond("WRONG", "BBG3", class_code="TQIE"),
        ]
        cls, _ = self._mock_client_for(bonds, "bonds")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_bonds()
        assert set(result) == {"OFZ", "CORP"}

    def test_ensure_bonds_skips_bad_status(self) -> None:
        bonds = [
            self._bond("GOOD", "BBG1", status=14),
            self._bond("BAD", "BBG2", status=99),
        ]
        cls, _ = self._mock_client_for(bonds, "bonds")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_bonds()
        assert set(result) == {"GOOD"}

    def test_ensure_bonds_non_integer_status_keeps_instrument(self) -> None:
        """ValueError on int() falls through `pass` — instrument kept (lines 380-381)."""
        bonds = [self._bond("OFZ", "BBG1", status=14)]
        bonds[0].trading_status = "garbage"
        cls, _ = self._mock_client_for(bonds, "bonds")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_bonds()
        assert "OFZ" in result

    def test_ensure_bonds_falls_back_to_rub_currency(self) -> None:
        """Missing/None currency must default to RUB (getattr default + `or 'RUB'`)."""
        bonds = [self._bond("OFZ", "BBG1", status=14)]
        bonds[0].currency = None
        cls, _ = self._mock_client_for(bonds, "bonds")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_bonds()
        assert result["OFZ"].currency == "RUB"

    def test_ensure_bonds_skips_no_api_trade(self) -> None:
        bonds = [
            self._bond("YES", "BBG1", api_trade=True),
            self._bond("NO", "BBG2", api_trade=False),
        ]
        cls, _ = self._mock_client_for(bonds, "bonds")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_bonds()
        assert set(result) == {"YES"}

    # ---------------------------------------------------------- _ensure_etfs (lines 401, 417-418, 420)

    def test_ensure_etfs_cache_hit(self) -> None:
        from src.data import TickerMeta

        loader = TinkoffDataLoader(token="t")
        meta = TickerMeta(
            ticker="TMOS",
            figi="BBGTMOS01",
            name="T-Капитал",
            lot=1,
            isin="RU000A101X68",
            currency="RUB",
            source="tkf",
            class_code="TQTE",
        )
        loader._etfs_cache = {"TMOS": meta}
        result = loader._ensure_etfs()
        assert result == {"TMOS": meta}

    def test_ensure_etfs_skips_wrong_class_code(self) -> None:
        etfs = [
            self._etf("GOOD", "BBG1", class_code="TQTE"),
            self._etf("WRONG", "BBG2", class_code="TQTD"),
        ]
        cls, _ = self._mock_client_for(etfs, "etfs")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_etfs()
        assert set(result) == {"GOOD"}

    def test_ensure_etfs_skips_bad_status(self) -> None:
        etfs = [
            self._etf("GOOD", "BBG1", status=14),
            self._etf("BAD", "BBG2", status=99),
        ]
        cls, _ = self._mock_client_for(etfs, "etfs")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_etfs()
        assert set(result) == {"GOOD"}

    def test_ensure_etfs_non_integer_status_keeps_instrument(self) -> None:
        """ValueError on int() falls through `pass` — instrument kept (lines 417-418)."""
        etfs = [self._etf("GOOD", "BBG1", status=14)]
        etfs[0].trading_status = "garbage"
        cls, _ = self._mock_client_for(etfs, "etfs")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_etfs()
        assert "GOOD" in result

    def test_ensure_etfs_skips_no_api_trade(self) -> None:
        """api_trade_available_flag=False must drop the ETF (line 420)."""
        etfs = [
            self._etf("YES", "BBG1", api_trade=True),
            self._etf("NO", "BBG2", api_trade=False),
        ]
        cls, _ = self._mock_client_for(etfs, "etfs")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_etfs()
        assert set(result) == {"YES"}

    def test_ensure_etfs_falls_back_to_rub_currency(self) -> None:
        etfs = [self._etf("GOOD", "BBG1", status=14)]
        etfs[0].currency = None
        cls, _ = self._mock_client_for(etfs, "etfs")
        with patch("t_tech.invest.Client", cls):
            loader = TinkoffDataLoader(token="t")
            result = loader._ensure_etfs()
        assert result["GOOD"].currency == "RUB"
