// settings.js — auto-extracted page module
import { $, safeFetch, esc, toast, mutationFetch } from './helpers.js';
import { getRouteToken, routeIsCurrent, showError } from './runtime.js';

export async function renderSettings(_params = {}, token = getRouteToken()) {
  let keys = {}, ver = {}, promptData = {prompts: []};
  try {
    const responses = await Promise.all([
      safeFetch('/api/settings'),
      safeFetch('/api/version'),
      safeFetch('/api/prompts'),
    ]);
    keys = await responses[0].json();
    ver = await responses[1].json();
    promptData = await responses[2].json();
  } catch(e) {
    if (e.name !== 'AbortError' && routeIsCurrent(token)) showError('设置加载失败', e.message);
    return;
  }
  if (!routeIsCurrent(token)) return;
  const updateInfo = ver.update
    ? `<div style="margin-top:20px;padding:14px 18px;background:var(--accent-soft);border:1px solid rgba(16,185,129,.2);border-radius:var(--radius-sm)">
        <span style="color:var(--accent);font-weight:600">发现新版本：${esc(ver.update.version)}</span>
        <span style="margin-left:12px;color:var(--text-muted)">重启 CrossPilot 后应用</span></div>`
    : '';
  const lockedInfo = (keys.locked_fields || []).length
    ? `<div class="form-note">以下字段由系统环境变量锁定，网页保存不会覆盖：${esc(keys.locked_fields.join(', '))}</div>`
    : '';
  const modelProfileOptions = (keys.model_profiles || ['production'])
    .map(profile => `<option value="${esc(profile)}" ${profile===keys.model_profile?'selected':''}>${esc(profile)}</option>`)
    .join('');
  const promptProfileOptions = (keys.prompt_profiles || ['production', 'test'])
    .map(profile => `<option value="${esc(profile)}" ${profile===keys.prompt_profile?'selected':''}>${esc(profile)}</option>`)
    .join('');
  const promptOptions = (promptData.prompts || [])
    .map(prompt => `<option value="${esc(prompt.id)}">${esc(prompt.id)}</option>`)
    .join('');
  $('#view').innerHTML = `
    <div class="section-head"><h2>设置</h2></div>
    <div class="card">
      <h3>模型提供商配置</h3>
      ${lockedInfo}
      <div class="form-group">
        <label>配置档 (model_profile)</label>
        <select id="model_profile" class="form-control">${modelProfileOptions}</select>
        <small>对应 crosspilot/model_profiles.json 中的配置档</small>
      </div>
      <div class="form-group">
        <label>Prompt 配置档 (prompt_profile)</label>
        <select id="prompt_profile" class="form-control">${promptProfileOptions}</select>
        <small>production 与 test 的 Prompt 覆盖互相隔离</small>
      </div>
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
          <option value="gpt" ${keys.image_gen_provider==='gpt'?'selected':''}>GPT Image</option>
        </select>
        <small>用于去水印、去人物、生成合规图</small>
      </div>
      <hr style="margin:20px 0;border:none;border-top:1px solid var(--border)" />
      <h3>Agnes 503 快速拥堵控制</h3>
      <div class="form-note">
        健康时保持当前并发；503 时只短暂重试，然后立即切换回退模型。
      </div>
      <div class="form-group">
        <label>503 快速重试次数</label>
        <select id="agnes_503_retry_limit" class="form-control">
          ${[0,1,2,3].map(value => `<option value="${value}" ${String(keys.agnes_503_retry_limit||'1')===String(value)?'selected':''}>${value}</option>`).join('')}
        </select>
        <small>推荐 1；设为 0 会在首次 503 后立即回退</small>
      </div>
      <div class="form-row">
        <div class="form-group" style="flex:1">
          <label>最短等待（秒）</label>
          <input id="agnes_503_backoff_min_s" type="number" min="0" max="60" step="0.5" class="form-control" value="${esc(keys.agnes_503_backoff_min_s || '3')}" />
        </div>
        <div class="form-group" style="flex:1">
          <label>最长等待（秒）</label>
          <input id="agnes_503_backoff_max_s" type="number" min="0" max="60" step="0.5" class="form-control" value="${esc(keys.agnes_503_backoff_max_s || '8')}" />
        </div>
      </div>
      <div class="form-row">
        <div class="form-group" style="flex:1">
          <label>连续 503 熔断阈值</label>
          <input id="agnes_503_circuit_threshold" type="number" min="1" max="20" step="1" class="form-control" value="${esc(keys.agnes_503_circuit_threshold || '3')}" />
        </div>
        <div class="form-group" style="flex:1">
          <label>熔断冷却（秒）</label>
          <input id="agnes_503_circuit_cooldown_s" type="number" min="10" max="1800" step="10" class="form-control" value="${esc(keys.agnes_503_circuit_cooldown_s || '120')}" />
        </div>
      </div>
      <hr style="margin: 20px 0; border: none; border-top: 1px solid var(--border);" />
      <h3>精确模型 ID</h3>
      <div class="form-group">
        <label>DeepSeek 文本主模型</label>
        <input id="deepseek_text_model" class="form-control" value="${esc(keys.deepseek_text_model || '')}" />
      </div>
      <div class="form-group">
        <label>DeepSeek 文本回退模型</label>
        <input id="deepseek_text_fallback_model" class="form-control" value="${esc(keys.deepseek_text_fallback_model || '')}" />
      </div>
      <div class="form-group">
        <label>Agnes 文本模型</label>
        <input id="agnes_text_model" class="form-control" value="${esc(keys.agnes_text_model || '')}" />
      </div>
      <div class="form-group">
        <label>Agnes 图审模型</label>
        <input id="agnes_vision_model" class="form-control" value="${esc(keys.agnes_vision_model || '')}" />
      </div>
      <div class="form-group">
        <label>Agnes 生图主模型</label>
        <input id="agnes_image_model" class="form-control" value="${esc(keys.agnes_image_model || '')}" />
      </div>
      <div class="form-group">
        <label>Agnes 生图回退模型</label>
        <input id="agnes_image_fallback_model" class="form-control" value="${esc(keys.agnes_image_fallback_model || '')}" />
      </div>
      <div class="form-group">
        <label>GPT 生图模型</label>
        <input id="gpt_image_model" class="form-control" value="${esc(keys.gpt_image_model || '')}" />
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
      <div class="form-group">
        <label for="gpt_image_key">GPT Image Key <span class="key-status ${keys.gpt_image_key_set?'set':'unset'}">${keys.gpt_image_key_set?'已配置':'未配置'}</span></label>
        <div class="form-row"><input type="password" id="gpt_image_key" placeholder="输入新的 GPT Image Key" autocomplete="off"></div>
        <small>仅在 GPT 生图主路由或回退启用时使用</small>
      </div>
      <div class="cta-row">
        <button class="btn btn-primary" id="save-keys" data-action="save-settings">保存设置</button>
        <button class="btn btn-ghost" id="enable-notifications" data-action="enable-notifications">启用完成通知</button>
      </div>
      ${updateInfo}
      <p style="font-size:11px;color:var(--text-muted);margin-top:16px">版本：${esc(ver.version||'dev')}</p>
    </div>
    <div class="card" style="margin-top:20px">
      <h3>Prompt 管理</h3>
      <div class="form-note">
        当前配置档：${esc(promptData.profile || keys.prompt_profile || 'production')}。
        保存前会校验模板变量并自动创建历史快照。
      </div>
      <div class="form-group">
        <label for="prompt_select">业务 Prompt</label>
        <select id="prompt_select" class="form-control">${promptOptions}</select>
      </div>
      <div id="prompt_meta" class="form-note">正在加载…</div>
      <div class="form-group">
        <label for="prompt_content">Prompt 内容</label>
        <textarea id="prompt_content" class="form-control" rows="18" spellcheck="false"></textarea>
      </div>
      <div class="cta-row">
        <button class="btn btn-primary" id="save-prompt">保存 Prompt</button>
        <button class="btn btn-ghost" id="reset-prompt">恢复发布默认值</button>
      </div>
      <hr style="margin:20px 0;border:none;border-top:1px solid var(--border)" />
      <div class="form-group">
        <label for="prompt_history">历史版本</label>
        <select id="prompt_history" class="form-control">
          <option value="">暂无历史版本</option>
        </select>
      </div>
      <button class="btn btn-ghost" id="rollback-prompt">回滚到所选版本</button>
    </div>
  `;
  const notificationButton = $('#enable-notifications');
  if (!('Notification' in window)) {
    notificationButton.hidden = true;
  } else if (Notification.permission === 'granted') {
    notificationButton.textContent = '完成通知已启用';
    notificationButton.disabled = true;
  }
  const promptSelect = $('#prompt_select');
  if (promptSelect && promptSelect.value) {
    promptSelect.addEventListener('change', () => {
      loadPromptEditor(promptSelect.value, token);
    });
    $('#save-prompt').addEventListener('click', () => savePromptEdit(token));
    $('#reset-prompt').addEventListener('click', () => resetPromptEdit(token));
    $('#rollback-prompt').addEventListener('click', () => rollbackPromptEdit(token));
    await loadPromptEditor(promptSelect.value, token);
  }
}

