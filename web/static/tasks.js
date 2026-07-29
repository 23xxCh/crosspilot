// tasks.js — task list + detail + SSE
import {
  $,
  $$,
  dur,
  esc,
  fmtDur,
  fmtTime,
  label,
  mutationFetch,
  navigateTo,
  pct,
  pctText,
  route,
  safeFetch,
  safeId,
  stats,
  statusKey,
  toast,
} from './helpers.js';
import {qualityReasonsHTML, qualityScoreHTML} from './quality.js';
import {
  getRouteToken,
  routeIsCurrent,
  routeSetInterval,
  routeSetTimeout,
  setRouteEventSource,
  showError,
} from './runtime.js';
import { getUploadLimits } from './upload.js';

let _taskPage = 1, _taskTotal = 0, _taskPages = 1, _taskFilter = 'all', _taskSort = 'created_desc';
let taskSelection = new Set();
let eventSource = null;
const taskNotifySent = {};
const PAGE_SIZE = 20;
const TASK_FILTERS = [
  ['all', '全部'], ['high_risk', '高风险'], ['low_quality', '低分'],
  ['needs_sample', '需抽检'], ['usable', '可用'], ['needs_review', '待复核'],
  ['failed', '失败'], ['running', '处理中'], ['done', '已完成'],
];

export function goPage(p) {
  _taskPage = Math.max(1, Math.min(p, _taskPages));
  renderTasks();
}
function taskRisk(t) {
  const st = t.stats || {};
  const metrics = st.metrics || {};
  const validation = st.validation || {};
  const quality = metrics.quality || {};
  const concurrency = metrics.concurrency || {};
  const issueCount = Array.isArray(validation.issues)
    ? validation.issues.length
    : Number(quality.issue_count || 0);
  return {
    issues: issueCount,
    retries: Number(metrics.http_retries || 0),
    circuit: Number(metrics.circuit_open || 0),
    reductions: Number(concurrency.reductions || 0),
    status: statusKey(t.status)
  };
}
function riskChipsHTML(t) {
  const r = taskRisk(t);
  const chips = [];
  if (r.status === 'needs_review') chips.push(['warn', '复核']);
  if (r.status === 'failed') chips.push(['danger', '失败']);
  if (r.issues) chips.push(['warn', '质' + r.issues]);
  if (r.retries) chips.push(['warn', '重' + r.retries]);
  if (r.reductions) chips.push(['warn', '降' + r.reductions]);
  return chips.map(function(c) { return '<span class=\"chip chip-'+c[0]+'\">'+esc(c[1])+'</span>'; }).join('');
}
function updateStepBoard(d) {
  var board = $('#steps-board');
  if (!board) return;
  var si = (d.stage_index||1) - 1;
  var steps = board.querySelectorAll('.step');
  for (var i = 0; i < steps.length; i++) {
    var step = steps[i], icon = step.querySelector('.step-icon'), detail = step.querySelector('.step-detail');
    step.className = 'step';
    if (i < si) {
      step.classList.add('done');
      if (icon && icon.innerHTML.indexOf('circle') < 0) icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="none" stroke="var(--green)" stroke-width="2"/><path d="M5 9l3 3 5-6" fill="none" stroke="var(--green)" stroke-width="2"/></svg>';
      if (detail) detail.textContent = '完成';
    } else if (i === si) {
      step.classList.add('active');
      if (icon && icon.innerHTML.indexOf('spinner-sm') < 0) icon.innerHTML = '<div class="spinner-sm"></div>';
      if (detail) detail.textContent = (d.current||0) + ' / ' + (d.total||0) + (d.eta_s ? ' · 预计剩余 ' + fmtDur(d.eta_s) : '');
    }
  }
  var dur = document.getElementById('dp-dur');
  if (dur) dur.textContent = '已运行 ' + fmtDur(d.total_elapsed_s||0);
}
export async function retryTask(id, fresh) {
  if (fresh && !confirm('从头重跑会清空图审、翻译和生图缓存，并重新消耗 API 额度。确定继续吗？')) return;
  try {
    await mutationFetch('/api/tasks/'+safeId(id)+'/retry?fresh='+(fresh?'true':'false'), {method:'POST'});
    toast(fresh ? '已从头重新加入队列' : '已从缓存继续处理');
    navigateTo('#/tasks/' + safeId(id));
  } catch(e) { toast('重试失败：' + e.message, 'error'); }
}
export async function cancelTask(id) {
  if (!confirm('取消后会停止当前进程，已完成的缓存会保留。确定取消吗？')) return;
  try {
    await mutationFetch('/api/tasks/'+safeId(id)+'/cancel', {method:'POST'});
    toast('任务已取消，可稍后继续处理');
    route();
  } catch(e) { toast('取消失败：' + e.message, 'error'); }
}

