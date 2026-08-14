"""Risk Agent scaffold — pure Python, NO ML, NO LLM.

This is the foundation for all trading decisions. Risk gate has final say.
Phase 0: minimal skeleton with hard limits only.
Phase 1.3: extended with sector/ADV/spread checks.
"""

from .gate import RiskGate, RiskDecision, TradeIntent, PortfolioState, RiskLimits

__all__ = ["RiskGate", "RiskDecision", "TradeIntent", "PortfolioState", "RiskLimits"]
