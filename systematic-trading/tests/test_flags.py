"""
Per-flag unit tests: fires, does not fire, boundary at threshold.

All thresholds are strict comparisons — the boundary cases assert that a
value exactly AT the threshold does not fire.
"""

from datetime import datetime, timedelta, timezone

import pytest

from signals.flags import (
    DEPRECATED_CONFIGS,
    FLAG_RULES,
    Flag,
    SignalSnapshot,
    StrategyRow,
    _validate_deprecated_configs,
    evaluate,
)

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
FRESH = NOW - timedelta(hours=1)

# The one audited instrument — rows built from this don't trip
# unaudited_construction, which keeps single-flag tests isolated.
AUDITED = {"strategy": "Stat-Arb", "pair_or_commodity": "WTI / Brent",
           "label": "WTI / Brent Mean Rev"}

# The corrected live config — doesn't trip deprecated_config.
LIVE_CONFIG = {"lookback": 90, "entry": 2.0, "month_offset": -1,
               "exit_mode": "mean_cross", "vol_scalar_cap": 4.0}

DEPRECATED = {"lookback": 20, "entry": 1.5, "month_offset": 0,
              "exit_mode": "mean_cross"}


def row(**overrides) -> StrategyRow:
    base = dict(**AUDITED, direction="Short", sharpe_1y=0.5, sharpe_all=0.5,
                n_trades=50, config=dict(LIVE_CONFIG))
    base.update(overrides)
    return StrategyRow(**base)


def snap(*rows, as_of=FRESH) -> SignalSnapshot:
    return SignalSnapshot(as_of=as_of, strategies=tuple(rows))


def fired(snapshot, flag_id, **kwargs) -> list[Flag]:
    return [f for f in evaluate(snapshot, now=NOW, **kwargs) if f.id == flag_id]


# ---------------------------------------------------------------------------
# sharpe_regime_divergence — sharpe_1y / sharpe_all > 2.0, both > 0
# ---------------------------------------------------------------------------

class TestSharpeRegimeDivergence:
    def test_fires(self):
        flags = fired(snap(row(sharpe_1y=2.18, sharpe_all=0.67)),
                      "sharpe_regime_divergence")
        assert len(flags) == 1
        ev = flags[0].evidence
        assert ev["sharpe_1y"] == 2.18
        assert ev["sharpe_all"] == 0.67
        assert ev["ratio"] == 3.25
        assert ev["threshold"] == 2.0
        assert flags[0].severity == "warn"

    def test_does_not_fire(self):
        assert not fired(snap(row(sharpe_1y=1.2, sharpe_all=0.9)),
                         "sharpe_regime_divergence")

    def test_boundary_exactly_2x_does_not_fire(self):
        assert not fired(snap(row(sharpe_1y=2.0, sharpe_all=1.0)),
                         "sharpe_regime_divergence")

    def test_negative_all_time_does_not_fire(self):
        assert not fired(snap(row(sharpe_1y=1.5, sharpe_all=-0.5)),
                         "sharpe_regime_divergence")

    def test_zero_all_time_does_not_fire(self):
        # also guards against ZeroDivisionError
        assert not fired(snap(row(sharpe_1y=1.5, sharpe_all=0.0)),
                         "sharpe_regime_divergence")

    def test_missing_sharpe_does_not_fire(self):
        assert not fired(snap(row(sharpe_1y=None, sharpe_all=None)),
                         "sharpe_regime_divergence")


# ---------------------------------------------------------------------------
# deprecated_config — subset match against DEPRECATED_CONFIGS
# ---------------------------------------------------------------------------

class TestDeprecatedConfig:
    def test_fires_on_old_wti_brent_default(self):
        flags = fired(snap(row(config=dict(DEPRECATED))), "deprecated_config")
        assert len(flags) == 1
        ev = flags[0].evidence
        assert ev["config"] == {"lookback": 20, "entry": 1.5}
        assert ev["reason"] == "negative Sharpe once the 2026 event window is excluded"
        assert ev["replacement"] == {"lookback": 90, "entry": 2.0, "month_offset": -1}
        assert ev["deprecated_on"] == "2026-07-13"
        assert flags[0].severity == "alert"

    def test_does_not_fire_on_live_config(self):
        assert not fired(snap(row(config=dict(LIVE_CONFIG))), "deprecated_config")

    def test_partial_match_does_not_fire(self):
        # boundary: one of the two deprecated keys differs
        assert not fired(snap(row(config={"lookback": 20, "entry": 2.0})),
                         "deprecated_config")

    def test_same_config_other_pair_does_not_fire(self):
        r = row(pair_or_commodity="Brent / Dubai", label="Brent / Dubai Mean Rev",
                config=dict(DEPRECATED))
        assert not fired(snap(r), "deprecated_config")

    def test_entry_missing_reason_rejected(self):
        with pytest.raises(ValueError, match="reason"):
            _validate_deprecated_configs([{"pair_or_commodity": "X",
                                           "replacement": {"lookback": 90}}])

    def test_entry_missing_replacement_rejected(self):
        with pytest.raises(ValueError, match="replacement"):
            _validate_deprecated_configs([{"pair_or_commodity": "X",
                                           "reason": "because"}])

    def test_registered_entries_are_valid(self):
        _validate_deprecated_configs(DEPRECATED_CONFIGS)


