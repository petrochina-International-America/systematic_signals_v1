import { useState, useEffect } from 'react';
import { fetchDbMeta } from '../api/client';
import { anyDbSource } from '../config/dataSources';

/**
 * Permanent post-migration guard: components on the 'db' source render last
 * night's published snapshot, so the live API's implicit freshness is gone.
 * This badge surfaces "data as of <date>" on every page load, and turns into
 * a loud warning when the snapshot is older than the last expected publish
 * (server-side check: /api/db/meta compares v_latest_date + publish_run
 * against the previous weekday) — old numbers must never render silently.
 */
export default function FreshnessBadge() {
  const [meta, setMeta] = useState(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!anyDbSource()) return;
    fetchDbMeta().then(setMeta).catch(() => setError(true));
  }, []);

  if (!anyDbSource()) return null;

  if (error || (meta && (meta.stale || meta.last_run?.status === 'failed'))) {
    const detail = error
      ? 'snapshot status unavailable'
      : meta.last_run?.status === 'failed'
        ? `last publish FAILED — showing ${meta.as_of_date}`
        : `snapshot ${meta.as_of_date} is stale (expected ${meta.expected_date})`;
    return (
      <span
        className="topbar-timestamp"
        style={{
          color: 'var(--red, #f87171)',
          border: '1px solid var(--red, #f87171)',
          borderRadius: 4,
          padding: '2px 8px',
          marginRight: 12,
          fontWeight: 700,
        }}
        title="The DB-backed tabs are rendering an outdated snapshot. Check the morning publish (systematic.publish_run)."
      >
        ⚠ {detail}
      </span>
    );
  }

  if (!meta?.as_of_date) return null;
  const d = new Date(meta.as_of_date + 'T00:00:00');
  return (
    <span className="topbar-timestamp" style={{ marginRight: 12 }}>
      Data as of {d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })}
    </span>
  );
}
