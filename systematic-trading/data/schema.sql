-- =============================================================================
-- systematic — published outputs of the SystematicTrading engines.
--
-- Everything in here is DERIVED. The source of truth for inputs stays
-- public.prices_daily (and, once it lands, cot_bbg); this schema only holds
-- what the strategy layer computes on top of them.
--
-- Scope (2026-07-23): exactly what the Signals and Levels tabs need.
--   Signals tab  -> signal_outright, signal_spread, strategy_run,
--                   strategy_performance, sizing_daily
--   Levels  tab  -> levels_card, levels_hot, levels_flip, chart_series
--
-- Grain: every fact table is keyed by as_of_date = the latest TRADING date in
-- the price store (data.loader.latest_data_date()), never wall-clock. Re-running
-- the publisher for the same date is idempotent (ON CONFLICT DO UPDATE), so a
-- mid-day re-publish corrects the row rather than duplicating it, and history
-- accrues one snapshot per trading day.
--
-- Apply with:  py -3.14 -m data.publish --init
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS systematic;


-- ── Audit ────────────────────────────────────────────────────────────────────
-- One row per publisher invocation. Lets the shared dashboard tell "no data
-- yet" apart from "the publisher failed", which a bare empty table cannot.

CREATE TABLE IF NOT EXISTS systematic.publish_run (
    run_id        BIGSERIAL   PRIMARY KEY,
    as_of_date    DATE        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL DEFAULT 'running',  -- running | ok | failed
    rows_written  INTEGER     NOT NULL DEFAULT 0,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS ix_publish_run_as_of
    ON systematic.publish_run (as_of_date DESC, run_id DESC);


-- ── Signals tab: outright grid ───────────────────────────────────────────────
-- Source: GET /api/signals/snapshot

-- The strength fields are strategy-specific: Momentum reports ma_value /
-- pct_from_ma, Carry reports spread / spread_pct / end_tenor, and a commodity
-- with no FlowsDB ticker reports direction '—' and nothing else. `detail` holds
-- the cell verbatim so readback is lossless whichever branch produced it; the
-- broken-out columns exist so the shared dashboard can filter and sort without
-- reaching into JSON.

CREATE TABLE IF NOT EXISTS systematic.signal_outright (
    as_of_date     DATE        NOT NULL,
    commodity      TEXT        NOT NULL,
    strategy       TEXT        NOT NULL,   -- Momentum | Carry
    product_group  TEXT,                   -- Crude Benchmarks, Products, ...
    direction      TEXT,                   -- Long | Short | Flat | '—'
    ma_value       NUMERIC,                -- Momentum: the slow MA level
    pct_from_ma    NUMERIC,                -- Momentum: %% of price above/below it
    spread         NUMERIC,                -- Carry: F(front) − F(end)
    spread_pct     NUMERIC,                -- Carry: that spread as %% of front
    end_tenor      TEXT,                   -- Carry: the back leg used, e.g. F13
    detail         JSONB       NOT NULL,   -- the cell as the API returns it
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, commodity, strategy)
);

CREATE INDEX IF NOT EXISTS ix_signal_outright_commodity
    ON systematic.signal_outright (commodity, as_of_date DESC);


-- ── Signals tab: spread grid ─────────────────────────────────────────────────
-- Source: GET /api/signals/spreads, merged with the spread panel of
-- GET /api/levels/proximity (same pairs, same pair_defaults() construction —
-- the band/std/in-trade columns only exist on the Levels side).

CREATE TABLE IF NOT EXISTS systematic.signal_spread (
    as_of_date          DATE        NOT NULL,
    pair                TEXT        NOT NULL,   -- "WTI / Brent"
    spread_group        TEXT,                   -- Location | Cracks | FFAs | NGL | Frac
    leg1                TEXT,
    leg2                TEXT,
    direction           TEXT,                   -- Long | Short | Flat | '—'
    zscore              NUMERIC,
    spread_value        NUMERIC,
    quoted_spread       NUMERIC,
    spread_mean         NUMERIC,
    spread_std          NUMERIC,
    upper_band          NUMERIC,
    lower_band          NUMERIC,
    deviation           NUMERIC,
    pct_from_mean       NUMERIC,
    dist_to_threshold   NUMERIC,
    pct_from_threshold  NUMERIC,
    in_trade            BOOLEAN,
    signal_prev         NUMERIC,
    lookback            INTEGER,
    threshold           NUMERIC,
    month_offset        INTEGER,
    construction        TEXT,
    precision_mode      TEXT,
    signal_series       TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, pair)
);

