// CrossPilot SPA - vanilla JS router + SSE + multi-platform support
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
let es = null, _clockTimer = null, _routeAbort = null;
let _routeToken = 0, _uploading = false, _uploadXhr = null;
const _routeTimeouts = new Set();
const _routeIntervals = new Set();

function _closeSSE() {
  if (es) { es.close(); es = null; }
  if (_routeAbort) { _routeAbort.abort(); _routeAbort = null; }
  _routeTimeouts.forEach(clearTimeout);
  _routeIntervals.forEach(clearInterval);
  _routeTimeouts.clear();
  _routeIntervals.clear();
}

function _routeSetTimeout(fn, delay) {
  const timer = setTimeout(() => {
    _routeTimeouts.delete(timer);
    fn();
  }, delay);
  _routeTimeouts.add(timer);
  return timer;
}

function _routeSetInterval(fn, delay) {
  const timer = setInterval(fn, delay);
  _routeIntervals.add(timer);
  return timer;
}

function _routeIsCurrent(token) {
  return token === _routeToken;
}

// ===== Toast =====
function toast(msg, type='success') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`; el.textContent = msg;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  $('#toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ===== Clock =====
function updateClock() {
  $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
}
updateClock(); _clockTimer = setInterval(updateClock, 30000);
addEventListener('beforeunload', () => {
  _closeSSE();
  clearInterval(_clockTimer);
  if (window._dashTimer) clearInterval(window._dashTimer);
});

async function loadRuntimeVersion() {
  try {
    const response = await safeFetch('/api/version');
    const data = await response.json();
    const version = String(data.version || '').replace(/^v/, '');
    if (version) $('#sidebar-version').textContent = `v${version}`;
  } catch (error) {
    $('#sidebar-version').textContent = 'dev';
  }
}
loadRuntimeVersion();

// ===== SPA Router =====
const ROUTES = {
  '/': renderDashboard,
  '/tasks': renderTasks,
  '/tasks/:id': renderTaskDetail,
  '/templates': renderTemplates,
  '/settings': renderSettings,
  '/analytics': renderAnalytics,
};

function _showError(title, detail) {
  $('#view').innerHTML = `<div style="text-align:center;padding:60px 20px">
    <div style="font-size:48px;margin-bottom:16px">&#9888;</div>
    <h3>${esc(title)}</h3>
    <p style="color:var(--text-muted);margin:8px 0 24px">${esc(detail||'')}</p>
    <button class="btn btn-primary" data-action="reload">刷新重试</button>
    <a class="btn btn-ghost" style="margin-left:8px" href="#/">返回首页</a>
  </div>`;
}

function _showLoading() {
  $('#view').innerHTML = `<div role="status" aria-live="polite" style="text-align:center;padding:80px 20px">
    <div class="spinner"></div>
    <p style="color:var(--text-muted);margin-top:16px">加载中...</p>
  </div>`;
}

async function route() {
  _closeSSE();
  const token = ++_routeToken;
  _routeAbort = new AbortController();
  _showLoading();
  const hash = (location.hash || '#/').slice(1) || '/';
  const parts = hash.replace(/^\/+/, '').split('/').filter(Boolean);
  let routeFn = null, params = {};
  if (parts[0] === 'tasks' && parts[1]) {
    routeFn = ROUTES['/tasks/:id']; params = {id: parts[1]};
  } else {
    routeFn = ROUTES[hash] || ROUTES['/'];
  }
  const activeRoute = (parts[0] === 'tasks' && parts[1])
    ? '/tasks'
    : (ROUTES[hash] ? hash : '/');
  $$('.sidebar-item').forEach(a => {
    const isActive = a.dataset.route === activeRoute;
    a.classList.toggle('active', isActive);
    if (isActive) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  const labels = {'/':'仪表盘','/tasks':'任务列表','/templates':'模板','/settings':'设置','/analytics':'分析'};
  const extra = (parts[0]==='tasks'&&parts[1]) ? ' / 任务详情 / ' + esc(parts[1]) : '';
  $('#breadcrumb').innerHTML = `<span style="color:var(--text-muted)">CrossPilot</span><span class="sep">/</span><span class="current">${labels[activeRoute]||''}${extra}</span>`;
  try {
    await routeFn(params, token);
  } catch (e) {
    if (e.name === 'AbortError' || !_routeIsCurrent(token)) return;
    console.error('route error:', e);
    _showError('页面加载失败', e.message || '未知错误');
  }
}

window.onhashchange = route;
route();

// ===== DASHBOARD =====
async function renderDashboard(_params = {}, token = _routeToken) {
  let d = {}, loadError = false;
  try {
    const r = await safeFetch('/api/dashboard');
    if (!r.ok) throw new Error('API ' + r.status);
    d = await r.json();
  } catch(e) { loadError = true; }
  if (!_routeIsCurrent(token)) return;
  if (loadError) {
    $('#view').innerHTML = '<div style="text-align:center;padding:80px 20px"><div style="font-size:48px;margin-bottom:16px">&#9888;</div><h3>加载失败</h3><p style="color:var(--text-muted);margin:8px 0 24px">无法连接到服务器，请检查网络后重试</p><button class="btn btn-primary" data-action="reload">重新加载</button></div>';
    return;
  }
  window._uploadLimits = {
    max_upload_mb: d.max_upload_mb || 50,
    max_batch_files: d.max_batch_files || 20,
  };

  const platforms = [
    {id:'ebay',name:'eBay',dot:'ebay',active:true,soon:false,desc:'全球拍卖 & 固定价格市场'},
    {id:'shopee',name:'Shopee',dot:'shopee',active:false,soon:true,desc:'东南亚 & 台湾'},
    {id:'amazon',name:'Amazon',dot:'amazon',active:true,soon:false,desc:'全球最大电商平台'},
    {id:'lazada',name:'Lazada',dot:'lazada',active:false,soon:true,desc:'东南亚阿里系'},
  ];

  $('#view').innerHTML = `
    <!-- Hero: Upload + Stats side by side -->
    <div class="hero-grid">
      <!-- Left: Upload Hero -->
      <div class="upload-hero" id="upload-hero" role="button" tabindex="0" aria-label="选择或拖入 Excel 或 Amazon JSON 文件开始处理">
        <div class="upload-hero-content">
          <div class="upload-hero-icon">&#128229;</div>
          <h2 class="upload-hero-title">拖入 .xlsx 或 Amazon .json</h2>
          <p class="upload-hero-sub">支持批量上传，自动识别 eBay、Amazon 表格与列式 JSON</p>
          <div class="upload-hero-meta">
            <span class="uh-tag">&#128247; AI 图审</span>
            <span class="uh-tag">&#127912; AI 生图</span>
            <span class="uh-tag">&#127760; 越南语翻译</span>
            <span class="uh-tag">&#128230; 自动注入</span>
          </div>
          <p class="upload-hero-hint">自动匹配平台流程，输出清洗表或 Amazon 回填表</p>
        </div>
      </div>

      <!-- Right: Stats -->
      <div class="stats-panel">
        <div class="stat-mini">
          <div class="stat-mini-val" style="color:var(--accent)">${d.today_count||0}</div>
          <div class="stat-mini-lbl">今日处理</div>
        </div>
        ${d.running_count > 0 || d.queue_depth > 0 ? `
        <div class="stat-mini">
          <div class="stat-mini-val" style="color:#f59e0b">${d.running_count||0} 运行 / ${d.queue_depth||0} 排队</div>
          <div class="stat-mini-lbl">任务状态</div>
        </div>` : ''}
        <div class="stat-mini">
          <div class="stat-mini-val" style="color:var(--blue)">${d.total_reviewed||0}</div>
          <div class="stat-mini-lbl">图片审查</div>
        </div>
        <div class="stat-mini">
          <div class="stat-mini-val" style="color:var(--amber)">${d.total_watermarks||0}</div>
          <div class="stat-mini-lbl">需处理图片</div>
        </div>
        <div class="stat-mini">
          <div class="stat-mini-val" style="color:var(--purple)">${d.total_generated||0}</div>
          <div class="stat-mini-lbl">AI 重新生成</div>
        </div>
      </div>
    </div>

    <!-- Platform selector -->
    <div class="section-head" style="margin-top:24px">
      <h3>来源平台</h3>
    </div>
    <div class="platform-row" id="platform-row">
      ${platforms.map(p=>`
        <span class="platform-chip ${p.active?'active':''} ${p.soon?'soon':''}" data-pid="${p.id}">
          <span class="chip-dot ${p.dot}"></span>${p.name}
          ${p.soon?`<span class="chip-soon">即将支持</span>`:''}
          ${p.active?`<span style="font-size:10px;margin-left:4px;opacity:.7">&#10003;</span>`:''}
        </span>
      `).join('')}
    </div>

    <!-- Drop zone (hidden, activated by hero click/drag) -->
    <div class="drop-zone" id="drop-zone" style="display:none">
      <span class="drop-zone-icon">&#128229;</span>
      <h2>拖入 .xlsx / Amazon .json 或点击选择</h2>
      <p>支持批量上传，自动识别来源格式，10 阶段流水线处理</p>
      <p class="drop-hint">处理流程: 提取图片 &#8594; AI 图审 &#8594; 删除人物附图 &#8594; 主图/变种去水印和人物 &#8594; 翻译清洗 &#8594; 按原格式输出回填文件</p>
    </div>
    <div id="dash-tasks"></div>

    <!-- Quick actions -->
    <div class="section-head" style="margin-top:28px"><h3>快捷操作</h3></div>
    <div class="quick-grid">
      <a class="quick-card" href="#/tasks">
        <div class="qc-icon generic">&#128196;</div>
        <div><div class="qc-text">查看所有任务</div><div class="qc-sub">管理历史记录与批量操作</div></div>
      </a>
      <a class="quick-card" href="#/templates">
        <div class="qc-icon generic">&#9881;</div>
        <div><div class="qc-text">管理来源模板</div><div class="qc-sub">配置各平台列映射适配器</div></div>
      </a>
      <a class="quick-card" href="#/settings">
        <div class="qc-icon generic">&#128273;</div>
        <div><div class="qc-text">API 密钥设置</div><div class="qc-sub">管理 DMXAPI & Agnes 密钥</div></div>
      </a>
    </div>

    <!-- Recent -->
    <div class="section-head"><h3>最近完成</h3></div>
    ${renderMiniTable(d.recent||[])}
  `;

  setupUploadZone($('#upload-hero'));
  if (d.running_count > 0) { $('#running-badge').hidden = false; $('#running-badge').textContent = d.running_count + ' running'; $('#running-badge').classList.add('running'); }
  else { $('#running-badge').hidden = true; }
  // 自动刷新仪表盘（仅当停留在首页时）
  if (!window._dashTimer) {
    window._dashTimer = setInterval(function() {
      if (location.hash === '#/' || location.hash === '') renderDashboard();
    }, 30000);
  }
}

// ===== TASKS LIST =====
let _taskPage = 1, _taskTotal = 0, _taskPages = 1, _taskFilter = 'all', _taskSort = 'created_desc';
const PAGE_SIZE = 20;
const TASK_FILTERS = [
  ['all', '全部'],
  ['high_risk', '高风险'],
  ['low_quality', '低分'],
  ['needs_sample', '需抽检'],
  ['usable', '可用'],
  ['needs_review', '待复核'],
  ['failed', '失败'],
  ['running', '处理中'],
  ['done', '已完成']
];
const TASK_SORTS = [
  ['created_desc', '最新优先'],
  ['created_asc', '最早优先'],
  ['quality_asc', '质量低到高'],
  ['quality_desc', '质量高到低']
];

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
  if (r.circuit) chips.push(['danger', '断' + r.circuit]);
  if (!chips.length) chips.push(['ok', '稳']);
  return '<div class="risk-chips">' + chips.map(([cls, label]) =>
    '<span class="risk-chip ' + cls + '">' + esc(label) + '</span>'
  ).join('') + '</div>';
}

