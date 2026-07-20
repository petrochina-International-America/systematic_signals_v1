# energy/strategies/rolling.py
import numpy as np
import pandas as pd
from typing import List
from energy.analytics.metrics import legacy_capstone_metrics

# =============================================================================
# Helpers
# =============================================================================
def _require_cols(df: pd.DataFrame, cols: List[str]):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")


def _next_expiry_for_each_date(
    idx: pd.DatetimeIndex, expiry_calendar: pd.DatetimeIndex
) -> pd.DatetimeIndex:
    exp = pd.DatetimeIndex(expiry_calendar).sort_values().unique()
    if len(exp) == 0:
        raise ValueError("Expiry calendar is empty.")

    next_exp_idx = np.searchsorted(exp.values, idx.values, side="left")
    next_exp_idx = np.clip(next_exp_idx, 0, len(exp) - 1)

    # if we're already after expiry on that date -> move to next
    after = idx.values > exp.values[next_exp_idx]
    next_exp_idx = np.clip(next_exp_idx + after.astype(int), 0, len(exp) - 1)

    return pd.DatetimeIndex(exp.values[next_exp_idx])


def _first_trading_day_of_month_flags(idx: pd.DatetimeIndex) -> np.ndarray:
    n = len(idx)
    out = np.zeros(n, dtype=int)
    if n > 1:
        m = idx.month.to_numpy()
        out[1:] = (m[1:] != m[:-1]).astype(int)
    return out


def _post_expiry_flags(idx: pd.DatetimeIndex, next_exp_dates: pd.DatetimeIndex) -> np.ndarray:
    """
    1 on the first trading day strictly AFTER the relevant expiry.
    This is the *source of truth* flag — everything else should look at t-1.
    """
    n = len(idx)
    out = np.zeros(n, dtype=int)
    for t in range(1, n):
        exp_date = next_exp_dates[t - 1]
        # flag when we *cross* the expiry date:
        # idx[t-1] <= exp_date < idx[t]  → first bar strictly after expiry
        if idx[t - 1] <= exp_date < idx[t]:
            out[t] = 1
    return out


def _expiry_today_flags(idx: pd.DatetimeIndex, next_exp_dates: pd.DatetimeIndex) -> np.ndarray:
    n = len(idx)
    out = np.zeros(n, dtype=int)
    for t in range(1, n):
        if next_exp_dates[t - 1] == idx[t]:
            out[t] = 1
    return out


# =============================================================================
# 1) Synchronous T-N / window roll 
# =============================================================================
def rolling_pnl(
    prices: pd.DataFrame,
    expiry_calendar: pd.DatetimeIndex,
    front_col: str = "F1",
    next_col: str = "F2",
    roll_window: int = 5,
) -> pd.DataFrame:

    df = prices.copy()
    idx = df.index

    exp = pd.DatetimeIndex(expiry_calendar).sort_values().unique()
    if len(exp) == 0:
        raise ValueError("Expiry calendar is empty.")

    next_exp_idx = np.searchsorted(exp.values, idx.values, side="left")
    next_exp_idx = np.clip(next_exp_idx, 0, len(exp) - 1)
    next_exp_dates = exp.values[next_exp_idx]
    exp_pos = np.searchsorted(idx.values, next_exp_dates, side="left")

    arange_idx = np.arange(len(idx))
    dte = (exp_pos - arange_idx - 1).astype(float)
    last_date = idx.values[-1]
    valid = next_exp_dates <= last_date
    dte[~valid] = np.inf

    _require_cols(df, [front_col, next_col])
    f1 = df[front_col].to_numpy(float)
    f2 = df[next_col].to_numpy(float)

    n = len(df)
    daily_pnl = np.zeros(n, dtype=float)
    held = np.empty(n, dtype=object)
    roll_day_flag = np.zeros(n, dtype=int)
    post_expiry_flag = np.zeros(n, dtype=int)

    daily_pnl[0] = 0.0
    held[0] = front_col

    rank_shift_flag = np.zeros(n, dtype=int)

    for t in range(1, n):
        d = dte[t - 1]

        if np.isfinite(d) and d == -1:
            # re-rank day: old next_col == new front_col, same contract —
            # a relabel, not a transaction
            daily_pnl[t] = f1[t] - f2[t - 1]
            held[t] = front_col
            post_expiry_flag[t] = 1
            rank_shift_flag[t] = 1

        elif np.isfinite(d) and d == roll_window:
            daily_pnl[t] = f1[t] - f1[t - 1]
            held[t] = next_col
            roll_day_flag[t] = 1

        elif np.isfinite(d) and 0 <= d < roll_window:
            daily_pnl[t] = f2[t] - f2[t - 1]
            held[t] = next_col

        elif np.isfinite(d) and d > roll_window:
            daily_pnl[t] = f1[t] - f1[t - 1]
            held[t] = front_col

        else:
            daily_pnl[t] = f1[t] - f1[t - 1]
            held[t] = front_col

    df["daily_pnl"] = daily_pnl
    df["held_contract"] = held
    df["roll_day_flag"] = roll_day_flag
    df["rank_shift_flag"] = rank_shift_flag
    df["post_expiry_flag"] = post_expiry_flag
    df["t_cost"] = 0.0
    df["net_pnl"] = df["daily_pnl"]
    return df


