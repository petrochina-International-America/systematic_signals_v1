# energy/accounting/mtm.py

from __future__ import annotations

import numpy as np
import pandas as pd

from energy.strategies.rolling import RollingStrategy
from energy.sizing.daily_size import compute_vol_scalar

try:
    from energy.accounting.contract_specs import CONTRACT_SPECS
except Exception:
    CONTRACT_SPECS = {}


def _require_cols(df: pd.DataFrame, cols: list[str]):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")


def get_contract_spec(commodity_name: str) -> dict:
    if commodity_name not in CONTRACT_SPECS:
        raise KeyError(
            f"Commodity '{commodity_name}' not found in CONTRACT_SPECS."
        )

    spec = CONTRACT_SPECS[commodity_name]
    if "contract_multiplier" not in spec:
        raise KeyError(
            f"Commodity '{commodity_name}' missing 'contract_multiplier' in CONTRACT_SPECS."
        )
    return spec


def build_roll_path(
    prices: pd.DataFrame,
    expiry_calendar: pd.DatetimeIndex,
    *,
    style: str = "window",
    roll_window: int = 5,
    front_col: str = "F1",
    next_col: str = "F2",
    third_col: str = "F3",
    mid_col: str = "F3",
    far_col: str = "F4",
) -> pd.DataFrame:
    rs = RollingStrategy(
        prices=prices,
        expiry_calendar=expiry_calendar,
        front_col=front_col,
        next_col=next_col,
    )

    if style == "window":
        out = rs.pnl(roll_window=roll_window)
    elif style == "eom_mid":
        out = rs.pnl_eom_midmonth()
    elif style == "eom_ngl":
        out = rs.pnl_eom_ngl(mid_col=mid_col, far_col=far_col)
    elif style == "eom_eom":
        out = rs.pnl_eom_eom(next_col=next_col, third_col=third_col)
    elif style == "eom_dynamic":
        out = rs.pnl_eom_dynamic(third_col=third_col)
    else:
        raise ValueError(f"Unknown style '{style}'.")

    _require_cols(out, ["daily_pnl", "held_contract", "roll_day_flag"])
    return out


def build_held_price_series(
    path_df: pd.DataFrame,
    prices: pd.DataFrame,
    held_col: str = "held_contract",
    output_name: str = "held_price",
) -> pd.Series:
    _require_cols(path_df, [held_col])

    out = pd.Series(index=path_df.index, dtype=float, name=output_name)

    for dt in path_df.index:
        contract = path_df.at[dt, held_col]

        if pd.isna(contract):
            out.at[dt] = np.nan
            continue

        contract = str(contract)
        if contract not in prices.columns:
            raise KeyError(
                f"Held contract '{contract}' on {dt} not found in prices columns."
            )
        if dt not in prices.index:
            raise KeyError(f"Date {dt} not found in prices index.")

        out.at[dt] = float(prices.at[dt, contract])

    return out