function qualityScoreHTML(score) {
  const q = score || {};
  const severity = ['ok','warn','danger','info'].includes(q.severity) ? q.severity : 'info';
  const value = q.score == null ? '—' : String(Math.round(Number(q.score)));
  const labelText = q.label || (q.score == null ? '未评分' : '评分');
  return '<div class="quality-score ' + severity + '">' +
    '<strong>' + esc(value) + '</strong><span>' + esc(labelText) + '</span>' +
    '</div>';
}

function qualityReasonsHTML(score) {
  const reasons = Array.isArray((score || {}).reasons) ? score.reasons : [];
  if (!reasons.length) return '';
  return '<div class="quality-reasons">' + reasons.slice(0, 6).map(reason => {
    const points = Number(reason.points || 0);
    return '<span>' + esc(reason.label || reason.code || '扣分') +
      (points ? ' -' + esc(points) : '') + '</span>';
  }).join('') + '</div>';
}

function pctText(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(n % 1 ? 1 : 0) + '%' : '—';
}

function severityClass(value) {
  return ['ok','warn','danger','info'].includes(value) ? value : 'info';
}

function qualityDistributionHTML(quality) {
  const items = Array.isArray((quality || {}).distribution) ? quality.distribution : [];
  const total = items.reduce((sum, item) => sum + Number(item.count || 0), 0);
  if (!total) {
    return '<div class="ops-empty">暂无质量评分数据。先跑完几个任务，这里会显示 pass / 抽检 / 复核 / 高危分布。</div>';
  }
  return '<div class="quality-bars">' + items.map(item => {
    const count = Number(item.count || 0);
    const pct = Math.round(count / Math.max(total, 1) * 100);
    const cls = severityClass(item.severity);
    return '<div class="quality-bar-row">' +
      '<div class="quality-bar-head"><span>' + esc(item.label || item.grade) + '</span><strong>' + count + ' · ' + pct + '%</strong></div>' +
      '<div class="quality-bar-track"><div class="quality-bar-fill ' + cls + '" style="width:' + pct + '%"></div></div>' +
    '</div>';
  }).join('') + '</div>';
}

