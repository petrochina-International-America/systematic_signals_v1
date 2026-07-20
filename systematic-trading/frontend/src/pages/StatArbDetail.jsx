import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import Plot from 'react-plotly.js';
import { runSweep, fetchStrategies } from '../api/client';
import { useApi } from '../hooks/useApi';
import { useBacktest, fmt, SAMPLE_OPTIONS } from '../hooks/useBacktest';
import MetricCard from '../components/MetricCard';
import TodaysSizePanel from '../components/TodaysSizePanel';
import { TablePanel, ChartPanel } from '../components/Panel';
import Loading, { ErrorNote } from '../components/Loading';
import {
  heroEquityChart, rollingSharpeChart, returnDistChart,
  zscoreBandsChart, spreadLevelChart, sweepHeatmapChart,
  computeDistStats, computeRangeStats, computeSampleMetrics, computeTradeStats,
  computeHitRate,
} from '../charts/detailCharts';
import { PLOTLY_CONFIG } from '../charts/theme';

// Tenor selector: contract placement for BOTH legs (delivery-month lead on
// the coordinated engine — Prompt=0, Q2=+3mo, Q3=+6mo, 1yr=+12mo — with the
// cross-leg month offset preserved and the roll cadence still anchored to
// the prompt cycle). Deferred tenors trade less and carried less edge in
// research: an exploration control, not a recommendation.
const ROLL_TENORS = ['Prompt', 'Q2', 'Q3', '1yr'];

// Per-pair defaults (lookback, entry, month_offset, cross_arb) come from
// the backend's shared config (/api/lab/strategies ->
// energy.analytics.signal_summary.STAT_ARB_PAIR_DEFAULTS) — the SAME object
// the Signals and Levels pages read. Nothing pair-specific is hardcoded
// here, so this page's first load cannot drift from those cards.
// FALLBACK is only for a pair the backend doesn't know at all.
const FALLBACK_PAIR_DEF = { lookback: 20, entry: 1.5, month_offset: 0, cross_arb: false };

const RATIONALE = [
  { label: 'Long', text: 'Spread below historical mean — buy the undervalued leg' },
  { label: 'Short', text: 'Spread above historical mean — sell the overvalued leg' },
  { label: 'Edge', text: 'Physical arb and substitution anchor spreads — extremes mean-revert' },
];

export default function StatArbDetail() {
  const { commodity: pair } = useParams();
  const { data: meta, error: metaError } = useApi(fetchStrategies);

  if (metaError) return <div className="page-content"><ErrorNote message={metaError} /></div>;
  if (!meta) return <div className="page-content"><Loading message="Loading pair defaults..." /></div>;

  const pairDef = meta.stat_arb_pair_defaults?.[pair] || FALLBACK_PAIR_DEF;
  // key={pair} remounts on pair change so all controls re-initialize from
  // the incoming pair's shared defaults.
  return <StatArbDetailInner key={pair} pair={pair} pairDef={pairDef} />;
}

