import { useState, useEffect, useCallback } from 'react';
import { runLab } from '../api/client';

export const SAMPLE_OPTIONS = [
  { label: '1Y', days: 252 },
  { label: '3Y', days: 756 },
  { label: '5Y', days: 1260 },
  { label: 'Full', days: null },
];

export const DEFAULT_VOL_TARGET = 15;

export function fmt(val, spec) {
  if (val == null || (typeof val === 'number' && !isFinite(val))) return '—';
  if (spec === 'pct') return `${(val * 100).toFixed(1)}%`;
  if (spec === 'f2') return val.toFixed(2);
  if (spec === '$') return `$${val.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  return String(val);
}

export function useBacktest({ strategy, commodity, params }) {
  const [volTargetPct, setVolTargetPct] = useState(DEFAULT_VOL_TARGET);
  const [sampleIdx, setSampleIdx] = useState(0);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [samples, setSamples] = useState([
    { from: '2015-01-01', to: '2022-02-24' },
    { from: '2022-02-24', to: new Date().getFullYear() + '-01-01' },
    { from: new Date().getFullYear() + '-01-01', to: '' },
  ]);

  const volTarget = volTargetPct / 100;
  const sampleDays = SAMPLE_OPTIONS[sampleIdx].days;
  const paramKey = JSON.stringify(params);

  const compute = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const isStatArb = strategy === 'Stat-Arb';
      const body = {
        strategy,
        vol_target: volTargetPct / 100,
        ...(isStatArb ? { pair: commodity } : { commodity }),
        ...JSON.parse(paramKey),
      };
      const r = await runLab(body);
      setResult(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [commodity, strategy, volTargetPct, paramKey]);

  useEffect(() => { compute(); }, [compute]);

  function snapToTradingDay(dateStr) {
    const tradingDays = result?.mtm?.dates || [];
    if (!dateStr || !tradingDays.length) return dateStr;
    const d = dateStr.replace(/\//g, '-');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return dateStr;
    for (let i = tradingDays.length - 1; i >= 0; i--) {
      if (tradingDays[i] <= d) return tradingDays[i];
    }
    return tradingDays[0];
  }

  function updateSample(idx, field, value) {
    const snapped = snapToTradingDay(value);
    const next = [...samples];
    next[idx] = { ...next[idx], [field]: snapped };
    setSamples(next);
  }

  return {
    result, loading, error,
    volTarget, volTargetPct, setVolTargetPct,
    sampleIdx, setSampleIdx, sampleDays,
    samples, setSamples, updateSample,
  };
}