async function loadPromptEditor(promptId, token) {
  try {
    const encoded = encodeURIComponent(promptId);
    const responses = await Promise.all([
      safeFetch(`/api/prompts/${encoded}`),
      safeFetch(`/api/prompts/${encoded}/history`),
    ]);
    const detail = await responses[0].json();
    const history = await responses[1].json();
    if (!routeIsCurrent(token)) return;
    $('#prompt_content').value = detail.content || '';
    $('#prompt_meta').textContent =
      `来源：${detail.source === 'override' ? '当前配置档覆盖' : '发布默认值'} · ` +
      `签名：${detail.signature} · 变量：${(detail.variables || []).join(', ') || '无'}`;
    const historySelect = $('#prompt_history');
    const revisions = history.revisions || [];
    historySelect.innerHTML = revisions.length
      ? revisions.map(item => {
          const when = item.timestamp ? new Date(item.timestamp).toLocaleString('zh-CN') : '';
          const label = `${when} · ${item.reason || 'edit'} · ${item.signature || ''}`;
          return `<option value="${esc(item.revision_id)}">${esc(label)}</option>`;
        }).join('')
      : '<option value="">暂无历史版本</option>';
  } catch(e) {
    if (e.name !== 'AbortError' && routeIsCurrent(token)) {
      toast('Prompt 加载失败：' + e.message, 'error');
    }
  }
}

