"""Macro Agent (Phase 2.3).

Pure regime classifier + persistence helpers. The fetcher itself lives in
``src/data/macro_fetcher.py`` (lives next to the other data-layer code so the
existing retry/backoff/cache pattern from ``cross_source_smoke`` is reused).

Public surface:
    * ``regime.classify(...)`` — pure Decimal math, no IO.
    * ``persistence.upsert_regime(...)`` — store-agnostic write (SQLite or
      Postgres through the ``DataStore`` factory).
    * ``models.MacroSnapshot`` — pydantic frozen model used as the typed
      contract between fetcher → classifier → persistence.

Coordinator (Phase 2.10) reads ``macro_regime_log`` via ``last_regime()``;
that wiring is NOT in this scope.
"""

from __future__ import annotations
