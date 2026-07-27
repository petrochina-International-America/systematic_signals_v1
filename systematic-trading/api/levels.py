"""
/api/levels — Proximity scanner with chart data, signal flips, and positioning.

Returns per-commodity 3-month price charts with horizontal level lines,
recent signal state changes, and near-trigger alerts.

Accepts ?tenor=1..4 (default 1): generic curve rank (M1 → F1), except
bal-month products (_BAL_MONTH_M1) which start at F2 and display M2…M5. MAs
are computed on a roll-continuous series anchored to today's quote so levels
are free of generic re-rank jumps; the plotted price history stays raw
quotes. Banners run on the spec-prompt series and, with spreads, are only
computed on the tenor=1 pass; the frontend selects tenor per card.
"""

from fastapi import APIRouter

router = APIRouter()

_cache: dict[int, tuple[float, dict]] = {}  # tenor → (loaded_at, payload)

_THREE_MONTHS = 63
_FLIP_LOOKBACK = 5  # trading days to check for signal flips

_TIER_SLOW_MA = {
    "Very Fast": 5,
    "Fast": 20,
    "Medium": 60,
    "Slow": 120,
}

_TIER_COLORS = {
    "Very Fast": "#f472b6",
    "Fast": "#34d399",
    "Medium": "#94a3b8",
    "Slow": "#a78bfa",
}

_TIER_LABELS = {
    "Very Fast": "Trend (VFast)",
    "Fast": "Trend (Fast)",
    "Medium": "Trend (Med)",
    "Slow": "Trend (Slow)",
}

_VOL_TARGET = 0.15
_VOL_WINDOW = 120

_MAX_TENOR = 4

# Products whose F1 generic is a balance-of-month (or otherwise non-viewable)
# contract, so the M-n selector starts them at F2 and cards label M2…M5.
# WTI/WTI Midland are NOT in this set: their specs put prompt_EOM_roll on F2
# as an EOM trading convention, but their F1 is a real monthly prompt and is
# fine to display. HTT's F1 quotes are unreliable, so it starts at F2 like
# its spec. Dubai's spec still says F1 — correcting it would change backtest
# defaults, so the levels view alone treats it as bal-month here.
_BAL_MONTH_M1 = {"Propane", "Ethane", "Butane", "Dubai", "HTT"}


def _rolling_mean(series, window):
    import pandas as pd
    return series.rolling(window, min_periods=window).mean()


