// Per-component data source for the API → DB migration.
//
//   'api' — live in-process compute (/api/signals/*, /api/levels/*)
//   'db'  — last published snapshot (/api/db/*, backed by systematic.* /
//           us_analysts via data.readback)
//
// Flip ONE component at a time, and only after its parity report
// (audit_artifacts/parity/<as_of>/parity_<component>.md) is clean for two
// consecutive daily snapshots. Keep the api path callable until every
// component has passed; then the dead path can be removed.
//
// Note: 'db' serves last night's publish, not intraday recompute — the
// FreshnessBadge in the topbar surfaces the snapshot date and warns when it
// is older than the last expected publish.
export const DATA_SOURCES = {
  // Flipped 2026-07-29 after parity PASS at as_of 2026-07-28
  // (audit_artifacts/parity/2026-07-28/). API path stays callable until the
  // 2-consecutive-snapshot criterion is met.
  signalsSnapshot: 'db',
  signalsSpreads: 'db',
  topPerformers: 'db',
  // STOP-THE-MIGRATION finding (parity clean at tenor=1, but the schema has
  // no tenor dimension): only M1 is published, and the Levels page serves an
  // M1–M4 selector per card. Flipping would silently drop M2–M4. Needs
  // `tenor` added to levels_card/chart_series PKs (both schemas) + a 4-tenor
  // publish before this can move to 'db'.
  levels: 'api',
};

export const anyDbSource = () =>
  Object.values(DATA_SOURCES).some(s => s === 'db');