async function savePromptEdit(token) {
  const promptId = $('#prompt_select').value;
  const button = $('#save-prompt');
  button.disabled = true;
  button.textContent = '保存中…';
  try {
    await mutationFetch(`/api/prompts/${encodeURIComponent(promptId)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: $('#prompt_content').value}),
    });
    toast('Prompt 已保存并创建历史快照');
    await loadPromptEditor(promptId, token);
  } catch(e) {
    toast('Prompt 保存失败：' + e.message, 'error');
  } finally {
    if (routeIsCurrent(token)) {
      button.disabled = false;
      button.textContent = '保存 Prompt';
    }
  }
}

async function resetPromptEdit(token) {
  const promptId = $('#prompt_select').value;
  if (!window.confirm('恢复发布默认值？当前覆盖会保留在历史版本中。')) return;
  try {
    await mutationFetch(
      `/api/prompts/${encodeURIComponent(promptId)}/override`,
      {method: 'DELETE'},
    );
    toast('已恢复发布默认值');
    await loadPromptEditor(promptId, token);
  } catch(e) {
    toast('恢复失败：' + e.message, 'error');
  }
}

async function rollbackPromptEdit(token) {
  const promptId = $('#prompt_select').value;
  const revisionId = $('#prompt_history').value;
  if (!revisionId) {
    toast('请先选择历史版本', 'error');
    return;
  }
  if (!window.confirm('回滚到所选版本？当前内容会先自动创建快照。')) return;
  try {
    await mutationFetch(
      `/api/prompts/${encodeURIComponent(promptId)}/rollback`,
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({revision_id: revisionId}),
      },
    );
    toast('Prompt 已回滚');
    await loadPromptEditor(promptId, token);
  } catch(e) {
    toast('回滚失败：' + e.message, 'error');
  }
}

export async function saveSettings() {
  const token = getRouteToken();
  const body = {};
  // Provider 配置
  body.text_provider = $('#text_provider').value;
  body.vision_provider = $('#vision_provider').value;
  body.image_gen_provider = $('#image_gen_provider').value;
  body.model_profile = $('#model_profile').value.trim();
  body.prompt_profile = $('#prompt_profile').value.trim();
  for (const field of [
    'deepseek_text_model',
    'deepseek_text_fallback_model',
    'agnes_text_model',
    'agnes_vision_model',
    'agnes_image_model',
    'agnes_image_fallback_model',
    'gpt_image_model',
  ]) {
    body[field] = $(`#${field}`).value.trim();
  }
  for (const field of [
    'agnes_503_retry_limit',
    'agnes_503_backoff_min_s',
    'agnes_503_backoff_max_s',
    'agnes_503_circuit_threshold',
    'agnes_503_circuit_cooldown_s',
  ]) {
    body[field] = $(`#${field}`).value.trim();
  }
  // API keys
  if ($('#deepseek_key').value) body.deepseek_key = $('#deepseek_key').value;
  if ($('#agnes_key').value) body.agnes_key = $('#agnes_key').value;
  if ($('#gpt_image_key').value) body.gpt_image_key = $('#gpt_image_key').value;
  const btn = $('#save-keys');
  btn.textContent = '保存中...'; btn.disabled = true;
  try {
    await mutationFetch('/api/settings', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body),
    });
    toast('设置已保存');
    if (routeIsCurrent(token) && location.hash === '#/settings') {
      await renderSettings({}, token);
    }
  } catch(e) {
    toast('保存失败：' + e.message, 'error');
    btn.textContent = '保存密钥';
    btn.disabled = false;
  }
}

export async function enableNotifications() {
  const token = getRouteToken();
  if (!('Notification' in window)) {
    toast('当前浏览器不支持系统通知', 'error');
    return;
  }
  const permission = await Notification.requestPermission();
  if (permission === 'granted') {
    toast('完成通知已启用');
    if (routeIsCurrent(token) && location.hash === '#/settings') {
      await renderSettings({}, token);
    }
  } else {
    toast('通知权限未启用', 'error');
  }
}
