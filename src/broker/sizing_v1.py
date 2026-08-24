"""Backward-compat alias for sizing v1.

Re-exports the canonical v1 function and the OrderSpec model so external
imports ``from src.broker.sizing_v1 import compute_position_size`` keep
working after v2 ships. ``v1`` here means "formula locked at FORMULA_VERSION
in sizing.py" — there is no separate v1 implementation yet, only the
stable entry point. When v2 ships, ``sizing.py::compute_position_size``
will gain new kwargs; ``compute_position_size_v1`` will be pinned to
the current signature so audit replay remains bit-identical for rows
opened under v1.

Locking rule (task body §3 "Rollback"):

    Sizing formula НЕ применяется ретроактивно. Live positions opened
    under v1 stay v1 even after v2 ships. При изменении formula: bump
    version, новые сделки по новой формуле, старые сделки без пересчёта.

This module owns that contract on the Python side; the database side is
the ``sizing_version`` column on the audit log.
"""

from __future__ import annotations

from .sizing import (  # noqa: F401
    FORMULA_VERSION,
    OrderSpec,
    SizingConfig,
    compute_position_size_v1,
)

__all__ = [
    "FORMULA_VERSION",
    "OrderSpec",
    "SizingConfig",
    "compute_position_size_v1",
]
