---
id: flag_catalog
title: Flag catalog
pages: [signals, levels, cot]
flags: []
status: draft
owner: mark
last_verified: 2026-07-14
verify_test: tests/knowledge/test_flag_catalog_sync.py
---

# Flag catalog

Every deterministic rule in `signals/flags.py`. This file is the spec; the code is the
implementation; `test_flag_catalog_sync.py` asserts they have not diverged (every live
flag id here appears in the code, every id in the code appears here, and **parked flags
are absent from the code** — all enforced both directions).

**A flag is a rule, not an observation.** If you cannot write it as a boolean over the
snapshot, it does not belong here — and it does not belong in the commentary either.

Severity levels:
- `info` — worth knowing, no action implied
- `warn` — the number on screen is likely to be misread without context
- `alert` — the number on screen is actively misleading; do not size off it

---

## `sharpe_regime_divergence`

**Rule:** `sharpe_1y / sharpe_all > 2.0`, evaluated only when **both Sharpes are
positive.** A non-positive all-time Sharpe makes the ratio meaningless; that case is
left to `unaudited_construction` and `low_trade_count` rather than given invented
semantics here.

**Severity:** `warn`

**Evidence:**
```json
{"strategy": "WTI / Brent Mean Rev", "sharpe_1y": 2.18, "sharpe_all": 0.67,
 "ratio": 3.25, "threshold": 2.0}
```

**Why it matters:** A strategy at the top of the 1Y board and the middle of the
all-time board is not a strategy that improved — it is a strategy that had an event.
The gap is the event. The leaderboard sorts by 1Y Sharpe, so the event *promotes itself
to the front page*, which is the opposite of what you want.

This is not hypothetical for us. WTI–Brent sits at #1 on the 1Y board and #3 all-time;
the gap is substantially attributable to the 2026 Iran-conflict window, and the
attribution work showed the large majority of that gain came from the vol-targeting
loop scaling the position up, not from the signal firing. See `wti_brent.md` and
`vol_targeting.md`.

**TODO(mark):** threshold of 2.0 is a first guess. Consider whether this should be
sample-size aware (a 1Y Sharpe on 8 trades is a different object from one on 80).

---

## `deprecated_config`

**Rule:** `strategy.config in DEPRECATED_CONFIGS` (subset match: an entry matches when
every key/value it specifies equals the strategy's live config for that instrument).

**Severity:** `alert`

**Evidence:**
```json
{"strategy": "WTI / Brent Mean Rev", "config": {"lookback": 20, "entry": 1.5},
 "deprecated_on": "2026-07-13", "reason": "negative Sharpe once the 2026 event window is excluded",
 "replacement": {"lookback": 90, "entry": 2.0, "month_offset": -1}}
```

**Why it matters:** This is the flag that exists because we shipped the bug. The old
production default (lb20 / 1.5σ) produces negative Sharpe once the windfall window is
excluded. The live default was corrected to the replacement above on 2026-07-13; this
flag guards against the old config being re-selected (the spread endpoints still accept
arbitrary lookback/threshold overrides).

`DEPRECATED_CONFIGS` is a list in code with a **required** `reason` and `replacement`.
An entry missing either is rejected at import — no exceptions, no "unaudited" sub-case.
`alert` means "do not size off this," which is a verdict, and a verdict requires an
audit. Only one entry exists: WTI–Brent at lb20 / 1.5σ. Strategies that have never been
audited are covered by `unaudited_construction`, not this list.

**TODO(mark):** whether the right move is to flag the old config or remove the override
path entirely. Flagging is the interim; removal is the fix.

---

## `unaudited_construction`

**Rule:** `strategy not in AUDITED_STRATEGIES`

**Severity:** `warn`

**Evidence:**
```json
{"strategy": "Brent / Dubai Stat-Arb", "audited": false,
 "reference_audit": "wti_brent",
 "known_impact": "on the one pair audited, three construction bugs (delivery-month mismatch, roll-flag off-by-one, stitched-level P&L) each independently inflated Sharpe"}
```

**Why it matters:** `AUDITED_STRATEGIES` is an **allowlist** — strategies whose
construction has been verified against raw FlowsDB. Right now that is WTI–Brent and
nothing else. Everything not on the list flags by default.

The direction of this list matters. A "known bad" list can only ever contain things we
already audited, so it structurally cannot warn about what we haven't looked at. An
allowlist can. It burns down toward empty as pairs get audited.

Applies to **all** strategies, outrights included. One flag per instrument, not per
parameter variant — WTI momentum flags once, not once per speed tier.

---

## `stale_price`

**Rule:** `now - snapshot.as_of > 6 hours` (threshold configurable per page; COT should
get a threshold matched to its weekly publication cadence when wired up in Phase 2)

**Severity:** `alert`

**Evidence:**
```json
{"as_of": "2026-07-13T09:21:00-05:00", "age_hours": 27.5, "threshold_hours": 6}
```

**Why it matters:** Trivial, and the most likely flag to actually save someone. The
page header shows a timestamp; nobody reads the page header.

---

## `low_trade_count`

**Rule:** `n_trades < 20` on any strategy displaying a Sharpe. `n_trades` is the
**count of direction changes**, not exits to flat: a long→short flip without passing
through flat is two trades, not one. Computed upstream in the snapshot builder, not
inside `flags.py`.

**Severity:** `info`

**Evidence:**
```json
{"strategy": "...", "n_trades": 11, "threshold": 20, "sharpe_shown": 2.18}
```

**Why it matters:** Raising the entry threshold to 2.5–3.0σ cuts time-in-market
sharply, which is the point — but it also means the Sharpe on screen rests on very few
trades. The number is not wrong; it is *thin*, and thin is a different thing to
communicate than wrong. This is also why stops remain untested at high thresholds.

---

## Parked

Specified but deliberately not implemented. Parked flags must **not** appear in
`signals/flags.py` — the sync test enforces this. They are kept here with their reasons
so they do not get re-proposed in their premature form.

### `factor_concentration` — parked 2026-07-14

Requires a factor map, and the map would be an assertion rather than a measurement.
Spreads don't fit it cleanly. It is not a flag until the groupings can be derived from
rolling correlations of realised signal returns. When that exists, the original spec
(same-sign share on a common factor > 0.70, severity `warn`) is the starting point.

### `carry_drag` — parked 2026-07-14

The snapshot has no carry fields, so it could never fire — and a flag that cannot fire
is worse than an absent one: it looks like coverage. Lands when the direction-split
carry analysis does, and becomes **direction-aware** at that time (long-spread pays
carry structurally; whether short-spread collects it is untested — see
`methodology/roll_carry.md`). Original spec: `expected_roll_carry / gross_expectancy >
0.50`, severity `warn`.

---

## Rejected rules

Kept here deliberately, so they do not get re-proposed.

- **"Regime detection"** — any rule that buckets days by what the spread did *after*
  entry is outcome-conditioned and contaminates both the attribution and the flag. We
  already made this mistake once and the corrected result was the opposite sign. If a
  regime flag is ever built, the regime must be defined from information available
  *before* entry, with no exceptions.
- **"Unusual correlation" / "divergence from historical relationship"** — this is the
  model-noticing-things pattern in a costume. If the relationship is worth flagging,
  write the rule.
