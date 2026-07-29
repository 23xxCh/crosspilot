// templates.js — auto-extracted page module
import { $, safeFetch, esc } from './helpers.js';
import { getRouteToken, routeIsCurrent, showError } from './runtime.js';

export async function renderTemplates(_params = {}, token = getRouteToken()) {
  let tmpl = [];
  try { const r = await safeFetch('/api/templates'); tmpl = (await r.json()).templates || []; }
  catch(e) {
    if (e.name !== 'AbortError' && routeIsCurrent(token)) showError('模板加载失败', e.message);
    return;
  }
  if (!routeIsCurrent(token)) return;
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
