// CrossPilot SPA - vanilla JS router + SSE + multi-platform support
// 公共工具函数从 helpers.js 导入
import { $, $$, esc, safeFetch, safeId, setRouteFn, updateClock } from './helpers.js';
import { renderDashboard } from './dashboard.js';
import { renderTemplates } from './templates.js';
import { enableNotifications, renderSettings, saveSettings } from './settings.js';
import { renderAnalytics } from './analytics.js';
import {
  cancelTask,
  delTask,
  goPage,
  renderTasks,
  renderTaskDetail,
  retryTask,
  setTaskSelected,
} from './tasks.js';
import { cancelUpload } from './upload.js';
import {
  beginRoute,
  closeRouteResources,
  routeIsCurrent,
  setErrorRenderer,
} from './runtime.js';

let _clockTimer = null;

// ===== Clock =====
updateClock(); _clockTimer = setInterval(updateClock, 30000);
addEventListener('beforeunload', () => {
  closeRouteResources();
  cancelUpload();
  clearInterval(_clockTimer);
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
setErrorRenderer(_showError);

function _showLoading() {
  $('#view').innerHTML = `<div role="status" aria-live="polite" style="text-align:center;padding:80px 20px">
    <div class="spinner"></div>
    <p style="color:var(--text-muted);margin-top:16px">加载中...</p>
  </div>`;
}

async function route() {
  const token = beginRoute();
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
    if (e.name === 'AbortError' || !routeIsCurrent(token)) return;
    console.error('route error:', e);
    _showError('页面加载失败', e.message || '未知错误');
  }
}

setRouteFn(route);
addEventListener('hashchange', route);
route();

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
    else if (action === 'cancel-upload') cancelUpload();
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
  if (!checkbox) return;
  const id = safeId(checkbox.dataset.selectTask);
  if (id) setTaskSelected(id, checkbox.checked);
});
