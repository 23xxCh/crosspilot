"""Render the offline interactive final-review document."""
from __future__ import annotations

import html
import json

def render_html(rows: list[dict], summary: dict) -> str:
    cards = []
    for row in rows:
        image_cards = []
        for image in row['images']:
            role = html.escape(image['role'])
            role_key = str(image.get('role_key') or 'attachment')
            assessment = image.get('assessment') or {}
            status = str(assessment.get('status') or 'unknown')
            reasons = (
                assessment.get('risk_categories')
                or assessment.get('reasons')
                or []
            )
            reason_text = '、'.join(str(item) for item in reasons) or '无'
            evidence = html.escape(
                str(image.get('evidence') or '暂无证据说明')
            )
            source_label = (
                '生成图' if image.get('source') == 'generated' else '源图'
            )
            decision_key = html.escape(
                '|'.join([
                    str(row['product_id']),
                    role_key,
                    str(image['url']),
                ]),
                quote=True,
            )
            source_compare = ''
            source_url = str(image.get('source_url') or '')
            if source_url:
                source_local = image.get('source_local_path') or ''
                if source_local:
                    source_compare = (
                        '<div class="before"><span>生成前</span>'
                        f'<img loading="lazy" src="{html.escape(source_local)}" '
                        f'alt="{role} 生成前"></div>'
                    )
            if image['download_ok']:
                src = html.escape(image['local_path'].replace('\\', '/'))
                visual = (
                    source_compare
                    + '<div class="after">'
                    f'<img loading="lazy" src="{src}" alt="{role}">'
                    '</div>'
                )
            else:
                remote = html.escape(image['url'])
                visual = (
                    '<div class="missing">'
                    f'<a href="{remote}">图片下载失败，打开原链接</a>'
                    '</div>'
                )
            if role_key == 'attachment':
                action_buttons = (
                    '<button data-action="delete_image">删除单张附图</button>'
                    '<button data-action="false_positive">标记误判</button>'
                )
            else:
                action_buttons = (
                    '<button data-action="regenerate_image">'
                    '重新生成主图/变种图</button>'
                    '<button data-action="false_positive">标记误判</button>'
                )
            image_cards.append(
                f'<figure class="image status-{html.escape(status)}" '
                f'data-status="{html.escape(status)}" '
                f'data-role="{html.escape(role_key)}" '
                f'data-decision-key="{decision_key}">'
                f'<div class="visuals">{visual}</div>'
                '<figcaption>'
                f'<strong>{role}</strong>'
                f'<span class="badge {html.escape(status)}">'
                f'{html.escape(status.upper())}</span>'
                f'<span class="source">{source_label}</span>'
                f'<small>类别：{html.escape(reason_text)}</small>'
                f'<small>位置：{html.escape(str(assessment.get("placement") or "unknown"))}</small>'
                f'<small>证据：{evidence}</small>'
                '</figcaption>'
                f'<div class="image-actions" data-product="{html.escape(str(row["product_id"]), quote=True)}" '
                f'data-url="{html.escape(str(image["url"]), quote=True)}" '
                f'data-role="{html.escape(role_key, quote=True)}">'
                f'{action_buttons}<span class="decision-label"></span>'
                '</div></figure>'
            )
        bullets = ''.join(
            f'<li>{html.escape(item)}</li>'
            for item in row['bullets']
        )
        quarantine_html = ''
        if row.get('quarantined'):
            reasons = []
            for reason in row.get('quarantine_reasons') or []:
                reasons.append(
                    '<li>'
                    f'<strong>{html.escape(str(reason.get("code") or "quarantined"))}</strong>：'
                    f'{html.escape(str(reason.get("message") or ""))}'
                    '</li>'
                )
            quarantine_html = (
                '<section class="quarantine-reasons"><h3>隔离原因</h3>'
                f'<ul>{"".join(reasons)}</ul></section>'
            )
        statuses = {
            str((image.get('assessment') or {}).get('status') or 'unknown')
            for image in row['images']
        }
        if any(
            image.get('source') == 'generated'
            for image in row['images']
        ):
            statuses.add('generated')
        if row.get('quarantined'):
            statuses.add('quarantined')
        search_text = ' '.join([
            str(row['product_id']),
            row['title'],
            row['description'],
            row['keywords'],
        ]).lower()
        product_class = (
            'product quarantined' if row.get('quarantined') else 'product'
        )
        release_badge = (
            '<span class="quarantine-badge">已隔离</span>'
            if row.get('quarantined')
            else '<span class="released-badge">正式表已放行</span>'
        )
        cards.append(
            f'<article class="{product_class}" id="row-{row["row"]}" '
            f'data-search="{html.escape(search_text, quote=True)}" '
            f'data-statuses="{html.escape(" ".join(sorted(statuses)), quote=True)}" '
            f'data-product="{html.escape(str(row["product_id"]), quote=True)}">'
            '<header>'
            f'<span class="row">第 {row["row"]} 行</span>'
            f'<span class="id">商品 ID：{html.escape(row["product_id"])}</span>'
            f'{release_badge}'
            '</header>'
            '<div class="product-actions">'
            '<button data-action="approve_product">确认通过</button>'
            '<button data-action="delete_product">删除整个商品</button>'
            '<button data-action="false_positive">标记误判</button>'
            '<input class="decision-note" placeholder="审核备注（可选）">'
            '<span class="decision-label"></span>'
            '</div>'
            f'<h2>{html.escape(row["title"])}</h2>'
            f'{quarantine_html}'
            '<section><h3>产品描述</h3>'
            f'<p class="description">{html.escape(row["description"])}</p>'
            '</section>'
            f'<section><h3>五点描述</h3><ol>{bullets}</ol></section>'
            '<section><h3>关键词</h3>'
            f'<p>{html.escape(row["keywords"])}</p></section>'
            '<section><h3>全部图片</h3>'
            f'<div class="images">{"".join(image_cards)}</div></section>'
            '</article>'
        )
    run_id = str(summary.get('run_id') or 'manual')
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amazon 中文文案与图片检查表</title>
<style>
body{{margin:0;background:#f4f6f8;color:#17202a;font:15px/1.65 Arial,"Microsoft YaHei",sans-serif}}
.toolbar{{position:sticky;top:0;z-index:5;padding:12px 22px;background:#17202a;color:white;box-shadow:0 2px 10px #0003;display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
.toolbar h1{{margin:0 12px 0 0;font-size:20px}}
.toolbar input,.toolbar select,.toolbar button{{padding:9px 12px;border:0;border-radius:6px}}
.toolbar input{{width:min(380px,48vw)}}
.toolbar button{{background:#33a06f;color:white;font-weight:700;cursor:pointer}}
.summary{{max-width:1500px;margin:18px auto;padding:0 20px;color:#445}}
.product{{max-width:1460px;margin:18px auto;padding:22px;background:white;border-radius:12px;box-shadow:0 2px 12px #2232}}
.product.quarantined{{border:3px solid #c0392b;background:#fff9f8}}
.product header{{display:flex;gap:18px;color:#667;font-size:13px}}
.quarantine-badge,.released-badge{{padding:2px 8px;border-radius:10px;font-weight:700}}
.quarantine-badge{{background:#c0392b;color:white}} .released-badge{{background:#dff4e8;color:#17613d}}
.product-actions,.image-actions{{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:10px 0}}
.product-actions button,.image-actions button{{border:1px solid #ccd5dc;border-radius:6px;padding:6px 9px;background:#f7f9fa;cursor:pointer}}
.product-actions button.selected,.image-actions button.selected{{background:#0f6b4b;color:white;border-color:#0f6b4b}}
.decision-note{{min-width:250px;padding:6px 9px;border:1px solid #ccd5dc;border-radius:6px}}
.decision-label{{color:#0f6b4b;font-weight:700}}
h2{{margin:10px 0 16px;font-size:21px}} h3{{margin:14px 0 6px;font-size:15px;color:#425}}
.description{{white-space:pre-wrap}} ol{{margin-top:5px}}
.images{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}}
.image{{margin:0;border:1px solid #dde3e8;border-radius:9px;overflow:hidden;background:#fafafa}}
.image.status-risk{{border-color:#d84b3e;box-shadow:0 0 0 2px #d84b3e22}}
.image.status-unknown{{border-color:#d68910;box-shadow:0 0 0 2px #d6891022}}
.visuals{{display:flex;gap:2px;background:#e8ecef}} .visuals>div{{flex:1;position:relative}}
.before span{{position:absolute;z-index:1;background:#111b;color:#fff;padding:2px 5px;font-size:11px}}
.image img{{display:block;width:100%;height:190px;object-fit:contain;background:white}}
.image figcaption{{padding:7px 10px;display:grid;gap:3px}}
.image figcaption small{{font-weight:400;color:#566}}
.badge{{justify-self:start;padding:2px 7px;border-radius:9px;font-size:11px}}
.badge.safe{{background:#dff4e8;color:#17613d}} .badge.risk{{background:#fee2df;color:#a22}} .badge.unknown{{background:#fff0c9;color:#7a5200}}
.source{{font-size:12px;color:#667}}
.image-actions{{padding:0 9px 9px}}
.missing{{min-height:120px;display:grid;place-items:center;padding:12px}}
.quarantine-reasons{{background:#fdecea;border-radius:8px;padding:8px 14px}}
</style>
</head>
<body>
<div class="toolbar"><h1>Amazon 中文文案与图片检查表</h1>
<input id="search" placeholder="搜索标题、商品 ID、描述、关键词">
<select id="risk-filter">
<option value="all">全部商品</option><option value="risk">含风险图</option>
<option value="unknown">含未知图</option><option value="quarantined">已隔离</option>
<option value="generated">含生成图</option><option value="undecided">未审核</option>
</select>
<button id="export-decisions">导出审核决定.json</button>
<button id="clear-decisions">清空本机决定</button></div>
<div class="summary">商品 {summary["products"]} 个；图片引用 {summary["image_occurrences"]} 个；
本地图片 {summary["downloaded_unique_images"]}/{summary["unique_images"]} 个；
隔离商品 {summary.get("quarantined_products", 0)} 个。审核决定只保存在当前浏览器，
请导出 JSON 后再由程序应用。</div>
<main>{''.join(cards)}</main>
<script>
const storageKey={json.dumps("amazon-review-decisions:" + run_id, ensure_ascii=False)};
let decisions=JSON.parse(localStorage.getItem(storageKey)||'{{}}');
function decisionKey(container){{
 if(container.classList.contains('product-actions')) return container.closest('.product').dataset.product+'|product';
 return container.closest('.image').dataset.decisionKey;
}}
function refreshDecisions(){{
 document.querySelectorAll('.product-actions,.image-actions').forEach(container=>{{
   const current=decisions[decisionKey(container)];
   container.querySelectorAll('button[data-action]').forEach(button=>button.classList.toggle('selected',current&&current.action===button.dataset.action));
   const label=container.querySelector('.decision-label');
   if(label) label.textContent=current?'已记录：'+current.action:'';
 }});
 applyFilters();
}}
document.querySelectorAll('.product-actions,.image-actions').forEach(container=>{{
 container.querySelectorAll('button[data-action]').forEach(button=>button.addEventListener('click',()=>{{
   const product=container.closest('.product').dataset.product;
   const key=decisionKey(container);
   const note=container.closest('.product').querySelector('.decision-note')?.value||'';
   decisions[key]={{
     product_id:product,action:button.dataset.action,
     image_url:container.dataset.url||'',role:container.dataset.role||'product',
     note:note,recorded_at:new Date().toISOString()
   }};
   localStorage.setItem(storageKey,JSON.stringify(decisions));
   refreshDecisions();
 }}));
}});
function applyFilters(){{
 const q=document.getElementById('search').value.trim().toLowerCase();
 const filter=document.getElementById('risk-filter').value;
 document.querySelectorAll('.product').forEach(card=>{{
   let match=!q||card.dataset.search.includes(q);
   if(filter==='risk') match=match&&card.dataset.statuses.includes('risk');
   if(filter==='unknown') match=match&&card.dataset.statuses.includes('unknown');
   if(filter==='quarantined') match=match&&card.dataset.statuses.includes('quarantined');
   if(filter==='generated') match=match&&card.dataset.statuses.includes('generated');
   if(filter==='undecided') match=match&&!decisions[card.dataset.product+'|product'];
   card.hidden=!match;
 }});
}}
document.getElementById('search').addEventListener('input',applyFilters);
document.getElementById('risk-filter').addEventListener('change',applyFilters);
document.getElementById('export-decisions').addEventListener('click',()=>{{
 const payload={{
   version:1,run_id:{json.dumps(run_id, ensure_ascii=False)},
   source:{json.dumps(summary.get("source", ""), ensure_ascii=False)},
   exported_at:new Date().toISOString(),
   decisions:Object.values(decisions)
 }};
 const blob=new Blob([JSON.stringify(payload,null,2)],{{type:'application/json'}});
 const link=document.createElement('a');
 link.href=URL.createObjectURL(blob);link.download='审核决定.json';link.click();
 URL.revokeObjectURL(link.href);
}});
document.getElementById('clear-decisions').addEventListener('click',()=>{{
 if(confirm('清空此终审包在当前浏览器中的全部审核决定？')){{
   decisions={{}};localStorage.removeItem(storageKey);refreshDecisions();
 }}
}});
refreshDecisions();
</script>
</body>
</html>'''



__all__ = ["render_html"]
