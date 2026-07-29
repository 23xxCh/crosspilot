// upload.js — file upload + XHR + mini table
import {
  $,
  dur,
  esc,
  label,
  navigateTo,
  pct,
  safeId,
  stats,
  statusKey,
  toast,
} from './helpers.js';

let uploading = false;
let uploadXhr = null;
let uploadLimits = {max_upload_mb: 50, max_batch_files: 20};

export function setUploadLimits(limits) {
  uploadLimits = {
    max_upload_mb: Number(limits?.max_upload_mb) || 50,
    max_batch_files: Number(limits?.max_batch_files) || 20,
  };
}

export function getUploadLimits() {
  return {...uploadLimits};
}

export function cancelUpload() {
  if (uploadXhr) uploadXhr.abort();
}

export function setupUploadZone(zone) {
  if (!zone) return;
  const fp = $('#filepick');
  zone.onclick = () => { if (!uploading) fp.click(); };
  zone.onkeydown = e => {
    if ((e.key === 'Enter' || e.key === ' ') && !uploading) {
      e.preventDefault();
      fp.click();
    }
  };
  zone.ondragover = e => {
    e.preventDefault();
    if (!uploading) zone.classList.add('over');
  };
  zone.ondragleave = () => zone.classList.remove('over');
  zone.ondrop = e => {
    e.preventDefault();
    zone.classList.remove('over');
    if (!uploading) batchUpload(e.dataTransfer.files);
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
export async function batchUpload(files) {
  if (uploading || !files || !files.length) return;
  const selected = Array.from(files);
  const limits = uploadLimits;
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
  uploading = true;
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
      uploadXhr = xhr;
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
    uploading = false;
    uploadXhr = null;
    if (hero) {
      hero.classList.remove('is-uploading');
      hero.removeAttribute('aria-disabled');
    }
  }
}
export function renderMiniTable(tasks) {
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