CREATE INDEX IF NOT EXISTS ix_signal_spread_pair
    ON systematic.signal_spread (pair, as_of_date DESC);


-- ── Canonical run registry ───────────────────────────────────────────────────
-- The answer to "which of the unbounded lab parameter space is worth storing".
-- run_key IS data.lab's cache key (the normalized-params JSON), so a row here
-- round-trips straight back into lab.get_result(run_key) with no translation.

CREATE TABLE IF NOT EXISTS systematic.strategy_run (
    run_key        TEXT        PRIMARY KEY,
    strategy       TEXT        NOT NULL,   -- Momentum | Carry | Stat-Arb | COT
    instrument     TEXT        NOT NULL,   -- commodity, or "A / B" for pairs
    label          TEXT,                   -- display label, e.g. "WTI Mom (Fast)"
    params         JSONB       NOT NULL,
    first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_computed  TIMESTAMPTZ
);


-- ── Signals tab: top-performers bar ──────────────────────────────────────────
-- Source: GET /api/signals/top-performers
-- rank_* is NULL for runs excluded from the bar by _best_momentum_per_commodity
-- (only a commodity's best momentum tier ranks). Storing the losers anyway keeps
-- the full cross-section queryable without changing what the bar renders.

CREATE TABLE IF NOT EXISTS systematic.strategy_performance (
    as_of_date  DATE        NOT NULL,
    run_key     TEXT        NOT NULL REFERENCES systematic.strategy_run (run_key),
    label       TEXT,
    strategy    TEXT,
    instrument  TEXT,
    direction   TEXT,
    sharpe_1y   NUMERIC,
    sharpe_all  NUMERIC,
    rank_1y     INTEGER,
    rank_all    INTEGER,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, run_key)
);

CREATE INDEX IF NOT EXISTS ix_strategy_performance_rank
    ON systematic.strategy_performance (as_of_date DESC, rank_1y);


-- ── Levels tab: per-commodity card facts ─────────────────────────────────────
-- Source: GET /api/levels/proximity -> groups[*][*], scalars only.
-- The 3-month display arrays live in chart_series.

CREATE TABLE IF NOT EXISTS systematic.levels_card (
    as_of_date          DATE        NOT NULL,
    commodity           TEXT        NOT NULL,
    product_group       TEXT,
    current_price       NUMERIC,
    mom_direction       TEXT,
    mom_distance_pct    NUMERIC,
    carry_direction     TEXT,
    carry_distance_pct  NUMERIC,
    carry_tenor         TEXT,                   -- F13, or deepest available
    carry_level         NUMERIC,
    carry_shape         TEXT,                   -- Backwardation | Contango
    carry_spread        NUMERIC,
    cta_direction       TEXT,                   -- Long | Short | Flat
    cta_net_signal      NUMERIC,                -- inverse-vol blended, -1..+1
    position_pct        NUMERIC,
    position_pct_prev   NUMERIC,
    position_chg        NUMERIC,
    vol_scalar          NUMERIC,
    cot_percentile      NUMERIC,
    cot_flag            TEXT,
    cot_pending         BOOLEAN,                -- TRUE while cot_bbg is synthetic
    closest_dist        NUMERIC,                -- card sort key
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, commodity)
);


-- ── Levels tab: near-trigger alerts ──────────────────────────────────────────
-- Source: GET /api/levels/proximity -> hot[]

CREATE TABLE IF NOT EXISTS systematic.levels_hot (
    as_of_date  DATE        NOT NULL,
    instrument  TEXT        NOT NULL,   -- commodity or "A − B" pair label
    strategy    TEXT        NOT NULL,   -- Trend Following | Mean Reversion
    direction   TEXT,
    distance    NUMERIC,                -- %% for trend, sigma for mean reversion
    detail      TEXT,
    level       NUMERIC,
    current     NUMERIC,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, instrument, strategy)
);


