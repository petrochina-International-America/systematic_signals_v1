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
  // Flipped after the tenor dimension landed (levels_card/chart_series PKs
  // now carry tenor; publish writes M1–M4) and levels_m1..m4 all passed
  // parity at run 16. The page sizes its selector from tenors_available, so
  // a pre-tenor snapshot degrades to M1-only instead of 404ing.
  levels: 'db',
};

export const anyDbSource = () =>
  Object.values(DATA_SOURCES).some(s => s === 'db');
