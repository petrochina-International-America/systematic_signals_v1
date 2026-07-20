import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import Plot from 'react-plotly.js';
import { fetchPrices, fetchMomentumSpeeds } from '../api/client';
import { useBacktest, fmt, SAMPLE_OPTIONS } from '../hooks/useBacktest';
import MetricCard from '../components/MetricCard';
import TodaysSizePanel from '../components/TodaysSizePanel';
import { TablePanel, ChartPanel } from '../components/Panel';
import Loading, { ErrorNote } from '../components/Loading';
import {
  rollingSharpeChart, returnDistChart, volScalarChart,
  maLevelsChart, computeDistStats, computeRangeStats, computeSampleMetrics, computeTradeStats,
  speedComparisonChart, SPEED_COLORS, SPEED_ORDER,
} from '../charts/detailCharts';
import { PLOTLY_CONFIG } from '../charts/theme';

const RATIONALE = [
  { label: 'Long', text: 'Price above moving average' },
  { label: 'Short', text: 'Price below moving average' },
  { label: 'Edge', text: 'Trend following — prices that are rising tend to keep rising, and vice versa' },
];

const TIER_DEFS = {
  'Very Fast': { pairs: '(1,5) · (2,10) · (3,15)', horizon: 'Days – 2 wks' },
  'Fast':      { pairs: '(1,5) · (5,20) · (10,60)', horizon: 'Wks – 2 mos' },
  'Medium':    { pairs: '(10,30) · (20,60) · (30,90)', horizon: '1 – 3 mos' },
  'Slow':      { pairs: '(20,120) · (40,180) · (60,250)', horizon: '3 – 12 mos' },
  'Averaged':  { pairs: 'Equal-weight blend of all four tiers', horizon: 'All horizons' },
};

const ALL_TIERS = [...SPEED_ORDER, 'Custom'];

const STAT_ROWS = [
  { key: 'cagr', label: 'CAGR', fmt: v => `${(v * 100).toFixed(1)}%` },
  { key: 'vol_ann', label: 'Vol (ann.)', fmt: v => `${(v * 100).toFixed(1)}%` },
  { key: 'sharpe', label: 'Sharpe', fmt: v => v.toFixed(2) },
  { key: 'max_dd', label: 'Max DD', fmt: v => `${(v * 100).toFixed(1)}%` },
  { key: 'trades_yr', label: 'Trades/yr', fmt: v => String(v) },
  { key: 'trades_wk', label: 'Trades/wk', fmt: v => String(v) },
];

