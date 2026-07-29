// dashboard.js — 仪表盘页面
import { $, safeFetch } from './helpers.js';
import { getRouteToken, routeIsCurrent, routeSetTimeout } from './runtime.js';
import {
  renderMiniTable,
  setUploadLimits,
  setupUploadZone,
} from './upload.js';

let dashboardTimer = null;

export async function renderDashboard(_params = {}, token = getRouteToken()) {
  // token 由 router 传入
  let d = {}, loadError = false;
  try {
    const r = await safeFetch('/api/dashboard');
    if (!r.ok) throw new Error('API ' + r.status);
    d = await r.json();
  } catch(e) { loadError = true; }
  if (!routeIsCurrent(token)) return;
  if (loadError) {
    $('#view').innerHTML = '<div style="text-align:center;padding:80px 20px"><div style="font-size:48px;margin-bottom:16px">&#9888;</div><h3>加载失败</h3><p style="color:var(--text-muted);margin:8px 0 24px">无法连接到服务器，请检查网络后重试</p><button class="btn btn-primary" data-action="reload">重新加载</button></div>';
    return;
  }
  setUploadLimits({
    max_upload_mb: d.max_upload_mb || 50,
    max_batch_files: d.max_batch_files || 20,
  });

  const platforms = [
    {id:'ebay',name:'eBay',dot:'ebay',active:true,soon:false,desc:'全球拍卖 & 固定价格市场'},
    {id:'shopee',name:'Shopee',dot:'shopee',active:false,soon:true,desc:'东南亚 & 台湾'},
    {id:'amazon',name:'Amazon',dot:'amazon',active:true,soon:false,desc:'全球最大电商平台'},
    {id:'lazada',name:'Lazada',dot:'lazada',active:false,soon:true,desc:'东南亚阿里系'},
  ];

  const miniTable = renderMiniTable(d.recent || []);

  $('#view').innerHTML = `
    <div class="hero-grid">
      <div class="upload-hero" id="upload-hero" role="button" tabindex="0" aria-label="选择或拖入 Excel 或 Amazon JSON 文件开始处理">
        <div class="upload-hero-content">
          <div class="upload-hero-icon">&#128229;</div>
          <h2 class="upload-hero-title">拖入 .xlsx 或 Amazon .json</h2>
          <p class="upload-hero-sub">支持批量上传，自动识别 eBay、Amazon 表格与列式 JSON</p>
          <div class="upload-hero-meta">
            <span class="uh-tag">&#128247; AI 图审</span><span class="uh-tag">&#127912; AI 生图</span>
            <span class="uh-tag">&#127760; 越南语翻译</span><span class="uh-tag">&#128230; 自动注入</span>
          </div>
          <p class="upload-hero-hint">自动匹配平台流程，输出清洗表或 Amazon 回填表</p>
        </div>
      </div>
      <div class="stats-panel">
        <div class="stat-mini"><div class="stat-mini-val" style="color:var(--accent)">${d.today_count||0}</div><div class="stat-mini-lbl">今日处理</div></div>
        <div class="health-row" id="health-row">
          <span class="health-dot" id="h-deepseek" title="DeepSeek">DS</span>
          <span class="health-dot" id="h-agnes-text" title="Agnes Text">AT</span>
          <span class="health-dot" id="h-agnes-img" title="Agnes Image">AI</span>
          <span class="health-dot" id="h-gpt-img" title="GPT Image">GI</span>
        </div>
        ${d.running_count > 0 || d.queue_depth > 0 ? `<div class="stat-mini"><div class="stat-mini-val" style="color:#f59e0b">${d.running_count||0} 运行 / ${d.queue_depth||0} 排队</div><div class="stat-mini-lbl">任务状态</div></div>` : ''}
        <div class="stat-mini"><div class="stat-mini-val" style="color:var(--blue)">${d.total_reviewed||0}</div><div class="stat-mini-lbl">图片审查</div></div>
        <div class="stat-mini"><div class="stat-mini-val" style="color:var(--amber)">${d.total_watermarks||0}</div><div class="stat-mini-lbl">需处理图片</div></div>
        <div class="stat-mini"><div class="stat-mini-val" style="color:var(--purple)">${d.total_generated||0}</div><div class="stat-mini-lbl">AI 重新生成</div></div>
      </div>
    </div>
    <div class="section-head" style="margin-top:24px"><h3>来源平台</h3></div>
    <div class="platform-row" id="platform-row">
      ${platforms.map(p=>`<span class="platform-chip ${p.active?'active':''} ${p.soon?'soon':''}" data-pid="${p.id}"><span class="chip-dot ${p.dot}"></span>${p.name}${p.soon?`<span class="chip-soon">即将支持</span>`:''}${p.active?`<span style="font-size:10px;margin-left:4px;opacity:.7">&#10003;</span>`:''}</span>`).join('')}
    </div>
    <div class="drop-zone" id="drop-zone" style="display:none">
      <span class="drop-zone-icon">&#128229;</span><h2>拖入 .xlsx / Amazon .json 或点击选择</h2>
      <p>支持批量上传，自动识别来源格式，10 阶段流水线处理</p>
    </div>
    <div id="dash-tasks"></div>
    <div class="section-head" style="margin-top:28px"><h3>快捷操作</h3></div>
    <div class="quick-grid">
      <a class="quick-card" href="#/tasks"><div class="qc-icon generic">&#128196;</div><div><div class="qc-text">查看所有任务</div><div class="qc-sub">管理历史记录与批量操作</div></div></a>
      <a class="quick-card" href="#/templates"><div class="qc-icon generic">&#9881;</div><div><div class="qc-text">管理来源模板</div><div class="qc-sub">配置各平台列映射适配器</div></div></a>
      <a class="quick-card" href="#/settings"><div class="qc-icon generic">&#128273;</div><div><div class="qc-text">API 密钥设置</div><div class="qc-sub">管理 DMXAPI & Agnes 密钥</div></div></a>
    </div>
    <div class="section-head"><h3>最近完成</h3></div>
    ${miniTable}
  `;

  setupUploadZone($('#upload-hero'));
  if (d.running_count > 0) { $('#running-badge').hidden = false; $('#running-badge').textContent = d.running_count + ' running'; $('#running-badge').classList.add('running'); }
  else { $('#running-badge').hidden = true; }
  if (dashboardTimer) clearTimeout(dashboardTimer);
  dashboardTimer = routeSetTimeout(() => {
    dashboardTimer = null;
    if (routeIsCurrent(token) && (location.hash === '#/' || location.hash === '')) {
      renderDashboard({}, token);
    }
  }, 30000);
  _fetchHealth();
}

async function _fetchHealth() {
  try {
    const r = await fetch('/api/v1/health');
    if (!r.ok) return;
    const h = await r.json();
    const svc = {};
    (h.services || []).forEach(s => {
      svc[s.name.replace(/[^a-z]/gi,'').toLowerCase()] = s.ok;
    });
    const ids = {
      deepseektext: 'h-deepseek', agnestext: 'h-agnes-text',
      agnesimagegen: 'h-agnes-img', gptimagegen: 'h-gpt-img',
    };
    Object.entries(ids).forEach(([k, id]) => {
      const el = document.getElementById(id);
      if (el) el.className = 'health-dot ' + (svc[k] ? 'ok' : 'fail');
    });
  } catch(e) {}
}
