---
id: roll_carry
title: Roll carry as a structural cost
pages: [signals]
flags: []
status: draft
owner: mark
last_verified: 2026-07-14
verify_test: tests/knowledge/test_roll_carry_claims.py
---

# Roll carry is structural, not noise

## Why the asymmetry exists

WTI is landlocked (Cushing delivery) and therefore contango-prone: when storage fills,
the front collapses relative to the deferred. Brent is waterborne and
backwardation-prone: floating storage and export optionality keep the front supported.

The result is a persistent, *directional* term-structure asymmetry between the two legs.
This is a physical fact about where the barrels sit, not a market anomaly that arbs
away.

## Why you cannot dodge it

The physical half-life of WTI-Brent reversion is roughly 48 days. The roll cycle is
roughly 21 days. **The reversion outlives the contract.**

That means the strategy must roll through to capture the reversion — it cannot hold to
expiry and exit at roll to avoid carry. Carry is a cost of doing business on this trade,
and it must appear on the card next to the gross number.

## Magnitude

On the production config, roll carry has run at roughly 249 bps/year against a gross
reversion of similar order — a very large share of the gross. A card that shows gross
reversion without the carry line is showing a number the trade will not realise.

## The open question

Long-spread trades persistently *pay* carry. The structural argument says short-spread
trades should persistently *collect* it — but **this has not been tested.** The
direction-split carry analysis is the last open structural question on this signal.

**The prose layer must not assert that the short side collects carry.** It is a
hypothesis with a good mechanism and no evidence. If the commentary needs to mention it,
it says "untested."

**TODO(mark):** this file should be updated the moment the direction-split lands, and
`carry_drag` in the flag catalog should become direction-aware at the same time.
