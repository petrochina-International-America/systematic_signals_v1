import time
import warnings
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Strategy parameters (mirror risk_premia_latest.ipynb)
# ---------------------------------------------------------------------------
_HIST_START      = "2010-01-01"
_TRADE_START     = "2015-01-01"
_BLEND_MA_PAIRS  = [(1, 5), (5, 20), (10, 60)]
_CARRY_FRONT_COL = "F4"
_CARRY_END_COL   = "F15"
_CACHE_TTL       = 300  # seconds — re-run strategies at most every 5 minutes

# ---------------------------------------------------------------------------
# Product groups and strategies shown on the monitor
# ---------------------------------------------------------------------------
# Commodities without FlowsDB prices yet are included for display completeness
# but will fall back to "—" in the live snapshot until their tickers are mapped.
PRODUCT_GROUPS = {
    "Crude Benchmarks": ["WTI", "Brent", "Dubai"],
    "Crude Grades":     ["HTT", "WTI Midland", "YV", "DAB"],
    "FFAs":             ["TDL", "WDF"],
    "Products":         ["RBOB", "ULSD", "Gasoil"],
    "NGLs":             ["Propane", "Ethane", "Butane"],
    "Natural Gas":      ["Natgas"],
}

STRATEGIES = ["Momentum", "Carry"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _direction_from_pos(pos: float) -> str:
    """Map a raw ±1/0 position to a display label."""
    if pos > 0:
        return "Long"
    if pos < 0:
        return "Short"
    return "Flat"


def _direction_from_z(z: float) -> str:
    return "Long" if z < -1.0 else ("Short" if z > 1.0 else "Flat")


# ---------------------------------------------------------------------------
# Live signal snapshot — runs energy strategies against FlowsDB
# ---------------------------------------------------------------------------
_snapshot_cache: dict = {"data": None, "ts": 0.0}


def get_live_signal_snapshot() -> dict[tuple[str, str], tuple[str, str | None]]:
    """
    Run Momentum and Carry strategies using the in-memory price store.

    Returns {(item, strategy): (direction, sub_label)} where:
      - item      is a commodity name or product-group name (basket)
      - direction is "Long" / "Short" / "Flat" / "—"
      - sub_label is None for flat-price signals (direction chip only)

    Results are cached for _CACHE_TTL seconds so repeated page loads
    don't re-run full strategy computations.
    """
    if _snapshot_cache["data"] is not None and time.time() - _snapshot_cache["ts"] < _CACHE_TTL:
        return _snapshot_cache["data"]

    from energy.accounting.contract_specs import CONTRACT_SPECS
    from energy.accounting.mtm import build_roll_path
    from energy.strategies.momentum import momentum
    from energy.strategies.carry import carry
    from data import loader

    snapshot: dict[tuple[str, str], tuple[str, str | None]] = {}
    basket_positions: dict[tuple[str, str], list[float]] = {
        (group, strat): [] for group in PRODUCT_GROUPS for strat in STRATEGIES
    }

    for group, commodities in PRODUCT_GROUPS.items():
        for commodity in commodities:
            spec = CONTRACT_SPECS.get(commodity)
            cfg  = spec.get("prompt_EOM_roll") if spec else None
            if spec is None or cfg is None:
                for strat in STRATEGIES:
                    snapshot[(commodity, strat)] = ("—", None)
                continue

            try:
                prices_full = loader.get_prices(commodity)
                if prices_full.empty:
                    raise ValueError("no price data")

                prices = prices_full[prices_full.index >= _TRADE_START].copy()

                expiry_cal = loader.get_expiry(spec["ticker"])
                expiry_cal = expiry_cal[expiry_cal >= pd.Timestamp(_TRADE_START)]

                front_col = cfg.get("front_col", "F1")
                roll_kwargs: dict = dict(
                    prices=prices,
                    expiry_calendar=expiry_cal,
                    style=cfg["style"],
                    front_col=front_col,
                    next_col=cfg.get("next_col", "F2"),
                    third_col=cfg.get("third_col", "F3"),
                    far_col=cfg.get("far_col", "F4"),
                )
                if cfg.get("roll_window") is not None:
                    roll_kwargs["roll_window"] = cfg["roll_window"]
                if cfg.get("mid_col") is not None:
                    roll_kwargs["mid_col"] = cfg["mid_col"]

                roll_path = build_roll_path(**roll_kwargs)

                # --- Momentum ---
                # Signal reads the full-history CLEAN roll-continuous series
                # (leg_signal_series) — never the raw stitched frame, whose
                # re-rank jumps fire phantom MA crossings (2026-07-14 audit;
                # constant-price gate in test_signal_series_integrity.py).
                try:
                    from data.lab import _clean_signal_prices
                    sig_prices = _clean_signal_prices(commodity)
                    sig_col = "SIGNAL"
                except Exception:
                    sig_prices = None   # momentum() falls back to its own
                    sig_col = front_col  # in-window clean default
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    mom_path = momentum(
                        prices=prices,
                        rolled_df=roll_path,
                        front_col=sig_col,
                        ma_pairs=_BLEND_MA_PAIRS,
                        prices_signal=sig_prices,
                    )
                mom_pos = float(mom_path["position"].iloc[-1])
                snapshot[(commodity, "Momentum")] = (_direction_from_pos(mom_pos), None)
                basket_positions[(group, "Momentum")].append(mom_pos)

                # --- Carry ---
                carry_path = carry(
                    prices=prices,
                    rolled_df=roll_path,
                    front_col=_CARRY_FRONT_COL,
                    end_col=_CARRY_END_COL,
                )
                carry_pos = float(carry_path["position"].iloc[-1])
                snapshot[(commodity, "Carry")] = (_direction_from_pos(carry_pos), None)
                basket_positions[(group, "Carry")].append(carry_pos)

            except Exception:
                for strat in STRATEGIES:
                    snapshot[(commodity, strat)] = ("—", None)

    # Baskets — majority vote (mean of ±1/0 positions across group members)
    for (group, strat), positions in basket_positions.items():
        if positions:
            avg = sum(positions) / len(positions)
            direction = "Long" if avg > 0.33 else ("Short" if avg < -0.33 else "Flat")
        else:
            direction = "—"
        snapshot[(group, strat)] = (direction, None)

    _snapshot_cache["data"] = snapshot
    _snapshot_cache["ts"]   = time.time()
    return snapshot


# ---------------------------------------------------------------------------
# Placeholder snapshot — used while FlowsDB pipeline is unavailable
# ---------------------------------------------------------------------------
def _fake_signal_snapshot() -> dict[tuple[str, str], tuple[str, str | None]]:
    rng = np.random.default_rng(123)
    snapshot: dict[tuple[str, str], tuple[str, str | None]] = {}
    basket_z: dict[tuple[str, str], list[float]] = {
        (g, s): [] for g in PRODUCT_GROUPS for s in STRATEGIES
    }
    for group, commodities in PRODUCT_GROUPS.items():
        for commodity in commodities:
            for strat in STRATEGIES:
                z = round(float(rng.normal(0, 1.2)), 2)
                snapshot[(commodity, strat)] = (_direction_from_z(z), None)
                basket_z[(group, strat)].append(z)
    for key, zs in basket_z.items():
        avg = round(sum(zs) / len(zs), 2)
        snapshot[key] = (_direction_from_z(avg), None)
    return snapshot


def get_signal_snapshot() -> dict[tuple[str, str], tuple[str, str | None]]:
    """Live snapshot with automatic fallback to placeholder data."""
    try:
        return get_live_signal_snapshot()
    except Exception:
        return _fake_signal_snapshot()


# ---------------------------------------------------------------------------
# Legacy stub kept for any callers that haven't been migrated yet
# ---------------------------------------------------------------------------
SIGMA_LEVELS = {
    "sigma_1_pos": 1.0,   "sigma_1_neg": -1.0,
    "sigma_1_5_pos": 1.5, "sigma_1_5_neg": -1.5,
    "sigma_2_pos": 2.0,   "sigma_2_neg": -2.0,
}
