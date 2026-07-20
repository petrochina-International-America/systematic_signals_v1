---
id: vol_targeting
title: Vol targeting and mean reversion
pages: [signals]
flags: [sharpe_regime_divergence]
status: draft
owner: mark
last_verified: 2026-07-14
verify_test: tests/knowledge/test_vol_targeting_claims.py
---

# Vol targeting and mean reversion are structurally opposed

## The mechanism

A standard vol-targeting loop scales position size inversely to realised vol: when vol
is low, leverage goes up; when vol is high, leverage comes down. For a trend-following
strategy this is correct — it equalises risk contribution across regimes.

**For a mean-reversion strategy it is backwards.**

The reversion signal wants to be *large* when the spread is dislocated, which is
precisely when realised vol is high — and the loop is cutting size there. Conversely it
raises leverage through calm periods, so the book is carrying maximum notional at the
exact moment the spread is most likely to break out of its range. The loop is not
neutral-but-suboptimal; it is anti-correlated with the signal's own edge.

## What it did to our numbers

The 2026 Iran-conflict window produced a large gain on WTI-Brent. Attribution found the
large majority of that gain was **sizing amplification, not signal** — the loop had
levered up through the preceding calm and was carrying an oversized position when the
move came.

That is a windfall, not an edge. It is repeatable only in the sense that a coin is
repeatable.

**Consequence for the prose layer:** never attribute the Iran-window P&L to the signal.
If the commentary is explaining `sharpe_regime_divergence` on WTI-Brent, the sizing
mechanism is the explanation.

## Status

The loop is still live and still backwards. Redesign is open work: the direction of
travel is to invert the scalar so that size *falls* in calm periods and *rises* into
dislocation, subject to a hard notional cap so the tail does not eat the book.

**TODO(mark):** decide whether the interim move is to disable the loop entirely (flat
sizing) rather than run a mechanism we know is inverted. Flat is wrong but honestly
wrong; inverted is wrong and flattering.
