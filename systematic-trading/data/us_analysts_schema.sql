-- =============================================================================
-- us_analysts — destination landing schema for the SystematicTrading feed.
--
-- This is the DEV create statement for the replication target. The source is
-- FlowsDB schema `systematic` (see data/schema.sql), written by data.publish.
-- The other repo's replication template moves systematic.<table> -> the
-- matching table here, so the COLUMN NAMES and TABLE NAMES below deliberately
-- match the source one-for-one — do not rename them or the template's
-- name-mapping breaks.
--
-- Differences from the source schema, all because rows here ARRIVE from
-- replication rather than being generated locally:
--   * no BIGSERIAL / GENERATED — run_id is carried over as a plain BIGINT
--   * no column DEFAULTs (now(), etc.) — timestamps come from the source row
--   * primary keys kept, so a re-replicated snapshot upserts by the same grain
--     the source uses (as_of_date + the natural key) instead of duplicating
--
-- ONE THING TO CONFIRM: this assumes `us_analysts` is a SCHEMA that holds the
-- full set of feed tables (mirroring the source). If the template instead wants
-- a single flat landing table, say so — the shape is completely different and
-- we'll collapse these into one wide/JSON table instead. Everything below
-- changes in exactly one place if the namespace name is different: the
-- CREATE SCHEMA line and the qualifier prefix.
--
-- Target: PostgreSQL (matches FlowsDB / us stack).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS us_analysts;


-- ── Freshness / audit ─────────────────────────────────────────────────────────
-- The gate for "is today's snapshot complete". Downstream reads should join
-- v_latest_date (below), which only trusts rows whose publish_run.status='ok'.

