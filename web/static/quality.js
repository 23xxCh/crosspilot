// Shared quality-score presentation helpers.
import {esc, severityClass} from './helpers.js';

export function qualityScoreHTML(score) {
  const quality = score || {};
  const severity = ['ok', 'warn', 'danger', 'info'].includes(quality.severity)
    ? quality.severity
    : 'info';
  const value = quality.score == null ? '—' : String(Math.round(Number(quality.score)));
  const label = quality.label || (quality.score == null ? '未评分' : '评分');
  return `<div class="quality-score ${severity}"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`;
}

export function qualityReasonsHTML(score) {
  const reasons = Array.isArray((score || {}).reasons) ? score.reasons : [];
  if (!reasons.length) return '';
  return '<div class="quality-reasons">' + reasons.slice(0, 6).map(reason =>
    `<span>${esc(reason.label || reason.code || '扣分')}</span>`
  ).join('') + '</div>';
}

export function qualityDistributionHTML(quality) {
  const items = Array.isArray((quality || {}).distribution) ? quality.distribution : [];
  const total = items.reduce((sum, item) => sum + Number(item.count || 0), 0);
  if (!total) return '<div class="ops-empty">暂无质量评分数据</div>';
  return '<div class="quality-bars">' + items.map(item => {
    const count = Number(item.count || 0);
    const percent = Math.round(count / Math.max(total, 1) * 100);
    const severity = severityClass(item.severity);
    return `<div class="quality-bar-row"><div class="quality-bar-head"><span>${esc(item.label || item.grade)}</span><strong>${count} · ${percent}%</strong></div><div class="quality-bar-track"><div class="quality-bar-fill ${severity}" style="width:${percent}%"></div></div></div>`;
  }).join('') + '</div>';
}

export function topQualityReasonsHTML(quality) {
  const reasons = Array.isArray((quality || {}).top_reasons) ? quality.top_reasons : [];
  if (!reasons.length) return '<div class="ops-empty">暂无扣分原因</div>';
  return '<div class="reason-list">' + reasons.map(reason =>
    `<div class="reason-item"><div><strong>${esc(reason.label || reason.code || '扣分')}</strong><span>${esc(reason.code || '')}</span></div><em>${Number(reason.count || 0)} 次</em></div>`
  ).join('') + '</div>';
}