function SpeedStatsTable({ speedData, selectedTier, hoveredTier }) {
  if (!speedData?.tiers) return null;
  const periods = ['2015-22', '22-26'];
  const activeTier = hoveredTier || selectedTier;

  return (
    <div className="table-panel">
      <div className="panel-heading">Parameter Speed Comparison</div>
      <div style={{ overflowX: 'auto' }}>
        <table className="data-table speed-stats-table">
          <thead>
            <tr>
              <th></th>
              {SPEED_ORDER.map(t => {
                const isActive = t === activeTier;
                return (
                  <th key={t} colSpan={2} style={{
                    textAlign: 'center', borderLeft: '1px solid var(--border)',
                    background: isActive ? 'rgba(55,138,221,0.08)' : undefined,
                    transition: 'background 0.2s',
                  }}>
                    <span style={{ color: SPEED_COLORS[t], fontWeight: 700 }}>{t}</span>
                  </th>
                );
              })}
            </tr>
            <tr>
              <th></th>
              {SPEED_ORDER.map(t => periods.map(p => (
                <th key={`${t}-${p}`} style={{
                  textAlign: 'center', fontSize: '10px',
                  borderLeft: p === '2015-22' ? '1px solid var(--border)' : 'none',
                  background: t === activeTier ? 'rgba(55,138,221,0.08)' : undefined,
                  transition: 'background 0.2s',
                }}>
                  {p}
                </th>
              )))}
            </tr>
          </thead>
          <tbody>
            {STAT_ROWS.map(row => (
              <tr key={row.key}>
                <td style={{ fontWeight: 600, fontSize: '12px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>{row.label}</td>
                {SPEED_ORDER.map(t => periods.map(p => {
                  const stats = speedData.tiers[t]?.periods?.[p];
                  const val = stats?.[row.key];
                  const isActive = t === activeTier;
                  const isSharpe = row.key === 'sharpe';
                  let cellBg;
                  if (isSharpe && val != null) {
                    cellBg = val >= 0.5 ? 'rgba(74,222,128,0.2)' : val < 0 ? 'rgba(226,75,74,0.15)' : undefined;
                  }
                  if (isActive && !cellBg) cellBg = 'rgba(55,138,221,0.06)';
                  return (
                    <td key={`${t}-${p}`} style={{
                      textAlign: 'center',
                      borderLeft: p === '2015-22' ? '1px solid var(--border)' : 'none',
                      fontWeight: isActive ? 600 : 400,
                      background: cellBg,
                      transition: 'background 0.2s, font-weight 0.2s',
                    }}>
                      {val != null ? row.fmt(val) : '—'}
                    </td>
                  );
                }))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function MomentumDetail() {
  const { commodity } = useParams();
  const navigate = useNavigate();

  const [tier, setTier] = useState('Averaged');
  const [hoveredTier, setHoveredTier] = useState(null);
  const [customFast, setCustomFast] = useState(5);
  const [customSlow, setCustomSlow] = useState(60);
  const [speedData, setSpeedData] = useState(null);
  const [speedLoading, setSpeedLoading] = useState(true);
  const [priceData, setPriceData] = useState(null);

  const isCustom = tier === 'Custom';
  const backtestParams = isCustom
    ? { tier: 'Custom', fast: customFast, slow: customSlow }
    : { tier };

  const {
    result, loading, error,
    volTarget, volTargetPct, setVolTargetPct,
    sampleIdx, setSampleIdx, sampleDays,
    samples, setSamples, updateSample,
  } = useBacktest({
    strategy: 'Momentum',
    commodity,
    params: backtestParams,
  });

  useEffect(() => {
    setSpeedLoading(true);
    fetchMomentumSpeeds(commodity, volTargetPct / 100)
      .then(d => { setSpeedData(d); setSpeedLoading(false); })
      .catch(() => setSpeedLoading(false));
  }, [commodity, volTargetPct]);

  useEffect(() => {
    if (result) fetchPrices(commodity).then(setPriceData).catch(() => {});
  }, [result, commodity]);

  if (loading && !result && speedLoading) return <div className="page-content"><Loading message="Computing backtests..." /></div>;
  if (error) return <div className="page-content"><ErrorNote message={error} /></div>;

  const m = result ? computeSampleMetrics(result, sampleDays) || {} : {};
  const mAll = result ? computeSampleMetrics(result, null) || {} : {};
  const ts = result ? computeTradeStats(result, sampleDays) || {} : {};
  const rollingFig = result ? rollingSharpeChart(result) : null;
  const volFig = result ? volScalarChart(result, sampleDays) : null;
  const distStats = result ? computeDistStats(result) : null;
  const maFig = maLevelsChart(priceData, tier, customFast, customSlow, hoveredTier);
  const speedFig = speedComparisonChart(speedData, tier, volTargetPct, sampleDays, hoveredTier);

  function handleSpeedClick(data) {
    const pt = data?.points?.[0];
    if (pt?.data?.meta?.tier) {
      setTier(pt.data.meta.tier);
    }
  }

  function handleSpeedHover(data) {
    const pt = data?.points?.[0];
    if (pt?.data?.meta?.tier) {
      setHoveredTier(pt.data.meta.tier);
    }
  }

  function handleSpeedUnhover() {
    setHoveredTier(null);
  }

  return (
    <div className="page-content">
      <div className="detail-toprow">
        <button className="signal-back-btn" onClick={() => navigate('/signals')}>&larr; Back to Signals</button>
      </div>

      <div className="detail-header">
        <div className="detail-header__top">
          <div className="detail-header__left">
            <div className="detail-header__title">{commodity}</div>
            <div className="detail-header__subtitle">Momentum · $1M capital</div>
          </div>
          <div className="detail-header__rationale">
            {RATIONALE.map((line, i) => (
              <div key={i} className={`detail-header__rationale-line${line.label === 'Edge' ? ' detail-header__rationale-edge' : ''}`}>
                <span className={`detail-header__rationale-tag detail-header__rationale-tag--${line.label.toLowerCase()}`}>{line.label}</span>
                {line.text}
              </div>
            ))}
          </div>
        </div>
        <div className="detail-header__controls">
          <label className="detail-header__label">Speed
            <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup tip-popup--right" style={{ width: 340 }}>
              Each tier blends 3 MA crossover pairs (fast, slow):<br/><br/>
              <b style={{ color: SPEED_COLORS['Very Fast'] }}>Very Fast</b> — {TIER_DEFS['Very Fast'].pairs}<br/>
              <span style={{ color: 'var(--muted)', fontSize: 10 }}>{TIER_DEFS['Very Fast'].horizon}</span><br/><br/>
              <b style={{ color: SPEED_COLORS['Fast'] }}>Fast</b> — {TIER_DEFS['Fast'].pairs}<br/>
              <span style={{ color: 'var(--muted)', fontSize: 10 }}>{TIER_DEFS['Fast'].horizon}</span><br/><br/>
              <b style={{ color: SPEED_COLORS['Medium'] }}>Medium</b> — {TIER_DEFS['Medium'].pairs}<br/>
              <span style={{ color: 'var(--muted)', fontSize: 10 }}>{TIER_DEFS['Medium'].horizon}</span><br/><br/>
              <b style={{ color: SPEED_COLORS['Slow'] }}>Slow</b> — {TIER_DEFS['Slow'].pairs}<br/>
              <span style={{ color: 'var(--muted)', fontSize: 10 }}>{TIER_DEFS['Slow'].horizon}</span><br/><br/>
              <b style={{ color: SPEED_COLORS['Averaged'] }}>Averaged</b> — {TIER_DEFS['Averaged'].pairs}<br/><br/>
              <b>Custom</b> — enter your own fast/slow MA pair.<br/>
              Click a line on the chart to switch tiers.
            </span></span>
          </label>
          <select className="commodity-select" value={tier} onChange={e => setTier(e.target.value)}>
            {ALL_TIERS.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          {isCustom && (
            <>
              <label className="detail-header__label">Fast MA
                <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Short-period moving average.<br/><br/>Signal is long when Fast MA crosses above Slow MA, short when it crosses below.</span></span>
              </label>
              <select className="commodity-select" value={customFast} onChange={e => { const v = +e.target.value; if (v < customSlow) setCustomFast(v); }}>
                {[1, 2, 3, 5, 10, 20, 30, 40, 60].filter(v => v < customSlow).map(v => (
                  <option key={v} value={v}>{v}d</option>
                ))}
              </select>
              <span style={{ color: 'var(--muted)', fontSize: 13, fontWeight: 600 }}>×</span>
              <label className="detail-header__label">Slow MA</label>
              <select className="commodity-select" value={customSlow} onChange={e => setCustomSlow(+e.target.value)}>
                {[5, 10, 15, 20, 30, 60, 90, 120, 180, 250].filter(v => v > customFast).map(v => (
                  <option key={v} value={v}>{v}d</option>
                ))}
              </select>
            </>
          )}
          <div className="detail-header__sep" />
          <label className="detail-header__label">Vol Target <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Annualized volatility target for position sizing.<br/><br/>Realized vol is estimated over a trailing 60-day window. Lower = smaller positions, smoother equity curve. Higher = more leverage, larger swings.</span></span></label>
          <select className="commodity-select" value={volTargetPct} onChange={e => setVolTargetPct(+e.target.value)}>
            {[5, 10, 15, 20, 25].map(v => <option key={v} value={v}>{v}%</option>)}
          </select>
          <label className="detail-header__label">Sample</label>
          <div className="detail-header__toggles">
            {SAMPLE_OPTIONS.map((opt, i) => (
              <button key={opt.label} className={`signals-toggle${i === sampleIdx ? ' signals-toggle--active' : ''}`}
                onClick={() => setSampleIdx(i)}>{opt.label}</button>
            ))}
          </div>
        </div>
      </div>

      {result && (
        <div className="metric-row metric-row--5">
          <MetricCard label="Sharpe" value={fmt(m.Sharpe, 'f2')} sub={`${isCustom ? `MA(${customFast},${customSlow})` : tier} · ${SAMPLE_OPTIONS[sampleIdx].label} · all-time ${fmt(mAll.Sharpe, 'f2')}`} />
          <MetricCard label="CAGR" value={fmt(m.CAGR, 'pct')} sub="annualized" />
          <MetricCard label="Total PnL" value={fmt(m['Total PnL'], '$')} sub="$1M capital" color="var(--green)" />
          <MetricCard label="Max Drawdown" value={fmt(m.Drawdown, 'pct')} sub="peak to trough" color="var(--red)" />
          <MetricCard
            label="Trade Profile"
            value={ts.tradesPerYear != null ? `${ts.tradesPerYear} trades/yr` : '—'}
            sub={ts.avgHoldDays != null ? `${ts.avgHoldDays}d avg hold · ${ts.pctPositiveDays}% pos days` : ''}
          />
        </div>
      )}

      {result && (
        <TodaysSizePanel sizingParams={{
          strategy: 'Momentum', commodity,
          ...backtestParams,
          vol_target: volTarget,
        }} />
      )}

      {speedFig ? (
        <ChartPanel tooltip={<>All five momentum speed tiers overlaid.<br/><br/>Click any line to select that speed — stats below will update.<br/><br/>Averaged (green) equal-weights all four speed tiers for horizon diversification.</>}>
          <Plot {...speedFig}
            onClick={handleSpeedClick}
            onHover={handleSpeedHover}
            onUnhover={handleSpeedUnhover}
            config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 420, cursor: 'pointer' }} />
        </ChartPanel>
      ) : speedLoading ? (
        <ChartPanel style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 420 }}>
          <Loading message="Computing speed tiers..." />
        </ChartPanel>
      ) : null}

      <SpeedStatsTable speedData={speedData} selectedTier={tier} hoveredTier={hoveredTier} />

      {result && (
        <>
          <div className="detail-chart-grid">
            {maFig ? (
              <ChartPanel tooltip={<>Price with the moving averages used by the selected speed tier.<br/><br/>Dotted lines show current MA levels.<br/>Long signal: fast MA above slow MA.<br/>Short signal: fast MA below slow MA.</>}>
                <Plot {...maFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
              </ChartPanel>
            ) : (
              <ChartPanel style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 250 }}>
                <div className="placeholder-note">Loading price data...</div>
              </ChartPanel>
            )}

            {rollingFig && (
              <ChartPanel tooltip={<>Annualized Sharpe over a rolling 1-year window for <b>{tier}</b>.<br/><br/>Green = positive risk-adjusted returns.<br/>Red = negative. Useful for spotting regime changes.</>}>
                <Plot {...rollingFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
              </ChartPanel>
            )}

            <div className="chart-panel detail-dist-panel">
              <div className="detail-dist-chart">
                <Plot {...returnDistChart(result)} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
              </div>
              {distStats && (
                <div className="detail-dist-stats">
                  <div className="detail-dist-stat">
                    <span className="detail-dist-stat__label">Mean</span>
                    <span className="detail-dist-stat__value">{distStats.meanBp} bp/day</span>
                    <span className="detail-dist-stat__sub">{distStats.meanAnnDollar}/yr</span>
                  </div>
                  <div className="detail-dist-stat">
                    <span className="detail-dist-stat__label">Std Dev</span>
                    <span className="detail-dist-stat__value">{distStats.stdBp} bp</span>
                    <span className="detail-dist-stat__sub">{distStats.stdAnnPct} ann.</span>
                  </div>
                  <div className="detail-dist-stat">
                    <span className="detail-dist-stat__label">Skew</span>
                    <span className="detail-dist-stat__value">{distStats.skew}</span>
                  </div>
                  <div className="detail-dist-stat">
                    <span className="detail-dist-stat__label">Kurtosis</span>
                    <span className="detail-dist-stat__value">{distStats.kurt}</span>
                  </div>
                </div>
              )}
            </div>

            {volFig && (
              <ChartPanel tooltip={<>Strategy leverage via vol targeting for <b>{tier}</b>.<br/><br/>Scalar = target vol / 60-day realized vol.<br/>Below 1 = high vol, leverage reduced.<br/>Above 1 = low vol, leverage increased.</>}>
                <Plot {...volFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
              </ChartPanel>
            )}
          </div>

          <TablePanel heading={`Sample Split Analysis — ${tier} ($1M per period)`} tooltip={<>Add custom date ranges to compare performance across regimes for the selected speed tier.</>}>
            <table className="data-table">
              <thead>
                <tr><th>From</th><th>To</th><th>Days</th><th>Return</th><th>Total PnL</th><th>Sharpe</th><th>Max DD</th><th></th></tr>
              </thead>
              <tbody>
                {samples.map((s, i) => {
                  const stats = computeRangeStats(result, s.from, s.to);
                  return (
                    <tr key={i}>
                      <td><input type="text" className="sample-date-input" defaultValue={s.from} placeholder="YYYY-MM-DD" onBlur={e => updateSample(i, 'from', e.target.value)} onKeyDown={e => e.key === 'Enter' && e.target.blur()} /></td>
                      <td><input type="text" className="sample-date-input" defaultValue={s.to} placeholder="latest" onBlur={e => updateSample(i, 'to', e.target.value)} onKeyDown={e => e.key === 'Enter' && e.target.blur()} /></td>
                      <td>{stats?.days ?? '—'}</td>
                      <td>{stats?.ret ?? '—'}</td>
                      <td>{stats?.pnl ?? '—'}</td>
                      <td>{stats?.sharpe ?? '—'}</td>
                      <td>{stats?.maxDD ?? '—'}</td>
                      <td>{samples.length > 1 && <button className="sample-remove-btn" onClick={() => setSamples(samples.filter((_, j) => j !== i))}>×</button>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="sample-add-row">
              <button className="lab-button" onClick={() => setSamples([...samples, { from: '', to: '' }])}>+ Add Sample</button>
            </div>
          </TablePanel>
        </>
      )}
    </div>
  );
}
