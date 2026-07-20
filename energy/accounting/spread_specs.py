# energy/accounting/spread_specs.py
"""
Cross-commodity SPREAD construction specs.

This is the spread-level counterpart of CONTRACT_SPECS: where CONTRACT_SPECS'
roll configs (eom_mid, eom_dynamic, ...) govern how a single commodity's own
curve rolls (outright signals: momentum, carry), SPREAD_SPECS governs how a
two-legged cross-commodity spread is constructed and rolled as one coordinated
position. The two families are deliberately separate — outright roll styles
are never reused, renamed, or affected by anything here.

spread_style
------------
leg_matched     (default) two individual leg positions keyed to one shared
                delivery-month state variable X: leg1 holds X + month_offset,
                leg2 holds X, and both legs roll simultaneously on the shared
                trigger. Default construction for any pair without a listed
                single-ticket spread instrument.
spread_matched  same state variable and same trigger, treated as a single
                packaged instrument (where a listed spread exists — e.g. the
                ICE WTI-Brent "Bullet"; crack spreads are same-delivery-month
                by definition). Secondary/alternative option; also what we
                validate against Bloomberg's S:XXYY cross spread series.

The old construction — each leg rolled independently on its own rank-based
(F-number) schedule with zero coordination — produced delivery-month drift
(23.6% of held days mismatched for WTI-Brent in the Jul–Dec 2022 audit, in
two opposite-direction phases per cycle) and is fully disbanded as a
selectable option. It survives only as
energy.strategies.spread_rolling.legacy_uncoordinated_reference(), a static
historical reference for re-validation comparisons.

roll_trigger_style
------------------
Two distinct philosophies, not one knob tuned differently:

prior_month_eom   (default) roll at the last trading day of the month BEFORE
                  the binding leg's expiry month — a full calendar checkpoint
                  ahead of the binding contract's own expiry month even
                  beginning. Deliberately decoupled from tracking the binding
                  LTD precisely: trades tracking precision (distance from the
                  true prompt spread) for execution safety (never anywhere
                  near the expiry-period liquidity dynamics). The checkpoint
                  month is derived from the expiry calendar's own dates, not
                  from hardcoded month arithmetic.
liquidity_buffer  roll `roll_buffer_days` trading days before the binding
                  leg's last trading day. Empirically grounded: Brent's
                  front/next volume crossover is median 4 (p75 6) trading
                  days before its LTD (198 cycles, 2010–2026), so the default
                  of 5 rolls right at the liquidity handover while staying
                  clear of the final-two-day collapse. Closer to the true
                  prompt spread than prior_month_eom, but operates inside the
                  expiry-period zone.

synced_eom        (investigative — not selectable via a pair's own spec
                  default) last trading day of calendar month
                  (X - SYNCED_EOM_LEAD_MONTHS), a pure function of the
                  delivery month alone with NO dependency on any leg's own
                  LTD or on which pair/construction is asking — every leg in
                  every pair gets the identical trigger for the same X. Built
                  to test whether prior_month_eom's per-pair "binding leg"
                  concept (below) is itself a source of cross-construction
                  tracking error: two constructions sharing a leg (e.g. a
                  cross-commodity pair and that same leg's own single-
                  commodity calendar spread) can get DIFFERENT trigger dates
                  for that leg under prior_month_eom, because "binding" is
                  computed per-pairing, not per-leg. See
                  energy.strategies.spread_rolling.verify_synced_eom_safe.

"binding leg" = whichever leg's anchor contract has the earlier last trading
day, taken as min(leg1 LTD, leg2 LTD) per delivery month — Brent for
WTI-Brent, WTI for the cracks. Never hardcoded per pair. (Only meaningful for
prior_month_eom/liquidity_buffer; synced_eom has no binding-leg concept.)

precision_mode
--------------
strict_delivery_match  every held day is validated: the legs' delivery months
                  must differ by exactly month_offset (0 = matched). The
                  validator raises immediately on unexpected drift — the
                  safeguard against the original bug creeping back in.
rank_approximate  the pair's settlement mechanics don't support precise
                  per-day delivery-month validation (Dubai is a
                  calendar-month-average cash-settled swap with its own
                  independent expiry — structurally NOT a two-leg futures
                  package; NGL swaps similar). Falls back to the per-leg
                  rank-based construction, holding a deferred tenor as the
                  safe proxy. This flag is surfaced in every downstream
                  output (signal snapshots, lab results, sizing panels) so a
                  rank-approximated pair is never presented with the same
                  tracking-precision implication as a validated one.

month_offset
------------
0 = delivery-month matched. A nonzero value is the deliberately-chosen
cross-arb variant (e.g. WTI(X+1) vs Brent(X)) — same state-variable/trigger
machinery, validated against "offset == chosen constant" instead of 0.
Clearly distinct from both matched (0) and from the disbanded uncoordinated
drift, which had no fixed offset at all.

deferred_rank  (rank_approximate pairs only)
----------------------------------------------
Hold no closer than this many ranks from the front (2 = never hold the front
month). More ranks = more safety margin, but further from the actual prompt
spread the signal may want — that tradeoff is deliberate and should stay
visible wherever the pair is reported.
"""

