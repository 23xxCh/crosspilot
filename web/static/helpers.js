// helpers.js — pure utility functions, zero module dependencies
export const $ = s => document.querySelector(s);
export const $$ = s => document.querySelectorAll(s);

export function esc(s) { return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
export function toast(msg, type='success') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`; el.textContent = msg;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  $('#toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3000);
}
export function fmtTime(ts) { if(!ts) return '-'; return new Date(ts*1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'}); }
export function fmtDur(s) {
  const seconds = Number(s);
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  return seconds >= 60 ? Math.floor(seconds/60)+'m '+Math.round(seconds%60)+'s' : Math.round(seconds)+'s';
}
export function dur(a,b) { if(!a||!b) return '-'; return fmtDur(Math.round(b-a)); }
export function label(s) { return {queued:'排队中',running:'处理中',done:'已完成',needs_review:'待复核',failed:'失败',cancelled:'已取消'}[s]||'未知'; }
export function stats(t,k) { return ((t.stats||{})[k])||0; }
export function pct(value) { const n=Number(value); return Number.isFinite(n) ? Math.max(0,Math.min(100,Math.round(n))) : 0; }
export function pctText(value) { const n=Number(value); return Number.isFinite(n) ? n.toFixed(n%1?1:0)+'%' : '—'; }
export function severityClass(value) { return ['ok','warn','danger','info'].includes(value) ? value : 'info'; }
export function safeId(id) { const v=String(id||''); return /^[0-9a-f]{12}$/.test(v) ? v : ''; }
export function statusKey(s) { return ['queued','running','done','needs_review','failed','cancelled'].includes(s) ? s : 'failed'; }

let _routeAbort = null;
export function setRouteAbort(c) { _routeAbort = c; }
export function getRouteAbort() { return _routeAbort; }

export async function safeFetch(url, opts) {
  const options = {...(opts||{})};
  if (!options.signal && (!options.method || options.method==='GET') && _routeAbort) {
    options.signal = _routeAbort.signal;
  }
  const r = await fetch(url, options);
  if (!r.ok) {
    const detail = await r.json().catch(()=>({}));
    throw new Error(detail.detail || `HTTP ${r.status}: ${url}`);
  }
  return r;
}
export function mutationFetch(url, opts) {
  const o = {...(opts||{})};
  o.headers = {...(o.headers||{}), 'X-CrossPilot-Request':'1'};
  return safeFetch(url, o);
}
export function navigateTo(hash) {
  if (location.hash === hash) route(); else location.hash = hash;
}
// Lazy ref — set by router.js after import
let _routeFn = null;
export function setRouteFn(fn) { _routeFn = fn; }
export function route() { if (_routeFn) _routeFn(); }

export function updateClock() {
  $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
}