# =============================================================================
# 2) EOM / midmonth family (all now use post_expiry_flag[t-1])
# =============================================================================
def roll_EOM_midmonth_expiry(
    prices: pd.DataFrame,
    expiry_calendar: pd.DatetimeIndex,
    *,
    front_col: str = "F1",  # the rank the held month falls to after expiry
    next_col: str = "F2",   # the rank we normally sit in (M2)
) -> pd.DataFrame:
    """
    WTI-style midmonth expiry, corrected state machine.

    Holding pattern (unchanged economics from the original implementation's
    effective path): live in the next_col contract; when the front month
    expires mid-month the generics re-rank and the held contract slides down
    to front_col (a RELABEL, not a trade); hold it there until month-end and
    roll back into the new next_col contract at the last close of the month.

    Accounting invariants (the original version violated all three):
      * every day's daily_pnl is one real contract's own price move — the
        cross-rank formula f_front[t] - f_next[t-1] appears only on the
        re-rank day, where both columns point at the SAME contract;
      * rank_shift_flag marks the re-rank day (relabel only — no transaction,
        no cost);
      * roll_day_flag marks the true transaction: pnl on that bar is the NEW
        contract's move from the prior close, i.e. the roll executed at the
        month-end close (sell fallen-front, buy new next). Costs belong here.
    """
    out = prices.copy()
    idx = out.index
    n = len(idx)

    _require_cols(out, [front_col, next_col])

    next_exp_dates = _next_expiry_for_each_date(idx, expiry_calendar)
    post_expiry_flag = _post_expiry_flags(idx, next_exp_dates)
    new_month = _first_trading_day_of_month_flags(idx)

    f1 = out[front_col].to_numpy(float)  # fallen rank
    f2 = out[next_col].to_numpy(float)   # home rank

    daily_pnl = np.zeros(n, dtype=float)
    held = np.empty(n, dtype=object)
    roll_day_flag = np.zeros(n, dtype=int)
    rank_shift_flag = np.zeros(n, dtype=int)

    daily_pnl[0] = 0.0
    held[0] = next_col
    in_front = False  # True while the held contract sits in front_col

    for t in range(1, n):
        if not in_front:
            if post_expiry_flag[t] == 1:
                # Re-rank day: the held contract is the same one as yesterday,
                # it just moved from next_col to front_col. Same-contract move.
                daily_pnl[t] = f1[t] - f2[t - 1]
                held[t] = front_col
                rank_shift_flag[t] = 1
                in_front = True
            else:
                daily_pnl[t] = f2[t] - f2[t - 1]
                held[t] = next_col
        else:
            if new_month[t] == 1:
                # First bar of the new month: the roll executed at the prior
                # (month-end) close — today's pnl is the NEW next_col
                # contract's own move. Transaction: cost applies here.
                daily_pnl[t] = f2[t] - f2[t - 1]
                held[t] = next_col
                roll_day_flag[t] = 1
                in_front = False
            else:
                daily_pnl[t] = f1[t] - f1[t - 1]
                held[t] = front_col

    out["daily_pnl"] = daily_pnl
    out["held_contract"] = held
    out["roll_day_flag"] = roll_day_flag
    out["rank_shift_flag"] = rank_shift_flag
    out["post_expiry_flag"] = post_expiry_flag
    out["t_cost"] = 0.0
    out["net_pnl"] = out["daily_pnl"]
    return out



def roll_EOM_NGL(
    prices: pd.DataFrame,
    expiry_calendar: pd.DatetimeIndex,
    *,
    mid_col: str = "F3",  # M3
    far_col: str = "F4",  # M4
) -> pd.DataFrame:
    """
    NGL-style EOM, t-1 view:
      - normal: M3[t] - M3[t-1]
      - if yesterday was post-expiry: M3[t] - M4[t-1]
    """
    out = prices.copy()
    idx = out.index
    n = len(idx)

    _require_cols(out, [mid_col, far_col])

    next_exp_dates = _next_expiry_for_each_date(idx, expiry_calendar)
    post_expiry_flag = _post_expiry_flags(idx, next_exp_dates)

    m3 = out[mid_col].to_numpy(float)
    m4 = out[far_col].to_numpy(float)

    daily_pnl = np.zeros(n, dtype=float)
    held = np.empty(n, dtype=object)
    roll_day_flag = np.zeros(n, dtype=int)

    daily_pnl[0] = 0.0
    held[0] = mid_col

    for t in range(1, n):
        if post_expiry_flag[t] == 1:
            # Re-rank day: ranks slid between t-1 and t. The roll executed at
            # the t-1 close into old M4, which is today's M3 — same contract,
            # so its move from t-1 is m3[t] - m4[t-1]. (Booking this one bar
            # later, off post_expiry_flag[t-1], booked two cross-contract
            # phantom diffs per cycle — caught by the constant-price test.)
            daily_pnl[t] = m3[t] - m4[t - 1]
            held[t] = mid_col
            roll_day_flag[t] = 1
        else:
            daily_pnl[t] = m3[t] - m3[t - 1]
            held[t] = mid_col

    out["daily_pnl"] = daily_pnl
    out["held_contract"] = held
    out["roll_day_flag"] = roll_day_flag
    out["rank_shift_flag"] = 0  # NGL post-expiry bar is a true roll (flagged above)
    out["post_expiry_flag"] = post_expiry_flag
    out["t_cost"] = 0.0
    out["net_pnl"] = out["daily_pnl"]
    return out


