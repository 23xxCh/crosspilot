"""Render the offline interactive final-review document."""
from __future__ import annotations

import html
import json


def _script_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def render_html(
    rows: list[dict],
    summary: dict,
    *,
    formal_payload: dict | None = None,
) -> str:
    metrics_text = html.escape(
        json.dumps(
            summary.get("run_metrics") or {},
            ensure_ascii=False,
            indent=2,
        )
    )
    formal_payload = formal_payload if isinstance(formal_payload, dict) else None
    formal_count = (
        len(formal_payload.get("商品id") or [])
        if formal_payload else 0
    )
    row_fields = (
        [
            field
            for field, values in formal_payload.items()
            if field != "有问题的产品id"
            and isinstance(values, list)
            and len(values) == formal_count
        ]
        if formal_payload else []
    )
    refill_button = (
        '<button id="export-refill">一键应用并导出回填表</button>'
        if formal_payload else ''
    )
    cards = []
    for row in rows:
        image_cards = []
        for image in row['images']:
            role = html.escape(image['role'])
            role_key = str(image.get('role_key') or 'attachment')
            assessment = image.get('assessment') or {}
            text_assessment = image.get('text_assessment') or {}
            general_status = str(assessment.get('status') or 'unknown')
            status = general_status
            action = str(
                image.get('image_action')
                or image.get('source_image_action')
                or ''
            )
            reasons = (
                assessment.get('risk_categories')
                or assessment.get('reasons')
                or []
            )
            reason_text = '、'.join(str(item) for item in reasons) or '无'
            evidence = html.escape(
                str(image.get('evidence') or '暂无证据说明')
            )
            detected_text = (
                image.get('detected_text')
                or text_assessment.get('detected_text')
                or []
            )
            detected_text_label = (
                '、'.join(str(item) for item in detected_text)
                or '无'
            )
            text_review = (
                f'<small>处理动作：{html.escape(action or "keep")}</small>'
                f'<small>检出文字：{html.escape(detected_text_label)}</small>'
            )
            if image.get('accepted_without_machine_review'):
                text_review += (
                    '<small class="human-review-note">'
                    '生成图未机器复审：请人工确认</small>'
                )
            source_detected = image.get('source_detected_text') or []
            if source_detected:
                text_review += (
                    f'<small>编辑前文字：{html.escape("、".join(str(item) for item in source_detected))}</small>'
                    f'<small>生成线路序号：{html.escape(str(image.get("generation_route_offset", 0)))}</small>'
                    f'<small>候选复审数：{html.escape(str(image.get("candidates_reviewed", 0)))}</small>'
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
                        f'<img loading="lazy" draggable="false" src="{html.escape(source_local)}" '
                        f'alt="{role} 生成前"></div>'
                    )
            if image['download_ok']:
                src = html.escape(image['local_path'].replace('\\', '/'))
                visual = (
                    source_compare
                    + '<div class="after">'
                    f'<img loading="lazy" draggable="false" src="{src}" alt="{role}">'
                    '</div>'
                )
            else:
                remote = html.escape(image['url'])
                visual = (
                    '<div class="missing">'
                    f'<a href="{remote}">图片下载失败，打开原链接</a>'
                    '</div>'
                )
            action_buttons = (
                '<button data-action="regenerate_image">'
                '重新生成主图/变种图</button>'
                '<button data-action="delete_image">删除单张附图</button>'
                '<button data-action="false_positive">标记误判</button>'
            )
            sortable = (
                role_key in {'main', 'attachment'}
                and not row.get('quarantined')
            )
            sortable_attrs = (
                ' data-sortable="product" draggable="true"'
                if sortable else ''
            )
            drag_handle = (
                '<div class="drag-handle" '
                'title="拖动调整主图和附图顺序" '
                'aria-label="拖动调整主图和附图顺序">'
                '<span aria-hidden="true">⠿</span> 拖动排序</div>'
                if sortable else ''
            )
            image_cards.append(
                f'<figure class="image status-{html.escape(status)}" '
                f'data-status="{html.escape(status)}" '
                f'data-role="{html.escape(role_key)}" '
                f'data-image-url="{html.escape(str(image["url"]), quote=True)}"'
                f'{sortable_attrs} '
                f'data-decision-key="{decision_key}">'
                f'{drag_handle}'
                f'<div class="visuals">{visual}</div>'
                '<figcaption>'
                f'<strong>{role}</strong>'
                f'<span class="badge {html.escape(status)}">'
                f'{html.escape(status.upper())}</span>'
                f'<span class="source">{source_label}</span>'
                f'<small>类别：{html.escape(reason_text)}</small>'
                f'<small>位置：{html.escape(str(assessment.get("placement") or "unknown"))}</small>'
                f'<small>证据：{evidence}</small>'
                f'{text_review}'
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
        statuses.update(
            str(image.get('image_action') or '')
            for image in row['images']
            if image.get('image_action')
        )
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
            row.get('subtitle', ''),
            row['description'],
            row['keywords'],
        ]).lower()
        subtitle_html = (
            '<section><h3>商品亮点</h3>'
            f'<p>{html.escape(str(row.get("subtitle") or ""))}</p></section>'
            if row.get('subtitle')
            else ''
        )
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
            f'{subtitle_html}'
            f'{quarantine_html}'
            '<section><h3>产品描述</h3>'
            f'<p class="description">{html.escape(row["description"])}</p>'
            '</section>'
            f'<section><h3>五点描述</h3><ol>{bullets}</ol></section>'
            '<section><h3>关键词</h3>'
            f'<p>{html.escape(row["keywords"])}</p></section>'
            '<section><h3>全部图片 '
            '<small class="image-order-help">拖动主图/附图可换位；第 1 张自动成为主图</small>'
            '</h3>'
            '<span class="image-order-label"></span>'
            f'<div class="images">{"".join(image_cards)}</div></section>'
            '</article>'
        )
    run_id = str(summary.get('run_id') or 'manual')
    certification_warning = (
        '<div class="certification-warning">'
        '⚠ 这是迁移保留的旧正式表，只读复审发现风险或未知图片；'
        '它尚未通过当前安全策略认证。请按风险筛选集中复核，'
        '新处理任务会在写入正式表前自动删除、修复或隔离。</div>'
        if summary.get('formal_safety_certified') is False
        else ''
    )
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
.certification-warning{{max-width:1460px;margin:18px auto;padding:14px 18px;border:2px solid #c0392b;border-radius:9px;background:#fff0ee;color:#8e241b;font-weight:700}}
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
.image[data-sortable="product"]{{position:relative;cursor:grab;will-change:opacity}}
.image[data-sortable="product"] .visuals{{cursor:grab;user-select:none}}
.image.dragging{{opacity:.38;outline:3px solid #167a57;cursor:grabbing}}
.drag-handle{{padding:7px 10px;background:#eef7f3;color:#17613d;font-weight:700;cursor:grab;user-select:none;border-bottom:1px solid #d6e7df}}
.drag-handle:active{{cursor:grabbing}}
.image-order-help{{font-weight:400;color:#68757e;margin-left:8px}}
.image-order-label{{display:block;min-height:22px;color:#0f6b4b;font-weight:700}}
.image[data-role="attachment"] .image-actions button[data-action="regenerate_image"]{{display:none}}
.image:not([data-role="attachment"]) .image-actions button[data-action="delete_image"]{{display:none}}
.image.status-risk{{border-color:#d84b3e;box-shadow:0 0 0 2px #d84b3e22}}
.image.status-unknown{{border-color:#d68910;box-shadow:0 0 0 2px #d6891022}}
.visuals{{display:flex;gap:2px;background:#e8ecef}} .visuals>div{{flex:1;position:relative}}
.before span{{position:absolute;z-index:1;background:#111b;color:#fff;padding:2px 5px;font-size:11px}}
.image img{{display:block;width:100%;height:190px;object-fit:contain;background:white;-webkit-user-drag:none;user-select:none}}
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
{refill_button}
<button id="clear-decisions">清空本机决定</button></div>
<div class="summary">商品 {summary["products"]} 个；图片引用 {summary["image_occurrences"]} 个；
本地图片 {summary["downloaded_unique_images"]}/{summary["unique_images"]} 个；
隔离商品 {summary.get("quarantined_products", 0)} 个。审核决定只保存在当前浏览器，
请导出 JSON 后再由程序应用。
<details><summary>运行稳定性与回退摘要</summary><pre>{metrics_text}</pre></details></div>
{certification_warning}
<main>{''.join(cards)}</main>
<script>
const storageKey={json.dumps("amazon-review-decisions:" + run_id, ensure_ascii=False)};
let decisions=JSON.parse(localStorage.getItem(storageKey)||'{{}}');
const embeddedFormalPayload={_script_json(formal_payload)};
const embeddedRowFields={_script_json(row_fields)};
function persistDecisions(){{
 localStorage.setItem(storageKey,JSON.stringify(decisions));
}}
function decisionKey(container){{
 if(container.classList.contains('product-actions')) return container.closest('.product').dataset.product+'|product';
 return container.closest('.image').dataset.decisionKey;
}}
function productImageCards(productCard){{
 return Array.from(productCard.querySelectorAll('.image[data-sortable="product"]'));
}}
function updateProductImageRoles(productCard){{
 const product=productCard.dataset.product;
 productImageCards(productCard).forEach((card,index)=>{{
   const oldKey=card.dataset.decisionKey;
   const role=index===0?'main':'attachment';
   const url=card.dataset.imageUrl;
   const newKey=product+'|'+role+'|'+url;
   if(oldKey!==newKey&&decisions[oldKey]){{
     const current=decisions[oldKey];
     delete decisions[oldKey];
     const compatible=current.action==='false_positive'||
       (role==='main'&&current.action==='regenerate_image')||
       (role==='attachment'&&current.action==='delete_image');
     if(compatible){{current.role=role;decisions[newKey]=current;}}
   }}
   card.dataset.role=role;
   card.dataset.decisionKey=newKey;
   const actions=card.querySelector('.image-actions');
   if(actions) actions.dataset.role=role;
   const title=card.querySelector('figcaption strong');
   if(title) title.textContent=index===0?'主图':'附图 '+index;
   const image=card.querySelector('.after img');
   if(image) image.alt=index===0?'主图':'附图 '+index;
 }});
}}
function recordImageOrder(productCard){{
 updateProductImageRoles(productCard);
 const product=productCard.dataset.product;
 const order=productImageCards(productCard).map(card=>card.dataset.imageUrl);
 decisions[product+'|product_images']={{
   product_id:product,action:'reorder_images',role:'product_images',
   image_url:'',image_urls:order,
   note:productCard.querySelector('.decision-note')?.value||'',
   recorded_at:new Date().toISOString()
 }};
 persistDecisions();
 refreshDecisions();
}}
function restoreImageOrders(){{
 document.querySelectorAll('.product').forEach(productCard=>{{
   const decision=decisions[productCard.dataset.product+'|product_images'];
   if(!decision||!Array.isArray(decision.image_urls)){{
     updateProductImageRoles(productCard);return;
   }}
   const container=productCard.querySelector('.images');
   const boundary=container?.querySelector('.image[data-role="variant"]')||null;
   const available=productImageCards(productCard);
   const used=new Set();
   decision.image_urls.forEach(url=>{{
     const card=available.find(item=>!used.has(item)&&item.dataset.imageUrl===url);
     if(card){{used.add(card);container.insertBefore(card,boundary);}}
   }});
   available.filter(card=>!used.has(card)).forEach(card=>container.insertBefore(card,boundary));
   updateProductImageRoles(productCard);
 }});
 persistDecisions();
}}
function refreshDecisions(){{
 document.querySelectorAll('.product-actions,.image-actions').forEach(container=>{{
   const current=decisions[decisionKey(container)];
   container.querySelectorAll('button[data-action]').forEach(button=>button.classList.toggle('selected',current&&current.action===button.dataset.action));
   const label=container.querySelector('.decision-label');
   if(label) label.textContent=current?'已记录：'+current.action:'';
 }});
 document.querySelectorAll('.product').forEach(card=>{{
   const label=card.querySelector('.image-order-label');
   const current=decisions[card.dataset.product+'|product_images'];
   if(label) label.textContent=current?'已记录图片新顺序；导出并应用后生效':'';
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
   persistDecisions();
   refreshDecisions();
 }}));
}});
let draggingCard=null;
document.querySelectorAll('.image[data-sortable="product"]').forEach(card=>{{
 card.addEventListener('dragstart',event=>{{
   if(event.target.closest('figcaption,.image-actions,button,a,input')){{
     event.preventDefault();return;
   }}
   draggingCard=card;
   draggingCard.classList.add('dragging');
   event.dataTransfer.effectAllowed='move';
   event.dataTransfer.setData('text/plain',draggingCard.dataset.imageUrl||'image');
 }});
 card.addEventListener('dragend',()=>{{
   if(!draggingCard) return;
   const productCard=draggingCard.closest('.product');
   draggingCard.classList.remove('dragging');
   draggingCard=null;
   recordImageOrder(productCard);
 }});
}});
document.querySelectorAll('.images').forEach(container=>{{
 container.addEventListener('dragenter',event=>{{
   if(!draggingCard||draggingCard.closest('.images')!==container) return;
   const target=event.target.closest('.image[data-sortable="product"]');
   if(!target||target===draggingCard) return;
   const rect=target.getBoundingClientRect();
   const sameRow=event.clientY>=rect.top&&event.clientY<=rect.bottom;
   const after=sameRow
     ? event.clientX>rect.left+rect.width/2
     : event.clientY>rect.top+rect.height/2;
   container.insertBefore(draggingCard,after?target.nextSibling:target);
 }});
 container.addEventListener('dragover',event=>{{
   if(!draggingCard||draggingCard.closest('.images')!==container) return;
   event.preventDefault();
   event.dataTransfer.dropEffect='move';
 }});
 container.addEventListener('drop',event=>{{
   if(draggingCard&&draggingCard.closest('.images')===container) event.preventDefault();
 }});
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
function downloadJson(filename,value){{
 const blob=new Blob([JSON.stringify(value,null,2)],{{type:'application/json'}});
 const link=document.createElement('a');
 link.href=URL.createObjectURL(blob);link.download=filename;link.click();
 setTimeout(()=>URL.revokeObjectURL(link.href),0);
}}
function sameImageMultiset(left,right){{
 if(!Array.isArray(left)||!Array.isArray(right)||left.length!==right.length) return false;
 const counts=new Map();
 left.forEach(url=>counts.set(String(url),(counts.get(String(url))||0)+1));
 for(const url of right){{
   const key=String(url);const count=counts.get(key)||0;
   if(!count) return false;
   if(count===1) counts.delete(key);else counts.set(key,count-1);
 }}
 return counts.size===0;
}}
function buildReviewedRefill(){{
 if(!embeddedFormalPayload) throw new Error('当前终审包没有内嵌正式回填数据');
 const items=Object.values(decisions);
 const regenerations=items.filter(item=>item.action==='regenerate_image');
 if(regenerations.length){{
   throw new Error('存在 '+regenerations.length+' 条重新生成图片决定。网页不能调用生图 API，请先导出审核决定并使用 02_应用审核决定.bat。');
 }}
 const output=JSON.parse(JSON.stringify(embeddedFormalPayload));
 const deleteIds=new Set(items.filter(item=>item.action==='delete_product').map(item=>String(item.product_id)));
 const rowIndex=productId=>output['商品id'].map(String).indexOf(String(productId));
 items.filter(item=>item.action==='reorder_images').forEach(item=>{{
   if(deleteIds.has(String(item.product_id))) return;
   const index=rowIndex(item.product_id);
   if(index<0) throw new Error('图片排序引用了不存在的商品 ID：'+item.product_id);
   const current=output['产品图片链接'][index];
   if(!sameImageMultiset(current,item.image_urls)){{
     throw new Error('商品 '+item.product_id+' 的图片排序不完整，拒绝导出');
   }}
   output['产品图片链接'][index]=Array.from(item.image_urls);
 }});
 items.filter(item=>item.action==='delete_image').forEach(item=>{{
   if(deleteIds.has(String(item.product_id))) return;
   const index=rowIndex(item.product_id);
   if(index<0) throw new Error('删除图片引用了不存在的商品 ID：'+item.product_id);
   const images=output['产品图片链接'][index];
   const imageIndex=images.indexOf(item.image_url);
   if(imageIndex<=0) throw new Error('商品 '+item.product_id+' 指定图片不存在或当前是主图，拒绝删除');
   images.splice(imageIndex,1);
 }});
 for(let index=output['商品id'].length-1;index>=0;index--){{
   if(!deleteIds.has(String(output['商品id'][index]))) continue;
   embeddedRowFields.forEach(field=>output[field].splice(index,1));
 }}
 output['有问题的产品id']=Array.from(new Set([
   ...(output['有问题的产品id']||[]).map(String),...deleteIds
 ]));
 const count=output['商品id'].length;
 embeddedRowFields.forEach(field=>{{
   if(!Array.isArray(output[field])||output[field].length!==count){{
     throw new Error('回填表字段长度不一致：'+field);
   }}
 }});
 output['产品图片链接'].forEach((images,index)=>{{
   if(!Array.isArray(images)||!images.length){{
     throw new Error('商品 '+output['商品id'][index]+' 没有主图，拒绝导出');
   }}
 }});
 return output;
}}
document.getElementById('export-decisions').addEventListener('click',()=>{{
 const payload={{
   version:1,run_id:{json.dumps(run_id, ensure_ascii=False)},
   source:{json.dumps(summary.get("source", ""), ensure_ascii=False)},
   exported_at:new Date().toISOString(),
   decisions:Object.values(decisions)
 }};
 downloadJson('审核决定.json',payload);
}});
document.getElementById('export-refill')?.addEventListener('click',()=>{{
 try{{
   const output=buildReviewedRefill();
   downloadJson('跨境电商自动化回填表.json',output);
   alert('已导出标准回填表。下载文件已包含当前图片排序和删除决定；项目目录内原文件不会被网页覆盖。');
 }}catch(error){{
   alert('无法一键导出回填表：'+(error?.message||String(error)));
 }}
}});
document.getElementById('clear-decisions').addEventListener('click',()=>{{
 if(confirm('清空此终审包在当前浏览器中的全部审核决定？')){{
   decisions={{}};localStorage.removeItem(storageKey);location.reload();
 }}
}});
restoreImageOrders();
refreshDecisions();
</script>
</body>
</html>'''



__all__ = ["render_html"]
