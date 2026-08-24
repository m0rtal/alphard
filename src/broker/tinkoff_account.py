"""TinkoffAccount — concrete broker via t-tech-investments SDK.

Sandbox auto-detect via TINKOFF_SANDBOX_TOKEN prefix 't.' (real tokens vary).

RiskGate integration: place_order() calls RiskGate.evaluate() BEFORE
Tinkoff SDK call. If RiskGate returns allowed=False, order is rejected
without touching the network.

If t-tech-investments SDK is not installed (sandbox or missing dep),
falls back to MockTinkoffClient for unit testing.
"""

from __future__ import annotations

import json
import logging
import os

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from src.broker.account import BrokerAccount, PortfolioSnapshot, Position
from src.broker.orders import (
    LimitOrder,
    MarketOrder,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.data.token_bucket import TokenBucket

logger = logging.getLogger("alphard.broker.tinkoff")


# Moscow-trading-day helper for daily P&L rollover (issue #197).
# The Moscow Exchange (MOEX) trades Mon-Fri; weekends/holidays must
# not split an "intraday" P&L into two separate days. We use
# ``MSK`` (UTC+3) to align with the broker's trading-day boundary.
_MSK = timezone(timedelta(hours=3))


def _msk_today() -> date:
    """Return the current date in Moscow time (UTC+3).

    Used by ``_fetch_real_portfolio_state`` to detect a trading-day
    rollover and refresh ``previous_close_equity``. Always returns a
    ``date`` — never a datetime — so JSON serialisation is trivial.
    """
    return datetime.now(tz=_MSK).date()


def _broker_position_to_gate_position(broker_pos: Any) -> Any:
    """Convert a broker ``Position`` (dataclass) to a gate ``Position`` (pydantic).

    The two models have the same fields except for the symbol/ticker
    naming difference. Sectors are not yet mapped from Tinkoff in
    Phase 1; the gate's sector check will skip on ``sector=None``
    until Phase 2 wires the sector map.
    """
    from src.risk.gate import Position as _GatePosition

    return _GatePosition(
        symbol=broker_pos.ticker,
        quantity=broker_pos.quantity,
        avg_price=broker_pos.avg_price,
        sector=None,
    )


class BrokerError(RuntimeError):
    """Technical failure from Tinkoff SDK."""


def _assert_not_live_trading(action: str, ticker: str = "") -> bool:
    """Phase 1 hard no-trade gate. Returns True if trading is allowed.

    This is the single source of truth for the LIVE_TRADING=false
    constraint. Every code path that could place a real order MUST
    call this before they touch the broker — not just place_order(),
    so that any future entry point (e.g. a background-driven dry-run,
    a coordinator flow, a manual CLI command) inherits the same
    guarantee.

    The check is env-driven so it works for sandbox, CI, and
    production alike. We refuse by default (LIVE_TRADING!="true")
    — the operator must explicitly opt-in to live trading for
    each fresh process.
    """
    if os.environ.get("LIVE_TRADING", "false").lower() == "true":
        return True
    logger.warning(
        "LIVE_TRADING=false — refusing %s%s (Phase 1 hard no-trade)",
        action,
        f" for {ticker}" if ticker else "",
    )
    return False


class TinkoffAccount(BrokerAccount):
    """Tinkoff Invest API account.

    Args:
        token: Tinkoff API token. Sandbox tokens start with 't.' (real have other prefix).
        account_id: Tinkoff account ID (sandbox default: 'SB1' for OpenSandboxAccount).
        risk_gate: Optional RiskGate instance. If None, all orders are rejected
            (fail-safe — explicit opt-in required).
        rate_limit_per_sec: Tinkoff API limit (default 60/sec).
    """

    def __init__(
        self,
        token: str,
        account_id: str = "SB1",
        # Use Any locally to satisfy --strict without runtime isinstance check
        risk_gate: Any = None,  # src.risk.gate.RiskGate (typed Any to satisfy --strict)
        rate_limit_per_sec: int = 60,
        # Issue #32: persistent peak-equity store path. If None, defaults
        # to $ALPHARD_PEAK_STORE_DIR or /var/lib/alphard. The file is
        # read once on construction and written on every successful
        # _fetch_real_portfolio_state() call (monotonically non-decreasing).
        peak_store_dir: Optional[str] = None,
    ):
        self._token = token
        self._account_id = account_id
        self._risk_gate = risk_gate
        self._rate_limit_per_sec = rate_limit_per_sec
        # Issue #103: use the shared, thread-safe TokenBucket primitive
        # instead of a hand-rolled list-without-lock. Two threads calling
        # place_order concurrently used to both observe an empty list,
        # both append, and burst above the SLA (Tinkoff's server-side
        # rate-limiter then 429s — order silently rejected AFTER RiskGate
        # had already approved). TokenBucket.acquire() is documented
        # thread-safe and is the same primitive data loaders use
        # (src/data/token_bucket.py).
        self._rate_bucket = TokenBucket(
            rate=float(rate_limit_per_sec),
            window_seconds=1.0,
        )
        # Issue #32: peak-equity high-water mark tracker. Holds the
        # maximum total_equity observed across this account's history so
        # that _check_drawdown() in src/risk/gate.py can compute
        # drawdown = (peak - current) / peak and trip the RISK_DD guard
        # when it exceeds max_dd_pct. Without this, peak == current and
        # drawdown is always 0% — the bug fixed here.
        if peak_store_dir is None:
            peak_store_dir = os.environ.get("ALPHARD_PEAK_STORE_DIR", "/var/lib/alphard")
        self._peak_store_dir: str = peak_store_dir
        self._peak_equity_path: str = os.path.join(peak_store_dir, f"peak_equity_{account_id}.json")
        # Issue #199: keep a sibling .bak file holding the
        # last-known-good peak so a corrupt primary can fall back to
        # it instead of silently resetting to zero (RISK_DD disarm).
        self._peak_equity_bak_path: str = self._peak_equity_path + ".bak"
        self._peak_equity: Decimal = self._load_peak_equity()
        # Issue #197: persist a sibling "daily_pnl_basis" tracker alongside
        # the peak-equity HWM so that ``PortfolioState.daily_pnl`` (which
        # drives ``RiskGate._check_daily_loss``) reflects the realised
        # today-vs-previous-close delta rather than defaulting to 0. The
        # previous hardcoded ``Decimal("0")`` meant ``RISK_DAILY_LOSS``
        # never tripped in production — same exploit class as issues
        # #195 (peak_equity not wired) and #11 (placeholder price
        # bypass). Same file-naming convention as the peak store:
        # one file per ``account_id``, best-effort writes, missing file
        # is treated as "no history yet" (daily_pnl == 0, no violation).
        self._daily_pnl_basis_path: str = os.path.join(peak_store_dir, f"daily_pnl_basis_{account_id}.json")
        # Issue #207: a persisted basis is "trusted" only if it was written
        # by this code path on a previous rollover AND the on-disk file is
        # structurally complete (schema_version + basis_valid). A missing
        # ``schema_version`` field means the file predates this issue and
        # must NOT be trusted without an explicit upgrade — otherwise a
        # legacy corrupt file would silently disarm ``RISK_DAILY_LOSS``
        # for the rest of the session. ``_load_daily_pnl_basis`` sets
        # this to True only on a fully-validated read.
        self._basis_trusted: bool = False
        self._previous_close_equity: Decimal = Decimal("0")
        self._last_trading_day: date | None = None
        self._load_daily_pnl_basis()

    def _load_peak_equity(self) -> Decimal:
        """Read the persisted peak-equity high-water mark from disk.

        Returns Decimal("0") if neither the primary nor the .bak file
        exists (cold start) or both are unparseable (corrupted).

        Issue #199: the previous implementation silently reset the peak
        to 0 on JSONDecodeError and overwrote the corrupt file on the
        next save — turning a recoverable corruption (e.g. trailing null
        byte from a partial write) into a permanent zero-reset that
        disarms the RISK_DD guard. The fix:

        1. Try the primary file first. On success, return its parsed
           value (and remove any stale .corrupt-* forensics file left
           by a previous load — clean state).
        2. If the primary is corrupt, rename it aside as
           ``peak_equity_{account_id}.corrupt-{ts}.json`` for
           post-mortem, then fall back to the .bak file. If the .bak
           parses, return its value (last-known-good). If neither
           parses, return 0 with a warning.
        3. Negative values are still clamped to 0 (defence-in-depth
           from issue #32 era).
        """
        value = self._try_load_peak_from(self._peak_equity_path)
        if value is not None:
            # Clean up any leftover .corrupt-* forensics file from a
            # previous recovery; the primary is now good so the
            # evidence has served its purpose.
            self._prune_corrupt_forensics()
            return value

        # Primary corrupt / missing — try .bak (last-known-good mirror).
        bak_value = self._try_load_peak_from(self._peak_equity_bak_path)
        if bak_value is not None:
            logger.warning(
                "Peak equity primary %s was missing/unparse; " "recovered from .bak mirror %s (value=%s)",
                self._peak_equity_path,
                self._peak_equity_bak_path,
                bak_value,
            )
            return bak_value

        # Both missing / corrupt. Preserved corruption moved aside
        # by _try_load_peak_from; fall back to zero.
        return Decimal("0")

    def _try_load_peak_from(self, path: str) -> Decimal | None:
        """Read a peak-equity file. Return None on missing/corrupt.

        On JSONDecodeError, the file is renamed aside as
        ``<path>.corrupt-<unix-ts>`` for post-mortem; on FileNotFound
        the path is left alone (cold-start path). A successfully
        parsed file with a negative value is clamped to 0.
        """
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
            value = Decimal(str(data.get("peak_equity", "0")))
            if value < 0:
                logger.warning(
                    "Peak equity file %s has negative value %s; treating as 0",
                    path,
                    value,
                )
                return Decimal("0")
            return value
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "Peak equity file %s is corrupt (%s); renaming aside for forensics",
                path,
                e,
            )
            # Non-destructive: rename the corrupt file aside rather
            # than letting the next save silently overwrite it. Two
            # files with the same mtime (rare but possible — both
            # written in the same millisecond by a backup mirror) must
            # NOT clobber each other's forensics; always suffix with
            # pid + a per-call counter.
            try:
                ts = int(os.path.getmtime(path))
                corrupt_target = f"{path}.corrupt-{ts}-{os.getpid()}"
                # If a stale forensics file at this exact target
                # already exists (re-recovery), suffix again so we
                # never clobber evidence.
                if os.path.exists(corrupt_target):
                    corrupt_target = f"{path}.corrupt-{ts}-{os.getpid()}-{id(e)}"
                os.replace(path, corrupt_target)
            except OSError as rename_err:
                logger.debug(
                    "Could not rename corrupt peak file %s aside: %s",
                    path,
                    rename_err,
                )
            return None

    def _prune_corrupt_forensics(self) -> None:
        """Remove stale .corrupt-* forensics files left by a previous
        recovery. Best-effort: a failure here is logged at DEBUG and
        does not propagate.
        """
        try:
            for entry in os.listdir(self._peak_store_dir):
                if entry.startswith(os.path.basename(self._peak_equity_path) + ".corrupt-"):
                    try:
                        os.remove(os.path.join(self._peak_store_dir, entry))
                    except OSError as prune_err:
                        logger.debug(
                            "Could not prune corrupt-forensic %s: %s",
                            entry,
                            prune_err,
                        )
        except OSError as list_err:
            logger.debug(
                "Could not list peak store dir %s for forensics prune: %s",
                self._peak_store_dir,
                list_err,
            )

    def _save_peak_equity(self) -> None:
        """Persist the current peak-equity to disk (best-effort, atomic).

        Write is best-effort: a failure here logs a warning but does
        not raise. The in-memory peak is the source of truth for the
        current process; on the next start the peak is reloaded from
        disk. The worst case is losing one cycle of drawdown tracking.

        Issue #199: the previous implementation used a non-atomic
        ``open(path, "w") + json.dump(...)`` (truncate-then-write).
        a SIGKILL, Docker healthcheck kill, or disk-full mid-write
        leaves a 0-byte or partial-JSON file; the next start logs
        'corrupt; starting from 0' and RISK_DD silently loses all
        drawdown history — the very guard the peak store was added
        to enable (issues #32, #195). The fix:

        1. Write to a sibling temp file in the SAME directory
           (same filesystem ⇒ ``os.replace`` is POSIX-atomic).
        2. ``fh.flush()`` + ``os.fsync(fh.fileno())`` before close
           so the bytes hit the platter before the rename publishes.
        3. Mirror the previous-good value into ``.bak`` BEFORE each
           successful write so a corrupt primary can fall back to
           the last-known-good value rather than zero.
        4. On load, if the primary is corrupt, rename it aside for
           forensics rather than silently overwriting on the next
           save; if a ``.bak`` is present and parseable, prefer it.
        """
        try:
            os.makedirs(self._peak_store_dir, exist_ok=True)
            # Mirror previous-good value to .bak BEFORE we touch the
            # primary. We snapshot the *current* primary content (if
            # any) — not the new value — so the .bak is always the
            # last-known-good peak, not the peak we're about to lose.
            if os.path.exists(self._peak_equity_path):
                try:
                    with open(self._peak_equity_path, "r") as src:
                        prev = src.read()
                    with open(self._peak_equity_bak_path, "w") as dst:
                        dst.write(prev)
                        dst.flush()
                        os.fsync(dst.fileno())
                except (OSError, ValueError) as backup_err:
                    # Non-fatal: just log and continue. The primary
                    # write is still atomic; we only lose the .bak
                    # mirror, not the new value.
                    logger.debug(
                        "Peak equity .bak mirror failed for %s: %s",
                        self._peak_equity_bak_path,
                        backup_err,
                    )

            # Atomic write: temp file in same dir → os.replace.
            tmp_path = self._peak_equity_path + ".tmp"
            try:
                with open(tmp_path, "w") as fh:
                    json.dump({"peak_equity": str(self._peak_equity)}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self._peak_equity_path)
            except (OSError, ValueError):
                # Best-effort cleanup: if the tmp file was created but
                # the rename never completed (mid-write crash, disk-full,
                # etc.), remove the orphan so it doesn't accumulate
                # across restarts and confuse the next loader.
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning(
                "Failed to persist peak equity to %s: %s",
                self._peak_equity_path,
                e,
            )

    def _load_daily_pnl_basis(self) -> None:
        """Read the persisted daily-P&L basis from disk.

        The basis is a ``(previous_close_equity, last_trading_day)``
        pair; on first read in a new trading day,
        ``_fetch_real_portfolio_state`` rolls it over. A missing /
        corrupt file is treated as "no history yet" — i.e.
        ``previous_close_equity == 0``, ``last_trading_day is None``
        AND ``_basis_trusted == False`` — so ``daily_pnl`` for the
        first call is 0 (no violation possible) and the upcoming
        snapshot becomes the new basis.

        Issue #207: an on-disk basis is only trusted when its payload
        carries ``schema_version >= 1`` AND ``basis_valid == True``.
        Legacy files written before this issue lack these fields and
        are treated as cold-start (basis untrusted → fail-closed in
        ``_fetch_real_portfolio_state`` if the day has rolled over,
        so a corrupted/stale file cannot silently disarm
        ``RISK_DAILY_LOSS``).
        """
        try:
            with open(self._daily_pnl_basis_path, "r") as fh:
                data = json.load(fh)
            value = Decimal(str(data.get("previous_close_equity", "0")))
            if value < 0:
                logger.warning(
                    "Daily-PnL basis file %s has negative value %s; treating as 0",
                    self._daily_pnl_basis_path,
                    value,
                )
                value = Decimal("0")
            self._previous_close_equity = value
            last_day_raw = data.get("last_trading_day")
            parsed_day: date | None = None
            if isinstance(last_day_raw, str):
                try:
                    parsed_day = date.fromisoformat(last_day_raw)
                except ValueError:
                    logger.warning(
                        "Daily-PnL basis file %s has unparseable last_trading_day %r; "
                        "ignoring and treating as cold-start",
                        self._daily_pnl_basis_path,
                        last_day_raw,
                    )
                    parsed_day = None
            self._last_trading_day = parsed_day
            # Issue #207: trust gate. A payload is fully valid only when
            # schema_version >= 1 and basis_valid == True. Anything else
            # (legacy file, missing marker, explicit False) → untrusted
            # basis. The caller (issue #207 fix in _fetch_real_portfolio_state)
            # will then fail-closed: refuse to overwrite basis on a
            # calendar mismatch, and instead surface the prior loss
            # (or raise BrokerError when prior loss is unmeasurable).
            schema_version = data.get("schema_version")
            basis_valid_flag = data.get("basis_valid")
            self._basis_trusted = bool(
                isinstance(schema_version, int)
                and schema_version >= 1
                and basis_valid_flag is True
                and parsed_day is not None
                and value > 0
            )
            if not self._basis_trusted and parsed_day is not None and value > 0:
                # Legacy file with valid fields but missing schema marker —
                # keep the values readable for diagnostics, but flag as
                # untrusted so the next rollover refuses to use them as
                # the rollover source.
                logger.warning(
                    "Daily-PnL basis file %s predates issue #207 schema "
                    "(schema_version=%r, basis_valid=%r); basis loaded "
                    "but will be treated as untrusted on next mismatch",
                    self._daily_pnl_basis_path,
                    schema_version,
                    basis_valid_flag,
                )
        except FileNotFoundError:
            # Cold start: keep defaults — daily_pnl == 0 on the first
            # call, no risk violation possible.
            return
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "Daily-PnL basis file %s is corrupt (%s); starting cold",
                self._daily_pnl_basis_path,
                e,
            )
            self._previous_close_equity = Decimal("0")
            self._last_trading_day = None

    def _save_daily_pnl_basis(self) -> None:
        """Persist the current daily-P&L basis to disk (best-effort).

        Write is best-effort: a failure here logs a warning but does
        not raise. The in-memory basis is the source of truth for the
        current process; on the next start the basis is reloaded from
        disk. The worst case is losing one trading day of basis —
        ``_check_daily_loss`` will then evaluate against 0 for one
        call (no violation possible), which is safer than crashing the
        order path.
        """
        try:
            os.makedirs(self._peak_store_dir, exist_ok=True)
            payload: dict[str, Any] = {
                "previous_close_equity": str(self._previous_close_equity),
            }
            if self._last_trading_day is not None:
                payload["last_trading_day"] = self._last_trading_day.isoformat()
            # Issue #207: stamp the basis as a v1 valid record so a future
            # process restart can trust the rollover source. Without this
            # marker, the load path would treat any persisted basis as
            # legacy/untrusted and refuse to use it as the rollover source,
            # which would re-disarm ``RISK_DAILY_LOSS`` on every restart.
            payload["schema_version"] = 1
            payload["basis_valid"] = True
            with open(self._daily_pnl_basis_path, "w") as fh:
                json.dump(payload, fh)
        except OSError as e:
            logger.warning(
                "Failed to persist daily-PnL basis to %s: %s",
                self._daily_pnl_basis_path,
                e,
            )

    def is_sandbox(self) -> bool:
        """Sandbox if token starts with 't.' (Tinkoff convention)."""
        return self._token.startswith("t.")

    def _rate_limit_acquire(self) -> None:
        """Block until we have capacity for 1 more request.

        Issue #103: delegates to the shared ``TokenBucket`` primitive
        which is documented thread-safe (``threading.Lock`` inside
        ``acquire``). The previous hand-rolled list-without-lock
        implementation could burst above the SLA under concurrent
        ``place_order`` calls.
        """
        self._rate_bucket.acquire()

    def _build_intent_and_state(
        self,
        order: MarketOrder | LimitOrder,
        client: Any = None,
    ) -> tuple[Any, Any]:
        """Build TradeIntent + PortfolioState for RiskGate.

        Issue #11 (CRITICAL): the historical implementation used
        ``Decimal("1")`` as a placeholder price for MarketOrder and
        ``Decimal("100000")`` as a hardcoded NAV. Both silently bypassed
        RISK_POSITION / RISK_SECTOR. The fix:

        1. For ``MarketOrder``: fetch a live quote via the Tinkoff
           ``market_data.get_last_prices`` API. If the quote is
           unavailable (network blip, SDK exception, instrument
           suspended), refuse the order — fail-safe. Never substitute a
           fake price.
        2. For ``LimitOrder``: use ``order.price`` as before.
        3. PortfolioState: fetch real NAV via ``get_portfolio()``. If the
           fetch fails, refuse the order — fail-safe.

        The ``client`` parameter is required for MarketOrder; it is the
        same ``Client`` instance used by the broker call so we don't
        open a second connection per order.
        """
        from src.risk.gate import TradeIntent

        order_type = OrderType.LIMIT if isinstance(order, LimitOrder) else OrderType.MARKET
        _ = order_type  # currently unused, reserved for Phase 2

        if isinstance(order, MarketOrder):
            # Fail-safe: refuse market orders when no live quote is
            # available. The caller (``place_order``) MUST pass a
            # connected ``client``; we open one here only if absent.
            if client is None:
                from t_tech.invest import Client as _Client

                client = _Client(self._token)
            price = self._fetch_live_quote_price(client, order.ticker)
        else:
            price = order.price

        intent = TradeIntent(
            symbol=order.ticker,
            # BUGFIX (C-4): the previous expression
            #   "buy" if order.side.value.lower() == "sell" else order.side.value.lower()
            # silently inverted SELL → BUY. Pass the side through as-is.
            side=order.side.value.lower(),
            quantity=order.quantity,
            price=price,
        )

        # Real portfolio state via the broker. Failure is fail-safe:
        # if we cannot determine current NAV, we refuse to evaluate the
        # trade rather than guess against a fake 100 000₽ baseline.
        state = self._fetch_real_portfolio_state()
        return intent, state

    def _fetch_live_quote_price(self, client: Any, ticker: str) -> Decimal:
        """Fetch the latest market price for a ticker via Tinkoff.

        Raises ``BrokerError`` if the quote is unavailable. Never
        returns a placeholder — the previous ``Decimal("1")``
        placeholder is the bug we are fixing here.
        """
        try:
            figi = self._ticker_to_figi(client, ticker)
            resp = client.market_data.get_last_prices(figi=[figi])
            for lp in getattr(resp, "last_prices", []):
                if getattr(lp, "figi", None) == figi:
                    q_price = getattr(lp, "price", None)
                    # Structural check (not isinstance) so the function
                    # works with both the real t_tech.invest.Quotation
                    # and MagicMock test doubles.
                    if q_price is None or not (hasattr(q_price, "units") and hasattr(q_price, "nano")):
                        raise BrokerError(f"no usable quote for {ticker} (figi={figi})")
                    units = int(getattr(q_price, "units", 0))
                    nano = int(getattr(q_price, "nano", 0))
                    price = Decimal(units) + Decimal(nano) / Decimal("1000000000")
                    if price <= Decimal("0"):
                        raise BrokerError(f"non-positive quote for {ticker}: {price}")
                    return price
            raise BrokerError(f"no quote in response for {ticker} (figi={figi})")
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"live quote fetch failed for {ticker}: {e}") from e

    def _fetch_real_portfolio_state(self) -> Any:
        """Fetch real NAV/cash/positions from Tinkoff.

        Raises ``BrokerError`` on failure. Returns ``PortfolioState``
        populated with live values. The previous hardcoded
        ``Decimal("100000")`` is the bug we are fixing here.

        Issue #32: peak_equity is now the running high-water mark read
        from disk on construction and updated here (monotonically
        non-decreasing per process). The RiskGate's ``_check_drawdown``
        computes drawdown as ``(peak - current) / peak`` — with peak
        always equal to current, the guard never trips in production,
        even after a 20-30% drawdown. Persisting the high-water mark
        means a single bad order in one cycle triggers the guard in the
        next.

        Issue #197: ``daily_pnl`` is computed as
        ``current_nav − previous_close_equity`` (where
        ``previous_close_equity`` is the NAV observed on the last
        trading day's rollover), persisted per-process via a sibling
        JSON file. Cold-start (no file) → ``daily_pnl == 0`` (no
        violation possible). Day rollover (today's MSK date != stored
        ``last_trading_day``) → stamp the new basis with the current
        NAV and report ``daily_pnl == 0`` (the new trading day just
        started). Without this wiring, ``_check_daily_loss`` defaults
        to ``daily_pnl == 0`` and ``RISK_DAILY_LOSS`` never trips —
        same kill-switch dead-code pattern as issue #195.
        """
        from src.risk.gate import PortfolioState

        snapshot = self.get_portfolio()
        # Issue #42: a zero-NAV account (cold sandbox, or gRPC response
        # missing total_amount_currencies) must raise a domain error
        # rather than letting pydantic escape with ValidationError —
        # PortfolioState requires gt=0 on both total_equity and peak_equity.
        if snapshot.cash <= 0:
            raise BrokerError(
                f"Portfolio NAV is {snapshot.cash} for account {self._account_id}; "
                "cannot evaluate risk. Fund the account or check that "
                "total_amount_currencies is present in the gRPC response."
            )
        # Issue #32: update the high-water mark BEFORE building the
        # snapshot so peak_equity >= total_equity (the PortfolioState
        # invariant). Persist best-effort; an OSError here does not
        # break the order path.
        if snapshot.cash > self._peak_equity:
            self._peak_equity = snapshot.cash
            self._save_peak_equity()
        # Issue #191: derive free cash as `NAV − Σ(quantity × avg_price)`
        # so `PortfolioState.cash` reflects tradeable cash, not NAV. The
        # bug is latent today (no `_check_*` in `src/risk/gate.py` reads
        # `state.cash`), but the field's semantic contract is "tradeable
        # cash"; a future cash-adequacy / buy-in-cash cap would silently
        # over-approve against NAV if this fix is missing. Use the same
        # `quantity × avg_price` formula already used by
        # `Position.market_value` at `src/risk/gate.py:135-137` so the
        # math stays consistent with `_check_sector_exposure`.
        positions_value = sum(
            (p.quantity * p.avg_price for p in snapshot.positions),
            Decimal("0"),
        )
        free_cash = snapshot.cash - positions_value
        if free_cash < Decimal("0"):
            # Tinkoff contract guarantees positions ≤ NAV, but a partial /
            # synthetic snapshot could exceed it; clamp rather than let
            # `PortfolioState.cash` ValidationError (`ge=Decimal("0")`).
            free_cash = Decimal("0")
        # Issue #197: compute ``daily_pnl`` from the persisted basis
        # before constructing ``PortfolioState``. Two cases:
        #
        # 1. Trading-day rollover (stored ``last_trading_day`` differs
        #    from today's MSK date AND the persisted basis is trusted):
        #    stamp ``previous_close_equity = current_nav`` and report
        #    ``daily_pnl == 0``. This is the "first snapshot of the
        #    new day" semantics — the kill-switch is silent until we
        #    have a real intraday delta to evaluate.
        #
        # 2. Same trading day: ``daily_pnl = current_nav −
        #    previous_close_equity``. A negative value trips
        #    ``_check_daily_loss`` when it exceeds
        #    ``RiskLimits.max_daily_loss_pct``.
        #
        # Issue #207: a third case — calendar mismatch with an
        # UNTRUSTED basis (corrupt file, legacy payload missing the
        # ``schema_version`` marker, partial / inconsistent state).
        # In that case we MUST NOT silently overwrite the basis with
        # today's NAV, because that would re-disarm
        # ``RISK_DAILY_LOSS`` for the rest of the session even though
        # a prior-day loss may still be in flight. Instead we raise
        # ``BrokerError`` so the caller fails-closed: OrderFlow
        # propagates the rejection up the pipeline and trading is
        # blocked until the operator intervenes (or the next clean
        # restart resolves the corruption). The kill-switch is a
        # financial safety control; an over-permissive fallback here
        # would be a release blocker.
        today = _msk_today()
        if self._last_trading_day != today:
            # Cold start (no basis file ever) is a known-valid state: the
            # previous trading session simply has no persisted anchor, so
            # stamping today's NAV as the new basis is correct — there is
            # no prior loss to preserve. Distinguish it from a corrupt /
            # stale persisted basis (issue #207): if ``_last_trading_day``
            # is None the file was either absent or unrecoverable, and the
            # rollover is safe.
            if not self._basis_trusted and self._last_trading_day is not None:
                # Stale/corrupt basis on a calendar mismatch — refuse to
                # overwrite and refuse to silently zero ``daily_pnl``.
                raise BrokerError(
                    f"Untrusted daily-P&L basis for account {self._account_id} "
                    f"on calendar mismatch (stored={self._last_trading_day}, "
                    f"today={today}, previous_close={self._previous_close_equity}, "
                    "basis_trusted=False). Refusing to silently disarm "
                    "RISK_DAILY_LOSS. Inspect the basis file at "
                    f"{self._daily_pnl_basis_path} and either restore a "
                    "known-good payload or delete it to force a fresh "
                    "cold-start on the next snapshot."
                )
            self._previous_close_equity = snapshot.cash
            self._last_trading_day = today
            self._basis_trusted = True
            self._save_daily_pnl_basis()
            daily_pnl = Decimal("0")
        else:
            daily_pnl = snapshot.cash - self._previous_close_equity
        return PortfolioState(
            total_equity=snapshot.cash,  # Tinkoff returns total_amount_currencies as cash-side NAV
            cash=free_cash,
            positions=[_broker_position_to_gate_position(p) for p in snapshot.positions],
            peak_equity=self._peak_equity,
            daily_pnl=daily_pnl,
        )

    def get_portfolio(self) -> PortfolioSnapshot:
        """Real call to Tinkoff. Returns empty mock if SDK unavailable.

        Live API contract (t_tech.invest SDK 1.49):
        - client.users.get_accounts() → .accounts (list of Account)
        - Account.id is the Tinkoff account_id
        - client.operations.get_portfolio(account_id=...) → PortfolioResponse
        - PortfolioResponse has .positions (list[PortfolioPosition]) and
          .total_amount_currencies (Money). Money has .units + .nano.
        """
        self._rate_limit_acquire()
        try:
            from t_tech.invest import Client

            with Client(self._token) as client:
                acc_response = client.users.get_accounts()
                ops = None
                for a in acc_response.accounts:
                    if a.id == self._account_id:
                        ops = a
                        break
                if ops is None:
                    raise BrokerError(f"account {self._account_id} not found")
                portfolio = client.operations.get_portfolio(account_id=self._account_id)
                positions = []
                if getattr(portfolio, "positions", None):
                    for p in portfolio.positions:
                        # average_position_price is a Quotation (.units + .nano)
                        # OR a legacy Money-like object (.value float) depending
                        # on SDK/mock version. Handle both.
                        avg_price_q = getattr(p, "average_position_price", None) or getattr(
                            p, "average_buy_price", None
                        )
                        if avg_price_q is None:
                            avg_price = Decimal("0")
                        elif hasattr(avg_price_q, "units") and hasattr(avg_price_q, "nano"):
                            avg_price = Decimal(str(getattr(avg_price_q, "units", 0))) + Decimal(
                                str(getattr(avg_price_q, "nano", 0))
                            ) / Decimal("1000000000")
                        else:
                            # Legacy: value is a float-like (e.g. Money.value).
                            try:
                                avg_price = Decimal(str(avg_price_q.value))
                            except (AttributeError, TypeError, ValueError):
                                avg_price = Decimal(str(avg_price_q))
                        positions.append(
                            Position(
                                ticker=getattr(p, "ticker", ""),
                                quantity=Decimal(str(getattr(p, "quantity", 0))),
                                avg_price=avg_price,
                            )
                        )
                total_amount = getattr(portfolio, "total_amount_currencies", None) or getattr(
                    portfolio, "total_amount", None
                )
                cash = Decimal("0")
                if total_amount is None:
                    # Issue #42: distinguish "gRPC gave us no NAV field"
                    # from "account genuinely holds 0 RUB". The default
                    # Decimal("0") conflates a parse/contract failure
                    # with a real zero balance; _fetch_real_portfolio_state
                    # raises BrokerError in either case, but logs the
                    # contract mismatch so the operator can investigate.
                    logger.warning(
                        "get_portfolio() for account %s returned neither "
                        "total_amount_currencies nor total_amount; "
                        "treating as zero NAV",
                        self._account_id,
                    )
                elif hasattr(total_amount, "units") and hasattr(total_amount, "nano"):
                    cash = Decimal(str(getattr(total_amount, "units", 0))) + Decimal(
                        str(getattr(total_amount, "nano", 0))
                    ) / Decimal("1000000000")
                else:
                    # Legacy Money.value
                    try:
                        cash = Decimal(str(total_amount.value))
                    except (AttributeError, TypeError, ValueError):
                        cash = Decimal(str(total_amount))
                return PortfolioSnapshot(
                    account_id=self._account_id,
                    cash=cash,
                    positions=positions,
                    timestamp=datetime.now(timezone.utc),
                )
        except Exception as e:
            raise BrokerError(f"Tinkoff portfolio fetch failed: {e}") from e

    def get_positions(self) -> list[Position]:
        return self.get_portfolio().positions

    def place_order(self, order: MarketOrder | LimitOrder) -> OrderStatus:
        """RiskGate first. Reject before touching broker.

        If risk_gate is None, order is rejected (fail-safe default).
        LIVE_TRADING gate: if env LIVE_TRADING=false, refuse ALL orders.
        This is a hard guarantee for Phase 1: real token may be present
        but no orders are placed regardless of RiskGate.
        """
        if not _assert_not_live_trading("LIVE_TRADING=false — refusing order", order.ticker):
            return OrderStatus.REJECTED
        if self._risk_gate is None:
            logger.warning("RiskGate not configured — rejecting all orders (fail-safe)")
            return OrderStatus.REJECTED

        # Issue #11: We open one Client connection and pass it through
        # both the `_build_intent_and_state` (for live quote + portfolio
        # fetch) and the post_order call. This avoids opening the same
        # connection twice and keeps the rate-limit semantics consistent.
        self._rate_limit_acquire()
        try:
            from t_tech.invest import (
                Client,
                OrderDirection,
                OrderType,
                Quotation,
            )

            with Client(self._token) as client:
                intent, state = self._build_intent_and_state(order, client=client)
                decision = self._risk_gate.evaluate(intent, state)

                if not decision.allowed:
                    logger.warning(
                        "RiskGate blocked order for %s: %s",
                        order.ticker,
                        decision.violations,
                    )
                    return OrderStatus.REJECTED

                # RiskGate approved. Submit to broker.
                direction = (
                    OrderDirection.ORDER_DIRECTION_BUY
                    if order.side == OrderSide.BUY
                    else OrderDirection.ORDER_DIRECTION_SELL
                )
                figi = self._ticker_to_figi(client, order.ticker)
                if isinstance(order, MarketOrder):
                    resp = client.orders.post_order(
                        figi=figi,
                        quantity=int(order.quantity),
                        account_id=self._account_id,
                        direction=direction,
                        order_type=OrderType.ORDER_TYPE_MARKET,
                    )
                else:
                    # Limit order — wrap price into Quotation.
                    # Issue #100: the wire format caps nano at 9 fractional
                    # digits (int<0, 1e9)). Input prices with more than 9
                    # fractional digits used to silently truncate via
                    # ``int((price - int(price)) * 1e9)``, e.g.
                    # ``Decimal("100.0000000001")`` would round-trip as
                    # ``Decimal("100.0")`` — operator places a limit at
                    # a price they did not intend. Floor to 9 fractional
                    # digits explicitly via ``quantize(Decimal("1e-9"))``
                    # with ``ROUND_DOWN`` so the truncation behaviour is
                    # at least deterministic and a warning is logged when
                    # the input had > 9 digits (operator should re-quote).
                    price_9 = order.price.quantize(Decimal("1e-9"), rounding=ROUND_DOWN)
                    fractional = price_9 - Decimal(int(price_9))
                    nano = int(fractional * Decimal(1_000_000_000))
                    # Belt-and-braces: if quantize happened to produce a
                    # non-9-digit residue (e.g. due to a negative price
                    # sneaking in), clamp into the wire-format range.
                    if not 0 <= nano < 1_000_000_000:
                        nano = max(0, min(nano, 999_999_999))
                    if order.price != price_9:
                        logger.warning(
                            "LimitOrder price %s has > 9 fractional digits; "
                            "rounded down to %s (units=%d, nano=%d). "
                            "Re-quote to wire precision to avoid silent loss.",
                            order.price,
                            price_9,
                            int(price_9),
                            nano,
                        )
                    price_q = Quotation(
                        units=int(price_9),
                        nano=nano,
                    )
                    resp = client.orders.post_order(
                        figi=figi,
                        quantity=int(order.quantity),
                        price=price_q,
                        account_id=self._account_id,
                        direction=direction,
                        order_type=OrderType.ORDER_TYPE_LIMIT,
                    )
                # PostOrderResponse.execution_report_status is OrderExecutionReportStatus
                # enum (real SDK) or a plain string (legacy/test mocks). Handle both.
                ers = resp.execution_report_status
                raw_name: str = getattr(ers, "name", None) or str(ers)
                return self._map_status(raw_name)
        except BrokerError as e:
            # Fail-safe: refuse the order if we cannot determine a real
            # price / portfolio state. Logger logs the broker-side reason.
            logger.error(
                "Refusing order for %s due to broker-side precondition: %s",
                order.ticker,
                e,
            )
            return OrderStatus.REJECTED
        except Exception as e:
            raise BrokerError(f"Tinkoff order submit failed: {e}") from e

    def cancel_order(self, order_id: str) -> OrderStatus:
        self._rate_limit_acquire()
        try:
            from t_tech.invest import Client

            with Client(self._token) as client:
                client.orders.cancel_order(account_id=self._account_id, order_id=order_id)
                return OrderStatus.CANCELLED
        except Exception as e:
            raise BrokerError(f"Tinkoff cancel failed: {e}") from e

    @staticmethod
    def _ticker_to_figi(client: Any, ticker: str) -> str:
        """Map ticker to FIGI via Tinkoff instruments API.

        Issue #187: the previous whitelist ``("TQBR", "TQOB")`` excluded
        TQTE ETFs, TQCB corporate/muni bonds, and every SPB foreign-share
        class (SPBXM, TQBS, TQDE, TQNO, TQLV, TQPI). All of those are
        tradable at the broker and supported by the data loaders, but
        the broker was rejecting them at FIGI-mapping time with a
        misleading ``"not found in TQBR/TQOB instrument universe"``
        error. The whitelist now defers to ``_TRADABLE_CLASS_CODES`` in
        ``src/data/tinkoff_loader.py`` — single source of truth shared
        with the data layer.

        Issue #13 (C.1): the historical implementation had
        ``except Exception: pass`` followed by a silent fallback
        ``return ticker`` that sent the literal string ``"SBER"`` to
        ``post_order``. That masked ``LoaderAuthError`` (compromised
        token), ``ConnectionError`` (Tinkoff API down), HTTP 4xx/5xx,
        and rate-limit responses. The order would then fail with
        ``INVALID_ARGUMENT`` deep in the broker, after RiskGate had
        already approved and the rate-limit token was spent.

        The fix: every error path raises ``BrokerError`` with a
        descriptive message. Operators MUST see the actual failure
        reason (auth, network, instrument not found) in the logs.
        """
        # Import here to keep the broker module loadable even if the
        # data package is not on the path (e.g. CLI-only deploys).
        from src.data.tinkoff_loader import _TRADABLE_CLASS_CODES

        try:
            response = client.instruments.find_instrument(query=ticker)
        except Exception as exc:
            raise BrokerError(f"instruments.find_instrument failed for {ticker}: {exc}") from exc
        for inst in getattr(response, "instruments", []):
            if getattr(inst, "ticker", None) == ticker and getattr(inst, "class_code", None) in _TRADABLE_CLASS_CODES:
                return str(inst.figi)
        # No matching tradable instrument — refuse, do NOT silently
        # return the ticker (which would then be sent as a FIGI and
        # produce a confusing INVALID_ARGUMENT deep in the broker).
        # The error message lists the class_codes Tinkoff actually
        # returned so operators can distinguish "wrong ticker" from
        # "right ticker in a class we don't trade" (the latter should
        # never happen given _TRADABLE_CLASS_CODES mirrors the loader
        # universe — if it does, the constant has drifted, file an
        # issue).
        seen_classes = sorted(
            str(c)
            for c in {getattr(inst, "class_code", None) for inst in getattr(response, "instruments", [])}
            if c is not None
        )
        raise BrokerError(
            f"ticker {ticker!r} not found in tradable instrument universe "
            f"(Tinkoff returned {len(getattr(response, 'instruments', []))} "
            f"matches with class_codes {seen_classes!r}; "
            f"expected one of {sorted(_TRADABLE_CLASS_CODES)!r})"
        )

    @staticmethod
    def _map_status(raw: str) -> OrderStatus:
        # raw is OrderExecutionReportStatus enum name (e.g. "EXECUTION_REPORT_STATUS_FILL")
        mapping: dict[str, OrderStatus] = {
            "EXECUTION_REPORT_STATUS_FILL": OrderStatus.FILLED,
            "EXECUTION_REPORT_STATUS_PARTIALLYFILL": OrderStatus.FILLED,
            "EXECUTION_REPORT_STATUS_REJECTED": OrderStatus.REJECTED,
            "EXECUTION_REPORT_STATUS_CANCELLED": OrderStatus.CANCELLED,
        }
        return mapping.get(raw, OrderStatus.SUBMITTED)


def from_env(
    env: Optional[dict[str, str]] = None,
    risk_gate: Any = None,
) -> "TinkoffAccount":
    """Construct TinkoffAccount from environment variables.

    Issue #26: `risk_gate` is forwarded to the constructor so callers
    can share a single RiskGate instance between the gate stage and the
    broker stage. If None, the broker falls back to a fail-safe default
    (all orders rejected, see TinkoffAccount.place_order).
    """
    if env is None:
        env = dict(os.environ)  # cast _Environ[str] to dict[str, str]
    sandbox_token = env.get("TINKOFF_SANDBOX_TOKEN")
    real_token = env.get("TINKOFF_REAL_TOKEN")
    account_id = env.get("TINKOFF_ACCOUNT_ID", "SB1")

    # Prefer REAL token (full universe, 200 req/min)
    if real_token and real_token.strip():
        return TinkoffAccount(token=real_token, account_id=account_id, risk_gate=risk_gate)
    if sandbox_token and sandbox_token.strip() and sandbox_token != "placeholder_get_from_tbank":
        return TinkoffAccount(token=sandbox_token, account_id=account_id, risk_gate=risk_gate)
    raise BrokerError("No TINKOFF_SANDBOX_TOKEN or TINKOFF_REAL_TOKEN set")
