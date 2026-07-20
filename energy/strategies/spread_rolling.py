# energy/strategies/spread_rolling.py
"""
Coordinated roll engine for cross-commodity spreads.

Replaces the disbanded construction in which each spread leg rolled
independently on its own rank-based (F-number) schedule — see
spread_specs.py for the full taxonomy (spread_style, roll_trigger_style,
precision_mode, month_offset) and legacy_uncoordinated_reference() below
for the retained historical reference.

Core idea: the state variable is a DELIVERY MONTH X, not a curve rank.
leg1 holds contract month X + month_offset, leg2 holds X, and both legs
roll to X+1 simultaneously on a shared trigger derived from the binding
leg's last trading day (min of the two expiry calendars — never hardcoded
to a specific commodity). Contract months per generic ticker come from
Bloomberg's CONTRACT_MONTH_YR field (wide F1..F24 frames of YYYYMM ints,
see data.loader.get_contract_months), and last trading days come from
expiry_calendars.xlsx — both existing sources of truth.

Leg frames produced here are drop-in compatible with the rank-path output
(_prepare_leg schema): daily_pnl, held_contract, roll_day_flag, held_price —
plus held_month (YYYYMM) for the continuous cross-leg validator.

Roll accounting convention: the roll executes at the close of the trigger
day T. daily_pnl(T) is the OLD month's move (we held it through T's close);
held_contract/held_price at T reference the NEW month (the end-of-day
position, which sizing rebalances against); roll_day_flag(T) = 1.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from energy.accounting.contract_specs import CONTRACT_SPECS
from energy.accounting.mtm import build_roll_path
from energy.accounting.spread_specs import (
    DEFAULT_SPREAD_STYLE,
    DEFAULT_TRIGGER_STYLE,
    SPREAD_STYLES,
    TRIGGER_STYLES,
    get_spread_spec,
)


# ============================================================
# Month arithmetic (YYYYMM ints <-> monthly periods)
# ============================================================
def _to_period(yyyymm: int) -> pd.Period:
    v = int(yyyymm)
    return pd.Period(year=v // 100, month=v % 100, freq="M")


def _to_int(p: pd.Period) -> int:
    return p.year * 100 + p.month


def _add_months(yyyymm: int, n: int) -> int:
    return _to_int(_to_period(yyyymm) + n)


def _month_diff(a: int, b: int) -> int:
    """a - b in whole months, both YYYYMM ints."""
    pa, pb = _to_period(a), _to_period(b)
    return (pa.year - pb.year) * 12 + (pa.month - pb.month)


# ============================================================
# Expiry calendar -> delivery month mapping (data-driven)
# ============================================================
def expiry_delivery_months(
    expiry_calendar,
    months_f1: pd.Series,
) -> pd.Series:
    """
    Map each last-trading-day in the calendar to its contract delivery month
    (YYYYMM int), data-driven rather than rule-derived: on any day up to and
    including its LTD, the F1 generic still points at the expiring contract
    (Bloomberg generics roll the day AFTER expiry — verified on all 199
    S:ENCO rolls 2010–2026), so F1's CONTRACT_MONTH_YR on the last trading
    day <= LTD is the expiring contract's delivery month.

    Expiries beyond price history continue monthly from the last observed
    month (all covered roots are monthly contracts — verified gap-free in
    expiry_calendars.xlsx for CL/CO/XB/HO/QS/NG).
    """
    exp = pd.DatetimeIndex(expiry_calendar).dropna().sort_values().unique()
    mf = months_f1.dropna()
    mf = mf[~mf.index.duplicated(keep="first")].sort_index()

    out: dict[pd.Timestamp, int] = {}
    last_m: int | None = None
    for E in exp:
        m: int | None = None
        if len(mf) and E >= mf.index[0] and E <= mf.index[-1]:
            pos = mf.index.searchsorted(E, side="right") - 1
            if pos >= 0:
                m = int(mf.iloc[pos])
        if m is None:
            if last_m is None:
                continue  # expiry precedes price history — unusable, skip
            m = _add_months(last_m, 1)
        if last_m is not None and m <= last_m:
            # monthly contracts: repair rare data artifacts monotonically
            m = _add_months(last_m, 1)
        out[pd.Timestamp(E)] = m
        last_m = m

    return pd.Series(out, dtype="int64")


# ============================================================
# Roll triggers
# ============================================================
def _last_trading_day_leq(trading_idx: pd.DatetimeIndex, date) -> pd.Timestamp | None:
    pos = trading_idx.searchsorted(pd.Timestamp(date), side="right") - 1
    return trading_idx[pos] if pos >= 0 else None


SYNCED_EOM_LEAD_MONTHS = 4
"""
Months before delivery that `synced_eom` rolls INTO a contract (held for one
calendar month, from EOM(X - SYNCED_EOM_LEAD_MONTHS) to EOM(X - SYNCED_EOM_LEAD_MONTHS + 1)).
Chosen empirically via verify_synced_eom_safe, not assumed: WTI/RBOB/ULSD
(CL/XB/HO) LTD is always ~1 calendar month before delivery (day 16-22 of
that month); Brent (CO) LTD is ~2 months before delivery for the whole
live-trading era (2016-01 onward) — a real ICE Brent rule change in Jan 2016,
not a data artifact (LTDs before then were only ~1 month before delivery,
same as WTI). Leads of 1-3 all produce violations for at least one
commodity when checked against real dates (lead=3 alone leaves Brent's
post-2016 convention only 0 to -3 days clear — NOT comfortable, still a
violation on 193/266 months); lead=4 is the smallest value with zero
violations for every commodity checked (worst-case margin: Brent +26 days,
WTI +47 days).
"""


def compute_roll_trigger(
    binding_ltd: pd.Timestamp,
    trading_idx: pd.DatetimeIndex,
    trigger_style: str = DEFAULT_TRIGGER_STYLE,
    roll_buffer_days: int = 5,
    delivery_month: int | None = None,
) -> pd.Timestamp | None:
    """
    Trigger date for one delivery month, given the binding leg's LTD.

    prior_month_eom : last trading day of the month BEFORE the binding leg's
        expiry month. The expiry month is read off the calendar's own LTD
        (holiday-adjusted at source), and "last trading day" comes from the
        spread's actual trading index — no separately-hardcoded month
        arithmetic or business-day rules. NOTE: "binding leg" is a PER-PAIR
        concept (whichever leg's LTD is earlier for THIS pairing) — the same
        commodity can therefore get different prior_month_eom trigger dates
        in different pairs/constructions (see synced_eom for the fix).
    liquidity_buffer : `roll_buffer_days` trading days before the binding LTD
        (counted on the spread's trading index).
    synced_eom : last trading day of calendar month (X - SYNCED_EOM_LEAD_MONTHS),
        where X is `delivery_month` (YYYYMM int) — a pure function of the
        delivery month alone, with NO dependency on any leg's own LTD or on
        which pair/construction is asking. Every leg, in every pair, in every
        construction gets the identical trigger date for the same X, so any
        linear combination of constructions sharing a leg telescopes exactly
        by roll-date construction, not just by delivery-month bookkeeping.
        Requires `delivery_month`.
    """
    if trigger_style == "liquidity_buffer":
        pos = trading_idx.searchsorted(pd.Timestamp(binding_ltd), side="right") - 1
        pos -= int(roll_buffer_days)
        return trading_idx[pos] if pos >= 0 else None

    if trigger_style == "prior_month_eom":
        expiry_month = pd.Period(pd.Timestamp(binding_ltd), freq="M")
        checkpoint_end = (expiry_month - 1).to_timestamp(how="end").normalize()
        return _last_trading_day_leq(trading_idx, checkpoint_end)

    if trigger_style == "synced_eom":
        if delivery_month is None:
            raise ValueError("synced_eom requires delivery_month (YYYYMM int).")
        checkpoint_month = _to_period(delivery_month) - SYNCED_EOM_LEAD_MONTHS
        checkpoint_end = checkpoint_month.to_timestamp(how="end").normalize()
        return _last_trading_day_leq(trading_idx, checkpoint_end)

    raise ValueError(
        f"Unknown roll_trigger_style '{trigger_style}'. Choose from: {TRIGGER_STYLES}."
    )


def verify_synced_eom_safe(
    expiry_calendar: pd.DatetimeIndex,
    months_f1: pd.Series,
    lead_months: int = SYNCED_EOM_LEAD_MONTHS,
) -> dict:
    """
    For every historical delivery month on this calendar, check that the
    synced_eom roll-OUT trigger for that month (i.e. the trigger for month+1)
    falls strictly before that month's own LTD — the "never dips into M1"
    guarantee, verified against real dates rather than assumed. Returns
    {"n_months", "n_violations", "worst_margin_days", "violations": [...]}.
    """
    exp_m = expiry_delivery_months(expiry_calendar, months_f1)
    ltd_by_month = {int(m): ts for ts, m in exp_m.items()}
    violations = []
    margins = []
    for X, ltd in ltd_by_month.items():
        next_x = _add_months(X, 1)
        checkpoint_month = _to_period(next_x) - lead_months
        rollout = checkpoint_month.to_timestamp(how="end").normalize()
        margin_days = (pd.Timestamp(ltd) - rollout).days
        margins.append(margin_days)
        if rollout >= pd.Timestamp(ltd):
            violations.append((X, str(ltd.date()), str(rollout.date())))
    return {
        "n_months": len(ltd_by_month),
        "n_violations": len(violations),
        "worst_margin_days": min(margins) if margins else None,
        "violations": violations[:10],
    }


# ============================================================
# Month -> price/label lookup on wide frames
# ============================================================
def _sorted_tenor_cols(months: pd.DataFrame, prices: pd.DataFrame) -> list[str]:
    cols = [c for c in months.columns if c in prices.columns and c.startswith("F")]
    return sorted(cols, key=lambda c: int(c[1:]))


def _lookup_month(
    prices: pd.DataFrame,
    months: pd.DataFrame,
    target_month: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """
    For each date, find the generic column whose CONTRACT_MONTH_YR equals the
    target delivery month. Returns (price, column_label); NaN/None where the
    month isn't on the curve that day.
    """
    cols = _sorted_tenor_cols(months, prices)
    eq = months[cols].eq(target_month, axis=0)
    has = eq.any(axis=1)
    label = eq.idxmax(axis=1).where(has, None)

    px = pd.Series(np.nan, index=target_month.index, dtype=float)
    for c in cols:
        mask = (label == c).to_numpy()
        if mask.any():
            px.loc[mask] = prices[c].reindex(target_month.index).loc[mask]
    return px, label


# ============================================================
# Matched-leg builder (leg_matched / spread_matched share this)
# ============================================================
def build_matched_legs(
    prices1: pd.DataFrame,
    months1: pd.DataFrame,
    prices2: pd.DataFrame,
    months2: pd.DataFrame,
    expiry1,
    expiry2,
    *,
    roll_trigger_style: str = DEFAULT_TRIGGER_STYLE,
    roll_buffer_days: int = 5,
    month_offset: int = 0,
    tenor_lead_months: int = 0,
    calendar_override: pd.DatetimeIndex | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build two delivery-month-coordinated leg roll paths.

    For anchor month X: leg1 holds X + month_offset + tenor_lead_months,
    leg2 holds X + tenor_lead_months. Both legs roll to X+1 at the shared
    trigger derived from min(leg1 LTD(X + month_offset), leg2 LTD(X)) —
    the roll cadence stays anchored to the prompt cycle even for deferred
    (tenor_lead_months > 0) holdings.

    calendar_override : restrict roll-trigger/price resolution to this
        calendar in addition to the two legs' own trading days — pass the
        SAME shared calendar used by a related construction (e.g. a
        single-commodity leg being combined with a cross-commodity pair) so
        both resolve "last trading day <= checkpoint" identically. Without
        this, two constructions that use an identical trigger FORMULA can
        still land on different actual trading days whenever the two legs'
        exchanges have different holiday calendars (e.g. NYMEX vs ICE) and
        one leg is evaluated against a fuller calendar than the other.

    Returns (leg1_df, leg2_df) in the _prepare_leg schema plus held_month.
    """
    trading_idx = (
        prices1.index.intersection(prices2.index)
        .intersection(months1.index)
        .intersection(months2.index)
        .sort_values()
    )
    if calendar_override is not None:
        trading_idx = trading_idx.intersection(calendar_override)
    if len(trading_idx) == 0:
        raise ValueError("No overlapping trading dates across prices/months frames.")

    exp_m1 = expiry_delivery_months(expiry1, months1["F1"])
    exp_m2 = expiry_delivery_months(expiry2, months2["F1"])
    ltd1 = {int(m): ts for ts, m in exp_m1.items()}
    ltd2 = {int(m): ts for ts, m in exp_m2.items()}

    # anchor months X: leg2's month sequence where both legs' LTDs are known
    anchor_months = sorted(
        X for X in ltd2 if _add_months(X, month_offset) in ltd1
    )
    if not anchor_months:
        raise ValueError("No delivery months shared by both expiry calendars.")

    # Trigger arithmetic runs on the trading index extended with future
    # business days, so months whose trigger falls beyond the data end get a
    # real future date (and the live end-of-history state stays defined)
    # instead of degenerately collapsing onto the final observed day.
    calc_idx = trading_idx.union(
        pd.bdate_range(
            trading_idx[-1] + pd.offsets.BDay(1),
            trading_idx[-1] + pd.offsets.BDay(140),
        )
    )

    triggers: list[tuple[int, pd.Timestamp]] = []
    for X in anchor_months:
        binding_ltd = min(ltd1[_add_months(X, month_offset)], ltd2[X])
        # The state machine consumes ROLL-OUT triggers: trigger(X) is the day
        # the position exits X (into X+1) at the close.  synced_eom's formula
        # is defined as the roll-IN checkpoint of a delivery month
        # (EOM(month - SYNCED_EOM_LEAD_MONTHS); see SYNCED_EOM_LEAD_MONTHS and
        # verify_synced_eom_safe), so the roll-out of X is the roll-in of
        # X + 1.  Passing X here (pre-2026-07-08) held every contract one
        # month more deferred than the documented, lead-calibrated schedule.
        trig_month = _add_months(X, 1) if roll_trigger_style == "synced_eom" else X
        trig = compute_roll_trigger(
            binding_ltd, calc_idx, roll_trigger_style, roll_buffer_days,
            delivery_month=trig_month,
        )
        if trig is not None:
            triggers.append((X, trig))

    # triggers must be strictly increasing in X for the state machine
    triggers.sort(key=lambda t: t[0])
    cleaned: list[tuple[int, pd.Timestamp]] = []
    for X, trig in triggers:
        if cleaned and trig <= cleaned[-1][1]:
            continue  # degenerate (very start of history) — earlier month wins
        cleaned.append((X, trig))
    if not cleaned:
        raise ValueError("No usable roll triggers computed.")

    months_arr = np.array([X for X, _ in cleaned])
    trig_arr = pd.DatetimeIndex([t for _, t in cleaned])

    # state machine, vectorised:
    #   intraday month (held DURING day t): smallest X with trigger(X) >= t
    #   end-of-day month (held AFTER t's close): smallest X with trigger(X) > t
    pos_in = trig_arr.searchsorted(trading_idx, side="left")
    pos_eod = trig_arr.searchsorted(trading_idx, side="right")

    valid = pos_eod < len(months_arr)  # beyond last trigger -> no defined state
    pos_in = np.clip(pos_in, 0, len(months_arr) - 1)
    pos_eod = np.clip(pos_eod, 0, len(months_arr) - 1)

    X_in = pd.Series(months_arr[pos_in], index=trading_idx).where(valid)
    X_eod = pd.Series(months_arr[pos_eod], index=trading_idx).where(valid)

    lead = int(tenor_lead_months)

    def _leg(prices, months, extra_offset: int) -> pd.DataFrame:
        prices = prices.reindex(trading_idx)
        months = months.reindex(trading_idx)
        shift = extra_offset + lead
        m_in = X_in.map(lambda x: _add_months(x, shift) if pd.notna(x) else np.nan)
        m_eod = X_eod.map(lambda x: _add_months(x, shift) if pd.notna(x) else np.nan)

        px_eod, label_eod = _lookup_month(prices, months, m_eod)

        # intraday price only differs from eod on roll days (old month at T)
        roll_mask = m_eod.ne(m_eod.shift(1)) & m_eod.notna() & m_eod.shift(1).notna()
        px_in = px_eod.copy()
        if roll_mask.any():
            px_old, _ = _lookup_month(
                prices.loc[roll_mask], months.loc[roll_mask], m_in.loc[roll_mask]
            )
            px_in.loc[roll_mask] = px_old

        daily_pnl = px_in - px_eod.shift(1)
        daily_pnl.iloc[0] = 0.0

        leg = pd.DataFrame(index=trading_idx)
        leg["daily_pnl"] = daily_pnl
        leg["held_contract"] = label_eod
        leg["held_month"] = m_eod
        leg["roll_day_flag"] = roll_mask.astype(int)
        leg["post_expiry_flag"] = 0  # schema compat with rank-path frames
        leg["held_price"] = px_eod
        return leg

    leg1_df = _leg(prices1, months1, month_offset)
    leg2_df = _leg(prices2, months2, 0)
    return leg1_df, leg2_df


def spread_level_series(leg1_df: pd.DataFrame, leg2_df: pd.DataFrame) -> pd.Series:
    """
    Stitched spread LEVEL (leg1 - leg2 held_price) on the joint index where
    both legs have a held price — for CHARTING / quoted-level display ONLY.

    Never difference it for P&L (it jumps at rolls/relabels — see
    spread_pnl_from_legs), and never feed it to a rolling-window SIGNAL
    either: the relabel jumps are persistently signed (~+0.21 $/bbl per roll
    for WTI-Brent since 2015), so any trailing mean/std computed on this
    series carries a structural bias (+0.38 sigma on the traded z at lb90 —
    Pass 8, notes/pass8_signal_series_integrity_2026-07-14.md). Signals must
    read spread_signal_series below.
    """
    idx = (leg1_df.loc[leg1_df["held_price"].notna()].index
           .intersection(leg2_df.loc[leg2_df["held_price"].notna()].index))
    return (leg1_df["held_price"].reindex(idx)
            - leg2_df["held_price"].reindex(idx)).rename("spread")


def spread_signal_series(leg1_df: pd.DataFrame, leg2_df: pd.DataFrame) -> pd.Series:
    """
    THE sanctioned signal-input series for a pair: cumulative sum of the
    leg-flow spread P&L (spread_pnl_from_legs — the validated flow engine),
    anchored at the first stitched level so it starts on the quoted spread's
    scale. Contains only tradable price changes: no relabel jump ever enters,
    so it is perfectly flat on a frozen market (gated by
    tests/test_signal_series_integrity.py) and a rolling z-score computed on
    it is unbiased by the roll cycle.

    The anchor is a display convenience only — adding a constant shifts the
    level and every rolling mean equally, so z-scores are anchor-invariant.
    Over long histories this series drifts away from the quoted level by
    exactly the accumulated roll carry (that is the point); use
    spread_level_series when a trader-facing quoted level is needed.
    """
    from energy.strategies.rolling import spread_pnl_from_legs

    level = spread_level_series(leg1_df, leg2_df)
    flows = spread_pnl_from_legs(leg1_df, leg2_df)["daily_pnl"]
    cum = flows.fillna(0.0).cumsum().reindex(level.index)
    anchor_pos = level.notna().to_numpy().argmax()
    return (cum - cum.iloc[anchor_pos] + level.iloc[anchor_pos]).rename("signal_spread")


def build_matched_spread(leg1_df: pd.DataFrame, leg2_df: pd.DataFrame) -> pd.DataFrame:
    """
    spread_matched view: the two coordinated legs as ONE packaged instrument
    (single roll flag, single price = leg1 - leg2, single daily_pnl). Legs
    share roll dates by construction. With per-leg t_cost_abs = 0 the P&L is
    identical to leg_matched; the distinction is execution/cost treatment
    (one spread ticket vs two leg tickets) and reporting.
    """
    idx = leg1_df.index.intersection(leg2_df.index).sort_values()
    l1, l2 = leg1_df.loc[idx], leg2_df.loc[idx]
    out = pd.DataFrame(index=idx)
    out["daily_pnl"] = l1["daily_pnl"] - l2["daily_pnl"]
    out["held_contract"] = (
        l1["held_contract"].astype("string") + "|" + l2["held_contract"].astype("string")
    )
    out["held_month"] = l1["held_month"]
    out["roll_day_flag"] = np.maximum(l1["roll_day_flag"], l2["roll_day_flag"])
    out["post_expiry_flag"] = 0
    out["held_price"] = l1["held_price"] - l2["held_price"]
    return out


# ============================================================
# Continuous contract-month-match validator
# ============================================================
def validate_month_match(
    leg1_df: pd.DataFrame,
    leg2_df: pd.DataFrame,
    month_offset: int = 0,
    *,
    raise_on_fail: bool = True,
) -> dict:
    """
    The safeguard against the original bug: for every held date, assert
    leg1_month - leg2_month == month_offset (in calendar months). Compares
    the two legs' emitted held months against each other — the cross-leg
    relationship is exactly what the disbanded construction silently broke.

    Returns {"n_days", "n_checked", "n_mismatch", "mismatch_dates"}; raises
    ValueError on any mismatch when raise_on_fail (strict pairs).
    """
    idx = leg1_df.index.intersection(leg2_df.index)
    m1 = leg1_df["held_month"].reindex(idx)
    m2 = leg2_df["held_month"].reindex(idx)
    both = m1.notna() & m2.notna()

    diff = pd.Series(np.nan, index=idx)
    diff.loc[both] = [
        _month_diff(int(a), int(b)) for a, b in zip(m1[both], m2[both])
    ]
    bad = diff.ne(month_offset) & both
    report = {
        "n_days": int(len(idx)),
        "n_checked": int(both.sum()),
        "n_mismatch": int(bad.sum()),
        "mismatch_dates": [str(d.date()) for d in idx[bad][:10]],
    }
    if raise_on_fail and report["n_mismatch"] > 0:
        raise ValueError(
            f"Delivery-month match violated on {report['n_mismatch']} of "
            f"{report['n_checked']} days (expected offset {month_offset}); "
            f"first mismatches: {report['mismatch_dates']}"
        )
    return report


# ============================================================
# Rank-based per-leg path (rank_approximate fallback + legacy reference)
# ============================================================
def _rank_leg(
    commodity_name: str,
    prices: pd.DataFrame,
    expiry_calendar,
    roll_config: str = "prompt_EOM_roll",
) -> pd.DataFrame:
    """One leg rolled on its own rank-based schedule (CONTRACT_SPECS config)."""
    spec = CONTRACT_SPECS.get(commodity_name, {})
    cfg = spec.get(roll_config)
    if cfg is None:
        raise ValueError(f"{commodity_name}: no '{roll_config}' config in CONTRACT_SPECS")
    leg = build_roll_path(
        prices=prices,
        expiry_calendar=expiry_calendar,
        style=cfg["style"],
        front_col=cfg.get("front_col", "F1"),
        next_col=cfg.get("next_col", "F2"),
        third_col=cfg.get("third_col"),
        mid_col=cfg.get("mid_col"),
        far_col=cfg.get("far_col"),
        roll_window=cfg.get("roll_window", 5),
    ).copy()

    held_price = pd.Series(index=leg.index, dtype=float)
    for dt, col in leg["held_contract"].items():
        if pd.notna(col) and str(col) in prices.columns and dt in prices.index:
            held_price.at[dt] = float(prices.at[dt, str(col)])
    leg["held_price"] = held_price
    leg["held_month"] = np.nan  # rank path carries no validated month
    return leg


def _held_rank(cfg: dict) -> int | None:
    """Rank the leg normally lives in, for deferred_rank visibility."""
    style = cfg.get("style")
    col = {
        "eom_mid": cfg.get("next_col", "F2"),
        "eom_eom": cfg.get("next_col", "F2"),
        "eom_dynamic": cfg.get("next_col", "F2"),
        "eom_ngl": cfg.get("mid_col", "F3"),
        "window": cfg.get("front_col", "F1"),
    }.get(style)
    try:
        return int(str(col)[1:])
    except (TypeError, ValueError):
        return None


def validate_against_listed_spread(
    leg1_df: pd.DataFrame,
    leg2_df: pd.DataFrame,
    listed_price: pd.Series,
    listed_month: pd.Series,
    *,
    month_offset: int = 0,
    tol_median: float = 0.05,
    tol_p95: float = 0.75,
    min_matched_days: int = 30,
) -> dict:
    """
    Ongoing sanity check: compare this construction's spread LEVEL
    (leg1_df/leg2_df held_price diff) against an independent listed spread
    series (e.g. Bloomberg S:ENCO), restricted to days where BOTH sides hold
    the same delivery month (per held_month vs listed_month).

    Comparing on every day is the wrong test for an early-rolling trigger
    (prior_month_eom rolls a full calendar checkpoint ahead of the binding
    leg's own expiry month): for most of history it deliberately holds a
    later delivery month than a spread quoted near-LTD, so a whole-history
    diff is dominated by that known, intentional timing gap, not by
    construction correctness. Restricting to month-matched days isolates
    genuine regressions (wrong leg, wrong sign, stitched-diff reintroduced,
    wrong month lookup, roll math error) from that expected timing gap —
    and gives a MUCH tighter signal: a real regression should degrade the
    month-matched comparison even though it can't fix the timing gap.

    Returns a dict; `ok` is False if either tolerance is breached or too few
    month-matched days were available to judge (a match-rate collapse is
    itself a regression signal — e.g. a broken CONTRACT_MONTH_YR lookup).
    """
    recon = (leg1_df["held_price"] - leg2_df["held_price"]).dropna()
    our_month = leg2_df["held_month"]

    common = (
        recon.index.intersection(listed_price.index)
        .intersection(listed_month.index)
        .intersection(our_month.dropna().index)
    )
    r = recon.reindex(common)
    b = listed_price.reindex(common)
    om = our_month.reindex(common)
    bm = listed_month.reindex(common)

    both = om.notna() & bm.notna()
    month_diff = pd.Series(np.nan, index=common)
    month_diff.loc[both] = [
        _month_diff(int(a), int(x)) for a, x in zip(om[both], bm[both])
    ]
    matched = both & month_diff.eq(month_offset)

    diff = (r - b)[matched]
    d = diff.abs()
    n_matched = int(matched.sum())

    result = {
        "n_days": int(len(common)),
        "n_month_matched": n_matched,
        "match_rate": float(matched.mean()) if len(common) else float("nan"),
        "mean_diff": float(diff.mean()) if n_matched else float("nan"),
        "median_abs_diff": float(d.median()) if n_matched else float("nan"),
        "p95_abs_diff": float(d.quantile(0.95)) if n_matched else float("nan"),
        "max_abs_diff": float(d.max()) if n_matched else float("nan"),
        "corr_level": float(r[matched].corr(b[matched])) if n_matched > 5 else float("nan"),
        "corr_change": float(r[matched].diff().corr(b[matched].diff())) if n_matched > 5 else float("nan"),
    }
    result["ok"] = bool(
        n_matched >= min_matched_days
        and result["median_abs_diff"] <= tol_median
        and result["p95_abs_diff"] <= tol_p95
    )
    return result


def legacy_uncoordinated_reference(
    leg1_name: str,
    prices1: pd.DataFrame,
    expiry1,
    leg2_name: str,
    prices2: pd.DataFrame,
    expiry2,
    roll_config: str = "prompt_EOM_roll",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    STATIC HISTORICAL REFERENCE ONLY — the disbanded construction (each leg
    independently rank-rolled, zero cross-leg coordination; 23.6% of held
    days delivery-month mismatched for WTI-Brent). Not selectable anywhere
    in live construction; exists solely so re-validation backtests can
    compare corrected series against what production used to produce.
    """
    return (
        _rank_leg(leg1_name, prices1, expiry1, roll_config),
        _rank_leg(leg2_name, prices2, expiry2, roll_config),
    )


# ============================================================
# Public entry point
# ============================================================

# roll_config -> delivery-month lead for the coordinated (strict) engine.
# Q2/Q3/1yr shift BOTH legs' held delivery months out by the lead while the
# roll cadence stays anchored to the prompt cycle (build_matched_legs
# derives the shared trigger from the PROMPT anchor pair's LTDs, so the
# deferred holdings roll on the same dates as the prompt construction and
# the cross-leg month_offset is preserved at every tenor by construction).
ROLL_CONFIG_LEAD_MONTHS = {
    "prompt_EOM_roll": 0,
    "q2_deferred_roll": 3,
    "q3_deferred_roll": 6,
    "1yr_deferred_roll": 12,
}


def prepare_spread_legs(
    leg1_name: str,
    leg2_name: str,
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    expiry1,
    expiry2,
    *,
    months1: pd.DataFrame | None = None,
    months2: pd.DataFrame | None = None,
    roll_config: str = "prompt_EOM_roll",
    spread_style: str | None = None,
    roll_trigger_style: str | None = None,
    month_offset: int | None = None,
    validate: bool = True,
    calendar_override: pd.DatetimeIndex | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Single construction gateway for all cross-commodity spread consumers.

    strict_delivery_match pairs -> coordinated delivery-month engine
        (build_matched_legs). Requires months1/months2 (wide CONTRACT_MONTH_YR
        frames from data.loader.get_contract_months); raises if missing rather
        than silently degrading to the disbanded rank construction.
    rank_approximate pairs (and pairs with no SPREAD_SPECS entry) -> per-leg
        rank path at each leg's CONTRACT_SPECS config, explicitly labeled.

    roll_config: "prompt_EOM_roll", "q2_deferred_roll", "q3_deferred_roll",
    or "1yr_deferred_roll". For strict pairs the deferred flavours map to
    tenor_lead_months = 3 / 6 / 12 on the matched engine (see
    ROLL_CONFIG_LEAD_MONTHS: same prompt-anchored roll cadence, held months
    shifted out by the lead, cross-leg month_offset preserved).

    Returns (leg1_df, leg2_df, meta). meta travels with the result to every
    downstream surface (API payloads, UI badges) — same treatment for
    roll_trigger_style as for precision_mode: both are deliberate
    precision-vs-safety tradeoffs the consumer must be able to see.
    """
    spec = get_spread_spec(leg1_name, leg2_name) or {}
    precision = spec.get("precision_mode", "rank_approximate")
    style = spread_style or spec.get("spread_style", DEFAULT_SPREAD_STYLE)
    if style not in SPREAD_STYLES:
        raise ValueError(f"Unknown spread_style '{style}'. Choose from: {SPREAD_STYLES}.")

    meta = {
        "pair": f"{leg1_name} / {leg2_name}",
        "spread_style": style,
        "precision_mode": precision,
        "month_offset": None,
        "roll_trigger_style": None,
        "roll_buffer_days": None,
        "deferred_rank": None,
        "construction": None,
        "validated": False,
    }

    if precision == "strict_delivery_match":
        if months1 is None or months2 is None:
            raise ValueError(
                f"{meta['pair']} is a strict_delivery_match pair: contract-month "
                "frames are required (data.loader.get_contract_months). The "
                "uncoordinated rank construction is disbanded for this pair — "
                "for historical comparison only, use legacy_uncoordinated_reference()."
            )
        trigger = roll_trigger_style or spec.get("roll_trigger_style", DEFAULT_TRIGGER_STYLE)
        if trigger not in TRIGGER_STYLES:
            raise ValueError(
                f"Unknown roll_trigger_style '{trigger}'. Choose from: {TRIGGER_STYLES}."
            )
        offset = month_offset if month_offset is not None else spec.get("month_offset", 0)
        buffer_days = int(spec.get("roll_buffer_days", 5))
        lead = ROLL_CONFIG_LEAD_MONTHS.get(roll_config, 0)

        leg1_df, leg2_df = build_matched_legs(
            prices1, months1, prices2, months2, expiry1, expiry2,
            roll_trigger_style=trigger,
            roll_buffer_days=buffer_days,
            month_offset=offset,
            calendar_override=calendar_override,
            tenor_lead_months=lead,
        )
        if validate:
            validate_month_match(leg1_df, leg2_df, offset, raise_on_fail=True)
            meta["validated"] = True

        meta.update(
            month_offset=offset,
            roll_trigger_style=trigger,
            roll_buffer_days=buffer_days if trigger == "liquidity_buffer" else None,
            construction="coordinated_delivery_month",
            tenor_lead_months=lead,
        )
        return leg1_df, leg2_df, meta

    # rank_approximate (or unspecified pair): per-leg rank construction,
    # explicitly labeled so it is never mistaken for a validated pair.
    leg1_df = _rank_leg(leg1_name, prices1, expiry1, roll_config)
    leg2_df = _rank_leg(leg2_name, prices2, expiry2, roll_config)

    cfg1 = CONTRACT_SPECS.get(leg1_name, {}).get(roll_config, {})
    cfg2 = CONTRACT_SPECS.get(leg2_name, {}).get(roll_config, {})
    meta.update(
        deferred_rank=spec.get("deferred_rank"),
        construction="per_leg_rank",
        held_ranks={leg1_name: _held_rank(cfg1), leg2_name: _held_rank(cfg2)},
    )
    return leg1_df, leg2_df, meta
