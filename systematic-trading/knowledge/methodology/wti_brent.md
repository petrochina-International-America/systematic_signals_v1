---
id: wti_brent
title: WTI–Brent mean reversion
pages: [signals, levels]
flags: [sharpe_regime_divergence, deprecated_config, unaudited_construction, low_trade_count]
status: draft
owner: mark
last_verified: 2026-07-14
verify_test: tests/knowledge/test_wti_brent_claims.py
---

# WTI–Brent mean reversion

The most heavily audited strategy on the platform, and the one most likely to be
misread from the dashboard as it currently stands.

## What the signal is

A rolling z-score on the WTI–Brent spread, entering when the z-score exceeds a
threshold and exiting on a mean cross.

**It is a reversion signal on a spread. It is not a formal statistical arbitrage.**
Rolling z-score stationarity does not demonstrate cointegration, and we do not claim
it. The operative proof is backtest performance under correct construction, not a
stationarity test. The prose layer must not describe this as "stat arb" or imply a
cointegration result.

## Offset convention — this is the part people get wrong

**Offset −1** means WTI is one delivery month *earlier* than Brent. This matches the
CME BVX product and the physical shipping-arb convention: a cargo lifted on the WTI
month arrives into the Brent market roughly a month later.

**Offset 0** (same delivery month) is the intuitive construction and it is the wrong
one. Under corrected construction it shows no robust edge.

The edge that survives audit is on offset −1, in a stable parameter region:
longer lookbacks (roughly 90–250 days) and higher entry thresholds (roughly 2.0–3.0σ),
with a mean-cross exit. "Stable" here means the result does not depend on landing on a
particular parameter — it holds across a contiguous region, which is the only kind of
parameter result worth anything.

## Construction bugs (fixed — do not reintroduce)

Three bugs were found and corrected. Each independently inflated Sharpe. They are
recorded here because they are the failure modes any future spread strategy on this
platform will re-encounter.

1. **Delivery-month mismatch.** The two legs were rolled by independently ranking each
   leg's contracts, which silently produced leg pairs that were not on the intended
   offset. The fix is to select the Brent leg *relative to* the WTI leg's delivery
   month, not to rank them separately.
2. **Roll flag consumed one bar late.** The roll-timing flag was read on the bar after
   the roll, so one day of P&L was attributed to the wrong contract pair.
3. **Spread P&L from stitched price levels.** P&L was computed by differencing a
   stitched spread series, which books the roll gap as a return. Spread P&L must be
   built from **leg-level daily flows** and then netted.

**The general lesson, which generalises past this strategy:** independent
re-derivation from raw FlowsDB (`CONTRACT_MONTH_YR`) against real contract codes is
the only verification that counts. Code review and plausibility arguments did not catch
any of these.

## What the dashboard currently shows, and why it is misleading

WTI–Brent is **#1 on the 1Y Sharpe leaderboard and #3 all-time.** That gap is the 2026
Iran-conflict window. The attribution work found the large majority of that gain came
from the vol-targeting loop scaling the position up into the move, not from the signal
itself — see `vol_targeting.md`.

The production default config (lb20 / 1.5σ) is stale and produces negative Sharpe once
the windfall window is excluded.

**Consequences for the prose layer:** when `sharpe_regime_divergence` or
`deprecated_config` fires on this strategy, the commentary should say plainly that the
headline Sharpe reflects a single event window and a deprecated config, and point at
the offset −1 stable region as the live result. It should not soften this.

## The finding that actually matters for the desk

Raising the entry threshold from 1.5σ to 3.0σ cuts time-in-market from roughly 65% to
roughly 15% **without reducing total P&L.** Same money, a fraction of the exposure.
This is the single most actionable result on the strategy and it should be what Chen
takes away from the card.

The trade-off is trade count: at 2.5σ+ there are too few trades to test stops, which is
why `low_trade_count` exists as an `info` flag rather than being hidden.

## Regime behaviour

Corrected, episode-level attribution shows the strategy **makes money in dislocation
episodes and bleeds in calm periods.**

An earlier version of this analysis found the opposite. It was wrong because it bucketed
days by what the spread did *after* entry — outcome-conditioned bucketing, which
contaminates the result. This is recorded in `flag_catalog.md` under rejected rules.
Do not let any future regime work reintroduce it.

## Known regime break

Ilia's published WTI–Brent result replicates cleanly inside his sample window
(Jan 2016 – Jun 2020) and is dead after July 2020, in the US export / backwardation era.
The break is real and structurally motivated, not a fitting artifact. Any claim about
this strategy that draws on the pre-2020 sample must say which era it is talking about.

---

**TODO(mark):** confirm the exact lookback/threshold you want quoted as the live
config, and whether Chen's card should default to it directly.
