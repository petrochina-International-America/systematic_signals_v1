import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import Plot from 'react-plotly.js';
import { fetchFcols, fetchPrices } from '../api/client';
import { useBacktest, fmt, SAMPLE_OPTIONS } from '../hooks/useBacktest';
import MetricCard from '../components/MetricCard';
import TodaysSizePanel from '../components/TodaysSizePanel';
import { TablePanel, ChartPanel } from '../components/Panel';
import Loading, { ErrorNote } from '../components/Loading';
import {
  heroEquityChart, rollingSharpeChart, returnDistChart, volScalarChart,
  forwardCurveEvolution, computeDistStats, computeRangeStats, computeSampleMetrics, computeTradeStats,
} from '../charts/detailCharts';
import { PLOTLY_CONFIG } from '../charts/theme';

const RATIONALE = [
  { label: 'Long', text: 'Backwardation (prompt > deferred)' },
  { label: 'Short', text: 'Contango (prompt < deferred)' },
  { label: 'Edge', text: 'Long tight supply, short oversupply — roll premium harvesting strategy' },
];

export default function CarryDetail() {
  const { commodity, strategy } = useParams();
  const navigate = useNavigate();
  const isCarry = strategy === 'Carry';

  const [carryFront, setCarryFront] = useState('F1');
  const [carryEnd, setCarryEnd] = useState('F13');
  const [epsilon, setEpsilon] = useState(0);
  const [fcols, setFcols] = useState([]);
  const [priceData, setPriceData] = useState(null);

  const {
    result, loading, error,
    volTarget, volTargetPct, setVolTargetPct,
    sampleIdx, setSampleIdx, sampleDays,
    samples, setSamples, updateSample,
  } = useBacktest({
    strategy: strategy,
    commodity,
    params: isCarry
      ? { carry_front: carryFront, carry_end: carryEnd, epsilon }
      : {},
  });

  useEffect(() => {
    if (isCarry) fetchFcols(commodity).then(r => setFcols(r.fcols)).catch(() => {});
  }, [commodity, isCarry]);

  useEffect(() => {
    if (result) fetchPrices(commodity).then(setPriceData).catch(() => {});
  }, [result, commodity]);

  if (loading && !result) return <div className="page-content"><Loading message="Computing backtest..." /></div>;
  if (error) return <div className="page-content"><ErrorNote message={error} /></div>;
  if (!result) return null;

  const m = computeSampleMetrics(result, sampleDays) || {};
  const mAll = computeSampleMetrics(result, null) || {};
  const ts = computeTradeStats(result, sampleDays) || {};
  const rollingFig = rollingSharpeChart(result);
  const volFig = volScalarChart(result, sampleDays);
  const distStats = computeDistStats(result);
  const curveFig = forwardCurveEvolution(priceData);

  return (
    <div className="page-content">
      <div className="detail-toprow">
        <button className="signal-back-btn" onClick={() => navigate('/signals')}>&larr; Back to Signals</button>
      </div>

      <div className="detail-header">
        <div className="detail-header__top">
          <div className="detail-header__left">
            <div className="detail-header__title">{commodity}</div>
            <div className="detail-header__subtitle">{strategy} · $1M capital</div>
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
          {isCarry && fcols.length > 0 && (
            <>
              <label className="detail-header__label">Front <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup tip-popup--right">Front leg of the curve slope. Default F1 (prompt).</span></span></label>
              <select className="commodity-select" value={carryFront} onChange={e => setCarryFront(e.target.value)}>
                {fcols.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
              <label className="detail-header__label">Back <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Back leg of the curve slope. Default F13 (~1 year out) to filter out seasonality.</span></span></label>
              <select className="commodity-select" value={carryEnd} onChange={e => setCarryEnd(e.target.value)}>
                {fcols.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
              <div className="detail-header__sep" />
              <label className="detail-header__label">Carry Buffer <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Filters strength of backwardation/contango by adding a buffer as a % of the 60-day average spread.<br/><br/>To trade, the spread must clear this % gap — keeps you out of weak or ambiguous carry signals.</span></span></label>
              <select className="commodity-select" value={epsilon} onChange={e => setEpsilon(+e.target.value)}>
                {[0, 5, 10, 20, 35, 50].map(v => <option key={v} value={v}>{v === 0 ? 'Off' : `${v}%`}</option>)}
              </select>
              <div className="detail-header__sep" />
            </>
          )}
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

      <div className="metric-row metric-row--5">
        <MetricCard label="Sharpe" value={fmt(m.Sharpe, 'f2')} sub={`${SAMPLE_OPTIONS[sampleIdx].label} · all-time ${fmt(mAll.Sharpe, 'f2')}`} />
        <MetricCard label="CAGR" value={fmt(m.CAGR, 'pct')} sub="annualized" />
        <MetricCard label="Total PnL" value={fmt(m['Total PnL'], '$')} sub="$1M capital" color="var(--green)" />
        <MetricCard label="Max Drawdown" value={fmt(m.Drawdown, 'pct')} sub="peak to trough" color="var(--red)" />
        <MetricCard
          label="Trade Profile"
          value={ts.tradesPerYear != null ? `${ts.tradesPerYear} trades/yr` : '—'}
          sub={ts.avgHoldDays != null ? `${ts.avgHoldDays}d avg hold · ${ts.pctPositiveDays}% pos days` : ''}
        />
      </div>

      <TodaysSizePanel sizingParams={{
        strategy, commodity,
        carry_front: isCarry ? carryFront : undefined,
        carry_end: isCarry ? carryEnd : undefined,
        epsilon: isCarry ? epsilon : undefined,
        vol_target: volTarget,
      }} />

      <ChartPanel tooltip={<>Equity curve starting at $1M, sized to the vol target.<br/><br/>Red shading = drawdown from peak.<br/>Bar below: green = long, red = short.</>}>
        <Plot {...heroEquityChart(result, volTarget, 1_000_000, sampleDays)}
          config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 360 }} />
      </ChartPanel>

      <div className="detail-chart-grid">
        {curveFig ? (
          <ChartPanel tooltip={<>Daily forward curve snapshots over the last month.<br/><br/>Darker lines = more recent.<br/>Shows how the term structure is steepening or flattening.</>}>
            <Plot {...curveFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
          </ChartPanel>
        ) : (
          <ChartPanel style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 250 }}>
            <div className="placeholder-note">Loading forward curve...</div>
          </ChartPanel>
        )}

        {rollingFig && (
          <ChartPanel tooltip={<>Annualized Sharpe over a rolling 1-year window.<br/><br/>Green = positive risk-adjusted returns.<br/>Red = negative. Useful for spotting regime changes.</>}>
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
                <span className="detail-dist-stat__label">Mean <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Average daily return in basis points.<br/><br/>Positive = profitable on an average day.<br/>Dollar figure annualizes this on $1M capital.</span></span></span>
                <span className="detail-dist-stat__value">{distStats.meanBp} bp/day</span>
                <span className="detail-dist-stat__sub">{distStats.meanAnnDollar}/yr</span>
              </div>
              <div className="detail-dist-stat">
                <span className="detail-dist-stat__label">Std Dev <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Daily P&L volatility.<br/>Annualized by ×√252.</span></span></span>
                <span className="detail-dist-stat__value">{distStats.stdBp} bp</span>
                <span className="detail-dist-stat__sub">{distStats.stdAnnPct} ann.</span>
              </div>
              <div className="detail-dist-stat">
                <span className="detail-dist-stat__label">Skew <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Return asymmetry.<br/><br/>Negative = fat left tail — large losses happen more often than large gains.<br/>Positive is preferable.</span></span></span>
                <span className="detail-dist-stat__value">{distStats.skew}</span>
              </div>
              <div className="detail-dist-stat">
                <span className="detail-dist-stat__label">Kurtosis <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Tail fatness vs. a normal distribution (baseline = 0).<br/><br/>Above 3 = significant extreme-day risk in both directions.</span></span></span>
                <span className="detail-dist-stat__value">{distStats.kurt}</span>
              </div>
            </div>
          )}
        </div>

        {volFig && (
          <ChartPanel tooltip={<>Strategy leverage via vol targeting.<br/><br/>Scalar = target vol ÷ 60-day realized vol.<br/>Below 1 = high vol, leverage reduced.<br/>Above 1 = low vol, leverage increased.</>}>
            <Plot {...volFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
          </ChartPanel>
        )}
      </div>

      <TablePanel heading="Sample Split Analysis ($1M per period)" tooltip={<>Add custom date ranges to compare performance across regimes.<br/><br/>Use for walk-forward validation, pre/post event analysis, or seasonal testing.</>}>
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
    </div>
  );
}
