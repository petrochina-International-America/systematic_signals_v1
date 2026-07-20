---
id: glossary
title: Glossary and house conventions
pages: [signals, levels, cot]
flags: []
status: draft
owner: mark
last_verified: 2026-07-14
verify_test: null
---

# Glossary and house conventions

Loaded on every page. Short by design — this is what the prose layer needs so it does
not misuse a term in front of a trader who will notice immediately.

## Terms

**Offset (spread construction)** — the delivery-month relationship between the two legs.
Offset 0 = same delivery month. Offset -1 = first leg one month *earlier* than the
second. See `methodology/wti_brent.md`; getting this wrong has been an actual source of
bugs, not a pedantic point.

**Backwardation / contango** — front above deferred / front below deferred. The carry
column on an outright card reports which state the curve is in, and the sign of the
carry signal follows from it.

**Time-in-market** — share of days the strategy holds a position. Distinct from trade
count. Raising entry thresholds cuts time-in-market sharply while leaving total P&L
roughly intact; this is a headline result, not a footnote.

**Mean-cross exit** — exit when the z-score crosses zero, as opposed to a fixed profit
target or a symmetric band. This is the exit rule in the validated region.

**Gross vs net expectancy** — gross excludes roll carry. On spreads with structural
carry (WTI-Brent long side) the gap is large and the gross number is not achievable.

## Conventions the prose layer must observe

1. **Never call a rolling-z-score reversion signal "statistical arbitrage."** We have not
   demonstrated cointegration and we do not claim it.
2. **Never describe a Sharpe without its sample.** "Sharpe 2.18" is meaningless without
   "1Y, n trades, this config."
3. **Never attribute event-window P&L to a signal** without the sizing decomposition.
4. **Never assert a result marked "untested"** in a methodology file, even where the
   mechanism is compelling. Mechanism is not evidence.
5. **Say "we have not tested this" rather than hedging vaguely.** A trader can work with
   a stated gap. A trader cannot work with a hedge that might be modesty and might be
   ignorance.

## The standing instruction

Commentary explains flags. Commentary does not find patterns, does not infer causation
from co-movement, and does not compute. If something is worth flagging, it gets a rule in
`signals/flags.py`.