# ---------------------------------------------------------------------------
# unaudited_construction — allowlist, deduped per instrument
# ---------------------------------------------------------------------------

class TestUnauditedConstruction:
    def test_audited_pair_does_not_fire(self):
        assert not fired(snap(row()), "unaudited_construction")

    def test_unaudited_pair_fires(self):
        r = row(pair_or_commodity="Brent / Dubai", label="Brent / Dubai Mean Rev")
        flags = fired(snap(r), "unaudited_construction")
        assert len(flags) == 1
        ev = flags[0].evidence
        assert ev["audited"] is False
        assert ev["reference_audit"] == "wti_brent"
        assert "delivery-month mismatch" in ev["known_impact"]
        assert flags[0].severity == "warn"

    def test_outrights_fire_too(self):
        r = row(strategy="Momentum", pair_or_commodity="WTI",
                label="WTI Mom (Fast)")
        assert len(fired(snap(r), "unaudited_construction")) == 1

    def test_deduped_per_instrument_not_per_tier(self):
        tiers = [row(strategy="Momentum", pair_or_commodity="WTI",
                     label=f"WTI Mom ({t})") for t in ("Fast", "Slow", "Averaged")]
        assert len(fired(snap(*tiers), "unaudited_construction")) == 1

    def test_distinct_instruments_fire_separately(self):
        r1 = row(strategy="Momentum", pair_or_commodity="WTI", label="WTI Mom")
        r2 = row(strategy="Carry", pair_or_commodity="WTI", label="WTI Carry")
        assert len(fired(snap(r1, r2), "unaudited_construction")) == 2


# ---------------------------------------------------------------------------
# stale_price — now - as_of > threshold (default 6h)
# ---------------------------------------------------------------------------

class TestStalePrice:
    def test_fires_when_stale(self):
        flags = fired(snap(row(), as_of=NOW - timedelta(hours=7.5)), "stale_price")
        assert len(flags) == 1
        ev = flags[0].evidence
        assert ev["age_hours"] == 7.5
        assert ev["threshold_hours"] == 6.0
        assert flags[0].severity == "alert"

    def test_does_not_fire_when_fresh(self):
        assert not fired(snap(row(), as_of=NOW - timedelta(hours=3)), "stale_price")

    def test_boundary_exactly_6h_does_not_fire(self):
        assert not fired(snap(row(), as_of=NOW - timedelta(hours=6)), "stale_price")

    def test_threshold_configurable_per_page(self):
        as_of = NOW - timedelta(hours=30)
        assert fired(snap(row(), as_of=as_of), "stale_price")
        assert not fired(snap(row(), as_of=as_of), "stale_price",
                         stale_threshold_hours=192.0)   # COT weekly cadence

    def test_missing_as_of_does_not_fire(self):
        assert not fired(snap(row(), as_of=None), "stale_price")


# ---------------------------------------------------------------------------
# low_trade_count — n_trades < 20 on any strategy displaying a Sharpe
# ---------------------------------------------------------------------------

class TestLowTradeCount:
    def test_fires(self):
        flags = fired(snap(row(n_trades=11, sharpe_1y=2.18)), "low_trade_count")
        assert len(flags) == 1
        ev = flags[0].evidence
        assert ev["n_trades"] == 11
        assert ev["threshold"] == 20
        assert ev["sharpe_shown"] == 2.18
        assert flags[0].severity == "info"

    def test_does_not_fire_with_enough_trades(self):
        assert not fired(snap(row(n_trades=80)), "low_trade_count")

    def test_boundary_exactly_20_does_not_fire(self):
        assert not fired(snap(row(n_trades=20)), "low_trade_count")

    def test_19_fires(self):
        assert fired(snap(row(n_trades=19)), "low_trade_count")

    def test_no_sharpe_displayed_does_not_fire(self):
        assert not fired(snap(row(n_trades=5, sharpe_1y=None, sharpe_all=None)),
                         "low_trade_count")

    def test_falls_back_to_all_time_sharpe(self):
        flags = fired(snap(row(n_trades=5, sharpe_1y=None, sharpe_all=0.4)),
                      "low_trade_count")
        assert flags[0].evidence["sharpe_shown"] == 0.4

    def test_missing_n_trades_does_not_fire(self):
        assert not fired(snap(row(n_trades=None)), "low_trade_count")


# ---------------------------------------------------------------------------
# evaluate() — ordering and registry consistency
# ---------------------------------------------------------------------------

class TestEvaluate:
    def test_alerts_sort_first(self):
        r = row(config=dict(DEPRECATED), sharpe_1y=2.18, sharpe_all=0.67,
                n_trades=11)
        flags = evaluate(snap(r, as_of=NOW - timedelta(hours=30)), now=NOW)
        severities = [f.severity for f in flags]
        assert severities == sorted(severities,
                                    key=["alert", "warn", "info"].index)
        assert severities[0] == "alert"

    def test_all_ids_registered(self):
        r = row(pair_or_commodity="Brent / Dubai", label="Brent / Dubai Mean Rev",
                config=dict(DEPRECATED), sharpe_1y=2.18, sharpe_all=0.67,
                n_trades=11)
        flags = evaluate(snap(r, as_of=NOW - timedelta(hours=30)), now=NOW)
        assert {f.id for f in flags} <= set(FLAG_RULES)

    def test_empty_snapshot_no_flags(self):
        assert evaluate(snap(as_of=FRESH), now=NOW) == []
