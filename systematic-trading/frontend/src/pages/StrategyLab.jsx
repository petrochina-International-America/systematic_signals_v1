import { useState, useEffect, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import Plot from 'react-plotly.js';
import { useCommodity } from '../hooks/useCommodity';
import { useApi } from '../hooks/useApi';
import { fetchStrategies, fetchFcols, runLab, runSweep, fetchDiagnostics, fetchSplitMetrics } from '../api/client';
import MetricCard from '../components/MetricCard';
import { TablePanel, ChartPanel } from '../components/Panel';
import DataTable from '../components/DataTable';
import Loading, { ErrorNote } from '../components/Loading';
import { priceSpaceFigure, spreadFigure, mtmFigure, sweepHeatmap } from '../charts/labCharts';
import { PLOTLY_CONFIG } from '../charts/theme';

function fmt(val, spec) {
  if (val == null || (typeof val === 'number' && !isFinite(val))) return '—';
  if (spec === 'pct') return `${(val * 100).toFixed(1)}%`;
  if (spec === 'pct0') return `${Math.round(val * 100)}%`;
  if (spec === 'f2') return val.toFixed(2);
  if (spec === '$') return `$${val.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  return String(val);
}

export default function StrategyLab() {
  const { commodity } = useCommodity();
  const [searchParams, setSearchParams] = useSearchParams();
  const { data: meta } = useApi(fetchStrategies);

  const [params, setParams] = useState(() => {
    const p = Object.fromEntries(searchParams.entries());
    return {
      strategy: p.strategy || 'Momentum',
      commodity: p.commodity || commodity,
      tier: p.tier || 'Averaged',
      fast: parseInt(p.fast) || 5,
      slow: parseInt(p.slow) || 60,
      carry_front: p.carry_front || 'F4',
      carry_end: p.carry_end || 'F15',
      pair: p.pair || 'WTI / Brent',
      lookback: parseInt(p.lookback) || 60,
      entry: parseFloat(p.entry) || 1.0,
      exit: parseFloat(p.exit) || 0.0,
      hedge: p.hedge || '50/50',
      roll_tenor: p.roll_tenor || 'Prompt',
      cot_signal: p.cot_signal || 'Follow the Flow',
      cot_fast: parseInt(p.cot_fast) || 4,
      cot_slow: parseInt(p.cot_slow) || 16,
      cot_threshold: parseFloat(p.cot_threshold) || 20,
      vol_target: parseFloat(p.vol_target) || 0.15,
      vol_window: parseInt(p.vol_window) || 120,
    };
  });

  const [result, setResult] = useState(null);
  const [diag, setDiag] = useState(null);
  const [splitData, setSplitData] = useState(null);
  const [sweep, setSweep] = useState(null);
  const [computing, setComputing] = useState(false);
  const [sweepLoading, setSweepLoading] = useState(false);
  const [error, setError] = useState(null);

  const [fcols, setFcols] = useState([]);

  useEffect(() => {
    setParams(p => ({ ...p, commodity }));
  }, [commodity]);

  useEffect(() => {
    fetchFcols(params.commodity).then(r => setFcols(r.fcols)).catch(() => setFcols([]));
  }, [params.commodity]);

  const set = (key, val) => setParams(p => ({ ...p, [key]: val }));

  const compute = useCallback(async () => {
    setComputing(true);
    setError(null);
    try {
      const r = await runLab(params);
      setResult(r);
      setSearchParams(new URLSearchParams(
        Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)])
      ), { replace: true });
      const [d, s] = await Promise.all([
        fetchDiagnostics(r.key),
        fetchSplitMetrics(r.key),
      ]);
      setDiag(d.data);
      setSplitData(s.data);
    } catch (e) {
      setError(e.message);
    } finally {
      setComputing(false);
    }
  }, [params, setSearchParams]);

  useEffect(() => { compute(); }, [compute]);

  async function handleSweep() {
    setSweepLoading(true);
    try {
      const r = await runSweep({
        strategy: params.strategy,
        commodity: params.commodity,
        pair: params.pair,
        roll_tenor: params.roll_tenor,
      });
      setSweep(r);
    } catch (e) {
      setSweep(null);
    } finally {
      setSweepLoading(false);
    }
  }

  const strat = params.strategy;
  const tiers = meta ? Object.keys(meta.momentum_tiers).concat('Custom') : [];
  const pairs = meta?.stat_arb_pairs || [];
  const cotSignals = meta?.cot_signals || [];
  const rollTenors = meta?.roll_tenors || [];

  const m = result?.mtm_metrics || {};

  return (
    <div className="page-content">
      <TablePanel heading="Strategy Configuration — commodity from topbar selector">
        <div className="lab-controls-grid">
          <div className="lab-control">
            <label className="lab-label">Strategy</label>
            <div className="lab-radio">
              {['Momentum', 'Carry', 'Stat-Arb', 'COT'].map(s => (
                <label key={s}>
                  <input type="radio" name="strategy" value={s} checked={strat === s} onChange={() => set('strategy', s)} />
                  {s}
                </label>
              ))}
            </div>
          </div>

          {strat === 'Momentum' && (
            <>
              <div className="lab-control">
                <label className="lab-label">Speed Tier</label>
                <select className="lab-select" value={params.tier} onChange={e => set('tier', e.target.value)}>
                  {tiers.map(t => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              {params.tier === 'Custom' && (
                <>
                  <div className="lab-control">
                    <label className="lab-label">Fast MA (days): {params.fast}</label>
                    <input type="range" min={1} max={60} value={params.fast} onChange={e => set('fast', +e.target.value)} />
                  </div>
                  <div className="lab-control">
                    <label className="lab-label">Slow MA (days): {params.slow}</label>
                    <input type="range" min={5} max={250} step={5} value={params.slow} onChange={e => set('slow', +e.target.value)} />
                  </div>
                </>
              )}
            </>
          )}

          {strat === 'Carry' && (
            <div className="lab-control">
              <label className="lab-label">Carry Legs (Front / End)</label>
              <div className="lab-subrow">
                <select className="lab-select" value={params.carry_front} onChange={e => set('carry_front', e.target.value)}>
                  {fcols.map(c => <option key={c} value={c}>{c}</option>)}
                </select>
                <select className="lab-select" value={params.carry_end} onChange={e => set('carry_end', e.target.value)}>
                  {fcols.map(c => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
            </div>
          )}

          {strat === 'Stat-Arb' && (
            <>
              <div className="lab-control">
                <label className="lab-label">Pair</label>
                <select className="lab-select" value={params.pair} onChange={e => set('pair', e.target.value)}>
                  {pairs.map(p => <option key={p.label} value={p.label}>{p.label}</option>)}
                </select>
              </div>
              <div className="lab-control">
                <label className="lab-label">Lookback (days): {params.lookback}</label>
                <input type="range" min={5} max={250} step={5} value={params.lookback} onChange={e => set('lookback', +e.target.value)} />
              </div>
              <div className="lab-control">
                <label className="lab-label">Entry threshold (σ): {params.entry.toFixed(2)}</label>
                <input type="range" min={0.5} max={3} step={0.05} value={params.entry} onChange={e => set('entry', +e.target.value)} />
              </div>
              <div className="lab-control">
                <label className="lab-label">Exit threshold (σ): {params.exit.toFixed(2)}</label>
                <input type="range" min={0} max={1.5} step={0.05} value={params.exit} onChange={e => set('exit', +e.target.value)} disabled={params.hedge === 'ols'} />
              </div>
              <div className="lab-control">
                <label className="lab-label">Hedge Ratio</label>
                <div className="lab-radio">
                  {[['50/50', '50/50 notional'], ['ols', 'OLS β (rolling)']].map(([v, l]) => (
                    <label key={v}><input type="radio" name="hedge" value={v} checked={params.hedge === v} onChange={() => set('hedge', v)} />{l}</label>
                  ))}
                </div>
              </div>
              <div className="lab-control">
                <label className="lab-label">Roll Tenor</label>
                <div className="lab-radio">
                  {rollTenors.map(t => (
                    <label key={t}><input type="radio" name="roll_tenor" value={t} checked={params.roll_tenor === t} onChange={() => set('roll_tenor', t)} />{t}</label>
                  ))}
                </div>
              </div>
            </>
          )}

          {strat === 'COT' && (
            <>
              <div className="lab-control">
                <label className="lab-label">COT Signal</label>
                <div className="lab-radio">
                  {cotSignals.map(s => (
                    <label key={s}><input type="radio" name="cot_signal" value={s} checked={params.cot_signal === s} onChange={() => set('cot_signal', s)} />{s}</label>
                  ))}
                </div>
              </div>
              {params.cot_signal === 'Follow the Flow' && (
                <>
                  <div className="lab-control">
                    <label className="lab-label">Fast MA (weeks): {params.cot_fast}</label>
                    <input type="range" min={2} max={26} value={params.cot_fast} onChange={e => set('cot_fast', +e.target.value)} />
                  </div>
                  <div className="lab-control">
                    <label className="lab-label">Slow MA (weeks): {params.cot_slow}</label>
                    <input type="range" min={4} max={52} value={params.cot_slow} onChange={e => set('cot_slow', +e.target.value)} />
                  </div>
                </>
              )}
              {params.cot_signal === 'Fade the Crowd' && (
                <div className="lab-control">
                  <label className="lab-label">SI Threshold (%): {params.cot_threshold}</label>
                  <input type="range" min={5} max={45} value={params.cot_threshold} onChange={e => set('cot_threshold', +e.target.value)} />
                </div>
              )}
            </>
          )}
        </div>

        <div className="lab-divider" />

        <div className="lab-controls-grid">
          <div className="lab-control">
            <label className="lab-label">Vol Target (ann. %): {Math.round(params.vol_target * 100)}%</label>
            <input type="range" min={5} max={25} value={Math.round(params.vol_target * 100)} onChange={e => set('vol_target', e.target.value / 100)} />
          </div>
          <div className="lab-control">
            <label className="lab-label">Vol Window (days): {params.vol_window}</label>
            <input type="range" min={20} max={250} step={10} value={params.vol_window} onChange={e => set('vol_window', +e.target.value)} />
          </div>
        </div>
      </TablePanel>

      {error && <ErrorNote message={error} />}
      {computing && <Loading message="Computing backtest..." />}

      <div className="metric-row">
        <MetricCard label="MTM Sharpe" value={fmt(m.Sharpe, 'f2')} sub="full sample" />
        <MetricCard label="MTM CAGR" value={fmt(m.CAGR, 'pct')} sub="full sample" />
        <MetricCard label="MTM Max Drawdown" value={fmt(m.Drawdown, 'pct')} sub="full sample" color="var(--red)" />
        <MetricCard label="MTM Total PnL" value={fmt(m['Total PnL'], '$')} sub="full sample, $" color="var(--green)" />
      </div>

      {result && (
        <div className="chart-grid-2">
          <ChartPanel>
            <Plot
              {...(result.kind === 'pair' ? spreadFigure(result) : priceSpaceFigure(result))}
              config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 620 }}
            />
          </ChartPanel>
          <ChartPanel>
            <Plot
              {...mtmFigure(result, params.vol_target)}
              config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 620 }}
            />
          </ChartPanel>
        </div>
      )}

      {(strat === 'Momentum' || strat === 'Stat-Arb') && (
        <TablePanel
          heading="Parameter Sweep — stable Sharpe regions"
          action={<button className="lab-button" onClick={handleSweep} disabled={sweepLoading}>{sweepLoading ? 'Computing...' : 'Run Sweep'}</button>}
        >
          {sweep ? (
            <Plot {...sweepHeatmap(sweep)} config={PLOTLY_CONFIG} useResizeHandler style={{ width: '100%', height: 440 }} />
          ) : (
            <div className="placeholder-note" style={{ margin: 16 }}>
              Click "Run Sweep" to compute the Sharpe grid
            </div>
          )}
        </TablePanel>
      )}

      {diag && (
        <TablePanel heading="Diagnostics — Full / Pre-Ukraine / Post-Ukraine (MTM)">
          <DataTable
            columns={Object.keys(diag[0] || {}).map(k => ({ id: k, name: k }))}
            data={diag}
          />
        </TablePanel>
      )}

      {splitData && (
        <TablePanel heading="Sample-Split Analytics (Full + Ukraine splits + YoY)">
          <DataTable
            columns={Object.keys(splitData[0] || {}).map(k => ({ id: k, name: k }))}
            data={splitData.map(row => Object.fromEntries(
              Object.entries(row).map(([k, v]) => [k, typeof v === 'number' ? v.toFixed(3) : v])
            ))}
          />
        </TablePanel>
      )}
    </div>
  );
}
