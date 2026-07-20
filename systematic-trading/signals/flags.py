"""
Layer 1 — deterministic rule engine for the AI panel.

Pure functions over a SignalSnapshot. No database access, no model, no I/O:
the snapshot is passed in, fully built, by signals/snapshot.py.

The spec for this module is knowledge/flags/flag_catalog.md.
tests/knowledge/test_flag_catalog_sync.py asserts the two have not diverged,
in both directions, and that parked flags are absent from FLAG_RULES.

Every number a flag asserts appears in its `evidence` dict — that dict is the
contract with the numeric grounding guard (Layer 3). A number not in evidence
(or in the snapshot) is a number the model may not utter.

No rule may condition on anything that happened after signal entry.
Outcome-conditioned logic is banned; see the catalog's Rejected section.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Severity = Literal["info", "warn", "alert"]

_SEVERITY_RANK = {"alert": 0, "warn": 1, "info": 2}


@dataclass(frozen=True)
class Flag:
    id: str
    severity: Severity
    evidence: dict          # every number the flag asserts, as data
    rule_expr: str          # human-readable, e.g. "sharpe_1y / sharpe_all > 2.0"


@dataclass(frozen=True)
class StrategyRow:
    strategy: str               # "Momentum" | "Carry" | "Stat-Arb"
    pair_or_commodity: str      # "WTI" or "WTI / Brent"
    label: str                  # display label, e.g. "WTI / Brent Mean Rev"
    direction: str              # "Long" | "Short" | "Flat"
    sharpe_1y: float | None
    sharpe_all: float | None
    n_trades: int | None        # direction changes; long→short flip = 2 trades
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LiveSignal:
    commodity: str
    strategy: str
    direction: str
    conviction: float | None


@dataclass(frozen=True)
class SignalSnapshot:
    as_of: datetime | None      # tz-aware; latest pull timestamp from prices_daily
    strategies: tuple[StrategyRow, ...] = ()
    live_signals: tuple[LiveSignal, ...] = ()


# ---------------------------------------------------------------------------
# Governed lists
# ---------------------------------------------------------------------------

# Construction verified against raw FlowsDB (CONTRACT_MONTH_YR, real contract
# codes). Allowlist by design: a "known bad" list can only contain what we
# already audited, so it structurally cannot warn about what we haven't
# looked at. This list burns down toward "everything" as audits land.
AUDITED_STRATEGIES: frozenset[tuple[str, str]] = frozenset({
    ("Stat-Arb", "WTI / Brent"),
})

# Configs a strategy must not be sized off. Entries REQUIRE both a reason and
# a replacement — an alert is a verdict, and a verdict requires an audit.
# `config` keys are matched as a subset of the strategy's live config.
DEPRECATED_CONFIGS: list[dict] = [
    {
        "strategy": "Stat-Arb",
        "pair_or_commodity": "WTI / Brent",
        "config": {"lookback": 20, "entry": 1.5},
        "deprecated_on": "2026-07-13",
        "reason": "negative Sharpe once the 2026 event window is excluded",
        "replacement": {"lookback": 90, "entry": 2.0, "month_offset": -1},
    },
]


def _validate_deprecated_configs(entries: list[dict]) -> None:
    """Reject any entry missing reason or replacement. No exceptions."""
    for entry in entries:
        missing = [k for k in ("reason", "replacement") if not entry.get(k)]
        if missing:
            raise ValueError(
                f"DEPRECATED_CONFIGS entry for {entry.get('pair_or_commodity')!r} "
                f"is missing required field(s): {', '.join(missing)}. "
                "A deprecation is a verdict; a verdict requires an audit."
            )


_validate_deprecated_configs(DEPRECATED_CONFIGS)


# ---------------------------------------------------------------------------
# Rules — uniform signature (snapshot, now, config) -> list[Flag]
# ---------------------------------------------------------------------------

_SHARPE_DIVERGENCE_THRESHOLD = 2.0
_MIN_TRADES = 20
_STALE_THRESHOLD_HOURS = 6.0

_KNOWN_IMPACT = (
    "on the one pair audited, three construction bugs (delivery-month "
    "mismatch, roll-flag off-by-one, stitched-level P&L) each independently "
    "inflated Sharpe"
)


def _rule_sharpe_regime_divergence(snapshot: SignalSnapshot, now: datetime,
                                   config: dict) -> list[Flag]:
    flags = []
    for row in snapshot.strategies:
        if row.sharpe_1y is None or row.sharpe_all is None:
            continue
        # Ratio is meaningless unless both are positive; the non-positive
        # all-time case is covered by unaudited_construction/low_trade_count.
        if row.sharpe_1y <= 0 or row.sharpe_all <= 0:
            continue
        ratio = row.sharpe_1y / row.sharpe_all
        if ratio > _SHARPE_DIVERGENCE_THRESHOLD:
            flags.append(Flag(
                id="sharpe_regime_divergence",
                severity="warn",
                evidence={
                    "strategy": row.label,
                    "sharpe_1y": row.sharpe_1y,
                    "sharpe_all": row.sharpe_all,
                    "ratio": round(ratio, 2),
                    "threshold": _SHARPE_DIVERGENCE_THRESHOLD,
                },
                rule_expr="sharpe_1y / sharpe_all > 2.0 (both > 0)",
            ))
    return flags


def _rule_deprecated_config(snapshot: SignalSnapshot, now: datetime,
                            config: dict) -> list[Flag]:
    flags = []
    for row in snapshot.strategies:
        for entry in DEPRECATED_CONFIGS:
            if row.strategy != entry["strategy"]:
                continue
            if row.pair_or_commodity != entry["pair_or_commodity"]:
                continue
            if any(row.config.get(k) != v for k, v in entry["config"].items()):
                continue
            flags.append(Flag(
                id="deprecated_config",
                severity="alert",
                evidence={
                    "strategy": row.label,
                    "config": {k: row.config.get(k) for k in entry["config"]},
                    "deprecated_on": entry["deprecated_on"],
                    "reason": entry["reason"],
                    "replacement": entry["replacement"],
                },
                rule_expr="strategy.config in DEPRECATED_CONFIGS",
            ))
    return flags


def _rule_unaudited_construction(snapshot: SignalSnapshot, now: datetime,
                                 config: dict) -> list[Flag]:
    flags = []
    seen: set[tuple[str, str]] = set()
    for row in snapshot.strategies:
        key = (row.strategy, row.pair_or_commodity)
        if key in AUDITED_STRATEGIES or key in seen:
            continue
        seen.add(key)   # one flag per instrument, not per parameter variant
        flags.append(Flag(
            id="unaudited_construction",
            severity="warn",
            evidence={
                "strategy": f"{row.pair_or_commodity} {row.strategy}",
                "audited": False,
                "reference_audit": "wti_brent",
                "known_impact": _KNOWN_IMPACT,
            },
            rule_expr="strategy not in AUDITED_STRATEGIES",
        ))
    return flags


def _rule_stale_price(snapshot: SignalSnapshot, now: datetime,
                      config: dict) -> list[Flag]:
    if snapshot.as_of is None:
        return []
    threshold = config.get("stale_threshold_hours", _STALE_THRESHOLD_HOURS)
    age_hours = (now - snapshot.as_of).total_seconds() / 3600.0
    if age_hours > threshold:
        return [Flag(
            id="stale_price",
            severity="alert",
            evidence={
                "as_of": snapshot.as_of.isoformat(),
                "age_hours": round(age_hours, 1),
                "threshold_hours": threshold,
            },
            rule_expr=f"now - snapshot.as_of > {threshold}h",
        )]
    return []


def _rule_low_trade_count(snapshot: SignalSnapshot, now: datetime,
                          config: dict) -> list[Flag]:
    flags = []
    for row in snapshot.strategies:
        sharpe_shown = row.sharpe_1y if row.sharpe_1y is not None else row.sharpe_all
        if sharpe_shown is None:        # not displaying a Sharpe
            continue
        if row.n_trades is None:
            continue
        if row.n_trades < _MIN_TRADES:
            flags.append(Flag(
                id="low_trade_count",
                severity="info",
                evidence={
                    "strategy": row.label,
                    "n_trades": row.n_trades,
                    "threshold": _MIN_TRADES,
                    "sharpe_shown": sharpe_shown,
                },
                rule_expr="n_trades < 20",
            ))
    return flags


# Registry — the sync test compares these ids against the flag catalog.
FLAG_RULES = {
    "sharpe_regime_divergence": _rule_sharpe_regime_divergence,
    "deprecated_config": _rule_deprecated_config,
    "unaudited_construction": _rule_unaudited_construction,
    "stale_price": _rule_stale_price,
    "low_trade_count": _rule_low_trade_count,
}


def evaluate(snapshot: SignalSnapshot, *, now: datetime | None = None,
             stale_threshold_hours: float = _STALE_THRESHOLD_HOURS) -> list[Flag]:
    """Run every rule over the snapshot. Returns flags, alerts first."""
    if now is None:
        now = datetime.now(timezone.utc)
    config = {"stale_threshold_hours": stale_threshold_hours}
    flags: list[Flag] = []
    for rule in FLAG_RULES.values():
        flags.extend(rule(snapshot, now, config))
    flags.sort(key=lambda f: _SEVERITY_RANK[f.severity])
    return flags
