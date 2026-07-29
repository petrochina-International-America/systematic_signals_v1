import { DATA_SOURCES } from '../config/dataSources';

const BASE = '/api';

// Prefix for components flipped to the published-snapshot source.
const src = component => (DATA_SOURCES[component] === 'db' ? '/db' : '');

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `API ${res.status}: ${res.statusText}`);
  }
  return res.json();
}

function get(path) {
  return request(path);
}

function post(path, body) {
  return request(path, { method: 'POST', body: JSON.stringify(body) });
}

// ── Market Data ─────────────────────────────────────────────────────────────

export function fetchCommodities() {
  return get('/market-data/commodities');
}

export function fetchHealth() {
  return get('/health');
}

export function fetchPrices(commodity, params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`/market-data/prices/${commodity}${qs ? '?' + qs : ''}`);
}

export function fetchFrontMonth(commodity, params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`/market-data/prices/${commodity}/front${qs ? '?' + qs : ''}`);
}

// ── Signals ─────────────────────────────────────────────────────────────────

export function fetchSignalSnapshot() {
  return get(`${src('signalsSnapshot')}/signals/snapshot`);
}

export function fetchSpreadSnapshot(params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`${src('signalsSpreads')}/signals/spreads${qs ? '?' + qs : ''}`);
}

export function fetchTopPerformers(n = 5) {
  return get(`${src('topPerformers')}/signals/top-performers?n=${n}`);
}

// ── Published-snapshot freshness (the permanent post-migration guard) ───────

export function fetchDbMeta() {
  return get('/db/meta');
}

// ── COT ─────────────────────────────────────────────────────────────────────

export function fetchCotStatus() {
  return get('/cot/status');
}

export function fetchCotSnapshot(commodities) {
  const qs = commodities ? `?commodities=${commodities.join(',')}` : '';
  return get(`/cot/snapshot${qs}`);
}

export function fetchCot(commodity, params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`/cot/${commodity}${qs ? '?' + qs : ''}`);
}

export function fetchFollowTheFlow(commodity, params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`/cot/${commodity}/follow-the-flow${qs ? '?' + qs : ''}`);
}

export function fetchFadeTheCrowd(commodity, params = {}) {
  const qs = new URLSearchParams(params).toString();
  return get(`/cot/${commodity}/fade-the-crowd${qs ? '?' + qs : ''}`);
}

// ── Levels ─────────────────────────────────────────────────────────────────

export function fetchLevelsProximity(tenor = 1) {
  return get(`${src('levels')}/levels/proximity?tenor=${tenor}`);
}

// ── Lab ─────────────────────────────────────────────────────────────────────

export function fetchStrategies() {
  return get('/lab/strategies');
}

export function fetchLabCommodities() {
  return get('/lab/commodities');
}

export function fetchFcols(commodity) {
  return get(`/lab/commodities/${commodity}/fcols`);
}

export function runLab(params) {
  return post('/lab/run', params);
}

export function fetchMomentumSpeeds(commodity, volTarget = 0.15) {
  return post('/lab/momentum-speeds', { commodity, vol_target: volTarget });
}

export function fetchLabResult(key) {
  return get(`/lab/result/${encodeURIComponent(key)}`);
}

export function fetchDiagnostics(key) {
  return get(`/lab/diagnostics/${encodeURIComponent(key)}`);
}

export function fetchSplitMetrics(key) {
  return get(`/lab/split-metrics/${encodeURIComponent(key)}`);
}

export function runSweep(params) {
  return post('/lab/sweep', params);
}

// ── Sizing ───────────────────────────────────────────────────────────────────

export function fetchTodaysSize(params) {
  return post('/sizing/today', params);
}
