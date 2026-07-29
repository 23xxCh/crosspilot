// analytics.js — auto-extracted page module
import { $, esc, fmtDur, pctText, safeFetch } from './helpers.js';
import {qualityDistributionHTML, topQualityReasonsHTML} from './quality.js';
import { getRouteToken, routeIsCurrent, routeSetTimeout } from './runtime.js';

function drawQualityTrendChart(canvas, trends) {
  if (!canvas || !Array.isArray(trends) || !trends.length) return;
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.offsetWidth * 2;
  canvas.height = canvas.offsetHeight * 2;
  ctx.scale(2, 2);
  const width = canvas.offsetWidth;
  const height = canvas.offsetHeight;
  const pad = {top: 14, right: 20, bottom: 26, left: 36};
  const styles = getComputedStyle(document.documentElement);
  const muted = styles.getPropertyValue('--text-muted').trim() || '#7c8298';
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  ctx.fillStyle = muted;
  ctx.font = '10px monospace';
  [0, 50, 100].forEach(value => {
    const y = pad.top + (height - pad.top - pad.bottom)
      - (value / 100 * (height - pad.top - pad.bottom));
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(String(value), 4, y + 3);
  });
  trends.forEach((trend, index) => {
    const x = pad.left + (
      index / Math.max(trends.length - 1, 1) * (width - pad.left - pad.right)
    );
    if (index % Math.ceil(trends.length / 6) === 0) {
      ctx.fillText(String(trend.day || '').slice(5), x - 10, height - 6);
    }
    const low = Number(trend.low_quality || 0);
    const scored = Math.max(Number(trend.scored || 0), 1);
    const barHeight = Math.min(
      height - pad.top - pad.bottom,
      (low / scored) * (height - pad.top - pad.bottom),
    );
    ctx.fillStyle = 'rgba(239,68,68,.22)';
    ctx.fillRect(
      x - 4,
      pad.top + (height - pad.top - pad.bottom) - barHeight,
      8,
      barHeight,
    );
  });
  ctx.strokeStyle = '#10b981';
  ctx.lineWidth = 2;
  ctx.beginPath();
  trends.forEach((trend, index) => {
    const average = trend.avg_score == null ? 0 : Number(trend.avg_score);
    const x = pad.left + (
      index / Math.max(trends.length - 1, 1) * (width - pad.left - pad.right)
    );
    const y = pad.top + (height - pad.top - pad.bottom)
      - (Math.max(0, Math.min(100, average)) / 100
        * (height - pad.top - pad.bottom));
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = '#10b981';
  ctx.fillRect(pad.left, 3, 10, 10);
  ctx.fillStyle = muted;
  ctx.fillText('均分', pad.left + 14, 12);
  ctx.fillStyle = 'rgba(239,68,68,.55)';
  ctx.fillRect(pad.left + 52, 3, 10, 10);
  ctx.fillStyle = muted;
  ctx.fillText('低质占比', pad.left + 66, 12);
}

export async function renderAnalytics(_params = {}, token = getRouteToken()) {
  let d = {}, loadError = false;
  try { var r = await safeFetch('/api/analytics'); d = await r.json(); }
  catch(e) { loadError = true; }
  if (!routeIsCurrent(token)) return;
  if (loadError) {
    $('#view').innerHTML = '<div style="text-align:center;padding:80px 20px"><div style="font-size:48px;margin-bottom:16px">&#9888;</div><h3>加载失败</h3><p style="color:var(--text-muted)">无法加载分析数据</p></div>';
    return;
  }
  const quality = d.quality || {};
  const avgQuality = quality.average_score == null ? '—' : String(quality.average_score);
  const reviewPressure = pctText(quality.review_pressure_rate);
  const lowQualityRate = pctText(quality.low_quality_rate);

  $('#view').innerHTML = `
    <a href="#/" class="back-link">&larr; 返回仪表盘</a>
    <h2 style="margin-bottom:8px">质量运营分析</h2>
    <p class="analytics-intro">优先看质量压力和扣分原因；趋势异常时，再回任务列表按“低分/高风险”筛选处理。</p>

    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-val" style="color:var(--accent)">${d.total||0}</div><div class="kpi-lbl">总任务数</div></div>
      <div class="kpi-card"><div class="kpi-val" style="color:var(--green)">${d.success_rate||0}%</div><div class="kpi-lbl">成功率 (${d.done||0}/${d.total||0})</div></div>
      <div class="kpi-card"><div class="kpi-val" style="color:var(--accent)">${avgQuality}</div><div class="kpi-lbl">平均质量分（已评分 ${quality.scored_count||0}）</div></div>
      <div class="kpi-card"><div class="kpi-val" style="color:var(--amber)">${reviewPressure}</div><div class="kpi-lbl">复核压力（抽检 + 低质）</div></div>
      <div class="kpi-card"><div class="kpi-val" style="color:var(--danger)">${lowQualityRate}</div><div class="kpi-lbl">低质率（复核 / 高危）</div></div>
      <div class="kpi-card"><div class="kpi-val" style="color:var(--purple)">${d.gen_rate||0}%</div><div class="kpi-lbl">生图成功率 (${d.generated||0}/${d.watermarks||0})</div></div>
    </div>

    <div class="chart-row quality-ops-row">
      <div class="chart-card chart-half">
        <h4 class="chart-title">质量分布</h4>
        ${qualityDistributionHTML(quality)}
        <div class="ops-note">可用 ${quality.usable_count||0} · 抽检 ${quality.needs_sample_count||0} · 低质 ${quality.low_quality_count||0}</div>
      </div>
      <div class="chart-card chart-half">
        <h4 class="chart-title">Top 扣分原因</h4>
        ${topQualityReasonsHTML(quality)}
      </div>
    </div>

    <div class="chart-card">
      <h4 class="chart-title">30 天质量趋势</h4>
      <canvas id="chart-quality" width="700" height="180" style="width:100%;max-height:200px"></canvas>
      <div class="ops-note">绿线是平均质量分，红色柱表示当天低质任务占比。目标不是追求 100 分，而是让低质原因可解释、可处理。</div>
    </div>

    <div class="chart-card"><h4 class="chart-title">30 天处理趋势</h4>
      <canvas id="chart-daily" width="700" height="200" style="width:100%;max-height:220px"></canvas>
    </div>

    <div class="chart-row"><div class="chart-card chart-half">
      <h4 class="chart-title">平台分布</h4>
      <canvas id="chart-platform" width="200" height="200" style="width:200px;height:200px;display:block;margin:0 auto"></canvas>
      ${(d.platform||[]).map(function(p) { return '<p style="text-align:center;font-size:12px;color:var(--text-muted);margin-top:8px">' + esc(p.name) + ': ' + Number(p.count||0) + ' 个任务</p>'; }).join('')}
    </div>

    <div class="chart-card chart-half">
      <h4 class="chart-title">处理效率</h4>
      <div style="text-align:center;padding:30px 0"><div style="font-size:32px;font-weight:700;color:var(--text);font-family:var(--font-mono)">${fmtDur(d.avg_duration||0)}</div><div style="font-size:12px;color:var(--text-muted);margin-top:4px">平均处理耗时</div></div>
      <div style="text-align:center;padding:10px 0"><div style="font-size:32px;font-weight:700;color:var(--text);font-family:var(--font-mono)">${d.generated||0}</div><div style="font-size:12px;color:var(--text-muted);margin-top:4px">累计 AI 生图数</div></div>
    </div></div>`;

  // Draw daily trend chart
  routeSetTimeout(function() {
    if (!routeIsCurrent(token)) return;
    drawQualityTrendChart(document.getElementById('chart-quality'), quality.daily_trends || []);
    var cv = document.getElementById('chart-daily');
    if (!cv || !d.daily_trends || !d.daily_trends.length) return;
    var ctx = cv.getContext('2d');
    cv.width = cv.offsetWidth * 2; cv.height = cv.offsetHeight * 2;
    ctx.scale(2, 2);
    var w = cv.offsetWidth, h = cv.offsetHeight, pad = {top:10,right:20,bottom:30,left:40};
    var pw = w - pad.left - pad.right, ph = h - pad.top - pad.bottom;

    var trends = d.daily_trends;
    var maxVal = Math.max.apply(null, trends.map(function(t){ return t.total; })) || 1;

    // Grid lines + labels
    var rootStyles = getComputedStyle(document.documentElement);
    var mutedColor = rootStyles.getPropertyValue('--text-muted').trim() || '#7c8298';
    ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 1;
    ctx.fillStyle = mutedColor; ctx.font = '10px monospace';
    for (var v = 0; v <= maxVal; v += Math.ceil(maxVal/4)) {
      var y = pad.top + ph - (v/maxVal * ph);
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
      ctx.fillText(v, 2, y + 4);
    }
    // X labels
    for (var i = 0; i < trends.length; i += Math.ceil(trends.length/7)) {
      var x = pad.left + (i/Math.max(trends.length-1,1) * pw);
      ctx.fillText(trends[i].day.slice(5), x - 10, h - 5);
    }

    // Line: total
    ctx.strokeStyle = '#3b82f6'; ctx.lineWidth = 2; ctx.beginPath();
    trends.forEach(function(t, i) {
      var x = pad.left + (i/Math.max(trends.length-1,1) * pw);
      var y = pad.top + ph - (t.total/maxVal * ph);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }); ctx.stroke();

    // Line: failed
    ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 1.5; ctx.setLineDash([3,3]); ctx.beginPath();
    trends.forEach(function(t, i) {
      var x = pad.left + (i/Math.max(trends.length-1,1) * pw);
      var y = pad.top + ph - (t.failed/maxVal * ph);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }); ctx.stroke(); ctx.setLineDash([]);

    // Legend
    ctx.fillStyle = '#3b82f6'; ctx.fillRect(pad.left, 4, 10, 10);
    ctx.fillStyle = mutedColor; ctx.fillText('处理量', pad.left + 14, 13);
    ctx.fillStyle = '#ef4444'; ctx.fillRect(pad.left + 60, 4, 10, 10);
    ctx.fillText('失败', pad.left + 74, 13);

    // Pie chart
    var pcv = document.getElementById('chart-platform');
    if (pcv && d.platform) {
      var pctx = pcv.getContext('2d');
      pcv.width = 400; pcv.height = 400;
      pctx.scale(2, 2);
      var cx = 100, cy = 100, r = 80, total = d.platform.reduce(function(s,p){ return s+p.count; }, 0) || 1;
      var colors = ['#3b82f6', '#f59e0b'];
      var start = -Math.PI/2;
      d.platform.forEach(function(p, i) {
        var slice = (p.count/total) * Math.PI * 2;
        pctx.fillStyle = colors[i % colors.length]; pctx.beginPath();
        pctx.moveTo(cx, cy);
        pctx.arc(cx, cy, r, start, start + slice);
        pctx.closePath(); pctx.fill();
        start += slice;
      });
    }
  }, 100);
}