def roll_EOM_EOM_expiry(
    prices: pd.DataFrame,
    expiry_calendar: pd.DatetimeIndex,
    *,
    next_col: str = "F2",   # M2
    third_col: str = "F3",  # M3
) -> pd.DataFrame:
    """
    Always-EOM-expiry style, but still using t-1:
      - normal: M2[t] - M2[t-1]
      - if yesterday was post-expiry: M2[t] - M3[t-1]
    """
    out = prices.copy()
    idx = out.index
    n = len(idx)

    _require_cols(out, [next_col, third_col])

    next_exp_dates = _next_expiry_for_each_date(idx, expiry_calendar)
    post_expiry_flag = _post_expiry_flags(idx, next_exp_dates)

    m2 = out[next_col].to_numpy(float)
    m3 = out[third_col].to_numpy(float)

    daily_pnl = np.zeros(n, dtype=float)
    held = np.empty(n, dtype=object)
    roll_day_flag = np.zeros(n, dtype=int)

    daily_pnl[0] = 0.0
    held[0] = next_col

    for t in range(1, n):
        if post_expiry_flag[t] == 1:
            # Re-rank day (see roll_EOM_NGL): roll executed at t-1 close;
            # old M3 == today's M2, same contract.
            daily_pnl[t] = m2[t] - m3[t - 1]
            held[t] = next_col
            roll_day_flag[t] = 1
        else:
            daily_pnl[t] = m2[t] - m2[t - 1]
            held[t] = next_col

    out["daily_pnl"] = daily_pnl
    out["held_contract"] = held
    out["roll_day_flag"] = roll_day_flag
    out["rank_shift_flag"] = 0  # EOM post-expiry bar is a true roll (flagged above)
    out["post_expiry_flag"] = post_expiry_flag
    out["t_cost"] = 0.0
    out["net_pnl"] = out["daily_pnl"]
    return out


