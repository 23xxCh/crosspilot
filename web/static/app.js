// CrossPilot SPA - vanilla JS router + SSE + multi-platform support
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
let es = null, _clockTimer = null;

// Helper: close SSE on route change
function _closeSSE() { if (es) { es.close(); es = null; } }

// ===== Toast =====
function toast(msg, type='success') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`; el.textContent = msg;
  $('#toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ===== Clock =====
function updateClock() {
  $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
}
updateClock(); _clockTimer = setInterval(updateClock, 30000);
addEventListener('beforeunload', () => { _closeSSE(); clearInterval(_clockTimer); });

// ===== SPA Router =====
const ROUTES = {
  '/': renderDashboard,
  '/tasks': renderTasks,
  '/tasks/:id': renderTaskDetail,
  '/templates': renderTemplates,
  '/settings': renderSettings,
};

function _showError(title, detail) {
  $('#view').innerHTML = `<div style="text-align:center;padding:60px 20px">
    <div style="font-size:48px;margin-bottom:16px">&#9888;</div>
    <h3>${esc(title)}</h3>
    <p style="color:var(--text-muted);margin:8px 0 24px">${esc(detail||'')}</p>
    <button class="btn btn-primary" onclick="location.reload()">刷新重试</button>
    <button class="btn" style="margin-left:8px" onclick="location.hash='#/'">返回首页</button>
  </div>`;
}

function _showLoading() {
  $('#view').innerHTML = `<div style="text-align:center;padding:80px 20px">
    <div class="spinner"></div>
    <p style="color:var(--text-muted);margin-top:16px">加载中...</p>
  </div>`;
}

function route() {
  _closeSSE();
  _showLoading();
  const hash = (location.hash || '#/').slice(1) || '/';
  const parts = hash.split('/');
  let routeFn = null, params = {};
  if (parts[0] === 'tasks' && parts[1]) {
    routeFn = ROUTES['/tasks/:id']; params = {id: parts[1]};
  } else {
    routeFn = ROUTES[hash] || ROUTES['/'];
  }
  const activeRoute = (parts[0] === 'tasks' && parts[1]) ? '/tasks' : hash;
  $$('.sidebar-item').forEach(a => a.classList.toggle('active', a.dataset.route === activeRoute));
  const labels = {'/':'CrossPilot','/tasks':'任务列表','/templates':'模板管理','/settings':'设置'};
  const extra = (parts[0]==='tasks'&&parts[1]) ? ' / 任务详情 / ' + esc(parts[1]) : '';
  $('#breadcrumb').innerHTML = `<span style="color:var(--text-muted)">CrossPilot</span><span class="sep">/</span><span class="current">${labels[activeRoute]||''}${extra}</span>`;
  try {
    routeFn(params);
  } catch (e) {
    console.error('route error:', e);
    _showError('页面加载失败', e.message || '未知错误');
  }
}

window.onhashchange = route;
route();

// ===== DASHBOARD =====
async function renderDashboard() {
  let d = {};
  try { const r = await safeFetch('/api/dashboard'); d = await r.json(); } catch(e) {}

  const platforms = [
    {id:'ebay',name:'eBay',dot:'ebay',active:true,soon:false,desc:'全球拍卖 & 固定价格市场'},
    {id:'shopee',name:'Shopee',dot:'shopee',active:false,soon:true,desc:'东南亚 & 台湾'},
    {id:'amazon',name:'Amazon',dot:'amazon',active:false,soon:true,desc:'全球最大电商平台'},
    {id:'lazada',name:'Lazada',dot:'lazada',active:false,soon:true,desc:'东南亚阿里系'},
  ];

  $('#view').innerHTML = `
    <!-- Hero: Upload + Stats side by side -->
    <div class="hero-grid">
      <!-- Left: Upload Hero -->
      <div class="upload-hero" id="upload-hero">
        <div class="upload-hero-content">
          <div class="upload-hero-icon">&#128229;</div>
          <h2 class="upload-hero-title">拖入 .xlsx 文件开始清洗</h2>
          <p class="upload-hero-sub">支持批量上传，自动识别 eBay/Shopee/Amazon 格式</p>
          <div class="upload-hero-meta">
            <span class="uh-tag">&#128247; AI 图审</span>
            <span class="uh-tag">&#127912; AI 生图</span>
            <span class="uh-tag">&#127760; 越南语翻译</span>
            <span class="uh-tag">&#128230; 自动注入</span>
          </div>
          <p class="upload-hero-hint">10 阶段流水线处理，输出 <code>_cleaned.xlsx</code></p>
        </div>
      </div>

      <!-- Right: Stats -->
      <div class="stats-panel">
        <div class="stat-mini">
          <div class="stat-mini-val" style="color:var(--accent)">${d.today_count||0}</div>
          <div class="stat-mini-lbl">今日处理</div>
        </div>
        <div class="stat-mini">
          <div class="stat-mini-val" style="color:var(--blue)">${d.total_reviewed||0}</div>
          <div class="stat-mini-lbl">图片审查</div>
        </div>
        <div class="stat-mini">
          <div class="stat-mini-val" style="color:var(--amber)">${d.total_watermarks||0}</div>
          <div class="stat-mini-lbl">发现水印</div>
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
        <div class="platform-chip ${p.active?'active':''} ${p.soon?'soon':''}" data-pid="${p.id}">
          <span class="chip-dot ${p.dot}"></span>${p.name}
          ${p.soon?`<span class="chip-soon">即将支持</span>`:''}
          ${p.active?`<span style="font-size:10px;margin-left:4px;opacity:.7">&#10003;</span>`:''}
        </div>
      `).join('')}
    </div>

    <!-- Drop zone (hidden, activated by hero click/drag) -->
    <div class="drop-zone" id="drop-zone" style="display:none">
      <span class="drop-zone-icon">&#128229;</span>
      <h2>拖入 .xlsx 文件 或 点击选择</h2>
      <p>支持批量上传，自动识别来源格式，10 阶段流水线处理</p>
      <p class="drop-hint">处理流程: 提取图片 &#8594; AI 图审 &#8594; AI 生图去水印 &#8594; 翻译越南语 &#8594; 清洗注入 &#8594; 输出 <code>_cleaned.xlsx</code></p>
    </div>
    <div id="dash-tasks"></div>

    <!-- Quick actions -->
    <div class="section-head" style="margin-top:28px"><h3>快捷操作</h3></div>
    <div class="quick-grid">
      <div class="quick-card" onclick="location.hash='#/tasks'">
        <div class="qc-icon generic">&#128196;</div>
        <div><div class="qc-text">查看所有任务</div><div class="qc-sub">管理历史记录与批量操作</div></div>
      </div>
      <div class="quick-card" onclick="location.hash='#/templates'">
        <div class="qc-icon generic">&#9881;</div>
        <div><div class="qc-text">管理来源模板</div><div class="qc-sub">配置各平台列映射适配器</div></div>
      </div>
      <div class="quick-card" onclick="location.hash='#/settings'">
        <div class="qc-icon generic">&#128273;</div>
        <div><div class="qc-text">API 密钥设置</div><div class="qc-sub">管理 DMXAPI & Agnes 密钥</div></div>
      </div>
    </div>

    <!-- Recent -->
    <div class="section-head"><h3>最近完成</h3></div>
    ${renderMiniTable(d.recent||[])}
  `;

  setupUploadZone($('#upload-hero'));
  if (d.running_count > 0) { $('#running-badge').hidden = false; $('#running-badge').textContent = d.running_count + ' running'; $('#running-badge').classList.add('running'); }
  else { $('#running-badge').hidden = true; }
}