function topQualityReasonsHTML(quality) {
  const reasons = Array.isArray((quality || {}).top_reasons) ? quality.top_reasons : [];
  if (!reasons.length) {
    return '<div class="ops-empty">暂无扣分原因。所有已评分任务目前没有可聚合的质量损耗。</div>';
  }
  return '<div class="reason-list">' + reasons.map(reason => {
    const points = Number(reason.points || 0);
    const count = Number(reason.count || 0);
    return '<div class="reason-item">' +
      '<div><strong>' + esc(reason.label || reason.code || '扣分') + '</strong><span>' + esc(reason.code || '') + '</span></div>' +
      '<em>' + count + ' 次' + (points ? ' / -' + points : '') + '</em>' +
    '</div>';
  }).join('') + '</div>';
}

function drawQualityTrendChart(cv, trends) {
  if (!cv || !Array.isArray(trends) || !trends.length) return;
  const ctx = cv.getContext('2d');
  cv.width = cv.offsetWidth * 2; cv.height = cv.offsetHeight * 2;
  ctx.scale(2, 2);
  const w = cv.offsetWidth, h = cv.offsetHeight, pad = {top:14,right:20,bottom:26,left:36};
  const pw = w - pad.left - pad.right, ph = h - pad.top - pad.bottom;
  const rootStyles = getComputedStyle(document.documentElement);
  const mutedColor = rootStyles.getPropertyValue('--text-muted').trim() || '#7c8298';
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 1;
  ctx.fillStyle = mutedColor; ctx.font = '10px monospace';
  [0, 50, 100].forEach(v => {
    const y = pad.top + ph - (v / 100 * ph);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
    ctx.fillText(String(v), 4, y + 3);
  });
  trends.forEach((t, i) => {
    const x = pad.left + (i / Math.max(trends.length - 1, 1) * pw);
    if (i % Math.ceil(trends.length / 6) === 0) ctx.fillText(String(t.day || '').slice(5), x - 10, h - 6);
    const low = Number(t.low_quality || 0);
    const scored = Math.max(Number(t.scored || 0), 1);
    const barHeight = Math.min(ph, (low / scored) * ph);
    ctx.fillStyle = 'rgba(239,68,68,.22)';
    ctx.fillRect(x - 4, pad.top + ph - barHeight, 8, barHeight);
  });
  ctx.strokeStyle = '#10b981'; ctx.lineWidth = 2; ctx.beginPath();
  trends.forEach((t, i) => {
    const avg = t.avg_score == null ? 0 : Number(t.avg_score);
    const x = pad.left + (i / Math.max(trends.length - 1, 1) * pw);
    const y = pad.top + ph - (Math.max(0, Math.min(100, avg)) / 100 * ph);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = '#10b981'; ctx.fillRect(pad.left, 3, 10, 10);
  ctx.fillStyle = mutedColor; ctx.fillText('均分', pad.left + 14, 12);
  ctx.fillStyle = 'rgba(239,68,68,.55)'; ctx.fillRect(pad.left + 52, 3, 10, 10);
  ctx.fillStyle = mutedColor; ctx.fillText('低质占比', pad.left + 66, 12);
}

async function renderTasks(_params = {}, token = _routeToken) {
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
    if (_routeIsCurrent(token)) _showError('任务加载失败', e.message);
    return;
  }
  if (!_routeIsCurrent(token)) return;
  window._sel = new Set();

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
      renderTasks({}, _routeToken);
    };
  });
  const sortSelect = $('#task-sort');
  if (sortSelect) sortSelect.onchange = () => {
    _taskSort = sortSelect.value || 'created_desc';
    _taskPage = 1;
    renderTasks({}, _routeToken);
  };
  $('#select-all').onclick = () => {
    const cbs = $$('#task-table-body input[type=checkbox]');
    const check = window._sel.size < tasks.length;
    cbs.forEach((cb,i) => { cb.checked = check; check ? window._sel.add(tasks[i].id) : window._sel.delete(tasks[i].id); });
  };
  $('#batch-delete').onclick = async () => {
    if (!window._sel.size) return;
    if (!confirm('确定删除选中的 ' + window._sel.size + ' 个任务吗？此操作无法撤销。')) return;
    const ids = [...window._sel];
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
    if (_routeIsCurrent(token)) renderTasks({}, token);
  };
}