def roll_EOM_dynamic_brent(
    prices: pd.DataFrame,
    expiry_calendar: pd.DatetimeIndex,
    *,
    front_col: str = "F1",   # M1
    next_col: str = "F2",    # M2 (the one we normally live in)
    third_col: str = "F3",   # M3 (for true EOM rolls)
) -> pd.DataFrame:
    """
    Brent-style dynamic roll with t-1 lookback.

    Idea:
    - If the relevant expiry rolls into a NEW MONTH (true EOM month) → use EOM flavour:
        normal:     M2[t] - M2[t-1]
        post-expiry: M2[t] - M3[t-1]
    - Otherwise (midmonth month) → use the UPDATED midmonth/WTI-style pattern:
        ... M2 ... M2 ... (expiry) ... M1 ... [next bar] sell M1, buy M2 ...
    """
    out = prices.copy()
    idx = out.index
    n = len(idx)

    _require_cols(out, [front_col, next_col, third_col])

    # per-date next expiry + flags
    next_exp_dates = _next_expiry_for_each_date(idx, expiry_calendar)
    post_expiry_flag = _post_expiry_flags(idx, next_exp_dates)

    # Detect if this expiry is EOM-like: the first TRADING day after the
    # expiry falls in a new month. Decided on the trading calendar, never
    # (expiry + 1 CALENDAR day): ICE moved Brent LTD to the last business
    # day of the month in 2016, and when that day is weekend/holiday-shifted
    # off the calendar month-end the old test classified the cycle as
    # midmonth — the held contract then fell to front and its own expiry
    # re-ranked the columns at the month boundary, booking a cross-contract
    # diff (28 phantom bars post-2016, VM understated −9.96 $/bbl over
    # 2010–2026; Pass 9a 2026-07-14, fixed 2026-07-15).
    def _eom_like(d: pd.Timestamp) -> bool:
        pos = idx.searchsorted(d, side="right")
        nxt = idx[pos] if pos < n else (d + pd.offsets.BDay(1))
        return bool(nxt.month != d.month or nxt.year != d.year)

    is_eom_next = np.array([_eom_like(d) for d in next_exp_dates])

    f1 = out[front_col].to_numpy(float)   # M1
    f2 = out[next_col].to_numpy(float)    # M2
    f3 = out[third_col].to_numpy(float)   # M3

    # start: if upcoming expiry is EOM → we live in M2; else midmonth → also live in M2
    daily_pnl = np.zeros(n, dtype=float)
    held = np.empty(n, dtype=object)
    roll_day_flag = np.zeros(n, dtype=int)
    rank_shift_flag = np.zeros(n, dtype=int)

    new_month = _first_trading_day_of_month_flags(idx)

    daily_pnl[0] = 0.0
    held[0] = next_col
    in_front = False  # midmonth flavour: held contract fell to front_col

    for t in range(1, n):
        if in_front:
            # -----------------------------------------------------------
            # MIDMONTH flavour, post-re-rank state (same as corrected
            # eom_mid): hold the fallen contract until month-end, roll at
            # the month-end close into the new next_col contract.
            # -----------------------------------------------------------
            if post_expiry_flag[t] == 1:
                # Guard: the fallen (held) contract's own LTD hit while we
                # were still in front (holiday-shifted year-ends — Dec-31
                # bars). Columns re-ranked overnight (old F2 → new F1), so
                # f1[t] − f1[t-1] would cross contracts. The roll executed
                # at t-1's close; book the new contract's own move.
                if new_month[t] == 1:
                    # expiry and month boundary coincide: per the midmonth
                    # design we are back in M2 (today's F2 = old F3)
                    daily_pnl[t] = f2[t] - f3[t - 1]
                    held[t] = next_col
                    in_front = False
                else:
                    # roll into the next contract out (today's F1 = old F2)
                    # and stay in front until the month boundary
                    daily_pnl[t] = f1[t] - f2[t - 1]
                    held[t] = front_col
                roll_day_flag[t] = 1
            elif new_month[t] == 1:
                daily_pnl[t] = f2[t] - f2[t - 1]   # new contract's own move
                held[t] = next_col
                roll_day_flag[t] = 1               # transaction: cost here
                in_front = False
            else:
                daily_pnl[t] = f1[t] - f1[t - 1]
                held[t] = front_col
            continue

        if is_eom_next[t - 1]:
            # ---------------------------------------------------------
            # EOM flavour: same as roll_EOM_EOM_expiry (M2 ↔ M3).
            # The re-rank bar books the NEW M2's move from the prior
            # close (old M3 == new M2, same contract): the roll executed
            # at the prior close. Transaction — flagged.
            # ---------------------------------------------------------
            if post_expiry_flag[t] == 1:
                daily_pnl[t] = f2[t] - f3[t - 1]
                held[t] = next_col
                roll_day_flag[t] = 1
            else:
                daily_pnl[t] = f2[t] - f2[t - 1]
                held[t] = next_col
        else:
            # ---------------------------------------------------------
            # MIDMONTH flavour, pre-re-rank: live in M2 until the front
            # expires; on the re-rank day the held contract slides to
            # front_col (relabel, no trade).
            # ---------------------------------------------------------
            if post_expiry_flag[t] == 1:
                daily_pnl[t] = f1[t] - f2[t - 1]   # same contract across ranks
                held[t] = front_col
                rank_shift_flag[t] = 1
                in_front = True
            else:
                daily_pnl[t] = f2[t] - f2[t - 1]
                held[t] = next_col

    out["daily_pnl"] = daily_pnl
    out["held_contract"] = held
    out["roll_day_flag"] = roll_day_flag
    out["rank_shift_flag"] = rank_shift_flag
    out["post_expiry_flag"] = post_expiry_flag
    out["t_cost"] = 0.0
    out["net_pnl"] = out["daily_pnl"]
    return out