// ===== TASKS LIST =====
async function renderTasks() {
  let tasks = [];
  try { const r = await safeFetch('/api/tasks'); tasks = (await r.json()).tasks || []; } catch(e) {}
  window._sel = new Set();

  function refresh() {
    const tbody = $('#task-table-body');
    if (!tbody) return;
    tbody.innerHTML = tasks.map(t=>`
      <tr>
        <td><input type="checkbox" onchange="this.checked?window._sel.add('${t.id}'):window._sel.delete('${t.id}')"></td>
        <td><span class="status-dot ${t.status}"></span></td>
        <td style="color:var(--text);font-weight:500"><a href="#/tasks/${t.id}" style="color:inherit">${esc(t.filename)}</a></td>
        <td><span class="badge badge-${t.status}">${label(t.status)}</span></td>
        <td>${fmtTime(t.created_at)}</td>
        <td>${dur(t.created_at,t.updated_at)}</td>
        <td style="font-family:var(--font-mono);font-size:11px">审${stats(t,'images_reviewed')} 印${stats(t,'watermarks')} 生${stats(t,'images_generated')}</td>
        <td>${t.status==='done'?`<a class="btn btn-sm btn-ghost" href="/api/tasks/${t.id}/download">下载</a>`:''}
          <button class="btn btn-sm btn-danger" onclick="delTask('${t.id}')">删除</button></td>
      </tr>`).join('');
  }

  $('#view').innerHTML = `
    <div class="section-head"><h3>所有任务</h3>
      <div class="section-actions">
        <button class="btn btn-ghost btn-sm" id="select-all">全选</button>
        <button class="btn btn-sm btn-danger" id="batch-delete">批量删除</button>
      </div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th style="width:32px"></th><th></th><th>文件名</th><th>状态</th><th>创建</th><th>耗时</th><th>统计</th><th>操作</th></tr></thead>
      <tbody id="task-table-body"></tbody></table></div>
    ${tasks.length===0?`<div class="empty-state" style="margin-top:24px"><span class="empty-state-icon">&#128203;</span><h3>还没有任务</h3><p>在仪表盘上传你的第一个表格吧</p></div>`:''}
  `;
  refresh();
  $('#select-all').onclick = () => {
    const cbs = $$('#task-table-body input[type=checkbox]');
    const check = window._sel.size < tasks.length;
    cbs.forEach((cb,i) => { cb.checked = check; check ? window._sel.add(tasks[i].id) : window._sel.delete(tasks[i].id); });
  };
  $('#batch-delete').onclick = async () => {
    if (!window._sel.size) return;
    if (!confirm('Delete ' + window._sel.size + ' tasks?')) return;
    const ids = [...window._sel];
    const total = ids.length;
    let ok = 0, fail = 0;
    const btn = $('#batch-delete');
    const origText = btn.textContent;
    // 并发删除（最多10个并行），显示进度
    const chunkSize = 10;
    for (let i = 0; i < ids.length; i += chunkSize) {
      const chunk = ids.slice(i, i + chunkSize);
      const results = await Promise.allSettled(chunk.map(id => fetch('/api/tasks/'+id, {method:'DELETE'})));
      for (const r of results) {
        if (r.status === 'fulfilled' && r.value.ok) ok++;
        else fail++;
      }
      btn.textContent = `删除中 ${Math.min(i+chunkSize, total)}/${total}...`;
    }
    btn.textContent = origText;
    toast(`Deleted ${ok}${fail?' (failed '+fail+')':''} / ${total} tasks`);
    renderTasks();
  };
}

// ===== TASK DETAIL =====
async function renderTaskDetail({id}) {
  let t = {};
  try { const r = await safeFetch('/api/tasks/'+id); t = await r.json(); } catch(e) { $('#view').innerHTML='<p class="error-block">Failed to load</p>'; return; }
  const si = (t.stage_index||1)-1;
  // 从后端动态读取管道阶段（避免前后端硬编码不同步）
  let STAGES = (t.stage_total && t.stage_total === 10) ? [] : [];
  if (!STAGES.length) {
    try { const sr = await fetch('/api/stages'); STAGES = (await sr.json()).stages || []; }
    catch(e) { STAGES = ['Extract Images','AI Review','AI Regenerate','Clear Attachments','Title Clean+Translate','Description Clean','Description Translate','Embed Images','Cleanup','Save']; }
  }

  const timeline = STAGES.map((s,i) => {
    let cls = '';
    if (t.status==='done' || i < si) cls = 'done';
    else if (i === si && t.status==='running') cls = 'current';
    else if (t.status==='failed') cls = 'failed';
    return `<div class="tl-item"><div class="tl-dot ${cls}"></div><div class="tl-stage">${s}</div><div class="tl-detail">${i===si&&t.status==='running'?`${t.current||0}/${t.total||0} items - ETA ${fmtDur(t.eta_s||0)}`:i<si?'Completed':''}</div></div>`;
  }).join('');

  let thumbHTML = '';
  // 加载每行处理结果
  let rowTableHTML = '';
  try {
    const rr = await fetch('/api/tasks/'+id+'/rows');
    const rj = await rr.json();
    if (rj.rows && rj.rows.length) {
      rowTableHTML = `
        <h3 style="margin-top:24px;margin-bottom:12px">Product Rows (${rj.rows.length})</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>#</th><th>Title</th><th>Images</th><th>Warnings</th><th>Status</th></tr></thead>
            <tbody>${rj.rows.map(row => `
              <tr>
                <td style="font-family:var(--font-mono);font-size:12px">${row.row}</td>
                <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(row.title)}">${esc(row.title)||'-'}</td>
                <td style="font-family:var(--font-mono);font-size:11px">${row.image_count||0} img</td>
                <td>
                  ${row.watermark_count>0?`<span style="color:var(--amber);font-weight:600">${row.watermark_count} watermark</span>`:''}
                  ${row.generated_count>0?`<span style="color:var(--blue);font-weight:600;margin-left:8px">${row.generated_count} regenerated</span>`:''}
                </td>
                <td><span class="badge ${row.all_clean?'badge-done':'badge-running'}" style="font-size:9px">${row.all_clean?'CLEAN':'ISSUES'}</span></td>
              </tr>`).join('')}
            </tbody></table></div>`;
    }
  } catch(e) {}

  if (t.cache) {
    const rev = t.cache.review_results || {}, gen = t.cache.gen_results || {};
    const wmUrls = Object.entries(rev).filter(([_,v])=>v===true);
    if (wmUrls.length || Object.keys(gen).length) {
      thumbHTML = '<h3 style="margin-top:24px;margin-bottom:12px">Image Review Results</h3><div class="thumb-grid">' +
        wmUrls.map(([url])=>`<div class="thumb-item"><img src="${esc(url)}" loading="lazy" onerror="this.parentElement.remove()"><div class="thumb-badge wm">WATERMARK</div></div>`).join('') +
        Object.entries(gen).map(([old,nu])=>`<div class="thumb-item"><img src="${esc(nu)}" loading="lazy" onerror="this.parentElement.remove()"><div class="thumb-badge gen">REGEN</div></div>`).join('') +
        '</div>';
    }
  }

  if (t.status === 'running') {
    let sseRetries = 0;
    const MAX_SSE_RETRIES = 10;
    function _connectSSE() {
      if (es) es.close();
      es = new EventSource('/api/tasks/'+id+'/events');
      es.onmessage = (ev) => {
        sseRetries = 0;  // 收到消息，重置重试计数
        const m = JSON.parse(ev.data);
        if (m.type === 'done' || m.type === 'failed') { es.close(); renderTaskDetail({id}); }
        if (m.type === 'progress') updateDetailProgress(m.data);
      };
      es.onerror = () => {
        es.close();
        if (sseRetries < MAX_SSE_RETRIES) {
          const delay = Math.min(1000 * Math.pow(2, sseRetries), 30000);
          sseRetries++;
          console.log(`SSE 断线，${delay/1000}s 后重连 (${sseRetries}/${MAX_SSE_RETRIES})`);
          setTimeout(_connectSSE, delay);
        } else {
          console.error('SSE 重连失败，已达最大重试次数');
        }
      };
    }
    setTimeout(_connectSSE, 100);
  }

  $('#view').innerHTML = `
    <a href="#/tasks" style="font-size:13px;color:var(--text-muted);display:block;margin-bottom:16px">&larr; Back to tasks</a>
    <div class="card task-card">
      <div class="task-head"><div><h2>${esc(t.filename)}</h2><div class="task-meta" style="margin-top:8px"><span class="badge badge-${t.status}">${label(t.status)}</span><span>ID: ${id}</span><span id="dp-duration">${t.status==='running'?`Elapsed: ${fmtDur(t.total_elapsed_s||0)}`:t.status==='done'?`Duration: ${dur(t.created_at,t.updated_at)}`:''}</span></div></div>
      ${t.status==='done'?`<a class="btn btn-primary" href="/api/tasks/${id}/download">Download Result</a>`:''}
      </div>
      ${t.status==='running'?`
        <div class="progress-section">
          <div class="progress-info"><span class="progress-title" id="dp-title">${t.stage||'Queued'}</span><span class="progress-stats" id="dp-stats"><span>${t.current||0}</span> / <span>${t.total||0}</span></span></div>
          <div class="progress-bar"><div class="progress-fill" id="dp-fill" style="width:${t.percent||0}%"></div></div>
          <div class="progress-eta" id="dp-eta">${t.eta_s?`ETA ${fmtDur(t.eta_s)}`:''}</div>
        </div>`:''}
      <h3 style="margin-top:20px;margin-bottom:14px">Pipeline</h3>
      <div class="timeline">${timeline}</div>
      ${rowTableHTML}
      ${thumbHTML}
    </div>`;
}

function updateDetailProgress(d) {
  const fill = $('#dp-fill');
  // 进度 DOM 不存在时触发完整重渲染（防止 innerHTML 被意外覆盖）
  if (!fill) {
    console.warn('progress DOM missing, re-rendering task detail');
    const hash = location.hash.slice(2);
    if (hash.startsWith('tasks/')) { renderTaskDetail({id: hash.split('/')[1]}); }
    return;
  }
  fill.style.width = (d.percent||0)+'%';
  const title = $('#dp-title'); if (title) title.textContent = d.stage||'';
  const stats = $('#dp-stats'); if (stats) stats.innerHTML = '<span>'+(d.current||0)+'</span> / <span>'+(d.total||0)+'</span>';
  const eta = $('#dp-eta'); if (eta) eta.textContent = d.eta_s ? 'ETA ' + fmtDur(d.eta_s) : '';
  const dur = $('#dp-duration'); if (dur && d.total_elapsed_s != null) dur.textContent = 'Elapsed: ' + fmtDur(d.total_elapsed_s);
  const si = (d.stage_index||1)-1;
  $$('.tl-dot').forEach((dot,i)=>{dot.className='tl-dot';if(i<si)dot.classList.add('done');if(i===si)dot.classList.add('current');});
  console.log('progress:', d.stage, d.current+'/'+d.total, 'dur='+d.total_elapsed_s+'s');
}

// ===== TEMPLATES =====
async function renderTemplates() {
  let tmpl = [];
  try { const r = await safeFetch('/api/templates'); tmpl = (await r.json()).templates || []; } catch(e) {}
  const presetPlatforms = [
    {id:'ebay_tk',name:'eBay',target:'TikTok Shop',desc:'45-column eBay export via dianxiaomi',active:true},
    {id:'shopee_tk',name:'Shopee',target:'TikTok Shop',desc:'Shopee seller center export format',active:false},
    {id:'amazon_tk',name:'Amazon',target:'TikTok Shop',desc:'Amazon inventory report',active:false},
    {id:'lazada_tk',name:'Lazada',target:'TikTok Shop',desc:'Lazada seller center export',active:false},
  ];
  const all = presetPlatforms.map(p => ({
    ...p,
    registered: tmpl.some(t => t.id === p.id)
  }));

  $('#view').innerHTML = `
    <div class="section-head"><h3>Source Templates</h3><span style="font-size:12px;color:var(--text-muted)">Column mapping adapters for each source platform</span></div>
    <p style="font-size:13px;color:var(--text-muted);margin-bottom:20px">Each source platform has a dedicated adapter that maps its export columns to the unified processing pipeline. Add new platforms by creating adapters in <code style="background:var(--surface);padding:2px 6px;border-radius:4px;font-family:var(--font-mono);font-size:11px">scripts/adapters/</code>.</p>
    ${all.map(t=>`<div class="template-card">
      <div>
        <code>${t.id}.py</code>
        <div class="tmpl-meta">${t.name} &#8594; ${t.target} &middot; ${t.desc}</div>
      </div>
      <span class="badge ${t.registered?'badge-done':'badge-queued'}">${t.registered?'Registered':'Coming Soon'}</span>
    </div>`).join('')}
  `;
}

// ===== SETTINGS =====
async function renderSettings() {
  let keys = {}, ver = {};
  try { const r = await safeFetch('/api/settings'); keys = await r.json(); } catch(e) {}
  try { const r = await safeFetch('/api/version'); ver = await r.json(); } catch(e) {}
  const updateInfo = ver.update
    ? `<div style="margin-top:20px;padding:14px 18px;background:var(--accent-soft);border:1px solid rgba(16,185,129,.2);border-radius:var(--radius-sm)">
        <span style="color:var(--accent);font-weight:600">Update Available: ${ver.update.version}</span>
        <button class="btn btn-sm btn-primary" style="margin-left:12px" onclick="doUpdate()">Download & Restart</button></div>`
    : '';
  $('#view').innerHTML = `
    <div class="section-head"><h3>Settings</h3></div>
    <div class="card">
      <div class="form-group">
        <label>DMXAPI Key <span class="key-status ${keys.dmx_key_set?'set':'unset'}">${keys.dmx_key_set?'Configured':'Not Set'}</span></label>
        <div class="form-row"><input type="password" id="dmx_key" placeholder="sk-..." autocomplete="off"></div>
        <small>AI Review (MiMo V2.5) + Text Translation/Cleaning (DeepSeek V4) + Image Gen (Wanxiang/Doubao)</small>
      </div>
      <div class="form-group">
        <label>Agnes Key <span class="key-status ${keys.agnes_key_set?'set':'unset'}">${keys.agnes_key_set?'Configured':'Not Set'}</span></label>
        <div class="form-row"><input type="password" id="agnes_key" placeholder="cpk-..." autocomplete="off"></div>
        <small>Fallback image generation engine. Token Plan daily quota: 4,000 images</small>
      </div>
      <div class="cta-row"><button class="btn btn-primary" id="save-keys" onclick="saveSettings()">Save Keys</button></div>
      ${updateInfo}
      <p style="font-size:11px;color:var(--text-muted);margin-top:16px">Version: ${ver.version||'dev'}</p>
    </div>
  `;
}

async function doUpdate() {
  toast('Downloading update...');
  try {
    const r = await fetch('/api/version');
    const v = await r.json();
    if (v.update && (v.update.url || v.update.path)) {
      toast('Update available. Restart to apply.');
    } else {
      toast('Already up to date');
    }
  } catch(e) { toast('Update check failed', 'error'); }
}

async function saveSettings() {
  const body = {};
  if ($('#dmx_key').value) body.dmx_key = $('#dmx_key').value;
  if ($('#agnes_key').value) body.agnes_key = $('#agnes_key').value;
  if (!body.dmx_key && !body.agnes_key) { toast('Enter at least one key', 'error'); return; }
  const btn = $('#save-keys');
  btn.textContent = 'Saving...'; btn.disabled = true;
  try {
    await fetch('/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast('Keys saved');
    renderSettings();
  } catch(e) { toast('Save failed', 'error'); }
  btn.textContent = 'Save Keys'; btn.disabled = false;
}

// ===== Upload =====
function setupUploadZone(zone) {
  if (!zone) return;
  const fp = $('#filepick');
  zone.onclick = () => fp.click();
  zone.ondragover = e => { e.preventDefault(); zone.classList.add('over'); };
  zone.ondragleave = () => zone.classList.remove('over');
  zone.ondrop = e => { e.preventDefault(); zone.classList.remove('over'); batchUpload(e.dataTransfer.files); };
  fp.onchange = () => { batchUpload(fp.files); fp.value = ''; };
}

async function batchUpload(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  const zone = $('.drop-zone');
  if (zone) zone.innerHTML = '<p style="color:var(--text-muted);padding:20px">Uploading...</p>';
  try {
    const r = await fetch('/api/upload/batch', {method:'POST', body:fd});
    const j = await r.json();
    const ok = (j.results||[]).filter(x=>x.job_id);
    toast(ok.length + ' files queued');
    if (ok.length === 1) location.hash = '#/tasks/' + ok[0].job_id;
    else location.hash = '#/tasks';
  } catch(e) {
    toast('Upload failed', 'error');
    if (zone) zone.innerHTML = `<div style="text-align:center;padding:20px">
      <p style="color:var(--danger);margin-bottom:12px">上传失败，请重试</p>
      <button class="btn btn-primary btn-sm" id="retry-upload-btn">重新上传</button>
    </div>`;
    setTimeout(() => {
      const btn = $('#retry-upload-btn');
      if (btn) btn.onclick = () => { setupUploadZone(zone); zone.click(); };
    }, 100);
  }
}

// ===== Delete =====
async function delTask(id) {
  if (!confirm('Delete this task and all associated files?')) return;
  const r = await fetch('/api/tasks/'+id, {method:'DELETE'});
  if (r.ok) { toast('Task deleted'); route(); }
  else { toast('Delete failed', 'error'); }
}

// ===== Helpers =====
function esc(s) { return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function safeFetch(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    const detail = await r.json().catch(()=>({}));
    throw new Error(detail.detail || `HTTP ${r.status}: ${url}`);
  }
  return r;
}
function fmtTime(ts) { if(!ts) return '-'; return new Date(ts*1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'}); }
function fmtDur(s) { if(!s) return ''; return s >= 60 ? Math.floor(s/60)+'m '+s%60+'s' : s+'s'; }
function dur(a,b) { if(!a||!b) return '-'; return fmtDur(Math.round(b-a)); }
function label(s) { return {queued:'Queued',running:'Running',done:'Done',failed:'Failed'}[s]||s; }
function stats(t,key) { return ((t.stats||{})[key])||0; }

// Init upload
setTimeout(() => { const z = $('#upload-hero'); if (z) setupUploadZone(z); }, 200);

function renderMiniTable(tasks) {
  if (!tasks.length) return '<div class="empty-state"><span class="empty-state-icon">&#128203;</span><h3>No records yet</h3></div>';
  return `<div class="table-wrap"><table>
    <thead><tr><th></th><th>File</th><th>Status</th><th>Duration</th><th>Stats</th><th></th></tr></thead>
    <tbody>${tasks.map(t=>`
      <tr>
        <td><span class="status-dot ${t.status}"></span></td>
        <td style="color:var(--text);font-weight:500"><a href="#/tasks/${t.id}" style="color:inherit">${esc(t.filename)}</a></td>
        <td><span class="badge badge-${t.status}">${label(t.status)}</span></td>
        <td>${dur(t.created_at,t.updated_at)}</td>
        <td style="font-family:var(--font-mono);font-size:11px">R:${stats(t,'images_reviewed')} W:${stats(t,'watermarks')} G:${stats(t,'images_generated')}</td>
        <td>${t.status==='done'?`<a class="btn btn-sm btn-ghost" href="/api/tasks/${t.id}/download">Download</a>`:''}</td>
      </tr>`).join('')}
    </tbody></table></div>`;
}