DEFAULT_SPREAD_STYLE = "leg_matched"
DEFAULT_TRIGGER_STYLE = "prior_month_eom"

SPREAD_STYLES = ("leg_matched", "spread_matched")
TRIGGER_STYLES = ("prior_month_eom", "liquidity_buffer", "synced_eom")
PRECISION_MODES = ("strict_delivery_match", "rank_approximate")

SPREAD_SPECS = {
    # ── Location ─────────────────────────────────────────────────────────────
    "WTI / Brent": {
        "legs": ("WTI", "Brent"),
        "spread_style": "leg_matched",          # default construction
        "alt_styles": ["spread_matched"],       # listed instrument exists (ICE Bullet)
        "precision_mode": "strict_delivery_match",
        "roll_trigger_style": "prior_month_eom",
        "roll_buffer_days": 5,                  # liquidity_buffer override only
        "month_offset": 0,
        "validate_ticker": "S:ENCO",            # Bloomberg BBG_SP_XCOMM family
    },
    "Brent / Dubai": {
        "legs": ("Brent", "Dubai"),
        "spread_style": "leg_matched",
        "alt_styles": [],
        # Dubai (DAT) is a calendar-month-average cash-settled swap with its
        # own independent expiry — no residual-leg mechanic, no per-day
        # delivery-month equivalence to validate against.
        "precision_mode": "rank_approximate",
        "deferred_rank": 2,
        "month_offset": 0,
        "validate_ticker": "S:CODAT",
    },
    # ── Cracks (same-delivery-month by definition; WTI leg binds first) ─────
    "Brent / RBOB": {
        "legs": ("Brent", "RBOB"),
        "spread_style": "leg_matched",
        "alt_styles": ["spread_matched"],
        "precision_mode": "strict_delivery_match",
        "roll_trigger_style": "prior_month_eom",
        "roll_buffer_days": 5,
        "month_offset": 0,
        "validate_ticker": None,
    },
    "Brent / ULSD": {
        "legs": ("Brent", "ULSD"),
        "spread_style": "leg_matched",
        "alt_styles": ["spread_matched"],
        "precision_mode": "strict_delivery_match",
        "roll_trigger_style": "prior_month_eom",
        "roll_buffer_days": 5,
        "month_offset": 0,
        "validate_ticker": None,
    },
    "ULSD / WTI": {
        "legs": ("ULSD", "WTI"),
        "spread_style": "leg_matched",
        "alt_styles": ["spread_matched"],
        "precision_mode": "strict_delivery_match",
        "roll_trigger_style": "prior_month_eom",
        "roll_buffer_days": 5,
        "month_offset": 0,
        "validate_ticker": "S:HOCL",
    },
    # ── NGL / Frac (swaps — no per-day delivery-month validation possible) ──
    "Propane / Ethane": {
        "legs": ("Propane", "Ethane"),
        "spread_style": "leg_matched",
        "alt_styles": [],
        "precision_mode": "rank_approximate",
        "deferred_rank": 2,
        "month_offset": 0,
        "validate_ticker": None,
    },
    "Propane / Butane": {
        "legs": ("Propane", "Butane"),
        "spread_style": "leg_matched",
        "alt_styles": [],
        "precision_mode": "rank_approximate",
        "deferred_rank": 2,
        "month_offset": 0,
        "validate_ticker": None,
    },
    "RBOB / Butane": {
        "legs": ("RBOB", "Butane"),
        "spread_style": "leg_matched",
        "alt_styles": [],
        "precision_mode": "rank_approximate",
        "deferred_rank": 2,
        "month_offset": 0,
        "validate_ticker": None,
    },
    "Ethane / Natgas": {
        "legs": ("Ethane", "Natgas"),
        "spread_style": "leg_matched",
        "alt_styles": [],
        "precision_mode": "rank_approximate",
        "deferred_rank": 2,
        "month_offset": 0,
        "validate_ticker": None,
    },
}


def get_spread_spec(leg1_name: str, leg2_name: str) -> dict | None:
    """
    Spec lookup, order-insensitive ("WTI / Brent" and "Brent / WTI" both hit
    the same entry). month_offset in the spec is defined relative to the
    spec's own legs order; all current entries use offset 0, so reversal is
    sign-neutral. If a nonzero-offset cross-arb entry is added, look it up in
    spec order or negate the offset for the reversed direction.
    """
    for key in (f"{leg1_name} / {leg2_name}", f"{leg2_name} / {leg1_name}"):
        if key in SPREAD_SPECS:
            return SPREAD_SPECS[key]
    return None