# =============================================================================
# 3) Costs & equity
# =============================================================================
def roll_EL(
    rolled_df: pd.DataFrame,
    prices: pd.DataFrame,
    front_col: str = "F1",
    t_cost: float | None = 0.00,
    pct_t_cost: float | None = None,
) -> pd.DataFrame:
    if "daily_pnl" not in rolled_df.columns or "roll_day_flag" not in rolled_df.columns:
        raise ValueError("rolled_df must include 'daily_pnl' and 'roll_day_flag'.")

    df = rolled_df.copy()
    idx = df.index
    n = len(df)

    if "held_contract" in df.columns and pd.notna(df["held_contract"].iloc[0]):
        seed_contract = str(df["held_contract"].iloc[0])
    else:
        seed_contract = front_col

    if seed_contract not in prices.columns:
        raise ValueError(f"Seed contract '{seed_contract}' not found in prices.")

    # Price series used for percentage costs
    price_series = prices[seed_contract].reindex(idx).astype(float)

    df["t_cost"] = 0.0

    # Identify roll days (where we do 2 legs: sell old, buy new)
    roll_days = np.flatnonzero(df["roll_day_flag"].to_numpy() == 1)

    # Decide which mode we're in
    use_pct = pct_t_cost is not None and pct_t_cost != 0.0
    use_abs = (not use_pct) and (t_cost is not None) and (t_cost != 0.0)


    if use_pct:
        # Entry cost (1 leg) on first day
        df.iat[0, df.columns.get_loc("t_cost")] -= abs(pct_t_cost) * price_series.iloc[0]

        # Exit cost (1 leg) on last day
        df.iat[-1, df.columns.get_loc("t_cost")] -= abs(pct_t_cost) * price_series.iloc[-1]

        # Roll costs: 2 legs per roll day
        for r in roll_days:
            df.iat[r, df.columns.get_loc("t_cost")] -= 2.0 * abs(pct_t_cost) * price_series.iloc[r]

    elif use_abs:
        # Old behaviour: fixed dollar cost per leg
        df.iat[0, df.columns.get_loc("t_cost")] -= abs(t_cost)
        df.iat[-1, df.columns.get_loc("t_cost")] -= abs(t_cost)

        for r in roll_days:
            df.iat[r, df.columns.get_loc("t_cost")] -= 2.0 * abs(t_cost)

    trade_count = np.zeros(n, dtype=float)
    trade_count[0]  += 1.0        # entry
    trade_count[-1] += 1.0        # exit
    for r in roll_days:
        trade_count[r] += 2.0     # sell old, buy new

    df["trade_count"] = trade_count

    eq = np.zeros(n, dtype=float)

    seed_price = price_series.iloc[0]
    # start equity: seed price plus any initial cost
    eq[0] = seed_price + df.iat[0, df.columns.get_loc("t_cost")]

    for t in range(1, n):
        eq[t] = (
            eq[t - 1]
            + df.iat[t, df.columns.get_loc("daily_pnl")]
            + df.iat[t, df.columns.get_loc("t_cost")]
        )

    df["equity_line"] = np.round(eq, 8)
    df["net_pnl"] = df["daily_pnl"] + df["t_cost"]
    return df


# =============================================================================
# 4) Strategy wrapper
# =============================================================================
class RollingStrategy:
    def __init__(self, prices, expiry_calendar, front_col="F1", next_col="F2"):
        self.prices = prices
        self.expiry_calendar = expiry_calendar
        self.front_col = front_col
        self.next_col = next_col
        self._rolled = None
        self._equity = None

    def pnl(self, roll_window=5):
        self._rolled = rolling_pnl(
            self.prices,
            self.expiry_calendar,
            front_col=self.front_col,
            next_col=self.next_col,
            roll_window=roll_window,
        )
        return self._rolled

    def pnl_eom_midmonth(self):
        self._rolled = roll_EOM_midmonth_expiry(
            self.prices,
            self.expiry_calendar,
            front_col=self.front_col,
            next_col=self.next_col,
        )
        return self._rolled

    def pnl_eom_ngl(self, mid_col: str = "F3", far_col: str = "F4"):
        self._rolled = roll_EOM_NGL(
            self.prices,
            self.expiry_calendar,
            mid_col=mid_col,
            far_col=far_col,
        )
        return self._rolled

    def pnl_eom_eom(self, next_col: str = None, third_col: str = "F3"):
        use_next = next_col if next_col is not None else self.next_col
        self._rolled = roll_EOM_EOM_expiry(
            self.prices,
            self.expiry_calendar,
            next_col=use_next,
            third_col=third_col,
        )
        return self._rolled

    def pnl_eom_dynamic(self, third_col: str = "F3"):
        self._rolled = roll_EOM_dynamic_brent(
            self.prices,
            self.expiry_calendar,
            front_col=self.front_col,
            next_col=self.next_col,
            third_col=third_col,
        )
        return self._rolled

    def equity(
        self,
        roll_window: int = 5,
        t_cost: float | None = 0.00,
        pct_t_cost: float | None = None,
        *,
        style: str = "window",
        third_col: str = "F3",
        mid_col: str = "F3",
        far_col: str = "F4",
    ):
        """
        Build equity_line for the chosen roll style.

        If pct_t_cost is provided, transaction costs are applied as a
        percentage of price. Otherwise, we fall back to absolute t_cost.
        """
        # 1) Choose roll flavour
        if style == "window":
            self.pnl(roll_window=roll_window)
        elif style == "eom_mid":
            self.pnl_eom_midmonth()
        elif style == "eom_ngl":
            self.pnl_eom_ngl(mid_col=mid_col, far_col=far_col)
        elif style == "eom_eom":
            self.pnl_eom_eom(third_col=third_col)
        elif style == "eom_dynamic":
            self.pnl_eom_dynamic(third_col=third_col)
        else:
            raise ValueError(f"Unknown style '{style}'.")

        # 2) Apply transaction costs & build equity_line
        self._equity = roll_EL(
            self._rolled,
            self.prices,
            front_col=self.front_col,
            t_cost=t_cost,
            pct_t_cost=pct_t_cost,
        )
        return self._equity

    def metrics(self, contracts=1, units=1000):
        if self._equity is None:
            raise ValueError("Must call .equity() before .metrics().")
        # legacy capstone metrics: the equity frame carries net_pnl/t_cost in
        # quote space. (The capital-account metrics() needs a `capital`
        # column this frame doesn't have — calling it here was a latent
        # TypeError found by the 2026-07-08 audit.)
        return legacy_capstone_metrics(self._equity, contracts=contracts, units=units)


