import { useState, useEffect } from 'react';
import Plot from 'react-plotly.js';
import { useCommodity } from '../hooks/useCommodity';
import { fetchCot, fetchFrontMonth, fetchFollowTheFlow, fetchFadeTheCrowd, fetchCotSnapshot, fetchCotStatus } from '../api/client';
import MetricCard from '../components/MetricCard';
import { TablePanel, ChartPanel } from '../components/Panel';
import DataTable, { positioningColorRules } from '../components/DataTable';
import Loading, { ErrorNote } from '../components/Loading';
import { cotChart, sentimentChart, percentileHistogram } from '../charts/cotCharts';
import { PLOTLY_CONFIG } from '../charts/theme';

const SNAPSHOT_COMMODITIES = ['WTI', 'Brent', 'Natgas', 'RBOB', 'ULSD', 'Gasoil'];
const MA_FAST = 4, MA_SLOW = 16, SI_THRESHOLD = 20;

export default function CotFlows() {
  const { commodity, setCommodity, commodities } = useCommodity();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    setLoading(true);
    const start = '2022-06-01';
    Promise.all([
      fetchCot(commodity, { start_date: start }),
      fetchFrontMonth(commodity, { start_date: start }),
      fetchFollowTheFlow(commodity, { fast: MA_FAST, slow: MA_SLOW, start_date: start }),
      fetchFadeTheCrowd(commodity, { threshold_pct: SI_THRESHOLD, start_date: start }),
      fetchCotSnapshot(SNAPSHOT_COMMODITIES),
      fetchCotStatus(),
    ]).then(([cot, price, ftf, ftc, snapshot, status]) => {
      setData({ cot, price, ftf, ftc, snapshot, synthetic: status.synthetic });
      setError(null);
    }).catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [commodity]);

  if (loading) return <Loading message="Loading COT data..." />;
  if (error) return <ErrorNote message={error} />;
  if (!data) return null;

  const { cot, price, ftf, ftc, snapshot, synthetic } = data;
  const latest = cot.latest;
  const chgColor = latest.mm_net_change >= 0 ? 'var(--green)' : 'var(--red)';
  const crowdColor = { Crowded: 'var(--red)', Washed: 'var(--green)' }[latest.crowding_flag] || 'var(--blue)';

  const cotFig = cotChart(cot.history, price, commodity, ftf.data);
  const siFig = sentimentChart(ftc.data, SI_THRESHOLD, commodity);
  const histFig = percentileHistogram(cot.history, commodity);

  const snapCols = [
    { id: 'commodity', name: 'Commodity' },
    { id: 'mm_net', name: 'MM Net' },
    { id: 'percentile_rank', name: 'Pct Rank' },
    { id: 'crowding_flag', name: 'Crowding' },
  ];
  const snapData = snapshot.data.map(r => ({
    ...r,
    mm_net: r.mm_net?.toLocaleString(),
    percentile_rank: r.percentile_rank != null ? `${r.percentile_rank.toFixed(1)}th` : '—',
  }));

  return (
    <div className="page-content">
      <div className="page-toolbar">
        <select
          className="commodity-select"
          value={commodity}
          onChange={e => setCommodity(e.target.value)}
        >
          {commodities.map(c => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        {synthetic && (
          <span
            className="scanner-info__tag"
            title="Real CFTC data isn't wired up yet — this entire page runs on a seeded placeholder series, not live positioning."
          >
            Pending
          </span>
        )}
      </div>
      <div className="metric-row">
        <MetricCard label="MM Net Position" value={latest.mm_net?.toLocaleString()} sub="contracts" />
        <MetricCard label="MM Net Change WoW" value={`${latest.mm_net_change >= 0 ? '+' : ''}${latest.mm_net_change?.toLocaleString()}`} sub="contracts" color={chgColor} />
        <MetricCard label="Percentile Rank" value={`${latest.percentile_rank?.toFixed(1)}th`} sub="52-week" />
        <MetricCard label="Crowding Flag" value={latest.crowding_flag} color={crowdColor} />
      </div>
      <ChartPanel>
        <Plot {...cotFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 380 }} />
      </ChartPanel>
      <div className="chart-grid-2">
        <ChartPanel>
          <Plot {...siFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 320 }} />
        </ChartPanel>
        <ChartPanel>
          <Plot {...histFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 320 }} />
        </ChartPanel>
      </div>
      <TablePanel heading="Positioning Snapshot — All Commodities">
        <DataTable columns={snapCols} data={snapData} colorRules={positioningColorRules()} />
      </TablePanel>
      {synthetic && (
        <div className="placeholder-note">
          COT data is synthetic (cot_bbg table pending) — layout and signal mechanics are final, values are not.
        </div>
      )}
    </div>
  );
}