function StatArbDetailInner({ pair, pairDef }) {
  const navigate = useNavigate();

  const [lookback, setLookback] = useState(pairDef.lookback);
  const [entry, setEntry] = useState(pairDef.entry);
  // Entry-band units: 'zscore' (σ, production default) | 'dollar' ($/bbl —
  // Bouchouev/Zuo convention, RESEARCH until the regime test clears it).
  // Separate threshold state per mode so toggling never reinterprets units.
  const [bandMode, setBandMode] = useState(pairDef.band_mode || 'zscore');
  const [dollarEntry, setDollarEntry] = useState(1.0);
  const [rollTenor, setRollTenor] = useState('Prompt');
  const [monthOffset, setMonthOffset] = useState(pairDef.month_offset); // -1 | 0
  const [sweepData, setSweepData] = useState(null);

  const isDollarBand = bandMode === 'dollar';
  const effectiveEntry = isDollarBand ? dollarEntry : entry;

  // Exit rule is always mean-cross (flat when the deviation crosses zero) —
  // the match-entry variant was removed 2026-07-16. Sizing is identity
  // (vol scalar ≡ 1.0), so there is no vol-target control on this page.
  const {
    result, loading, error,
    volTarget,
    sampleIdx, setSampleIdx, sampleDays,
    samples, setSamples, updateSample,
  } = useBacktest({
    strategy: 'Stat-Arb',
    commodity: pair,
    params: {
      lookback, entry: effectiveEntry,
      band_mode: bandMode, hedge: '50/50',
      roll_tenor: rollTenor, month_offset: monthOffset,
    },
  });

  useEffect(() => {
    runSweep({ strategy: 'Stat-Arb', pair, roll_tenor: rollTenor, band_mode: bandMode, month_offset: monthOffset })
      .then(setSweepData)
      .catch(() => {});
  }, [pair, rollTenor, bandMode, monthOffset]);

  if (loading && !result) return <div className="page-content"><Loading message="Computing backtest..." /></div>;
  if (error) return <div className="page-content"><ErrorNote message={error} /></div>;
  if (!result) return null;

  const m = computeSampleMetrics(result, sampleDays) || {};
  const mAll = computeSampleMetrics(result, null) || {};
  const ts = computeTradeStats(result, sampleDays) || {};
  const hr = computeHitRate(result, sampleDays);
  // Strategy params flow through `result` (auto-recomputed by useBacktest on
  // any control change). The vol-scalar panel follows the 1Y/3Y/5Y/Full
  // metrics window; the two SIGNAL panels (z-score, trade view) default to a
  // 3-month monitoring view (~63 trading days) — pan / range buttons reach
  // the full history.
  const SIGNAL_VIEW_DAYS = 63;
  const rollingFig = rollingSharpeChart(result);
  const distStats = computeDistStats(result);
  const zscoreFig = zscoreBandsChart(result, SIGNAL_VIEW_DAYS);
  const spreadFig = spreadLevelChart(result, SIGNAL_VIEW_DAYS);

  const sweepWithCursor = sweepData ? { ...sweepData, cur_x: effectiveEntry, cur_y: lookback } : null;
  const heatmapFig = sweepHeatmapChart(sweepWithCursor);

  return (
    <div className="page-content">
      <div className="detail-toprow">
        <button className="signal-back-btn" onClick={() => navigate('/signals')}>&larr; Back to Signals</button>
      </div>

      <div className="detail-header">
        <div className="detail-header__top">
          <div className="detail-header__left">
            <div className="detail-header__title">
              {pair?.replace(/ \/ /g, ' − ')}
              {(pairDef.research_only || isDollarBand) && (
                <span
                  title={isDollarBand && !pairDef.research_only
                    ? 'Dollar-band entry mode is a research configuration (2026-07-15 regime test: a fixed $ threshold over-fires when spread vol rises). Not a tradable default.'
                    : "This pair's default config has not cleared the desk's pre-registered 1.0 pre-cost Sharpe floor. Research view — not a tradable signal."}
                  style={{
                    marginLeft: '0.6em', padding: '0.15em 0.5em', fontSize: '0.45em',
                    fontWeight: 700, letterSpacing: '0.08em', verticalAlign: 'middle',
                    color: '#b45309', background: 'rgba(217, 119, 6, 0.15)',
                    border: '1px solid rgba(217, 119, 6, 0.45)', borderRadius: '4px',
                  }}
                >
                  {isDollarBand && !pairDef.research_only ? '$-BAND — RESEARCH' : 'RESEARCH-ONLY'}
                </span>
              )}
            </div>
            <div className="detail-header__subtitle">Stat-Arb · $1M capital</div>
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
          <label className="detail-header__label">Lookback <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">Rolling window (trading days) for computing the mean and standard deviation of the spread.<br/><br/>Shorter = more reactive to recent moves. Longer = smoother, fewer trades.</span></span></label>
          <select className="commodity-select" value={lookback} onChange={e => setLookback(+e.target.value)}>
            {[10, 20, 40, 60, 90, 120, 180, 250].map(v => <option key={v} value={v}>{v}d</option>)}
          </select>
          <label className="detail-header__label">Band <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup" style={{ width: 300 }}>
            Units of the entry threshold. Everything else (lookback, roll-adjusted signal series, direction logic, exit rule, sizing) is identical.<br/><br/>
            <b>Z-score</b> — deviation / rolling std (σ). Self-adjusts when spread volatility shifts. Production convention.<br/><br/>
            <b>$ band</b> — absolute $/bbl deviation from the rolling mean (Bouchouev &amp; Zuo 2020). RESEARCH: a fixed $ threshold fires more often when vol rises, so post-2020 it trades near-constantly by construction (see notes/dollar_band_regime_test_2026-07-15.md).
          </span></span></label>
          <div className="detail-header__toggles">
            <button className={`signals-toggle${!isDollarBand ? ' signals-toggle--active' : ''}`} onClick={() => setBandMode('zscore')}>Z-score</button>
            <button className={`signals-toggle${isDollarBand ? ' signals-toggle--active' : ''}`} onClick={() => setBandMode('dollar')}>$ band</button>
          </div>
          <label className="detail-header__label">Entry {isDollarBand ? '$' : 'Z'} <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup">{isDollarBand
            ? <>Dollar threshold for entering a trade — enter when the spread deviates more than $X/bbl from its rolling mean.<br/><br/>Bouchouev &amp; Zuo's canonical value for WTI-Brent is $1.00 (≈ pipeline transport + quality differential).</>
            : <>Z-score threshold for entering a trade.<br/><br/>Higher = fewer but stronger signals. 1.0σ = ~16% of observations in one tail under normality.</>}</span></span></label>
          {isDollarBand ? (
            <select className="commodity-select" value={dollarEntry} onChange={e => setDollarEntry(+e.target.value)}>
              {[0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5].map(v => <option key={v} value={v}>${v.toFixed(2)}</option>)}
            </select>
          ) : (
            <select className="commodity-select" value={entry} onChange={e => setEntry(+e.target.value)}>
              {[0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0].map(v => <option key={v} value={v}>{v}σ</option>)}
            </select>
          )}
          {!isDollarBand && entry <= 0.5 && (
            <span
              title="0.5σ is the busiest, weakest-per-trade cell: per-trade edge rises with entry |z|, and the 'more trades' hypothesis was refuted in research. Use for signal visibility, not performance."
              style={{
                padding: '0.15em 0.5em', fontSize: '0.62em', fontWeight: 700,
                letterSpacing: '0.05em', alignSelf: 'center',
                color: '#b45309', background: 'rgba(217, 119, 6, 0.15)',
                border: '1px solid rgba(217, 119, 6, 0.45)', borderRadius: '4px',
              }}
            >
              HIGH TURNOVER / LOW PER-TRADE EDGE
            </span>
          )}
          <label className="detail-header__label">Tenor <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup" style={{ width: 300 }}>
            Contract placement for BOTH legs — shifts the position out the curve while preserving the legs' relative delivery-month offset and the prompt-anchored roll cadence.<br/><br/>
            <b>Prompt</b> — front placement (production default).<br/>
            <b>Q2</b> — ~3 months out. <b>Q3</b> — ~6 months out. <b>1yr</b> — ~12 months out.<br/><br/>
            Deferred tenors trade less and carried less edge in research — an exploration control, not a recommendation.
          </span></span></label>
          <select className="commodity-select" value={rollTenor} onChange={e => setRollTenor(e.target.value)}>
            {ROLL_TENORS.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          <div className="detail-header__sep" />
          <label className="detail-header__label">Sample</label>
          <div className="detail-header__toggles">
            {SAMPLE_OPTIONS.map((opt, i) => (
              <button key={opt.label} className={`signals-toggle${i === sampleIdx ? ' signals-toggle--active' : ''}`}
                onClick={() => setSampleIdx(i)}>{opt.label}</button>
            ))}
          </div>
          {pairDef.cross_arb && (<>
            <div className="detail-header__sep" />
            <label className="detail-header__label">Desired Exposure
              <span className="tip-wrap"><span className="tip-icon">?</span><span className="tip-popup" style={{ width: 300 }}>
                Delivery-month relationship between the two legs (leg1 relative to leg2).<br/><br/>
                <b>Spread</b> — same delivery month (Bloomberg-validated matched construction, ≈ S:ENCO).<br/>
                <b>Cross-Arb</b> — WTI one delivery month earlier than Brent: the market-convention prompt pairing (CL front is structurally one month before CO front). Default for this pair.
              </span></span>
            </label>
            <div className="detail-header__toggles">
              {[[0, 'Spread'], [-1, 'Cross-Arb']].map(([v, label]) => (
                <button key={v} className={`signals-toggle${monthOffset === v ? ' signals-toggle--active' : ''}`}
                  onClick={() => setMonthOffset(v)}>{label}</button>
              ))}
            </div>
          </>)}
        </div>
      </div>

      <div className="metric-row metric-row--6">
        <MetricCard label="Sharpe" value={fmt(m.Sharpe, 'f2')} sub={`${SAMPLE_OPTIONS[sampleIdx].label} · all-time ${fmt(mAll.Sharpe, 'f2')}`} />
        <MetricCard label="CAGR" value={fmt(m.CAGR, 'pct')} sub="annualized" />
        <MetricCard label="Total PnL" value={fmt(m['Total PnL'], '$')} sub="$1M capital" color="var(--green)" />
        <MetricCard label="Max Drawdown" value={fmt(m.Drawdown, 'pct')} sub="peak to trough" color="var(--red)" />
        <MetricCard
          label="Hit Rate"
          value={hr ? `${(hr.hitRate * 100).toFixed(1)}%` : '—'}
          sub={hr ? `${hr.nClosed} closed trades · gross` : 'no closed trades'}
        />
        <MetricCard
          label="Trade Profile"
          value={ts.tradesPerYear != null ? `${ts.tradesPerYear} trades/yr` : '—'}
          sub={ts.avgHoldDays != null ? `${ts.avgHoldDays}d avg hold · ${ts.pctPositiveDays}% pos days` : ''}
        />
      </div>

      <TodaysSizePanel sizingParams={{
        strategy: 'Stat-Arb', pair,
        lookback, entry: effectiveEntry,
        band_mode: bandMode, hedge: '50/50',
        roll_tenor: rollTenor, month_offset: monthOffset,
        vol_target: volTarget,
      }} />

      <ChartPanel tooltip={<>Equity curve starting at $1M, sized to the vol target.<br/><br/>Red shading = drawdown from peak.<br/>Bar below: green = long spread, red = short spread.</>}>
        <Plot {...heroEquityChart(result, volTarget, 1_000_000, sampleDays)}
          config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 360 }} />
      </ChartPanel>

      <div className="detail-chart-grid">
        {zscoreFig && (
          <ChartPanel tooltip={<>Z-score of the roll-adjusted signal series vs its rolling mean (computed on cumulative tradable spread moves, so roll gaps never bias it).{isDollarBand && <><br/><br/><b>$-band mode:</b> entries fire on the raw $ deviation, not this z-score — the panel stays for vol context; the trade view's bands (mean ± ${dollarEntry.toFixed(2)}) show the actual trigger.</>}<br/><br/>{isDollarBand ? 'Entry when the $ deviation crosses the band.' : 'Entry when z-score crosses a threshold band.'}<br/>Exit when the spread reverts through its rolling mean (mean-cross).</>}>
            <Plot {...zscoreFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
          </ChartPanel>
        )}

        {spreadFig && (
          <ChartPanel tooltip={<>The trade view: the QUOTED spread (leg1 − leg2 settles — the number you'd see on screen) with the strategy's rolling mean and entry bands, plus the actual signal events — ▲ long entry, ▼ short entry, ✕ exit, and green/red shading while a position is on. Shows WHERE each trade fired in dollar terms and what the spread did while held.<br/><br/>The mean and bands are computed on the roll-adjusted signal series (no roll gaps — the series the z-score reads), re-anchored to today's quote so everything shares one dollar axis; the quoted line and the signal series differ only by within-window roll carry (typically under $1).</>}>
            <Plot {...spreadFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
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

        {rollingFig && (
          <ChartPanel tooltip={<>Annualized Sharpe over a rolling 1-year window.<br/><br/>Green = positive risk-adjusted returns.<br/>Red = negative.</>}>
            <Plot {...rollingFig} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 250 }} />
          </ChartPanel>
        )}

        {/* Vol-scalar panel removed 2026-07-16: stat-arb sizing is identity
            (vol scalar ≡ 1.0) — a flat line at 1.0 carries no information. */}
        {heatmapFig && (
          <ChartPanel tooltip={<>Price-space Sharpe across all lookback × entry threshold combinations.<br/><br/>Click a cell to load those parameters. White box = current.</>}>
            <Plot {...heatmapFig} config={PLOTLY_CONFIG} useResizeHandler
              style={{ width: '100%', height: heatmapFig.layout.height || 250 }}
              onClick={(e) => {
                const pt = e.points?.[0];
                if (!pt || !heatmapFig._labelX || !heatmapFig._labelY) return;
                const xi = pt.pointIndex?.[1] ?? pt.x;
                const yi = pt.pointIndex?.[0] ?? pt.y;
                const entryLabel = heatmapFig._labelX[xi];
                const lookbackLabel = heatmapFig._labelY[yi];
                if (entryLabel != null) {
                  const v = parseFloat(entryLabel);
                  if (isDollarBand) setDollarEntry(v); else setEntry(v);
                }
                if (lookbackLabel != null) setLookback(parseInt(lookbackLabel, 10));
              }}
            />
          </ChartPanel>
        )}
      </div>

      <TablePanel heading="Sample Split Analysis ($1M per period)" tooltip={<>Add custom date ranges to compare performance across regimes.</>}>
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
