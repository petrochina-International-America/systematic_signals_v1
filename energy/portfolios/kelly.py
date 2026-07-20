"""
Kelly criterion portfolio sizing.

Placeholder — to be implemented.

Planned approach:
    - Full Kelly: w = Σ^{-1} * μ  (mean-variance optimal, unconstrained)
    - Fractional Kelly: w = f * Σ^{-1} * μ  (f < 1 for drawdown control,
      typically f = 0.25–0.5 in practice)
    - Rolling re-estimation of μ and Σ on expanding or rolling window
    - Combine with vol target cap so Kelly sizing doesn't blow through
      a max leverage constraint
"""
from __future__ import annotations

import pandas as pd


def build_kelly_portfolio(
    results: dict[str, dict],
    members: list[str],
    *,
    kelly_fraction: float = 0.5,
    vol_window: int = 120,
    vol_target_ann: float = 0.15,
    initial_capital: float = 1_000_000,
    warmup_returns_dict: dict[str, pd.Series] | None = None,
) -> dict:
    raise NotImplementedError("Kelly criterion portfolio not yet implemented.")