CREATE TABLE IF NOT EXISTS us_analysts.publish_run (
    run_id        BIGINT      PRIMARY KEY,
    as_of_date    DATE        NOT NULL,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL,   -- running | ok | failed
    rows_written  INTEGER,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS ix_usan_publish_run_as_of
    ON us_analysts.publish_run (as_of_date DESC, run_id DESC);


-- ── Signals tab: outright grid ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS us_analysts.signal_outright (
    as_of_date     DATE        NOT NULL,
    commodity      TEXT        NOT NULL,
    strategy       TEXT        NOT NULL,   -- Momentum | Carry
    product_group  TEXT,
    direction      TEXT,                   -- Long | Short | Flat | '—'
    ma_value       NUMERIC,                -- Momentum: slow MA level
    pct_from_ma    NUMERIC,                -- Momentum: % of price above/below it
    spread         NUMERIC,                -- Carry: F(front) − F(end)
    spread_pct     NUMERIC,                -- Carry: that spread as % of front
    end_tenor      TEXT,                   -- Carry: back leg used, e.g. F13
    detail         JSONB,                  -- the API cell, verbatim
    updated_at     TIMESTAMPTZ,
    PRIMARY KEY (as_of_date, commodity, strategy)
);


-- ── Signals tab: spread grid ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS us_analysts.signal_spread (
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
    updated_at          TIMESTAMPTZ,
    PRIMARY KEY (as_of_date, pair)
);


-- ── Canonical run registry ────────────────────────────────────────────────────
-- run_key IS the source lab cache key (normalized-params JSON).

CREATE TABLE IF NOT EXISTS us_analysts.strategy_run (
    run_key        TEXT        PRIMARY KEY,
    strategy       TEXT        NOT NULL,
    instrument     TEXT        NOT NULL,   -- commodity, or "A / B" for pairs
    label          TEXT,
    params         JSONB,
    first_seen     TIMESTAMPTZ,
    last_computed  TIMESTAMPTZ
);


-- ── Signals tab: top-performers bar ───────────────────────────────────────────
-- rank_* is NULL for runs excluded from the bar (only a commodity's best
-- momentum tier ranks); the losers are still carried for the full cross-section.

CREATE TABLE IF NOT EXISTS us_analysts.strategy_performance (
    as_of_date  DATE        NOT NULL,
    run_key     TEXT        NOT NULL,
    label       TEXT,
    strategy    TEXT,
    instrument  TEXT,
    direction   TEXT,
    sharpe_1y   NUMERIC,
    sharpe_all  NUMERIC,
    rank_1y     INTEGER,
    rank_all    INTEGER,
    updated_at  TIMESTAMPTZ,
    PRIMARY KEY (as_of_date, run_key)
);

CREATE INDEX IF NOT EXISTS ix_usan_perf_rank
    ON us_analysts.strategy_performance (as_of_date DESC, rank_1y);


-- ── Levels tab: per-commodity card facts ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS us_analysts.levels_card (
    as_of_date          DATE        NOT NULL,
    commodity           TEXT        NOT NULL,
    product_group       TEXT,
    current_price       NUMERIC,
    mom_direction       TEXT,
    mom_distance_pct    NUMERIC,
    carry_direction     TEXT,
    carry_distance_pct  NUMERIC,
    carry_tenor         TEXT,
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
    closest_dist        NUMERIC,
    updated_at          TIMESTAMPTZ,
    PRIMARY KEY (as_of_date, commodity)
);


-- ── Levels tab: near-trigger alerts ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS us_analysts.levels_hot (
    as_of_date  DATE        NOT NULL,
    instrument  TEXT        NOT NULL,   -- commodity or "A − B" pair label
    strategy    TEXT        NOT NULL,   -- Trend Following | Mean Reversion
    direction   TEXT,
    distance    NUMERIC,                -- % for trend, sigma for mean reversion
    detail      TEXT,
    level       NUMERIC,
    current     NUMERIC,
    updated_at  TIMESTAMPTZ,
    PRIMARY KEY (as_of_date, instrument, strategy)
);


-- ── Levels tab: recent signal flips ───────────────────────────────────────────
-- as_of_date = the snapshot that observed the flip; flip_date = when it happened.

CREATE TABLE IF NOT EXISTS us_analysts.levels_flip (
    as_of_date      DATE        NOT NULL,
    instrument      TEXT        NOT NULL,
    flip_date       DATE        NOT NULL,
    strategy        TEXT        NOT NULL,
    tier            TEXT,
    from_direction  TEXT,
    to_direction    TEXT,
    price           NUMERIC,
    level           NUMERIC,
    updated_at      TIMESTAMPTZ,
    PRIMARY KEY (as_of_date, instrument, flip_date, strategy)
);


-- ── Display series ────────────────────────────────────────────────────────────
-- 63-day arrays the Levels charts draw. JSONB, presentation-shaped.
--   scope 'levels_card'   series_key = commodity
--   scope 'levels_spread' series_key = pair label ("WTI − Brent")

CREATE TABLE IF NOT EXISTS us_analysts.chart_series (
    as_of_date  DATE        NOT NULL,
    scope       TEXT        NOT NULL,
    series_key  TEXT        NOT NULL,
    payload     JSONB,
    updated_at  TIMESTAMPTZ,
    PRIMARY KEY (as_of_date, scope, series_key)
);


-- ── Daily sizing ──────────────────────────────────────────────────────────────
-- Calibration-basis lots (batch has no live-price override): SIZING_CONFIGS
-- ref_price defaults, which are MANUAL. Not desk-live numbers.

CREATE TABLE IF NOT EXISTS us_analysts.sizing_daily (
    as_of_date            DATE        NOT NULL,
    run_key               TEXT        NOT NULL,
    strategy              TEXT,
    instrument            TEXT,
    is_pair               BOOLEAN,
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
    payload               JSONB,
    updated_at            TIMESTAMPTZ,
    PRIMARY KEY (as_of_date, run_key)
);


-- ── Latest-snapshot views ─────────────────────────────────────────────────────
-- What the dashboard should read — it never has to know today's trading date
-- or handle the pre-open gap, and it only ever sees a fully-published snapshot.

CREATE OR REPLACE VIEW us_analysts.v_latest_date AS
    SELECT max(as_of_date) AS as_of_date
    FROM us_analysts.publish_run
    WHERE status = 'ok';

CREATE OR REPLACE VIEW us_analysts.v_signal_outright AS
    SELECT s.* FROM us_analysts.signal_outright s
    JOIN us_analysts.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW us_analysts.v_signal_spread AS
    SELECT s.* FROM us_analysts.signal_spread s
    JOIN us_analysts.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW us_analysts.v_strategy_performance AS
    SELECT s.* FROM us_analysts.strategy_performance s
    JOIN us_analysts.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW us_analysts.v_levels_card AS
    SELECT s.* FROM us_analysts.levels_card s
    JOIN us_analysts.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW us_analysts.v_levels_hot AS
    SELECT s.* FROM us_analysts.levels_hot s
    JOIN us_analysts.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW us_analysts.v_levels_flip AS
    SELECT s.* FROM us_analysts.levels_flip s
    JOIN us_analysts.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW us_analysts.v_chart_series AS
    SELECT s.* FROM us_analysts.chart_series s
    JOIN us_analysts.v_latest_date d USING (as_of_date);

CREATE OR REPLACE VIEW us_analysts.v_sizing_daily AS
    SELECT s.* FROM us_analysts.sizing_daily s
    JOIN us_analysts.v_latest_date d USING (as_of_date);