# =============================================================================
# 5) Spread rolling
# =============================================================================
def _tenor_num(tenor: str) -> int:
    """'F3' -> 3"""
    return int(tenor[1:])


def _build_spread_leg(
    prices: pd.DataFrame,
    expiry: pd.DatetimeIndex,
    tenor: str,
    style: str,
    roll_window: int = 5,
) -> pd.DataFrame:
    """
    Build a single-leg roll path for one side of a spread.

    Column conventions by style (n = _tenor_num(tenor)):
      window     : front=Fn, next=F(n+1)
      eom_mid    : front=Fn, next=F(n+1)  — leg lives in F(n+1)
      eom_eom    : next=Fn,  third=F(n+1)
      eom_ngl    : mid=Fn,   far=F(n+1)
      eom_dynamic: front=F(n-1), next=Fn, third=F(n+1)  — requires n >= 2
    """
    n = _tenor_num(tenor)

    if style == "window":
        return rolling_pnl(
            prices, expiry,
            front_col=f"F{n}",
            next_col=f"F{n + 1}",
            roll_window=roll_window,
        )
    elif style == "eom_mid":
        return roll_EOM_midmonth_expiry(
            prices, expiry,
            front_col=f"F{n}",
            next_col=f"F{n + 1}",
        )
    elif style == "eom_eom":
        return roll_EOM_EOM_expiry(
            prices, expiry,
            next_col=f"F{n}",
            third_col=f"F{n + 1}",
        )
    elif style == "eom_ngl":
        return roll_EOM_NGL(
            prices, expiry,
            mid_col=f"F{n}",
            far_col=f"F{n + 1}",
        )
    elif style == "eom_dynamic":
        if n < 2:
            raise ValueError(
                f"eom_dynamic needs F{{n-1}} as front_col; tenor '{tenor}' requires n >= 2."
            )
        return roll_EOM_dynamic_brent(
            prices, expiry,
            front_col=f"F{n - 1}",
            next_col=f"F{n}",
            third_col=f"F{n + 1}",
        )
    else:
        raise ValueError(
            f"Unknown style '{style}'. "
            "Choose from: window, eom_mid, eom_eom, eom_ngl, eom_dynamic."
        )