const TASK_SORTS = [
  ['created_desc', '最新优先'], ['created_asc', '最早优先'],
  ['quality_asc', '质量低到高'], ['quality_desc', '质量高到低']
];

export async function renderTasks(_params = {}, token = getRouteToken()) {
  let tasks = [], total = 0, pages = 1;
  try {
    const r = await safeFetch(
      `/api/tasks?page=${_taskPage}&limit=${PAGE_SIZE}&filter=${encodeURIComponent(_taskFilter)}&sort=${encodeURIComponent(_taskSort)}`
    );
    const data = await r.json();
    tasks = data.tasks || [];
    total = data.total || 0;
    pages = data.pages || 1;
    _taskFilter = data.filter || _taskFilter;
    _taskSort = data.sort || _taskSort;
    _taskTotal = total; _taskPages = pages;
  } catch(e) {
    if (e.name === 'AbortError') return;
    if (routeIsCurrent(token)) showError('任务加载失败', e.message);
    return;
  }
  if (!routeIsCurrent(token)) return;
  taskSelection = new Set();

  function refresh() {
    const tbody = $('#task-table-body');
    if (!tbody) return;
    tbody.innerHTML = tasks.map(function(t) {
      const id = safeId(t.id);
      const status = statusKey(t.status);
      const filename = esc(t.filename);
      return `<tr data-task-row="${id}">
        <td><input type="checkbox" data-select-task="${id}" aria-label="选择 ${filename}"></td>
        <td><span class="status-dot ${status}" aria-label="${label(status)}"></span></td>
        <td style="color:var(--text);font-weight:500"><a href="#/tasks/${id}" style="color:inherit">${filename}</a></td>
        <td><span class="badge badge-${status}">${label(status)}</span></td>
        <td>${fmtTime(t.created_at)}</td>
        <td>${dur(t.created_at,t.updated_at)}</td>
        <td>${riskChipsHTML(t)}</td>
        <td>${qualityScoreHTML(t.quality_score)}</td>
        <td style="font-family:var(--font-mono);font-size:11px;min-width:180px">${status==='running' && t.percent != null ? `<div class="mini-progress"><div class="mini-bar"><div class="mini-fill" style="width:${pct(t.percent)}%"></div></div><span class="mini-pct">${pct(t.percent)}%</span></div>` : `审${stats(t,'images_reviewed')} 问题${stats(t,'watermarks')} 生${stats(t,'images_generated')}`}</td>
        <td>${status==='done'||status==='needs_review'?`<a class="btn btn-sm btn-ghost" href="/api/tasks/${id}/download">${status==='needs_review'?'下载复核':'下载'}</a>`:''}
          <button class="btn btn-sm btn-danger" data-action="delete-task" data-id="${id}" aria-label="删除 ${filename}">删除</button></td>
      </tr>`;
    }).join('');
  }

  function renderPagination() {
    if (pages <= 1) return '';
    let html = '<div class="pagination">';
    html += `<button class="btn btn-sm btn-ghost page-btn" ${_taskPage<=1?'disabled':''} data-page="${_taskPage-1}">上一页</button>`;
    for (let p = 1; p <= pages; p++) {
      if (pages <= 7 || p === 1 || p === pages || Math.abs(p - _taskPage) <= 1) {
        html += `<button class="btn btn-sm ${p===_taskPage?'btn-primary':'btn-ghost'} page-btn" data-page="${p}" ${p===_taskPage?'aria-current="page"':''}>${p}</button>`;
      } else if (p === 2 && _taskPage > 4) {
        html += '<span class="page-ellipsis">...</span>';
      } else if (p === pages - 1 && _taskPage < pages - 3) {
        html += '<span class="page-ellipsis">...</span>';
      }
    }
    html += `<button class="btn btn-sm btn-ghost page-btn" ${_taskPage>=pages?'disabled':''} data-page="${_taskPage+1}">下一页</button>`;
    html += `<span class="page-info">共 ${total} 条</span></div>`;
    return html;
  }

  $('#view').innerHTML = `
    <div class="section-head"><h3>所有任务</h3>
      <div class="section-actions">
        <button class="btn btn-ghost btn-sm" id="select-all">全选本页</button>
        <button class="btn btn-sm btn-danger" id="batch-delete">批量删除</button>
      </div>
    </div>
    <div class="task-list-controls">
      <div class="task-filterbar">
        ${TASK_FILTERS.map(([key, text]) =>
          `<button class="filter-pill ${key===_taskFilter?'active':''}" data-task-filter="${key}">${text}</button>`
        ).join('')}
      </div>
      <label class="task-sortbar"><span>排序</span><select id="task-sort">
        ${TASK_SORTS.map(([key, text]) =>
          `<option value="${key}" ${key===_taskSort?'selected':''}>${text}</option>`
        ).join('')}
      </select></label>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th style="width:32px">选择</th><th>指示</th><th>文件名</th><th>状态</th><th>创建</th><th>耗时</th><th>风险</th><th>质量</th><th>统计</th><th>操作</th></tr></thead>
      <tbody id="task-table-body"></tbody></table></div>
    ${tasks.length===0?`<div class="empty-state" style="margin-top:24px"><span class="empty-state-icon">&#128203;</span><h3>没有匹配任务</h3><p>换个筛选条件，或在仪表盘上传新表格</p></div>`:renderPagination()}
  `;
  refresh();
  $$('[data-task-filter]').forEach(button => {
    button.onclick = () => {
      _taskFilter = button.dataset.taskFilter || 'all';
      _taskPage = 1;
      renderTasks({}, getRouteToken());
    };
  });
  const sortSelect = $('#task-sort');
  if (sortSelect) sortSelect.onchange = () => {
    _taskSort = sortSelect.value || 'created_desc';
    _taskPage = 1;
    renderTasks({}, getRouteToken());
  };
  $('#select-all').onclick = () => {
    const cbs = $$('#task-table-body input[type=checkbox]');
    const check = taskSelection.size < tasks.length;
    cbs.forEach((cb,i) => { cb.checked = check; check ? taskSelection.add(tasks[i].id) : taskSelection.delete(tasks[i].id); });
  };
  $('#batch-delete').onclick = async () => {
    if (!taskSelection.size) return;
    if (!confirm('确定删除选中的 ' + taskSelection.size + ' 个任务吗？此操作无法撤销。')) return;
    const ids = [...taskSelection];
    const total = ids.length;
    let ok = 0, fail = 0;
    const btn = $('#batch-delete');
    const origText = btn.textContent;
    const chunkSize = 10;
    for (let i = 0; i < ids.length; i += chunkSize) {
      const chunk = ids.slice(i, i + chunkSize);
      const results = await Promise.allSettled(chunk.map(id => mutationFetch('/api/tasks/'+id, {method:'DELETE'})));
      for (const r of results) {
        if (r.status === 'fulfilled' && r.value.ok) ok++;
        else fail++;
      }
      btn.textContent = `删除中 ${Math.min(i+chunkSize, total)}/${total}...`;
    }
    btn.textContent = origText;
    toast(`已删除 ${ok}/${total} 个任务${fail ? `，${fail} 个失败` : ''}`, fail ? 'error' : 'success');
    if (routeIsCurrent(token)) renderTasks({}, token);
  };
}

export function setTaskSelected(id, selected) {
  if (selected) taskSelection.add(id);
  else taskSelection.delete(id);
}
function _failureSuggestion(error) {
  var e = (error||'').toLowerCase();
  if (e.includes('key') || e.includes('unauthorized') || e.includes('401'))
    return 'API key 无效或过期。请前往 <a href="#/settings">设置页</a> 重新配置。';
  if (e.includes('格式') || e.includes('adapter') || e.includes('不识别'))
    return '格式不识别。请确认是受支持的 .xlsx，或符合模板的 Amazon 列式 JSON。';
  if (e.includes('表头') || e.includes('header'))
    return '表格列名与预期不符。TikTok 可能更新了导出格式，请联系开发者。';
  if (e.includes('timeout') || e.includes('连接') || e.includes('网络') || e.includes('dns'))
    return '网络连接失败。请检查网络后重试，或确认 API 服务是否正常。';
  if (e.includes('memory') || e.includes('文件过大') || e.includes('大小'))
    return '文件太大（当前上限 ' + (getUploadLimits().max_upload_mb || 50) + 'MB）。请拆分为多个小文件后上传。';
  if (e.includes('损坏') || e.includes('corrupt') || e.includes('无效') || e.includes('zip'))
    return '文件已损坏或格式无效。请从 eBay/Amazon 重新导出后重试。';
  if (e.includes('权限') || e.includes('permission') || e.includes('denied'))
    return '文件权限不足。请检查文件是否被其他程序占用，关闭后重试。';
  if (e.includes('队列') || e.includes('满'))
    return '当前任务队列已满。请等待前面的任务完成后重试。';
  return '处理过程中出现意外错误。请尝试重新上传，或联系开发者。';
}
function _qualityMetricsHTML(metrics, validation) {
  if (!metrics || typeof metrics !== 'object') return '';
  const stages = Object.entries(metrics.stages || {}).map(([name, value]) => ({
    name,
    duration: Number(value.duration_s || 0),
    items: Number(value.items || 0),
    success: Number(value.success || 0),
    throughput: Number(value.items_per_s || 0)
  })).sort((a, b) => b.duration - a.duration);
  const calls = Number(metrics.api_calls || 0);
  const errors = Number(metrics.api_errors || 0);
  const successRate = metrics.api_success_rate == null
    ? '—'
    : Math.round(Number(metrics.api_success_rate) * 100) + '%';
  const avgLatency = calls
    ? (Number(metrics.api_latency_s || 0) / calls).toFixed(2) + 's'
    : '—';
  const httpAttempts = Number(metrics.http_attempts || 0);
  const httpRetries = Number(metrics.http_retries || 0);
  const rateWait = Number(metrics.rate_wait_s || 0);
  const rateWaitLabel = rateWait
    ? rateWait.toFixed(rateWait >= 10 ? 0 : 1) + 's'
    : '—';
  const circuitOpen = Number(metrics.circuit_open || 0);
  const cache = metrics.cache || {};
  const cacheHits = Number(cache.hits || 0);
  const cacheMisses = Number(cache.misses || 0);
  const cacheHitRate = cache.hit_rate == null
    ? '—'
    : Math.round(Number(cache.hit_rate) * 100) + '%';
  const concurrency = metrics.concurrency || {};
  const reductions = Number(concurrency.reductions || 0);
  const concurrencyFailures = Number(concurrency.failures || 0);
  const degraded = stages.reduce(
    (total, stage) => total + Math.max(0, stage.items - stage.success),
    0
  );
  const quality = metrics.quality || {};
  const issueCount = Array.isArray((validation || {}).issues)
    ? validation.issues.length
    : Number(quality.issue_count || 0);
  const slowest = stages[0];
  const rows = stages.map(stage =>
    '<tr><td>' + esc(stage.name) + '</td>' +
    '<td>' + stage.items + '</td>' +
    '<td>' + stage.success + '</td>' +
    '<td>' + stage.throughput.toFixed(1) + '/s</td>' +
    '<td>' + stage.duration.toFixed(1) + 's</td></tr>'
  ).join('');
  return '<section class="quality-metrics">' +
    '<div class="quality-metrics-head"><h4>质量与效率</h4><span>本次任务真实数据</span></div>' +
    '<div class="quality-metrics-grid">' +
    '<div><strong>' + calls + '</strong><span>AI 调用</span></div>' +
    '<div><strong>' + successRate + '</strong><span>AI 成功率' + (errors ? ' · ' + errors + ' 次失败' : '') + '</span></div>' +
    '<div><strong>' + degraded + '</strong><span>阶段降级项</span></div>' +
    '<div><strong class="' + (issueCount ? 'metric-warn' : '') + '">' + issueCount + '</strong><span>质量问题</span></div>' +
    '<div><strong>' + avgLatency + '</strong><span>平均 AI 耗时</span></div>' +
    '<div><strong>' + (httpAttempts || '—') + '</strong><span>HTTP 尝试' + (httpRetries ? ' · 重试 ' + httpRetries : '') + '</span></div>' +
    '<div><strong class="' + (rateWait > 300 ? 'metric-warn' : '') + '">' + rateWaitLabel + '</strong><span>限速等待</span></div>' +
    '<div><strong class="' + (circuitOpen ? 'metric-warn' : '') + '">' + circuitOpen + '</strong><span>Circuit 拦截</span></div>' +
    '<div><strong>' + cacheHitRate + '</strong><span>缓存命中' + (cacheHits || cacheMisses ? ' · ' + cacheHits + '/' + (cacheHits + cacheMisses) : '') + '</span></div>' +
    '<div><strong class="' + (reductions ? 'metric-warn' : '') + '">' + reductions + '</strong><span>并发降级' + (concurrencyFailures ? ' · 失败 ' + concurrencyFailures : '') + '</span></div>' +
    '</div>' +
    (slowest ? '<div class="quality-metrics-note">最慢阶段：' + esc(slowest.name) + ' · ' + slowest.duration.toFixed(1) + 's</div>' : '') +
    (rows ? '<div class="metrics-table-wrap"><table class="metrics-table"><thead><tr><th>阶段</th><th>处理</th><th>成功</th><th>吞吐</th><th>耗时</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '') +
    '</section>';
}
function _reviewSeverityText(severity) {
  return {review:'需复核', warning:'警告', error:'错误', info:'信息'}[severity] || '信息';
}
function _reviewWorkbenchHTML(reviewData, jobId) {
  if (!reviewData || typeof reviewData !== 'object') return '';
  if (reviewData.error) {
    return '<section class="review-workbench"><div class="quality-metrics-head"><h4>复核台</h4><span>加载失败</span></div>' +
      '<div class="quality-metrics-note">复核数据暂时不可用：' + esc(reviewData.error) + '</div></section>';
  }
  const summary = reviewData.summary || {};
  const qualityScore = summary.quality_score || {};
  const items = Array.isArray(reviewData.items) ? reviewData.items : [];
  const visible = items.filter(item =>
    item.section !== 'summary' && (
    item.section === 'audit' ||
    item.section === 'validation' ||
    item.severity === 'review' ||
    item.severity === 'warning' ||
    item.severity === 'error'
    )
  );
  const rows = visible.map(item => {
    const severity = item.severity || 'info';
    const row = item.row ? '第 ' + esc(item.row) + ' 行' : '任务级';
    const field = item.field ? esc(item.field) : '—';
    const value = item.value ? esc(item.value) : '—';
    const action = item.action ? esc(item.action) : '—';
    return '<tr>' +
      '<td><span class="review-severity ' + esc(severity) + '">' + _reviewSeverityText(severity) + '</span></td>' +
      '<td>' + row + '</td>' +
      '<td>' + field + '</td>' +
      '<td class="review-message">' + esc(item.message || '') + '</td>' +
      '<td class="review-value">' + value + '</td>' +
      '<td class="review-action">' + action + '</td>' +
      '</tr>';
  }).join('');
  const noRows = '<div class="review-empty">没有行级复核项。可以下载结果；如果仍担心质量，抽样检查标题、图片和关键词。</div>';
  return '<section class="review-workbench">' +
    '<div class="quality-metrics-head"><h4>复核台</h4><span>先处理高风险行，再下载结果</span></div>' +
    '<div class="review-summary-grid">' +
      '<div class="review-score-card">' + qualityScoreHTML(qualityScore) + '</div>' +
      '<div><strong class="' + (Number(summary.issue_count || 0) ? 'metric-warn' : '') + '">' + esc(summary.issue_count ?? 0) + '</strong><span>质量问题</span></div>' +
      '<div><strong class="' + (Number(summary.validation_item_count || 0) ? 'metric-warn' : '') + '">' + esc(summary.validation_item_count ?? 0) + '</strong><span>待复核项</span></div>' +
      '<div><strong>' + esc(summary.audit_item_count ?? 0) + '</strong><span>阶段审计项</span></div>' +
      '<div><strong class="' + (Number(summary.http_retries || 0) ? 'metric-warn' : '') + '">' + esc(summary.http_retries ?? 0) + '</strong><span>HTTP 重试</span></div>' +
      '<div><strong class="' + (Number(summary.concurrency_reductions || 0) ? 'metric-warn' : '') + '">' + esc(summary.concurrency_reductions ?? 0) + '</strong><span>并发降级</span></div>' +
    '</div>' +
    qualityReasonsHTML(qualityScore) +
    (rows
      ? '<div class="review-table-wrap"><table class="review-table"><thead><tr><th>级别</th><th>行</th><th>字段</th><th>问题/信号</th><th>证据值</th><th>建议动作</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
      : noRows) +
    '<div class="quality-metrics-note">完整明细可下载 <a href="/api/tasks/' + esc(jobId) + '/review-report">复核报告 CSV</a>。证据值是短上下文，不是完整数据审计。</div>' +
    '</section>';
}
export async function renderTaskDetail({id}, token = getRouteToken()) {
  id = safeId(id);
  if (!id) { showError('任务地址无效', '任务 ID 格式不正确'); return; }
  let t = {};
  try { const r = await safeFetch('/api/tasks/'+id); t = await r.json(); }
  catch(e) {
    if (e.name !== 'AbortError' && routeIsCurrent(token)) showError('任务加载失败', e.message);
    return;
  }
  if (!routeIsCurrent(token)) return;
  const status = statusKey(t.status);
  const si = Math.max(0, (t.stage_index||1) - 1);
  const done = status === 'done';
  const needsReview = status === 'needs_review';
  const complete = done || needsReview;

  let STAGES = [];
  try { const sr = await safeFetch('/api/stages?pipeline=' + encodeURIComponent(t.pipeline || 'ebay')); STAGES = (await sr.json()).stages || []; }
  catch(e) { STAGES = ['提取图片','AI 图审','AI 生图','附图清空','标题翻译','描述清洗','描述翻译','嵌入图片','视频清理','保存']; }
  if (!routeIsCurrent(token)) return;
  let reviewData = null;
  if (complete) {
    try {
      const rr = await safeFetch('/api/tasks/' + id + '/review-data');
      reviewData = await rr.json();
    } catch(e) {
      if (e.name === 'AbortError') return;
      reviewData = {error: e.message};
    }
  }
  if (!routeIsCurrent(token)) return;

  const stepsHTML = STAGES.map((s,i) => {
    let icon = '', cls = 'pending', detail = '等待中';
    if (complete || i < si) {
      icon = '<svg width="18" height="18" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="none" stroke="var(--green)" stroke-width="2"/><path d="M5 9l3 3 5-6" fill="none" stroke="var(--green)" stroke-width="2"/></svg>';
      cls = 'done'; detail = '完成';
    } else if (i === si && status === 'running') {
      icon = '<div class="spinner-sm"></div>';
      cls = 'active';
      detail = (t.current||0) + ' / ' + (t.total||0) + (t.eta_s ? ' · 预计剩余 ' + fmtDur(t.eta_s) : '');
    } else if (i === si && (status === 'failed' || status === 'cancelled')) {
      icon = '<svg width="18" height="18" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="none" stroke="var(--danger)" stroke-width="2"/><path d="M6 6l6 6M12 6l-6 6" fill="none" stroke="var(--danger)" stroke-width="2"/></svg>';
      cls = 'failed'; detail = esc((t.error||'').substring(0, 60));
    }
    return '<div class="step ' + cls + '"><div class="step-icon">' + icon + '</div><div class="step-body"><div class="step-name">' + esc(s) + '</div><div class="step-detail">' + detail + '</div></div></div>';
  }).join('');

  let errorHTML = '';
  if ((status === 'failed' || status === 'cancelled') && t.error) {
    const title = status === 'cancelled' ? '任务已取消' : '处理失败';
    errorHTML = '<div class="error-card"><div class="error-card-title">' + title + '</div><div class="error-card-msg">' + esc(t.error) + '</div><div class="error-card-fix">' + _failureSuggestion(t.error) + '</div><div class="cta-row" style="margin-top:12px"><button class="btn btn-primary" data-action="retry-task" data-id="' + id + '" data-fresh="false">继续处理</button><button class="btn btn-ghost" data-action="retry-task" data-id="' + id + '" data-fresh="true">从头重跑</button></div></div>';
  }
  if (needsReview) {
    errorHTML = '<div class="review-card"><div class="review-card-title">处理完成，但需要人工复核</div><div class="error-card-msg">' +
      esc(t.error || '输出存在质量问题，请复核后使用') + '</div>' +
      '<div class="error-card-fix">下面的复核台已按行号和字段整理问题，先看“需复核/警告”项。</div>' +
      '</div>';
  }

  let doneHTML = '';
  if (complete) {
    doneHTML = '<div class="done-actions"><a class="btn btn-primary" href="/api/tasks/' + id + '/download">' +
      (needsReview ? '下载并复核' : '下载结果') +
      '</a><a class="btn btn-ghost" href="/api/tasks/' + id + '/review-report">复核报告 CSV</a>' +
      '<button class="btn btn-danger" data-action="delete-task" data-id="' + id + '" data-after-delete="home">删除</button></div>';
    if ('Notification' in window && !taskNotifySent[id] && Notification.permission === 'granted') {
      taskNotifySent[id] = true;
      new Notification('CrossPilot', { body: t.filename + ' 清洗完成！' });
    }
  }

  if (status === 'running' || status === 'queued') {
    let sseRetries = 0;
    const MAX_SSE_RETRIES = 10;
    var _pollTimer = null;

    function _startPolling() {
      if (_pollTimer || !routeIsCurrent(token)) return;
      _pollTimer = routeSetInterval(async function() {
        if (!routeIsCurrent(token)) return;
        try {
          var r = await safeFetch('/api/tasks/' + id);
          var task = await r.json();
          if (task.status === 'done' || task.status === 'needs_review' || task.status === 'failed' || task.status === 'cancelled') {
            clearInterval(_pollTimer);
            _pollTimer = null;
            route();
            return;
          }
          if (task.status === 'running') {
            updateStepBoard({
              stage_index: task.stage_index || 1,
              stage_total: task.stage_total || 10,
              current: task.current || 0,
              total: task.total || 0,
              eta_s: task.eta_s || 0,
              total_elapsed_s: task.total_elapsed_s || 0
            });
          }
        } catch(e) {}
      }, 5000);
    }

    function _connectSSE() {
      if (!routeIsCurrent(token)) return;
      if (eventSource) eventSource.close();
      eventSource = new EventSource('/api/tasks/'+id+'/events');
      setRouteEventSource(eventSource);
      eventSource.onmessage = (ev) => {
        if (!routeIsCurrent(token)) return;
        var m;
        try {
          m = JSON.parse(ev.data);
        } catch (_) {
          return;
        }
        sseRetries = 0;
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
        if (m.type === 'done' || m.type === 'needs_review' || m.type === 'failed' || m.type === 'cancelled') {
          eventSource.close();
          setRouteEventSource(null);
          route();
        }
        if (m.type === 'progress') updateStepBoard(m.data);
      };
      eventSource.onerror = () => {
        eventSource.close();
        setRouteEventSource(null);
        if (!routeIsCurrent(token)) return;
        if (sseRetries < MAX_SSE_RETRIES) {
          var delay = Math.min(1000 * Math.pow(2, sseRetries), 30000);
          sseRetries++;
          routeSetTimeout(_connectSSE, delay);
        } else {
          _startPolling();
        }
      };
    }
    routeSetTimeout(_connectSSE, 100);
  }

  // Build view with steps board instead of old timeline+progress
  var viewHTML = '<a href="#/tasks" class="back-link">&larr; 返回任务列表</a>';
  viewHTML += '<div class="card task-card">';
  viewHTML += '<div class="task-head"><div><h2>' + esc(t.filename) + '</h2>';
  viewHTML += '<div class="task-meta" style="margin-top:6px"><span class="badge badge-' + status + '">' + label(status) + '</span>';
  if (complete) viewHTML += '<span style="font-family:var(--font-mono);font-size:11px;color:var(--text-muted)">耗时 ' + dur(t.created_at, t.updated_at) + '</span>';
  if (status === 'running') viewHTML += '<span id="dp-dur" style="font-family:var(--font-mono);font-size:11px;color:var(--text-muted)">已运行 ' + fmtDur(t.total_elapsed_s||0) + '</span>';
  viewHTML += '</div></div>';
  if (status === 'running' || status === 'queued') viewHTML += '<div class="done-actions"><button class="btn btn-ghost btn-sm" data-action="refresh-task" data-id="' + id + '">刷新</button><button class="btn btn-danger btn-sm" data-action="cancel-task" data-id="' + id + '">取消任务</button></div>';
  viewHTML += doneHTML + '</div>';
  viewHTML += errorHTML;
  if (complete) viewHTML += _reviewWorkbenchHTML(reviewData, id);
  if (complete) viewHTML += _qualityMetricsHTML(t.stats && t.stats.metrics, t.stats && t.stats.validation);
  viewHTML += '<h4 style="margin:20px 0 12px;font-size:13px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.05em">处理步骤</h4>';
  viewHTML += '<div class="steps-board" id="steps-board" role="status" aria-live="polite">' + stepsHTML + '</div>';
  viewHTML += '</div>';
  $('#view').innerHTML = viewHTML;
}

export async function delTask(id, afterDelete) {
  if (!confirm('确定删除此任务及其全部文件吗？此操作无法撤销。')) return;
  try {
    await mutationFetch('/api/tasks/'+safeId(id), {method:'DELETE'});
    toast('任务已删除');
    if (afterDelete === 'home') navigateTo('#/');
    else if (location.hash.includes('/tasks/')) navigateTo('#/tasks');
    else await renderTasks({}, getRouteToken());
  } catch(e) { toast('删除失败：' + e.message, 'error'); }
}