function goPage(p) {
  _taskPage = Math.max(1, Math.min(p, _taskPages));
  renderTasks();
}

// ===== TASK DETAIL =====
let _taskNotifySent = {};

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
    return '文件太大（当前上限 ' + ((window._uploadLimits||{}).max_upload_mb||50) + 'MB）。请拆分为多个小文件后上传。';
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

async function renderTaskDetail({id}, token = _routeToken) {
  id = safeId(id);
  if (!id) { _showError('任务地址无效', '任务 ID 格式不正确'); return; }
  let t = {};
  try { const r = await safeFetch('/api/tasks/'+id); t = await r.json(); }
  catch(e) {
    if (e.name !== 'AbortError' && _routeIsCurrent(token)) _showError('任务加载失败', e.message);
    return;
  }
  if (!_routeIsCurrent(token)) return;
  const status = statusKey(t.status);
  const si = Math.max(0, (t.stage_index||1) - 1);
  const done = status === 'done';
  const needsReview = status === 'needs_review';
  const complete = done || needsReview;

  let STAGES = [];
  try { const sr = await safeFetch('/api/stages?pipeline=' + encodeURIComponent(t.pipeline || 'ebay')); STAGES = (await sr.json()).stages || []; }
  catch(e) { STAGES = ['提取图片','AI 图审','AI 生图','附图清空','标题翻译','描述清洗','描述翻译','嵌入图片','视频清理','保存']; }
  if (!_routeIsCurrent(token)) return;
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
  if (!_routeIsCurrent(token)) return;

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
    if ('Notification' in window && !_taskNotifySent[id] && Notification.permission === 'granted') {
      _taskNotifySent[id] = true;
      new Notification('CrossPilot', { body: t.filename + ' 清洗完成！' });
    }
  }

  if (status === 'running' || status === 'queued') {
    let sseRetries = 0;
    const MAX_SSE_RETRIES = 10;
    var _pollTimer = null;

    function _startPolling() {
      if (_pollTimer || !_routeIsCurrent(token)) return;
      _pollTimer = _routeSetInterval(async function() {
        if (!_routeIsCurrent(token)) return;
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
      if (!_routeIsCurrent(token)) return;
      if (es) es.close();
      es = new EventSource('/api/tasks/'+id+'/events');
      es.onmessage = (ev) => {
        if (!_routeIsCurrent(token)) return;
        var m;
        try {
          m = JSON.parse(ev.data);
        } catch (_) {
          return;
        }
        sseRetries = 0;
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
        if (m.type === 'done' || m.type === 'needs_review' || m.type === 'failed' || m.type === 'cancelled') {
          es.close();
          route();
        }
        if (m.type === 'progress') updateStepBoard(m.data);
      };
      es.onerror = () => {
        es.close();
        if (!_routeIsCurrent(token)) return;
        if (sseRetries < MAX_SSE_RETRIES) {
          var delay = Math.min(1000 * Math.pow(2, sseRetries), 30000);
          sseRetries++;
          _routeSetTimeout(_connectSSE, delay);
        } else {
          _startPolling();
        }
      };
    }
    _routeSetTimeout(_connectSSE, 100);
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

// ===== TEMPLATES =====
async function renderTemplates(_params = {}, token = _routeToken) {
  let tmpl = [];
  try { const r = await safeFetch('/api/templates'); tmpl = (await r.json()).templates || []; }
  catch(e) {
    if (e.name !== 'AbortError' && _routeIsCurrent(token)) _showError('模板加载失败', e.message);
    return;
  }
  if (!_routeIsCurrent(token)) return;
  const presetPlatforms = [
    {id:'ebay_tk',name:'eBay',target:'TikTok Shop',desc:'店小秘导出的 eBay 商品表',active:true},
    {id:'shopee_tk',name:'Shopee',target:'TikTok Shop',desc:'Shopee Seller Center 商品表',active:false},
    {id:'amazon_tk',name:'Amazon',target:'Amazon 回填表',desc:'Amazon 商品采集表',active:true},
    {id:'lazada_tk',name:'Lazada',target:'TikTok Shop',desc:'Lazada Seller Center 商品表',active:false},
  ];
  const all = presetPlatforms.map(p => ({
    ...p,
    registered: tmpl.some(t => t.id === p.id)
  }));

  $('#view').innerHTML = `
    <div class="section-head"><h2>来源模板</h2><span style="font-size:12px;color:var(--text-muted)">当前可识别的表格格式</span></div>
    ${all.map(t=>`<div class="template-card">
      <div>
        <code>${esc(t.id)}.py</code>
        <div class="tmpl-meta">${esc(t.name)} &#8594; ${esc(t.target)} &middot; ${esc(t.desc)}</div>
      </div>
      <span class="badge ${t.registered?'badge-done':'badge-queued'}">${t.registered?'已支持':'即将支持'}</span>
    </div>`).join('')}
  `;
}

// ===== SETTINGS =====
async function renderSettings(_params = {}, token = _routeToken) {
  let keys = {}, ver = {};
  try {
    const responses = await Promise.all([safeFetch('/api/settings'), safeFetch('/api/version')]);
    keys = await responses[0].json();
    ver = await responses[1].json();
  } catch(e) {
    if (e.name !== 'AbortError' && _routeIsCurrent(token)) _showError('设置加载失败', e.message);
    return;
  }
  if (!_routeIsCurrent(token)) return;
  const updateInfo = ver.update
    ? `<div style="margin-top:20px;padding:14px 18px;background:var(--accent-soft);border:1px solid rgba(16,185,129,.2);border-radius:var(--radius-sm)">
        <span style="color:var(--accent);font-weight:600">发现新版本：${esc(ver.update.version)}</span>
        <span style="margin-left:12px;color:var(--text-muted)">重启 CrossPilot 后应用</span></div>`
    : '';
  $('#view').innerHTML = `
    <div class="section-head"><h2>设置</h2></div>
    <div class="card">
      <h3>模型提供商配置</h3>
      <div class="form-group">
        <label>文本模型 (text_provider)</label>
        <select id="text_provider" class="form-control">
          <option value="deepseek" ${keys.text_provider==='deepseek'?'selected':''}>DeepSeek</option>
          <option value="agnes" ${keys.text_provider==='agnes'?'selected':''}>Agnes</option>
        </select>
        <small>用于文本翻译、描述清洗、Bullet/关键词生成</small>
      </div>
      <div class="form-group">
        <label>图审模型 (vision_provider)</label>
        <select id="vision_provider" class="form-control">
          <option value="agnes" ${keys.vision_provider==='agnes'?'selected':''}>Agnes</option>
        </select>
        <small>用于检测水印、品牌、人物</small>
      </div>
      <div class="form-group">
        <label>生图模型 (image_gen_provider)</label>
        <select id="image_gen_provider" class="form-control">
          <option value="agnes" ${keys.image_gen_provider==='agnes'?'selected':''}>Agnes</option>
        </select>
        <small>用于去水印、去人物、生成合规图</small>
      </div>
      <hr style="margin: 20px 0; border: none; border-top: 1px solid var(--border);" />
      <h3>API 密钥</h3>
      <div class="form-group">
        <label for="deepseek_key">DeepSeek Key <span class="key-status ${keys.deepseek_key_set?'set':'unset'}">${keys.deepseek_key_set?'已配置':'未配置'}</span></label>
        <div class="form-row"><input type="password" id="deepseek_key" placeholder="输入新的 DeepSeek Key" autocomplete="off"></div>
        <small>用于文本翻译、描述清洗、Bullet/关键词生成</small>
      </div>
      <div class="form-group">
        <label for="agnes_key">Agnes Key <span class="key-status ${keys.agnes_key_set?'set':'unset'}">${keys.agnes_key_set?'已配置':'未配置'}</span></label>
        <div class="form-row"><input type="password" id="agnes_key" placeholder="输入新的 Agnes Key" autocomplete="off"></div>
        <small>用于图审、图生图</small>
      </div>
      <div class="cta-row">
        <button class="btn btn-primary" id="save-keys" data-action="save-settings">保存设置</button>
        <button class="btn btn-ghost" id="enable-notifications" data-action="enable-notifications">启用完成通知</button>
      </div>
      ${updateInfo}
      <p style="font-size:11px;color:var(--text-muted);margin-top:16px">版本：${esc(ver.version||'dev')}</p>
    </div>
  `;
  const notificationButton = $('#enable-notifications');
  if (!('Notification' in window)) {
    notificationButton.hidden = true;
  } else if (Notification.permission === 'granted') {
    notificationButton.textContent = '完成通知已启用';
    notificationButton.disabled = true;
  }
}

async function saveSettings() {
  const token = _routeToken;
  const body = {};
  // Provider 配置
  body.text_provider = $('#text_provider').value;
  body.vision_provider = $('#vision_provider').value;
  body.image_gen_provider = $('#image_gen_provider').value;
  // API keys
  if ($('#deepseek_key').value) body.deepseek_key = $('#deepseek_key').value;
  if ($('#agnes_key').value) body.agnes_key = $('#agnes_key').value;
  const btn = $('#save-keys');
  btn.textContent = '保存中...'; btn.disabled = true;
  try {
    await mutationFetch('/api/settings', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body),
    });
    toast('设置已保存');
    if (_routeIsCurrent(token) && location.hash === '#/settings') {
      await renderSettings({}, token);
    }
  } catch(e) {
    toast('保存失败：' + e.message, 'error');
    btn.textContent = '保存密钥';
    btn.disabled = false;
  }
}

// ===== ANALYTICS =====
async function renderAnalytics(_params = {}, token = _routeToken) {
  let d = {}, loadError = false;
  try { var r = await safeFetch('/api/analytics'); d = await r.json(); }
  catch(e) { loadError = true; }
  if (!_routeIsCurrent(token)) return;
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
  _routeSetTimeout(function() {
    if (!_routeIsCurrent(token)) return;
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

// ===== Upload =====
function setupUploadZone(zone) {
  if (!zone) return;
  const fp = $('#filepick');
  zone.onclick = () => { if (!_uploading) fp.click(); };
  zone.onkeydown = e => {
    if ((e.key === 'Enter' || e.key === ' ') && !_uploading) {
      e.preventDefault();
      fp.click();
    }
  };
  zone.ondragover = e => {
    e.preventDefault();
    if (!_uploading) zone.classList.add('over');
  };
  zone.ondragleave = () => zone.classList.remove('over');
  zone.ondrop = e => {
    e.preventDefault();
    zone.classList.remove('over');
    if (!_uploading) batchUpload(e.dataTransfer.files);
  };
  fp.onchange = () => { batchUpload(fp.files); fp.value = ''; };
}

function _showUploadResults(results, queue = {}) {
  const hero = $('#upload-hero');
  if (!hero) return;
  const content = hero.querySelector('.upload-hero-content');
  const ok = results.filter(item => item.job_id && !item.error);
  const failed = results.filter(item => item.error);
  content.innerHTML = `
    <div class="upload-results" role="status">
      <h2 class="upload-hero-title">上传结果</h2>
      <p class="upload-summary">成功 ${ok.length} 个，失败 ${failed.length} 个</p>
      <div class="upload-result-list">
        ${results.slice(0, 20).map(item => `
          <div class="upload-result-item ${item.error?'failed':'done'}">
            <span>${esc(item.filename || '未命名文件')}</span>
            <strong>${item.error ? esc(item.error) : '已加入队列'}</strong>
          </div>
        `).join('')}
      </div>
      ${queue.queue_depth ? `<p class="upload-queue">排队 ${Number(queue.queue_depth)} 个，处理中 ${Number(queue.running_count||0)} 个</p>` : ''}
      <div class="cta-row" style="justify-content:center">
        ${ok.length ? '<a class="btn btn-primary" href="#/tasks">查看任务</a>' : ''}
        <button class="btn btn-ghost" data-action="upload-again">继续上传</button>
      </div>
    </div>`;
}

async function batchUpload(files) {
  if (_uploading || !files || !files.length) return;
  const selected = Array.from(files);
  const limits = window._uploadLimits || {max_upload_mb:50, max_batch_files:20};
  if (selected.length > limits.max_batch_files) {
    _showUploadResults(selected.map(file => ({
      filename:file.name,
      error:`单次最多上传 ${limits.max_batch_files} 个文件`,
    })));
    return;
  }

  const maxBytes = limits.max_upload_mb * 1024 * 1024;
  const invalid = selected
    .filter(file => {
      const name = file.name.toLowerCase();
      return (!name.endsWith('.xlsx') && !name.endsWith('.json')) || file.size > maxBytes;
    })
    .map(file => ({
      filename:file.name,
      error:!['.xlsx','.json'].some(ext => file.name.toLowerCase().endsWith(ext))
        ? '只接受 .xlsx 或 Amazon .json 文件'
        : `文件超过 ${limits.max_upload_mb}MB 上限`,
    }));
  if (invalid.length) {
    _showUploadResults(invalid);
    return;
  }

  const fd = new FormData();
  for (const f of selected) fd.append('files', f);
  const zone = $('.drop-zone');
  const hero = $('#upload-hero');
  _uploading = true;
  if (hero) {
    hero.classList.add('is-uploading');
    hero.setAttribute('aria-disabled', 'true');
  }

  // 显示上传进度条
  if (zone) zone.innerHTML = '<div class="upload-progress"><div class="up-bar"><div class="up-fill" id="up-fill" style="width:0%"></div></div><span class="up-pct" id="up-pct">0%</span><p style="font-size:11px;color:var(--text-muted);margin-top:8px">上传中</p><button class="btn btn-ghost btn-sm" data-action="cancel-upload">取消上传</button></div>';
  if (hero) hero.querySelector('.upload-hero-content').innerHTML = '<div class="upload-progress"><div class="up-bar"><div class="up-fill" id="up-fill2" style="width:0%"></div></div><span class="up-pct" id="up-pct2">0%</span><p style="font-size:11px;color:var(--text-muted);margin-top:8px">上传中，请勿关闭页面</p><button class="btn btn-ghost btn-sm" data-action="cancel-upload">取消上传</button></div>';

  function _setPct(pct) {
    ['up-fill','up-fill2'].forEach(function(id) {
      var el = document.getElementById(id); if (el) el.style.width = pct + '%';
    });
    ['up-pct','up-pct2'].forEach(function(id) {
      var el = document.getElementById(id); if (el) el.textContent = pct + '%';
    });
  }

  try {
    const payload = await new Promise(function(resolve, reject) {
      var xhr = new XMLHttpRequest();
      _uploadXhr = xhr;
      xhr.open('POST', '/api/upload/batch');
      xhr.setRequestHeader('X-CrossPilot-Request', '1');
      xhr.timeout = 10 * 60 * 1000;
      xhr.upload.onprogress = function(e) { if (e.lengthComputable) _setPct(Math.round(e.loaded / e.total * 100)); };
      xhr.onload = function() {
        try {
          const body = JSON.parse(xhr.responseText || '{}');
          if (xhr.status < 200 || xhr.status >= 300) throw new Error(body.detail || `HTTP ${xhr.status}`);
          resolve(body);
        } catch(e) { reject(e); }
      };
      xhr.onerror = function() { reject(new Error('网络连接失败')); };
      xhr.ontimeout = function() { reject(new Error('上传超时，请减小文件或检查网络')); };
      xhr.onabort = function() { reject(new Error('上传已取消')); };
      xhr.send(fd);
    });
    if (!Array.isArray(payload.results) || !payload.results.length) {
      throw new Error('服务器未返回上传结果');
    }
    const results = payload.results.slice(0, selected.length).map(function(item, index) {
      const result = item && typeof item === 'object' ? item : {};
      const jobId = safeId(result.job_id);
      return {
        filename: result.filename || selected[index].name,
        job_id: jobId,
        error: result.error || (jobId ? '' : '服务器未返回有效任务 ID'),
      };
    });
    while (results.length < selected.length) {
      results.push({
        filename: selected[results.length].name,
        error: '服务器未返回处理结果',
      });
    }
    const ok = results.filter(item => item.job_id && !item.error);
    const failed = results.filter(item => item.error);
    if (failed.length) {
      _showUploadResults(results, payload);
      toast(`${failed.length} 个文件上传失败`, 'error');
    } else if (ok.length === 1) {
      toast('文件已加入处理队列');
      navigateTo('#/tasks/' + safeId(ok[0].job_id));
    } else {
      toast(`${ok.length} 个文件已加入处理队列`);
      navigateTo('#/tasks');
    }
  } catch(e) {
    _showUploadResults([{filename:'上传请求', error:e.message}]);
    toast(e.message, 'error');
  } finally {
    _uploading = false;
    _uploadXhr = null;
    if (hero) {
      hero.classList.remove('is-uploading');
      hero.removeAttribute('aria-disabled');
    }
  }
}

// ===== Delete =====
async function retryTask(id, fresh) {
  if (fresh && !confirm('从头重跑会清空图审、翻译和生图缓存，并重新消耗 API 额度。确定继续吗？')) return;
  try {
    await mutationFetch('/api/tasks/'+safeId(id)+'/retry?fresh='+(fresh?'true':'false'), {method:'POST'});
    toast(fresh ? '已从头重新加入队列' : '已从缓存继续处理');
    navigateTo('#/tasks/' + safeId(id));
  } catch(e) { toast('重试失败：' + e.message, 'error'); }
}

async function cancelTask(id) {
  if (!confirm('取消后会停止当前进程，已完成的缓存会保留。确定取消吗？')) return;
  try {
    await mutationFetch('/api/tasks/'+safeId(id)+'/cancel', {method:'POST'});
    toast('任务已取消，可稍后继续处理');
    route();
  } catch(e) { toast('取消失败：' + e.message, 'error'); }
}

async function delTask(id, afterDelete) {
  if (!confirm('确定删除此任务及其全部文件吗？此操作无法撤销。')) return;
  try {
    await mutationFetch('/api/tasks/'+safeId(id), {method:'DELETE'});
    toast('任务已删除');
    if (afterDelete === 'home') navigateTo('#/');
    else if (location.hash.includes('/tasks/')) navigateTo('#/tasks');
    else await renderTasks({}, _routeToken);
  } catch(e) { toast('删除失败：' + e.message, 'error'); }
}

// ===== Helpers =====
function esc(s) { return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function safeFetch(url, opts) {
  const options = {...(opts || {})};
  if (!options.signal && (!options.method || options.method === 'GET') && _routeAbort) {
    options.signal = _routeAbort.signal;
  }
  const r = await fetch(url, options);
  if (!r.ok) {
    const detail = await r.json().catch(()=>({}));
    throw new Error(detail.detail || `HTTP ${r.status}: ${url}`);
  }
  return r;
}
function mutationFetch(url, opts) {
  const options = {...(opts || {})};
  options.headers = {...(options.headers || {}), 'X-CrossPilot-Request':'1'};
  return safeFetch(url, options);
}
function navigateTo(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}
function safeId(id) { const value=String(id||''); return /^[0-9a-f]{12}$/.test(value) ? value : ''; }
function statusKey(status) { return ['queued','running','done','needs_review','failed','cancelled'].includes(status) ? status : 'failed'; }
function pct(value) { const n=Number(value); return Number.isFinite(n) ? Math.max(0,Math.min(100,Math.round(n))) : 0; }
function fmtTime(ts) { if(!ts) return '-'; return new Date(ts*1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'}); }
function fmtDur(s) {
  const seconds = Number(s);
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  return seconds >= 60
    ? Math.floor(seconds / 60) + 'm ' + Math.round(seconds % 60) + 's'
    : Math.round(seconds) + 's';
}
function dur(a,b) { if(!a||!b) return '-'; return fmtDur(Math.round(b-a)); }
function label(s) { return {queued:'排队中',running:'处理中',done:'已完成',needs_review:'待复核',failed:'失败',cancelled:'已取消'}[s]||'未知'; }
function stats(t,key) { return ((t.stats||{})[key])||0; }

async function enableNotifications() {
  const token = _routeToken;
  if (!('Notification' in window)) {
    toast('当前浏览器不支持系统通知', 'error');
    return;
  }
  const permission = await Notification.requestPermission();
  if (permission === 'granted') {
    toast('完成通知已启用');
    if (_routeIsCurrent(token) && location.hash === '#/settings') {
      await renderSettings({}, token);
    }
  } else {
    toast('通知权限未启用', 'error');
  }
}

document.addEventListener('click', event => {
  const actionElement = event.target.closest('[data-action]');
  if (actionElement) {
    event.preventDefault();
    event.stopPropagation();
    const action = actionElement.dataset.action;
    const id = safeId(actionElement.dataset.id);
    if (action === 'reload') location.reload();
    else if (action === 'save-settings') saveSettings();
    else if (action === 'enable-notifications') enableNotifications();
    else if (action === 'upload-again') $('#filepick').click();
    else if (action === 'cancel-upload' && _uploadXhr) _uploadXhr.abort();
    else if (action === 'retry-task' && id) retryTask(id, actionElement.dataset.fresh === 'true');
    else if (action === 'delete-task' && id) delTask(id, actionElement.dataset.afterDelete);
    else if (action === 'cancel-task' && id) cancelTask(id);
    else if (action === 'refresh-task' && id) route();
    return;
  }

  const pageButton = event.target.closest('[data-page]');
  if (pageButton) {
    event.preventDefault();
    goPage(Number(pageButton.dataset.page));
    return;
  }

  const row = event.target.closest('[data-task-row]');
  if (row && !event.target.closest('a,button,input,label')) {
    const id = safeId(row.dataset.taskRow);
    if (id) location.hash = '#/tasks/' + id;
  }
});

document.addEventListener('change', event => {
  const checkbox = event.target.closest('[data-select-task]');
  if (!checkbox || !window._sel) return;
  const id = safeId(checkbox.dataset.selectTask);
  if (!id) return;
  checkbox.checked ? window._sel.add(id) : window._sel.delete(id);
});

// Init upload
setTimeout(() => { const z = $('#upload-hero'); if (z) setupUploadZone(z); }, 200);

function renderMiniTable(tasks) {
  if (!tasks.length) return '<div class="empty-state"><span class="empty-state-icon" aria-hidden="true">&#128203;</span><h3>暂无记录</h3></div>';
  return `<div class="table-wrap"><table>
    <thead><tr><th>状态</th><th>文件</th><th>状态</th><th>耗时</th><th>统计</th><th>操作</th></tr></thead>
    <tbody>${tasks.map(t=>{const id=safeId(t.id),status=statusKey(t.status);return `
      <tr>
        <td><span class="status-dot ${status}" aria-label="${label(status)}"></span></td>
        <td style="color:var(--text);font-weight:500"><a href="#/tasks/${id}" style="color:inherit">${esc(t.filename)}</a></td>
        <td><span class="badge badge-${status}">${label(status)}</span></td>
        <td>${dur(t.created_at,t.updated_at)}</td>
        <td style="font-family:var(--font-mono);font-size:11px;min-width:160px">${status==='running' && t.percent != null ? `<div class="mini-progress"><div class="mini-bar"><div class="mini-fill" style="width:${pct(t.percent)}%"></div></div><span class="mini-pct">${pct(t.percent)}%</span></div>` : `审:${stats(t,'images_reviewed')} 问题:${stats(t,'watermarks')} 生:${stats(t,'images_generated')}`}</td>
        <td>${status==='done'||status==='needs_review'?`<a class="btn btn-sm btn-ghost" href="/api/tasks/${id}/download">${status==='needs_review'?'下载复核':'下载'}</a>`:''}</td>
      </tr>`}).join('')}
    </tbody></table></div>`;
}