-- ── Levels tab: recent signal flips ──────────────────────────────────────────
-- Source: GET /api/levels/proximity -> recent_trades[]
-- as_of_date is the snapshot that observed the flip; flip_date is when it
-- happened (up to 5 trading days earlier).

CREATE TABLE IF NOT EXISTS systematic.levels_flip (
    as_of_date      DATE        NOT NULL,
    instrument      TEXT        NOT NULL,
    flip_date       DATE        NOT NULL,
    strategy        TEXT        NOT NULL,
    tier            TEXT,                   -- "Trend (Fast)", "Carry (Contango)", "z=1.8σ"
    from_direction  TEXT,
    to_direction    TEXT,
    price           NUMERIC,
    level           NUMERIC,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, instrument, flip_date, strategy)
);


-- ── Display series ───────────────────────────────────────────────────────────
-- The 63-day arrays the Levels charts draw (prices, MA histories, position
-- history, spread bands). Deliberately JSONB, not normalized: these are
-- presentation-shaped, they change whenever the chart changes, and nothing
-- downstream joins on them. Promote a field to a real column only once
-- something needs to query it.
--
--   scope 'levels_card'   series_key = commodity
--   scope 'levels_spread' series_key = pair label ("WTI − Brent")

CREATE TABLE IF NOT EXISTS systematic.chart_series (
    as_of_date  DATE        NOT NULL,
    scope       TEXT        NOT NULL,
    series_key  TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, scope, series_key)
);


-- ── Daily sizing ─────────────────────────────────────────────────────────────
-- Source: POST /api/sizing/today, run over the calibrated universe in
-- energy.sizing.daily_size.SIZING_CONFIGS.
--
-- NOTE: lots/notional/VaR are computed against SIZING_CONFIGS ref_price
-- defaults, which are MANUAL and must be reviewed when price levels shift.
-- The live-price override in the request body is not available to a batch
-- publisher, so these are calibration-basis numbers, not desk-live numbers.

CREATE TABLE IF NOT EXISTS systematic.sizing_daily (
    as_of_date            DATE        NOT NULL,
    run_key               TEXT        NOT NULL,
    strategy              TEXT,
    instrument            TEXT,
    is_pair               BOOLEAN     NOT NULL DEFAULT FALSE,
    signal                NUMERIC,
    direction             TEXT,
    vol_scalar            NUMERIC,
    realized_vol_ann_pct  NUMERIC,
    lots                  NUMERIC,    -- single-leg only; pairs are in payload.legs
    notional_usd          NUMERIC,
    var_95_usd            NUMERIC,
    capital_base          NUMERIC,
    ref_price             NUMERIC,
    sizing_mode           TEXT,
    payload               JSONB       NOT NULL,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, run_key)
);


-- ── Latest-snapshot views ────────────────────────────────────────────────────
-- The shared dashboard should read these, not the base tables — it then never
-- has to know today's trading date or handle the pre-open gap.

CREATE OR REPLACE VIEW systematic.v_latest_date AS
    SELECT max(as_of_date) AS as_of_date
    FROM systematic.publish_run
    WHERE status = 'ok';

CREATE OR REPLACE VIEW systematic.v_signal_outright AS
    SELECT s.* FROM systematic.signal_outright s
    JOIN systematic.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW systematic.v_signal_spread AS
    SELECT s.* FROM systematic.signal_spread s
    JOIN systematic.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW systematic.v_strategy_performance AS
    SELECT s.* FROM systematic.strategy_performance s
    JOIN systematic.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW systematic.v_levels_card AS
    SELECT s.* FROM systematic.levels_card s
    JOIN systematic.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW systematic.v_levels_hot AS
    SELECT s.* FROM systematic.levels_hot s
    JOIN systematic.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW systematic.v_levels_flip AS
    SELECT s.* FROM systematic.levels_flip s
    JOIN systematic.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW systematic.v_chart_series AS
    SELECT s.* FROM systematic.chart_series s
    JOIN systematic.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW systematic.v_sizing_daily AS
    SELECT s.* FROM systematic.sizing_daily s
    JOIN systematic.v_latest_date d USING (as_of_date);