def _extract_held_prices(
    path_df: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.Series:
    """Look up the held contract price for each date in path_df."""
    out = pd.Series(index=path_df.index, dtype=float)
    for dt, contract in path_df["held_contract"].items():
        if pd.isna(contract):
            out.at[dt] = np.nan
        else:
            out.at[dt] = float(prices.at[dt, str(contract)])
    return out


def spread_pnl_from_legs(
    leg1_path: pd.DataFrame,
    leg2_path: pd.DataFrame,
    t_cost_leg1: float = 0.0,
    t_cost_leg2: float = 0.0,
    pct_cost_leg1: float = 0.0,
    pct_cost_leg2: float = 0.0,
) -> pd.DataFrame:
    """
    Leg-level spread P&L on the UNION of both legs' trading calendars.

    This is the validated way to turn two per-leg roll paths into a spread
    P&L stream. NEVER compute spread P&L by differencing a stitched
    held-price level series (held_price1 - held_price2).diff(): the stitched
    series jumps whenever a leg's held contract is relabeled or rolled, and
    those jumps are not tradable P&L. Each leg's daily_pnl is roll-aware; the
    only correct spread P&L is their difference.

    Alignment: each leg's daily_pnl is cumsum'ed, reindexed onto the union
    calendar with ffill (a closed market earns nothing while the other
    trades), and differenced back. Handles differing holiday calendars
    (e.g. NYMEX vs ICE) without dropping the days one leg traded.

    Costs: 2 sides turn over on each leg's own roll days (roll_day_flag).
    rank_shift_flag days are relabels — no transaction, no cost.
    Two cost models, additive if both given:
      t_cost_leg{1,2}   : fixed $/unit per side (legacy absolute model)
      pct_cost_leg{1,2} : fraction of the leg's held price per side
                          (e.g. 0.0001 = 1bp of price); requires the leg
                          frame to carry a 'held_price' column.

    Returns columns: leg1_daily_pnl, leg2_daily_pnl, daily_pnl (leg1 - leg2),
    roll/shift flags per leg and combined, t_cost, net_pnl.
    """
    union = leg1_path.index.union(leg2_path.index).sort_values()

    def _leg_pnl_on(idx, leg):
        cum = leg["daily_pnl"].fillna(0.0).cumsum()
        cum = cum.reindex(idx).ffill().fillna(0.0)
        d = cum.diff()
        if len(d):
            d.iloc[0] = 0.0
        return d

    pnl1 = _leg_pnl_on(union, leg1_path)
    pnl2 = _leg_pnl_on(union, leg2_path)

    def _flag_on(idx, leg, col):
        if col not in leg.columns:
            return pd.Series(0, index=idx, dtype=int)
        return leg[col].reindex(idx).fillna(0).astype(int)

    r1 = _flag_on(union, leg1_path, "roll_day_flag")
    r2 = _flag_on(union, leg2_path, "roll_day_flag")
    s1 = _flag_on(union, leg1_path, "rank_shift_flag")
    s2 = _flag_on(union, leg2_path, "rank_shift_flag")

    tc = pd.Series(0.0, index=union)
    if t_cost_leg1 != 0.0:
        tc[r1 == 1] -= abs(t_cost_leg1) * 2
    if t_cost_leg2 != 0.0:
        tc[r2 == 1] -= abs(t_cost_leg2) * 2

    def _pct_cost(leg, roll_flag, pct):
        if pct == 0.0:
            return None
        if "held_price" not in leg.columns:
            raise ValueError(
                "pct_cost requires a 'held_price' column on the leg frame "
                "(use _prepare_leg or add it via build_held_price_series)."
            )
        px = leg["held_price"].reindex(union).ffill()
        return -abs(pct) * 2.0 * px.where(roll_flag == 1, 0.0).fillna(0.0)

    for leg, flag, pct in ((leg1_path, r1, pct_cost_leg1),
                           (leg2_path, r2, pct_cost_leg2)):
        c = _pct_cost(leg, flag, pct)
        if c is not None:
            tc = tc + c

    out = pd.DataFrame(index=union)
    out["leg1_daily_pnl"] = pnl1
    out["leg2_daily_pnl"] = pnl2
    out["daily_pnl"] = pnl1 - pnl2
    out["leg1_roll_flag"] = r1
    out["leg2_roll_flag"] = r2
    out["roll_day_flag"] = ((r1 == 1) | (r2 == 1)).astype(int)
    out["rank_shift_flag"] = ((s1 == 1) | (s2 == 1)).astype(int)
    out["t_cost"] = tc
    out["net_pnl"] = out["daily_pnl"] + out["t_cost"]
    return out


def leg_signal_series(roll_path: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    THE sanctioned signal-input series for a single rolled leg: cumulative
    sum of the roll path's own roll-aware daily_pnl, anchored at the first
    held contract's price so it starts on the quoted price's scale.

    Use this (not a raw generic F-column) as the price series any
    trailing-window signal reads — MAs, z-scores, value anchors. Raw generic
    columns jump at every re-rank even when no price moved (they fail the
    constant-price gate in tests/test_signal_series_integrity.py), which
    inflated momentum's Sharpe 0.282 -> 0.125 when corrected (2026-07-14
    audit). The anchored cumsum contains only same-contract price changes.
    """
    _require_cols(roll_path, ["daily_pnl", "held_contract"])
    anchor = np.nan
    for dt, col in roll_path["held_contract"].items():
        if pd.notna(col) and str(col) in prices.columns and dt in prices.index:
            px = prices.at[dt, str(col)]
            if pd.notna(px):
                anchor = float(px)
                break
    cum = roll_path["daily_pnl"].fillna(0.0).cumsum()
    return (cum - cum.iloc[0] + anchor).rename("signal_price")


def _combine_spread_legs(
    leg1_path: pd.DataFrame,
    leg1_prices: pd.DataFrame,
    leg2_path: pd.DataFrame,
    leg2_prices: pd.DataFrame,
    t_cost_leg1: float = 0.0,
    t_cost_leg2: float = 0.0,
) -> pd.DataFrame:
    """
    Core combinator: long leg1, short leg2.

    P&L comes from spread_pnl_from_legs (leg-level, union-aligned); this
    wrapper adds held contracts/prices for display and signal construction.
    spread_price is a STITCHED LEVEL series (jumps at rolls/relabels) — fine
    for signals and charts, never difference it for P&L.
    """
    core = spread_pnl_from_legs(leg1_path, leg2_path, t_cost_leg1, t_cost_leg2)
    common = core.index

    p1 = leg1_path.reindex(common)
    p2 = leg2_path.reindex(common)

    leg1_price = _extract_held_prices(
        p1.dropna(subset=["held_contract"]), leg1_prices
    ).reindex(common).ffill()
    leg2_price = _extract_held_prices(
        p2.dropna(subset=["held_contract"]), leg2_prices
    ).reindex(common).ffill()

    out = core.copy()
    out["leg1_held"] = p1["held_contract"].ffill()
    out["leg2_held"] = p2["held_contract"].ffill()
    out["leg1_price"] = leg1_price
    out["leg2_price"] = leg2_price
    out["spread_price"] = leg1_price - leg2_price

    return out.dropna(subset=["leg1_price", "leg2_price"])


def roll_time_spread(
    prices: pd.DataFrame,
    expiry: pd.DatetimeIndex,
    near_tenor: str = "F1",
    far_tenor: str = "F2",
    *,
    style: str = "eom_mid",
    roll_window: int = 5,
    t_cost_near: float = 0.0,
    t_cost_far: float = 0.0,
) -> pd.DataFrame:
    """
    Continuously-rolled calendar (time) spread for a single commodity:
        long near_tenor leg, short far_tenor leg.

    Both legs use the same prices and expiry calendar but roll
    independently at their own expiry dates.

    Parameters
    ----------
    prices : DataFrame
        DatetimeIndex, columns F1, F2, ... (from load_prices).
    expiry : DatetimeIndex
        Contract expiry calendar for this commodity.
    near_tenor : str
        Primary tenor for the long leg, e.g. 'F1'.
    far_tenor : str
        Primary tenor for the short leg, e.g. 'F2'.
    style : str
        Roll style: window | eom_mid | eom_eom | eom_ngl | eom_dynamic.
    roll_window : int
        Days before expiry to begin rolling (window style only).
    t_cost_near, t_cost_far : float
        Absolute $/BBL transaction cost per roll for each leg.

    Returns
    -------
    DataFrame
        Columns: leg1_daily_pnl, leg2_daily_pnl, daily_pnl, leg1_held,
        leg2_held, leg1_price, leg2_price, spread_price, leg1_roll_flag,
        leg2_roll_flag, roll_day_flag, t_cost, net_pnl.
    """
    near_path = _build_spread_leg(prices, expiry, near_tenor, style, roll_window)
    far_path  = _build_spread_leg(prices, expiry, far_tenor,  style, roll_window)
    return _combine_spread_legs(near_path, prices, far_path, prices, t_cost_near, t_cost_far)


def roll_product_spread(
    prices1: pd.DataFrame,
    expiry1: pd.DatetimeIndex,
    prices2: pd.DataFrame,
    expiry2: pd.DatetimeIndex,
    tenor: str = "F1",
    *,
    style1: str = "eom_mid",
    style2: str = "eom_mid",
    roll_window: int = 5,
    t_cost_leg1: float = 0.0,
    t_cost_leg2: float = 0.0,
) -> pd.DataFrame:
    """
    Continuously-rolled product (cross-commodity) spread at the same tenor:
        long leg1[tenor], short leg2[tenor].

    Each leg rolls using its own prices and expiry calendar, allowing
    for different roll styles per commodity (e.g. eom_mid for WTI,
    eom_ngl for Propane).

    Parameters
    ----------
    prices1, prices2 : DataFrames
        Price DataFrames for commodity 1 and 2.
    expiry1, expiry2 : DatetimeIndex
        Expiry calendars for commodity 1 and 2.
    tenor : str
        Contract month tenor for both legs, e.g. 'F1'.
    style1, style2 : str
        Roll style for each leg independently.
    roll_window : int
        Days before expiry for window-style rolls.
    t_cost_leg1, t_cost_leg2 : float
        Absolute $/BBL transaction cost per roll for each leg.

    Returns
    -------
    Same column layout as roll_time_spread.
    """
    leg1_path = _build_spread_leg(prices1, expiry1, tenor, style1, roll_window)
    leg2_path = _build_spread_leg(prices2, expiry2, tenor, style2, roll_window)
    return _combine_spread_legs(leg1_path, prices1, leg2_path, prices2, t_cost_leg1, t_cost_leg2)


def roll_cross_arb(
    prices1: pd.DataFrame,
    expiry1: pd.DatetimeIndex,
    tenor1: str,
    prices2: pd.DataFrame,
    expiry2: pd.DatetimeIndex,
    tenor2: str,
    *,
    style1: str = "eom_mid",
    style2: str = "eom_mid",
    roll_window: int = 5,
    t_cost_leg1: float = 0.0,
    t_cost_leg2: float = 0.0,
) -> pd.DataFrame:
    """
    Continuously-rolled cross-arb spread: different commodity AND different tenor.
        long prices1[tenor1], short prices2[tenor2].

    Generalises both roll_time_spread and roll_product_spread:
    - Same commodity, consecutive tenors → use roll_time_spread instead.
    - Same tenor, different commodities → use roll_product_spread instead.
    - Different commodity + different tenor → this function.

    Parameters
    ----------
    prices1, prices2 : DataFrames
        Price DataFrames for commodity 1 and 2.
    expiry1, expiry2 : DatetimeIndex
        Expiry calendars for commodity 1 and 2.
    tenor1, tenor2 : str
        Contract month tenor for leg 1 and leg 2 respectively.
    style1, style2 : str
        Roll style for each leg independently.
    roll_window : int
        Days before expiry for window-style rolls.
    t_cost_leg1, t_cost_leg2 : float
        Absolute $/BBL transaction cost per roll for each leg.

    Returns
    -------
    Same column layout as roll_time_spread.
    """
    leg1_path = _build_spread_leg(prices1, expiry1, tenor1, style1, roll_window)
    leg2_path = _build_spread_leg(prices2, expiry2, tenor2, style2, roll_window)
    return _combine_spread_legs(leg1_path, prices1, leg2_path, prices2, t_cost_leg1, t_cost_leg2)