def _current_vol_scalar(rets, vol_window=_VOL_WINDOW, vol_target=_VOL_TARGET):
    """Current vol scalar: target_daily_vol / realized_daily_vol."""
    import numpy as np
    vals = rets.dropna().values
    if len(vals) < max(2, vol_window // 2):
        return 1.0
    tail = vals[-vol_window:]
    rv = float(np.std(tail, ddof=1))
    target_daily = vol_target / np.sqrt(252.0)
    return target_daily / rv if rv > 1e-8 else 1.0


def _roll_clean_series(prices, months, col_num, px_raw):
    """Roll-continuous series for signal math on generic column F{col_num}.

    Raw generic columns re-rank the day after expiry — yesterday's F(n+1)
    becomes today's F(n) — stepping the series by the calendar spread even
    when nothing traded. MAs on the raw column fire phantom crossings (see
    the signal-series audit note in energy/strategies/momentum.py). Rebuild
    from contract-internal flows: on a re-rank day the held contract's move
    is F(n)[t] − F(n+1)[t−1]. Anchor the cumsum so the last value equals
    today's quote, so levels read in current screen dollars (a constant
    shift changes no crossings). Falls back to the raw column when
    contract-month data is unavailable.
    """
    col, nxt_col = f"F{col_num}", f"F{col_num + 1}"
    if months is None or col not in months.columns or nxt_col not in prices.columns:
        return px_raw
    m = months[col].reindex(px_raw.index)
    rerank = m.ne(m.shift(1)) & m.notna() & m.shift(1).notna()
    if not rerank.any():
        return px_raw
    flow = px_raw.diff()
    nxt_prev = prices[nxt_col].astype(float).reindex(px_raw.index).shift(1)
    roll_flow = (px_raw - nxt_prev).where(lambda s: s.notna(), flow)
    cont = flow.where(~rerank, roll_flow).fillna(0.0).cumsum()
    return cont + (float(px_raw.iloc[-1]) - float(cont.iloc[-1]))


def _append_banner_signals(commodity, prices, px, px_raw, hot_signals, recent_trades):
    """Near-trigger and recent-flip entries for the banner rows.

    Runs on the spec-prompt series (what the strategies actually trade),
    which can differ from a card's displayed tenor — e.g. WTI's strategy
    rolls on F2 while its M1 card shows F1. px is the roll-clean series for
    crossings; px_raw supplies the quoted prices reported in the pills.
    """
    import pandas as pd

    current = float(px_raw.iloc[-1])

    levels = []
    for tier_name, slow_w in _TIER_SLOW_MA.items():
        ma = _rolling_mean(px, slow_w)
        if pd.isna(ma.iloc[-1]):
            continue
        levels.append({
            "label": _TIER_LABELS[tier_name],
            "window": slow_w,
            "value": float(ma.iloc[-1]),
            "ma": ma,
        })

    # Near-trigger: closest meaningful-tier (20d+) MA within 1.5%
    if levels:
        meaningful = [l for l in levels if l["window"] >= 20] or levels
        closest = min(meaningful, key=lambda l: abs(current - l["value"]))
        dollar_dist = round(current - closest["value"], 2)
        pct_dist = (round((current / closest["value"] - 1) * 100, 2)
                    if closest["value"] != 0 else None)
        if pct_dist is not None and abs(pct_dist) < 1.5:
            hot_signals.append({
                "commodity": commodity,
                "strategy": "Trend Following",
                "direction": "Long" if dollar_dist > 0 else "Short",
                "distance": abs(pct_dist),
                "detail": f"${abs(dollar_dist):.2f} from {closest['label']}",
                "level": round(closest["value"], 4),
                "current": round(current, 2),
            })

    # MA-crossover flips in the last _FLIP_LOOKBACK days
    for l in levels:
        ma = l["ma"]
        for lookback in range(1, _FLIP_LOOKBACK + 1):
            if len(px) <= lookback or len(ma) <= lookback:
                continue
            cur_above = current > l["value"]
            prev_ma_val = float(ma.iloc[-lookback - 1]) if pd.notna(ma.iloc[-lookback - 1]) else None
            if prev_ma_val is None:
                continue
            prev_above = float(px.iloc[-lookback - 1]) > prev_ma_val
            if cur_above != prev_above:
                recent_trades.append({
                    "commodity": commodity,
                    "strategy": "Trend Following",
                    "from": "Long" if prev_above else "Short",
                    "to": "Short" if prev_above else "Long",
                    "price": round(float(px_raw.iloc[-lookback]), 2),
                    "level": round(l["value"], 2),
                    "date": px.index[-lookback].strftime("%Y-%m-%d"),
                    "tier": l["label"],
                })
                break  # only report most recent flip per tier

    # Carry flips: curve-shape compare vs the deferred anchor (F13 or the
    # last populated column), raw contemporaneous quotes.
    try:
        f13 = prices.get("F13")
        if f13 is None or f13.dropna().empty:
            available = sorted(
                [c for c in prices.columns if c.startswith("F") and prices[c].notna().any()],
                key=lambda x: int(x[1:]),
            )
            end_col = available[-1] if len(available) >= 2 else None
        else:
            end_col = "F13"
        if end_col:
            f_end = prices[end_col].astype(float).dropna()
            f_end_val = float(f_end.iloc[-1])
            spread = current - f_end_val
            carry_direction = "Long" if spread > 0 else "Short"
            shape = "Backwardation" if spread > 0 else "Contango"
            for lookback in range(1, _FLIP_LOOKBACK + 1):
                if len(px_raw) <= lookback or len(f_end) <= lookback:
                    continue
                prev_spread = (float(px_raw.iloc[-lookback - 1])
                               - float(f_end.iloc[-lookback - 1]))
                prev_dir = "Long" if prev_spread > 0 else "Short"
                if prev_dir != carry_direction:
                    recent_trades.append({
                        "commodity": commodity,
                        "strategy": "Carry",
                        "from": prev_dir,
                        "to": carry_direction,
                        "price": round(float(px_raw.iloc[-lookback]), 2),
                        "level": round(f_end_val, 2),
                        "date": px_raw.index[-lookback].strftime("%Y-%m-%d"),
                        "tier": f"Carry ({shape})",
                    })
                    break
    except Exception:
        pass


@router.get("/proximity")
def proximity(tenor: int = 1):
    from data import loader
    from data.signals import PRODUCT_GROUPS
    from energy.accounting.contract_specs import CONTRACT_SPECS
    from energy.analytics.signal_summary import (
        SPREAD_GROUPS, build_pair_signal_frame, live_state,
    )
    from data.cot import get_cot_snapshot, is_synthetic
    import pandas as pd
    import numpy as np

    tenor = min(max(int(tenor), 1), _MAX_TENOR)

    cached = _cache.get(tenor)
    if cached and cached[0] == loader._loaded_at and cached[1]:
        return cached[1]

    # Banners (near-trigger, recent flips) and the spreads panel are pinned to
    # M1 — the tenor the production strategies actually trade — so they're
    # only computed on the tenor=1 pass. The frontend holds on to them across
    # tenor switches.
    is_base_tenor = tenor == 1

    # COT positioning snapshot
    all_commodities = [c for group in PRODUCT_GROUPS.values() for c in group]
    cot_pending = is_synthetic()
    try:
        cot_df = get_cot_snapshot(all_commodities)
        cot_map = {}
        for _, row in cot_df.iterrows():
            name = row.get("commodity", "")
            pct = row.get("percentile_rank")
            flag = row.get("crowding_flag", "")
            if name and pct is not None:
                cot_map[name] = {"percentile": round(float(pct)), "flag": str(flag), "pending": cot_pending}
    except Exception:
        cot_map = {}

    groups = {}  # grouped output
    hot_signals = []
    recent_trades = []

    for group_name, commodities in PRODUCT_GROUPS.items():
        group_cards = []

        for commodity in commodities:
            spec = CONTRACT_SPECS.get(commodity)
            if spec is None:
                continue
            try:
                prices = loader.get_prices(commodity)
                front_col = spec.get("prompt_EOM_roll", {}).get("front_col", "F1")
                # Selector tenor maps straight onto generic rank (M1 → F1),
                # except bal-month names start at F2 and display their actual
                # rank (M2…M5).
                base = 2 if commodity in _BAL_MONTH_M1 else 1
                col_num = base + tenor - 1
                col = f"F{col_num}"
                if col not in prices.columns:
                    continue
                px_raw = prices[col].astype(float).dropna()
                if len(px_raw) < 120:
                    continue
                try:
                    months = loader.get_contract_months(commodity)
                except (KeyError, RuntimeError):
                    months = None
                # px drives all signal math (MAs, crossings, vol); px_raw is
                # what's plotted and quoted. Anchoring makes their last values
                # equal, so "distance from level" reads in screen dollars.
                px = _roll_clean_series(prices, months, col_num, px_raw)
            except Exception:
                continue

            tail = px_raw.iloc[-_THREE_MONTHS:]
            current = float(px_raw.iloc[-1])
            dates = [d.strftime("%Y-%m-%d") for d in tail.index]
            values = [round(float(v), 4) for v in tail.values]

            # ── Momentum levels: MA per speed tier ──
            ma_levels = []
            for tier_name, slow_w in _TIER_SLOW_MA.items():
                if tier_name == "Averaged":
                    continue
                ma = _rolling_mean(px, slow_w)
                ma_val = float(ma.iloc[-1]) if pd.notna(ma.iloc[-1]) else None
                if ma_val is None:
                    continue
                ma_tail = ma.reindex(tail.index)
                ma_history = [None if pd.isna(v) else round(float(v), 4) for v in ma_tail.values]
                ma_levels.append({
                    "tier": tier_name,
                    "label": _TIER_LABELS[tier_name],
                    "window": slow_w,
                    "value": round(ma_val, 4),
                    "color": _TIER_COLORS[tier_name],
                    "history": ma_history,
                })

            # ── Proximity to the closest meaningful-tier MA (card sorting) ──
            # Banner near-trigger entries are handled separately on the
            # spec-prompt series in _append_banner_signals.
            mom_dist = None
            if ma_levels:
                meaningful = [l for l in ma_levels if l["window"] >= 20]
                if not meaningful:
                    meaningful = ma_levels
                closest_ma = min(meaningful, key=lambda l: abs(current - l["value"]))
                closest_ma_val = closest_ma["value"]
                mom_dist = (round((current / closest_ma_val - 1) * 100, 2)
                            if closest_ma_val != 0 else None)

            # ── Carry level ──
            carry_level = None
            carry_direction = None
            carry_dist = None
            end_col = None
            try:
                f13 = prices.get("F13")
                if f13 is None or f13.dropna().empty:
                    available = sorted(
                        [c for c in prices.columns if c.startswith("F") and prices[c].notna().any()],
                        key=lambda x: int(x[1:]),
                    )
                    end_col = available[-1] if len(available) >= 2 else None
                else:
                    end_col = "F13"

                if end_col:
                    f_end = prices[end_col].astype(float)
                    f_end_val = float(f_end.dropna().iloc[-1])
                    spread = current - f_end_val
                    carry_direction = "Long" if spread > 0 else "Short"
                    carry_dist = round(spread / current * 100, 2) if current != 0 else None
                    shape = "Backwardation" if spread > 0 else "Contango"
                    f_end_tail = f_end.reindex(tail.index)
                    carry_history = [None if pd.isna(v) else round(float(v), 4) for v in f_end_tail.values]
                    carry_level = {
                        "tenor": end_col,
                        "value": round(f_end_val, 4),
                        "shape": shape,
                        "spread": round(spread, 4),
                        "history": carry_history,
                    }

            except Exception:
                pass

            # ── Blended CTA position (inverse-vol risk parity) ──
            # Weight each strategy by 1/vol of its rolling equity curve.
            # Slow strategies have smoother P&L → higher weight.
            # Fast strategies have more whipsaw → lower weight.
            import numpy as np

            # Roll-clean returns: contract-internal flow over the prior raw
            # quote. px.pct_change() would book the re-rank step as a return.
            daily_rets = (px.diff() / px_raw.shift(1)).dropna()

            strat_signals_today = {}
            strat_signals_prev = {}
            strat_inv_vol = {}
            sig_series_map = {}

            for ml in ma_levels:
                ma_series = _rolling_mean(px, ml["window"])
                sig_series = (px > ma_series).astype(float) * 2 - 1
                sig_series_map[ml["label"]] = sig_series
                strat_pnl = (sig_series.shift(1) * daily_rets).dropna()

                # Realized vol of the strategy's daily P&L (not the cumulative curve)
                tail_pnl = strat_pnl.iloc[-_VOL_WINDOW:]
                vol = float(tail_pnl.std(ddof=1)) if len(tail_pnl) > 20 else None

                strat_signals_today[ml["label"]] = 1.0 if current > ml["value"] else -1.0
                if vol and vol > 1e-8:
                    strat_inv_vol[ml["label"]] = 1.0 / vol

                if len(px) > 1 and len(ma_series) > 1:
                    prev_px = float(px.iloc[-2])
                    prev_ma = float(ma_series.iloc[-2]) if pd.notna(ma_series.iloc[-2]) else None
                    strat_signals_prev[ml["label"]] = (1.0 if prev_px > prev_ma else -1.0) if prev_ma else 0.0
                else:
                    strat_signals_prev[ml["label"]] = strat_signals_today[ml["label"]]

            if carry_direction is not None and end_col:
                carry_spread_series = px_raw.astype(float) - prices[end_col].astype(float)
                carry_sig_series = (carry_spread_series > 0).astype(float) * 2 - 1
                sig_series_map["Carry"] = carry_sig_series
                strat_pnl = (carry_sig_series.shift(1) * daily_rets).dropna()
                tail_pnl = strat_pnl.iloc[-_VOL_WINDOW:]
                vol = float(tail_pnl.std(ddof=1)) if len(tail_pnl) > 20 else None

                strat_signals_today["Carry"] = 1.0 if carry_direction == "Long" else -1.0
                if vol and vol > 1e-8:
                    strat_inv_vol["Carry"] = 1.0 / vol

                if len(carry_spread_series.dropna()) > 1:
                    prev_spread = float(carry_spread_series.dropna().iloc[-2])
                    strat_signals_prev["Carry"] = 1.0 if prev_spread > 0 else -1.0
                else:
                    strat_signals_prev["Carry"] = strat_signals_today["Carry"]

            vol_scalar = _current_vol_scalar(daily_rets)
            if strat_inv_vol:
                total_iv = sum(strat_inv_vol.values())
                weights = {n: iv / total_iv for n, iv in strat_inv_vol.items()}
                net_signal = sum(strat_signals_today.get(n, 0) * w for n, w in weights.items())
                net_signal_prev = sum(strat_signals_prev.get(n, 0) * w for n, w in weights.items())
                position_pct = round(net_signal * vol_scalar * 100, 1)
                position_pct_prev = round(net_signal_prev * vol_scalar * 100, 1)
                position_chg = round(position_pct - position_pct_prev, 1)
            else:
                net_signal = 0.0
                position_pct = 0.0
                position_pct_prev = 0.0
                position_chg = 0.0
                weights = {}

            # Position history (trailing window) using today's risk-parity weights
            # held fixed back through time, with a rolling vol-target scalar.
            if weights:
                net_signal_series = pd.Series(0.0, index=px.index)
                for n, w in weights.items():
                    if n in sig_series_map:
                        net_signal_series = net_signal_series.add(sig_series_map[n] * w, fill_value=0)
                rolling_std = daily_rets.rolling(_VOL_WINDOW, min_periods=_VOL_WINDOW // 2).std(ddof=1)
                target_daily = _VOL_TARGET / np.sqrt(252.0)
                vol_scalar_series = (target_daily / rolling_std.replace(0, np.nan)).reindex(px.index).fillna(1.0)
                position_pct_series = (net_signal_series * vol_scalar_series * 100).reindex(tail.index)
                position_history = [None if pd.isna(v) else round(float(v), 1) for v in position_pct_series.values]
                # Share of the vol-target cap in use, historically. position_pct = net_signal *
                # vol_scalar * 100 and max = vol_scalar * 100, so the ratio is just net_signal * 100
                # — the vol_scalar cancels, leaving pure signal-agreement magnitude (±100%).
                util_pct_series = (net_signal_series * 100).reindex(tail.index)
                position_util_history = [None if pd.isna(v) else round(float(v)) for v in util_pct_series.values]
            else:
                position_history = [None] * len(tail)
                position_util_history = [None] * len(tail)

            cta_direction = "Long" if net_signal > 0.05 else ("Short" if net_signal < -0.05 else "Flat")

            # ── Banners (M1 pass only) ──
            # Computed on the spec's prompt column — the series the production
            # strategies trade — which can differ from the displayed M1 card
            # (WTI/HTT/Midland roll on F2 but display F1; Dubai's true prompt
            # is F2 despite its spec saying F1).
            if is_base_tenor:
                try:
                    b_num = 2 if commodity in _BAL_MONTH_M1 else int(front_col[1:])
                    if b_num == col_num:
                        px_b, px_b_raw = px, px_raw
                    else:
                        px_b_raw = prices[f"F{b_num}"].astype(float).dropna()
                        px_b = _roll_clean_series(prices, months, b_num, px_b_raw)
                    if len(px_b_raw) >= 120:
                        _append_banner_signals(
                            commodity, prices, px_b, px_b_raw,
                            hot_signals, recent_trades,
                        )
                except Exception:
                    pass

            # Closest distance for sorting
            dists = [abs(d) for d in [mom_dist, carry_dist] if d is not None]
            closest = min(dists) if dists else 999

            group_cards.append({
                "commodity": commodity,
                "tenor_label": f"M{col_num}",
                "tenor_col": col,
                "dates": dates,
                "prices": values,
                "current": round(current, 4),
                "ma_levels": ma_levels,
                "carry": {"direction": carry_direction, "distance_pct": carry_dist, "level": carry_level},
                "cot": cot_map.get(commodity),
                "cta": {
                    "direction": cta_direction,
                    "net_signal": round(net_signal, 2),
                    # Normalized inverse-vol weights per strategy (keys match
                    # ma_levels labels + "Carry") — lets the frontend
                    # recompute a what-if position for a typed live price.
                    "weights": {n: round(w, 4) for n, w in weights.items()},
                    "position_pct": position_pct,
                    "position_pct_prev": position_pct_prev,
                    "position_chg": position_chg,
                    "vol_scalar": round(vol_scalar, 2),
                    "position_history": position_history,
                    "position_util_history": position_util_history,
                },
                "closest_dist": closest,
            })

        group_cards.sort(key=lambda c: c["closest_dist"])
        groups[group_name] = group_cards

    # ── Spreads (tenor-independent → M1 pass only) ──
    spread_data = {}
    for pairs in (SPREAD_GROUPS.values() if is_base_tenor else []):
        for leg1, leg2 in pairs:
            try:
                prices1 = loader.get_prices_normalized(leg1)
                prices2 = loader.get_prices_normalized(leg2)
                spec1, spec2 = CONTRACT_SPECS[leg1], CONTRACT_SPECS[leg2]
                expiry1 = loader.get_expiry(spec1["ticker"])
                expiry2 = loader.get_expiry(spec2["ticker"])
            except (KeyError, RuntimeError):
                continue

            try:
                # Same shared builder as the Signals cards and the lab's
                # default backtest view — construction, lookback, exit rule
                # and default exposure all come from pair_defaults().
                pair_key = f"{leg1} / {leg2}"

                def _months_or_none(commodity):
                    try:
                        return loader.get_contract_months(commodity)
                    except (KeyError, RuntimeError):
                        return None

                sig_df, rcfg = build_pair_signal_frame(
                    leg1, leg2, prices1, prices2, expiry1, expiry2,
                    months1=_months_or_none(leg1), months2=_months_or_none(leg2),
                    roll_config="prompt_EOM_roll",
                )
                lb, th = rcfg["lookback"], rcfg["entry"]

                tail_sig = sig_df.iloc[-_THREE_MONTHS:]
                latest = sig_df.iloc[-1]
                zscore = float(latest["deviation_pct"]) if pd.notna(latest.get("deviation_pct")) else None
                prev_state = float(latest.get("signal_raw", 0))
                zscore_val = float(latest["deviation_pct"]) if pd.notna(latest.get("deviation_pct")) else None
                live = live_state(prev_state, zscore_val, th, rcfg["exit_threshold"])
                signal = live
                spread_val = float(latest["spread"]) if pd.notna(latest.get("spread")) else None
                spread_mean = float(latest["spread_mean"]) if pd.notna(latest.get("spread_mean")) else None
                spread_std = float(latest["spread_std"]) if pd.notna(latest.get("spread_std")) else None
                upper = float(latest["upper_band"]) if pd.notna(latest.get("upper_band")) else None
                lower = float(latest["lower_band"]) if pd.notna(latest.get("lower_band")) else None
                pair_label = f"{leg1} − {leg2}"

                # Display re-anchoring: the signal series (flow cumsum) drifts
                # from the quoted spread by the accumulated roll carry. Shift
                # every dollar level by TODAY's constant offset so the panel
                # reads in screen-quote dollars (the live-price override then
                # works natively). A constant shift changes no z-score and no
                # level difference — presentation only.
                quoted_val = (float(latest["quoted_spread"])
                              if "quoted_spread" in sig_df.columns
                              and pd.notna(latest.get("quoted_spread")) else None)
                k = (quoted_val - spread_val
                     if quoted_val is not None and spread_val is not None else 0.0)
                if k:
                    spread_val += k
                    spread_mean = spread_mean + k if spread_mean is not None else None
                    upper = upper + k if upper is not None else None
                    lower = lower + k if lower is not None else None

                dates = [d.strftime("%Y-%m-%d") for d in tail_sig.index]
                spread_vals = [round(float(v) + k, 4) if pd.notna(v) else None
                               for v in tail_sig["spread"]]

                direction = "Long" if signal > 0 else ("Short" if signal < 0 else "Flat")

                # Signal flip detection
                for lookback in range(1, _FLIP_LOOKBACK + 1):
                    if len(sig_df) <= lookback:
                        continue
                    prev_row = sig_df.iloc[-lookback - 1]
                    prev_signal = float(prev_row.get("signal_raw", 0))
                    prev_dir = "Long" if prev_signal > 0 else ("Short" if prev_signal < 0 else "Flat")
                    if prev_dir != direction:
                        flip_date = sig_df.index[-lookback].strftime("%Y-%m-%d")
                        flip_spread = (float(sig_df.iloc[-lookback]["spread"]) + k
                                       if pd.notna(sig_df.iloc[-lookback].get("spread")) else None)
                        recent_trades.append({
                            "commodity": pair_label,
                            "strategy": "Mean Reversion",
                            "from": prev_dir,
                            "to": direction,
                            "price": round(flip_spread, 4) if flip_spread else None,
                            "level": round(spread_mean, 4) if spread_mean else None,
                            "date": flip_date,
                            "tier": f"z={round(zscore, 2)}σ" if zscore else None,
                        })
                        break

                # Near-trigger: flat pair within 0.3σ of entry band
                if direction == "Flat" and zscore is not None and abs(zscore) >= th - 0.3:
                    fire_dir = "Short" if zscore > 0 else "Long"
                    sigma_away = round(th - abs(zscore), 2)
                    band = upper if zscore > 0 else lower
                    if band is not None and spread_val is not None:
                        dollar_away = round(abs(band - spread_val), 2)
                        detail = f"${dollar_away:.2f} from {fire_dir}"
                    else:
                        detail = f"{sigma_away:.2f}σ from {fire_dir}"
                    hot_signals.append({
                        "commodity": pair_label,
                        "strategy": "Mean Reversion",
                        "direction": fire_dir,
                        "distance": sigma_away,
                        "detail": detail,
                        "current": round(spread_val, 2) if spread_val is not None else None,
                    })

                # Signal history (+1/0/-1) and zscore history for position chart
                signal_history = []
                zscore_history = []
                for _i, _row in enumerate(tail_sig.itertuples()):
                    _z = getattr(_row, 'deviation_pct', None)
                    zscore_history.append(round(float(_z), 2) if _z is not None and pd.notna(_z) else None)
                    if _i == len(tail_sig) - 1:
                        signal_history.append(float(signal))
                    else:
                        _v = getattr(_row, 'signal_raw', None)
                        signal_history.append(float(_v) if _v is not None and pd.notna(_v) else None)

                # Previous bar's live signal (for daily chg display)
                signal_prev = None
                if len(sig_df) >= 2:
                    prev_state2 = float(sig_df["signal_raw"].iloc[-2]) if pd.notna(sig_df["signal_raw"].iloc[-2]) else 0.0
                    z_prev = float(sig_df["deviation_pct"].iloc[-2]) if pd.notna(sig_df["deviation_pct"].iloc[-2]) else None
                    if z_prev is not None:
                        if prev_state2 == 0.0:
                            signal_prev = -1.0 if z_prev > th else (1.0 if z_prev < -th else 0.0)
                        elif prev_state2 == 1.0:
                            signal_prev = 0.0 if z_prev > -th else 1.0
                        else:
                            signal_prev = 0.0 if z_prev < th else -1.0
                    else:
                        signal_prev = prev_state2

                spread_data[pair_label] = {
                    "dates": dates,
                    "spread": spread_vals,
                    "mean": round(spread_mean, 4) if spread_mean is not None else None,
                    "upper": round(upper, 4) if upper is not None else None,
                    "lower": round(lower, 4) if lower is not None else None,
                    "current": round(spread_val, 4) if spread_val is not None else None,
                    "spread_std": round(spread_std, 4) if spread_std is not None else None,
                    "zscore": round(zscore, 2) if zscore is not None else None,
                    "direction": direction,
                    "in_trade": signal != 0,
                    "lookback": lb,
                    "threshold": th,
                    "signal_history": signal_history,
                    "zscore_history": zscore_history,
                    "signal_prev": signal_prev,
                }
            except Exception:
                continue

    hot_signals.sort(key=lambda r: r["distance"])

    # Deduplicate recent trades: one per commodity per date
    seen = set()
    deduped = []
    recent_trades.sort(key=lambda r: r.get("date", ""), reverse=True)
    for t in recent_trades:
        key = (t["commodity"], t.get("date", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(t)
    recent_trades = deduped[:5]

    result = {
        "tenor": tenor,
        "groups": groups,
        "spreads": spread_data,
        "hot": hot_signals,
        "recent_trades": recent_trades,
    }
    _cache[tenor] = (loader._loaded_at, result)
    return result
