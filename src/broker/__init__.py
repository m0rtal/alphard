"""Tinkoff Broker Connector — Package init.

Phase 1.3 deliverable. Provides:
- BrokerAccount ABC (interface)
- TinkoffAccount (sandbox + real via OpenSandboxAccount)
- OrderSlicer (5% ADV chunks, rate-limited)
- MarketOrder, LimitOrder (pydantic)
- Sandbox auto-detect via TINKOFF_SANDBOX_TOKEN prefix

All money-movement operations call RiskGate BEFORE TinkoffAccount.place_order().
Non-overridable. Default long-only, no margin, no shorts.
"""