def mtm_from_path(
    path_df: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    initial_capital: float,
    contract_multiplier: float,
    pnl_col: str = "daily_pnl",
    held_col: str = "held_contract",
    rebalance_col: str = "rebalance_flag",
    rebalance_mode: str = "signal",
    signal_col: str | None = None,
    t_cost_abs: float = 0.0,
    include_exit_cost: bool = False,
    round_contracts: bool = False,
    output_prefix: str = "",
    self_vol_window: int = 0,              # 0 = OFF
    self_vol_target_ann: float = 0.15,     # used only if self_vol_window > 0
    self_vol_min_obs: int | None = None,   # default below
    self_vol_floor: float = 1e-8,          # avoid divide-by-zero
) -> pd.DataFrame:
    """
    Generic MTM engine supporting both:
      - long-only rolling paths (signal_col=None)
      - signal strategies (signal_col provided, usually in {-1,0,+1})

    Conventions:
      - pnl_col is per-1-contract unit pnl in price units
      - if signal_col is provided, contracts become signed
      - transaction costs are applied on contract turnover

    Self vol scaling:
      - if self_vol_window == 0: disabled
      - if self_vol_window > 0: scale target contracts using trailing realized
        vol of THIS strategy's own daily return stream

    WARNING — self-vol is deliberate here (unlike build_measures and the RV /
    stat-arb capital loops, which size on the instrument's own returns) and is
    only safe for strategies that hold a position continuously.  A strategy
    that goes flat books exact-zero returns into the window; a long flat
    streak collapses the vol estimate and the next re-entry is sized
    target/near-zero (the 300x sizing-spike failure mode fixed elsewhere on
    2026-07-08).  Do NOT enable self_vol_window for signal strategies that
    can be flat for extended periods.
    """
    if initial_capital <= 0:
        raise ValueError("initial_capital must be > 0.")
    if contract_multiplier <= 0:
        raise ValueError("contract_multiplier must be > 0.")
    if self_vol_window < 0:
        raise ValueError("self_vol_window must be >= 0.")
    if self_vol_target_ann <= 0:
        raise ValueError("self_vol_target_ann must be > 0.")

    if self_vol_min_obs is None:
        self_vol_min_obs = self_vol_window

    required = [pnl_col, held_col, rebalance_col]
    if signal_col is not None:
        required.append(signal_col)
    _require_cols(path_df, required)

    out = path_df.copy().sort_index()
    n = len(out)

    col_held_price = f"{output_prefix}held_price"
    col_signal = f"{output_prefix}signal_used"
    col_contracts = f"{output_prefix}contracts_held"
    col_trade_count = f"{output_prefix}trade_count_mtm"
    col_dollar_pnl = f"{output_prefix}dollar_pnl"
    col_txn_cost = f"{output_prefix}txn_cost_mtm"
    col_capital = f"{output_prefix}capital"
    col_gross = f"{output_prefix}gross_exposure"
    col_net = f"{output_prefix}net_exposure"
    col_rebalance = f"{output_prefix}rebalance_flag_mtm"
    col_daily_ret = f"{output_prefix}daily_ret"
    col_equity = f"{output_prefix}equity_index"
    col_self_vol = f"{output_prefix}self_realized_vol"
    col_self_scalar = f"{output_prefix}self_vol_scalar"

    if n == 0:
        for c in [
            col_held_price, col_signal, col_contracts, col_trade_count,
            col_dollar_pnl, col_txn_cost, col_capital, col_gross,
            col_net, col_rebalance, col_daily_ret, col_equity,
            col_self_vol, col_self_scalar,
        ]:
            out[c] = pd.Series(dtype=float)
        return out

    held_price = build_held_price_series(
        out,
        prices,
        held_col=held_col,
        output_name=col_held_price,
    )

    unit_pnl = out[pnl_col].astype(float).to_numpy()
    rebalance_signal = out[rebalance_col].fillna(0).to_numpy(int)

    if signal_col is not None:
        signal_arr = out[signal_col].fillna(0.0).astype(float).to_numpy()
    else:
        signal_arr = np.ones(n, dtype=float)

    contracts = np.full(n, np.nan, dtype=float)
    trade_count = np.zeros(n, dtype=float)
    dollar_pnl = np.zeros(n, dtype=float)
    txn_cost = np.zeros(n, dtype=float)
    capital = np.full(n, np.nan, dtype=float)
    gross_exposure = np.full(n, np.nan, dtype=float)
    net_exposure = np.full(n, np.nan, dtype=float)
    rebalance_flag = np.zeros(n, dtype=int)
    daily_ret_arr = np.full(n, np.nan, dtype=float)
    self_realized_vol = np.full(n, np.nan, dtype=float)
    self_vol_scalar = np.ones(n, dtype=float)

    def _compute_self_vol_scalar(t: int) -> float:
        """Uses ONLY information available through t-1 (daily_ret_arr[:t])."""
        scalar, realized = compute_vol_scalar(
            daily_ret_arr[:t],
            self_vol_window,
            self_vol_target_ann,
            self_vol_min_obs,
            self_vol_floor,
        )
        if not np.isnan(realized):
            self_realized_vol[t] = realized
        return scalar

    def _target_contracts(capital_base: float, px: float, sig: float, scalar: float) -> float:
        if np.isnan(px) or px == 0:
            raise ValueError("Held price invalid during sizing.")
        raw = sig * capital_base * scalar / (px * contract_multiplier)
        return np.round(raw) if round_contracts else raw

    # ------------------------------------------------------------------
    # Initial sizing
    # ------------------------------------------------------------------
    px0 = float(held_price.iloc[0])
    sig0 = float(signal_arr[0])

    scalar0 = 1.0  # no history yet
    self_vol_scalar[0] = scalar0

    target0 = _target_contracts(initial_capital, px0, sig0, scalar0)
    entry_turnover = abs(target0)
    entry_cost = entry_turnover * abs(t_cost_abs)

    capital0 = initial_capital - entry_cost
    if capital0 <= 0:
        raise ValueError("Initial capital exhausted by entry costs.")

    if not round_contracts:
        if sig0 != 0:
            target0 = sig0 * capital0 * scalar0 / (px0 * contract_multiplier)
        else:
            target0 = 0.0

    contracts[0] = target0
    trade_count[0] = entry_turnover
    txn_cost[0] = entry_cost
    capital[0] = capital0
    gross_exposure[0] = abs(target0) * px0 * contract_multiplier
    net_exposure[0] = target0 * px0 * contract_multiplier
    rebalance_flag[0] = 1
    daily_ret_arr[0] = np.nan

    # ------------------------------------------------------------------
    # Daily loop
    # ------------------------------------------------------------------
    for t in range(1, n):
        prev_contracts = contracts[t - 1]

        # PnL from previous signed position
        dollar_pnl[t] = unit_pnl[t] * contract_multiplier * prev_contracts
        capital_pre_rebal = capital[t - 1] + dollar_pnl[t]

        # realized return for day t from position held coming into t
        daily_ret_arr[t] = (
            dollar_pnl[t] / capital[t - 1]
            if capital[t - 1] not in (0, np.nan) and pd.notna(capital[t - 1])
            else np.nan
        )

        if rebalance_mode == "signal":
            do_rebalance = bool(rebalance_signal[t] == 1)
        elif rebalance_mode == "daily":
            do_rebalance = True
        elif rebalance_mode == "never":
            do_rebalance = False
        else:
            raise ValueError(
                f"Unknown rebalance_mode '{rebalance_mode}'. "
                "Use one of: 'signal', 'daily', 'never'."
            )

        if do_rebalance:
            px_t = float(held_price.iloc[t])
            sig_t = float(signal_arr[t])

            scalar_t = _compute_self_vol_scalar(t)
            self_vol_scalar[t] = scalar_t

            new_contracts = _target_contracts(capital_pre_rebal, px_t, sig_t, scalar_t)
            contract_turnover = abs(new_contracts - prev_contracts)
            cost_t = contract_turnover * abs(t_cost_abs)

            capital_post_cost = capital_pre_rebal - cost_t
            if capital_post_cost <= 0:
                raise ValueError(
                    f"Capital became non-positive after costs on {out.index[t]}."
                )

            if not round_contracts:
                if sig_t != 0:
                    new_contracts = (
                        sig_t * capital_post_cost * scalar_t
                        / (px_t * contract_multiplier)
                    )
                else:
                    new_contracts = 0.0

            contracts[t] = new_contracts
            trade_count[t] = contract_turnover
            txn_cost[t] = cost_t
            capital[t] = capital_post_cost
            rebalance_flag[t] = 1
        else:
            self_vol_scalar[t] = self_vol_scalar[t - 1] if t > 0 else 1.0
            contracts[t] = prev_contracts
            capital[t] = capital_pre_rebal

        px_t = held_price.iloc[t]
        if pd.notna(px_t):
            gross_exposure[t] = abs(contracts[t]) * float(px_t) * contract_multiplier
            net_exposure[t] = contracts[t] * float(px_t) * contract_multiplier

    if include_exit_cost:
        exit_turnover = abs(contracts[-1])
        exit_cost = exit_turnover * abs(t_cost_abs)
        txn_cost[-1] += exit_cost
        capital[-1] -= exit_cost
        trade_count[-1] += exit_turnover

    capital_series = pd.Series(capital, index=out.index, name=col_capital)
    daily_ret_series = capital_series.pct_change()

    out[col_held_price] = held_price
    out[col_signal] = signal_arr
    out[col_contracts] = contracts
    out[col_trade_count] = trade_count
    out[col_dollar_pnl] = dollar_pnl
    out[col_txn_cost] = txn_cost
    out[col_capital] = capital_series
    out[col_gross] = gross_exposure
    out[col_net] = net_exposure
    out[col_rebalance] = rebalance_flag
    out[col_daily_ret] = daily_ret_series
    out[col_equity] = capital_series / float(initial_capital)
    out[col_self_vol] = pd.Series(self_realized_vol, index=out.index)
    out[col_self_scalar] = pd.Series(self_vol_scalar, index=out.index)

    return out
