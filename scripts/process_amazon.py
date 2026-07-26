#!/usr/bin/env python3
"""Amazon 采集表 → 回填表 管道（7 阶段）。复用 services/ 全栈。

用法: uv run python scripts/process_amazon.py "亚马逊表/跨境电商自动化采集表.xlsx"
"""
import sys, os, json, re, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openpyxl
from concurrent.futures import ThreadPoolExecutor, as_completed
from adapters import detect_adapter
from pipeline_log import log as _log, new_request_id, PipelineMetrics
from services import TranslationService, ImageReviewService, ImageGenService
import requests as _requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load_key():
    kf = os.environ.get('CROSSPILOT_KEYS_PATH') or os.path.join(_ROOT, 'keys.json')
    try:
        with open(kf, encoding='utf-8') as f:
            return json.load(f).get('dmx_key', '')
    except Exception:
        return ''

# Service instance
_http = _requests.Session()
_http.headers.update({
    'Authorization': f'Bearer {_load_key()}',
    'Content-Type': 'application/json'
})
_translate_svc = TranslationService(_http)
_review_svc = ImageReviewService(_http)
_gen_svc = ImageGenService(_http)

# Brand patterns for cleaning
BRANDS = ['bmw','porsche','toyota','honda','mercedes','audi','vw','ford','hyundai',
          'nissan','kia','mazda','lexus','benz','xiaomi','redmi',
          '宝马','保时捷','小米','红米','丰田','本田','奔驰','奥迪','大众','福特','现代','日产','起亚','马自达','雷克萨斯']
_BRAND_RE = re.compile('|'.join(re.escape(b) for b in BRANDS), re.IGNORECASE)
_OEM_RE = re.compile(r'\b(OEM|Original|Factory|原厂|原装|正品|Genuine)\b', re.IGNORECASE)
_LOGISTICS_RE = re.compile(r'(交货时间|发货时间|运输方式|快递|物流|Shipping|Delivery|Express|Freight|Carrier)[：:].*?(?=\n|$)', re.IGNORECASE)
_RETURN_RE = re.compile(r'(退货|退款|Return|Refund|Warranty|保修|Payment|支付).*?(?=\n|$)', re.IGNORECASE)
_IMG_RE = re.compile(r'<img[^>]*>', re.IGNORECASE)

# Prompt templates
TITLE_OPTIMIZE_PROMPT = (
    "Optimize the following product title for Amazon listing.\n"
    "Rules:\n"
    "- MAX 75 characters (including spaces). If over, trim colors > decorative words > dimensions, keep product name intact.\n"
    "- Add 'For' or 'Compatible with' BEFORE any brand/vehicle names (e.g. 'For Ford F-150').\n"
    "- Keep model numbers, years, specs unchanged.\n"
    "- Output optimized title only, no explanation.\n\n"
    "Title: {}"
)

DESC_CLEAN_PROMPT = (
    "Clean the following product description for Amazon listing.\n"
    "Rules:\n"
    "- Remove ALL brand names (BMW, Toyota, Honda, Ford, etc.), OEM, Original, Factory references.\n"
    "- Remove ALL shipping/delivery info, return policy, payment info, FAQ, store info.\n"
    "- Keep ONLY: product features, specifications, materials, dimensions, compatibility, usage.\n"
    "- Keep HTML formatting (<p>, <ul>, <li>, <br>) intact.\n"
    "- Replace <img ...> with __IMG__ placeholder.\n"
    "- Output cleaned description only, no explanation.\n\n"
    "Description:\n{}"
)

BULLET_KEYWORD_PROMPT = (
    "Based on this Amazon product title and description, generate:\n\n"
    "1. BULLET POINTS (5 items):\n"
    "   - Each ~200 characters, English\n"
    "   - Cover: material, dimensions, features, compatibility, package contents\n"
    "   - No numbering, just the content\n"
    "   - Natural language for Amazon buyers\n\n"
    "2. SEARCH KEYWORDS (10 items):\n"
    "   - 10 relevant search terms\n"
    "   - NO brand names allowed\n"
    "   - Comma-separated\n\n"
    "Return JSON: {{\"bullets\": [\"point1\",\"point2\",\"point3\",\"point4\",\"point5\"], \"keywords\": \"kw1,kw2,...,kw10\"}}\n\n"
    "Title: {title}\n"
    "Description: {desc}"
)


# === Pipeline ===

def _stage_read(ws, adapter):
    """读采集表数据到内存，处理多 URL 图片列（换行分隔）。"""
    total = ws.max_row - 1
    print(f"读取 {total} 行...", flush=True)
    data = []
    for r in range(2, ws.max_row + 1):
        img_raw = str(ws.cell(r, adapter.cols['main_image']).value or '').strip()
        var_raw = str(ws.cell(r, adapter.cols['variant']).value or '').strip()
        # 多 URL 用换行分隔 → 拆成列表
        img_urls = [u.strip() for u in img_raw.replace('\r', '').split('\n') if u.strip().startswith('http')]
        var_urls = [u.strip() for u in var_raw.replace('\r', '').split('\n') if u.strip().startswith('http')]
        all_urls = img_urls + var_urls
        # 主图=第一张，其余全部注入描述作为附图
        main_img = all_urls[0] if all_urls else ''
        extra_imgs = all_urls[1:] if len(all_urls) > 1 else []
        data.append({
            'id': str(ws.cell(r, 1).value or '').strip(),
            'title': str(ws.cell(r, adapter.cols['title']).value or '').strip(),
            'desc': str(ws.cell(r, adapter.cols['desc']).value or ''),
            'main_img': main_img,
            'var_img': extra_imgs[0] if extra_imgs else '',  # 变种图=第二张
            'extra_imgs': extra_imgs,
        })
    return data


def _stage_optimize_titles(data):
    """标题优化：≤75 字符 + For/Compatible with 前缀。"""
    print(f"标题优化 {len(data)} 条...", flush=True)
    changed = 0
    for i, row in enumerate(data):
        title = row['title']
        if not title:
            continue
        # Rule-based: add "For" prefix if brand detected
        if _BRAND_RE.search(title) and not re.match(r'(For|Compatible with)', title, re.IGNORECASE):
            title = 'For ' + title
        # Truncate to 75 chars (word boundary)
        if len(title) > 75:
            title = title[:75].rsplit(' ', 1)[0]
        if title != row['title']:
            row['title'] = title
            changed += 1
    print(f"  规则优化: {changed} 行", flush=True)

    # API-based refinement for long titles (10 并发)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    to_optimize = [(i, row) for i, row in enumerate(data) if row['title'] and len(row['title']) > 65]
    if to_optimize:
        print(f"  API 优化 {len(to_optimize)} 条（10 并发）...", flush=True)

        def _opt_one(idx, title):
            try:
                result = _translate_svc.dmx_call({
                    "model": _translate_svc.TEXT_MODEL,
                    "messages": [{"role": "user", "content": TITLE_OPTIMIZE_PROMPT.format(title)}],
                    "max_completion_tokens": 128
                }, max_tokens_override=128)
                if result and len(result.strip()) <= 80:
                    return idx, result.strip()
            except Exception as e:
                _log.warn("标题优化 API 异常", error=str(e))
            return idx, None

        done = 0
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_opt_one, i, row['title']): i for i, row in to_optimize}
            for future in as_completed(futures):
                idx, new_title = future.result()
                if new_title:
                    data[idx]['title'] = new_title
                done += 1
                if done % 10 == 0:
                    print(f"    API标题: {done}/{len(to_optimize)}", flush=True)
    return data


def _stage_clean_descs(data):
    """描述清洗：去品牌/OEM/物流/FAQ。"""
    print(f"描述清洗 {len(data)} 条...", flush=True)
    changed = 0
    for row in data:
        desc = row['desc']
        if not desc:
            continue
        original = desc
        desc = _BRAND_RE.sub('', desc)
        desc = _OEM_RE.sub('', desc)
        desc = _LOGISTICS_RE.sub('', desc)
        desc = _RETURN_RE.sub('', desc)
        desc = _IMG_RE.sub('__IMG__', desc)
        # Clean up extra whitespace
        desc = re.sub(r'\n{3,}', '\n\n', desc)
        desc = desc.strip()
        if desc != original:
            row['desc'] = desc
            changed += 1
    print(f"  规则清洗: {changed} 行", flush=True)
    return data


def _stage_generate_bullets_keywords(data):
    """API 生成 Bullet Point 1-5 + 10 关键词（20 并发）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    items = [(i, row) for i, row in enumerate(data) if row['title']]
    print(f"生成 Bullet Point + 关键词 {len(items)} 条（20 并发）...", flush=True)

    def _gen_one(idx, row):
        try:
            raw = _translate_svc.dmx_call({
                "model": _translate_svc.TEXT_MODEL,
                "messages": [{"role": "user", "content": BULLET_KEYWORD_PROMPT.format(
                    title=row['title'], desc=row.get('desc', '')[:500]
                )}],
                "max_completion_tokens": 2048
            }, max_tokens_override=2048)
            if raw:
                parsed = _parse_bullet_json(raw)
                if parsed:
                    return idx, parsed.get('bullets', [''] * 5)[:5], parsed.get('keywords', '')
        except Exception as e:
            _log.warn("Bullet/关键词生成异常", row=idx, error=str(e))
        return idx, [''] * 5, ''

    done = 0
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_gen_one, i, row): i for i, row in items}
        for future in as_completed(futures):
            idx, bullets, keywords = future.result()
            data[idx]['bullets'] = bullets
            data[idx]['keywords'] = keywords
            done += 1
            if done % 20 == 0:
                print(f"  进度: {done}/{len(items)}", flush=True)

    # Fill defaults + retry empty bullets
    empty_rows = [(i, row) for i, row in enumerate(data)
                  if row['title'] and (not row.get('bullets') or not any(row['bullets']))]
    if empty_rows:
        print(f"  重试 {len(empty_rows)} 行空 Bullet（10 并发）...", flush=True)
        retry_done = 0
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_gen_one, i, row): i for i, row in empty_rows}
            for future in as_completed(futures):
                idx, bullets, keywords = future.result()
                if any(bullets):
                    data[idx]['bullets'] = bullets
                    data[idx]['keywords'] = keywords
                else:
                    # Rule-based fallback: extract sentences from description
                    desc = data[idx].get('desc', '')
                    sentences = [s.strip() for s in re.split(r'[.!?]\s*', desc) if len(s.strip()) > 20][:5]
                    if sentences:
                        data[idx]['bullets'] = (sentences + [''] * 5)[:5]
                        data[idx]['keywords'] = ', '.join(
                            re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?', desc)[:10])
                retry_done += 1
                if retry_done % 10 == 0:
                    print(f"    重试: {retry_done}/{len(empty_rows)}", flush=True)

    for row in data:
        if 'bullets' not in row:
            row['bullets'] = [''] * 5
        if 'keywords' not in row:
            row['keywords'] = ''
    return data


def _parse_bullet_json(raw):
    """解析 Bullet + Keyword JSON 响应。"""
    if not raw:
        return None
    try:
        items = json.loads(raw)
        if isinstance(items, dict) and 'bullets' in items:
            return items
    except json.JSONDecodeError:
        pass
    # Try markdown code block
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def _stage_review_images(data, cache_path=None):
    """图审：检查所有图片 URL 是否有水印/Logo/品牌名。50 并发，缓存断点续跑。"""
    # 收集所有唯一图片 URL
    all_urls = set()
    for row in data:
        if row['main_img']:
            all_urls.add(row['main_img'])
        for u in row.get('extra_imgs', []):
            all_urls.add(u)

    if not all_urls:
        print("无水印图需审", flush=True)
        return data, {}

    # Load cache
    review_results = {}
    if cache_path:
        try:
            with open(cache_path, encoding='utf-8') as f:
                review_results = json.load(f).get('review_results', {})
            print(f"图审缓存命中: {len(review_results)}/{len(all_urls)}", flush=True)
        except Exception as e:
            _log.warn("缓存读取失败，重新审图", error=str(e))

    to_review = [u for u in all_urls if u not in review_results]
    if not to_review:
        print("图审: 全部缓存命中", flush=True)
        return data, review_results

    print(f"图审 {len(to_review)} 张（50 并发）...", flush=True)
    reviewed = 0
    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(_review_svc.review_once, url): url for url in to_review}
        for future in as_completed(futures):
            url = futures[future]
            review_results[url] = future.result()  # True/False/None
            reviewed += 1
            if reviewed % 100 == 0:
                print(f"  图审: {reviewed}/{len(to_review)}", flush=True)

    watermarked = [u for u, r in review_results.items() if r is True]
    failed = [u for u, r in review_results.items() if r is None]
    for u in failed:
        review_results[u] = False  # 安全默认
    print(f"图审完成: {len(watermarked)} 水印, {len(failed)} 失败, {len(all_urls)} 总计", flush=True)
    # Save cache
    if cache_path:
        try:
            cache = {}
            if os.path.exists(cache_path):
                with open(cache_path, encoding='utf-8') as f:
                    cache = json.load(f)
            cache['review_results'] = {u: r for u, r in review_results.items() if r is not None}
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception as e:
            _log.warn("缓存保存失败", error=str(e))
    return data, review_results


def _stage_gen_images(data, review_results):
    """图生图：水印图重新生成去水印版本。并发 20。"""
    to_gen = [u for u, r in review_results.items() if r is True]
    if not to_gen:
        print("无水印图需生成", flush=True)
        return data, {}

    print(f"图生图 {len(to_gen)} 张（20 并发）...", flush=True)
    gen_results = {}
    gen_done = 0
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_gen_svc.generate, url): url for url in to_gen}
        for future in as_completed(futures):
            url = futures[future]
            new_url = future.result()
            if new_url:
                gen_results[url] = new_url
            gen_done += 1
            if gen_done % 20 == 0:
                print(f"  图生图: {gen_done}/{len(to_gen)}", flush=True)

    # 替换 data 中的水印图片 URL
    replaced = 0
    for row in data:
        if row['main_img'] in gen_results:
            row['main_img'] = gen_results[row['main_img']]
            replaced += 1
        row['extra_imgs'] = [gen_results.get(u, u) for u in row.get('extra_imgs', [])]
        row['var_img'] = gen_results.get(row['var_img'], row['var_img'])

    print(f"图生图完成: {len(gen_results)}/{len(to_gen)} 成功, {replaced} 主图替换", flush=True)
    return data, gen_results


def _stage_write_output(data, input_path):
    """写入回填表（24 列格式，对齐模板）。"""
    output = os.path.splitext(input_path)[0] + '_回填.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Sheet1'

    # Row 1: Headers (exact match to template)
    headers = [
        '产品标题', '产品描述', '产品图片(本地地址)', '变体图片(本地地址)',
        '制造商', 'Model Number(型号)', 'Model Name(型号名称)',
        'Item Package Length(包装长度)', 'Package Length Unit(包装长度单位)',
        'Item Package Width(包装宽度)', 'Package Width Unit(包装宽度单位)',
        'Item Package Height(包装高度)', 'Package Height Unit(包装高度单位)',
        'Package Weight(包装重量)', 'Package Weight Unit(包装重量单位)',
        'MPN', '促销价 (USD)', 'Bullet Point1', 'Bullet Point2',
        'Bullet Point3', 'Bullet Point4', 'Bullet Point5',
        '关键词信息', 'UPC豁免:'
    ]
    for c, h in enumerate(headers, 1):
        ws.cell(1, c).value = h

    # Row 2: Default values (template reference row)
    defaults = [
        '同事提供', '同事提供', '同事提供', '同事提供',
        'Generic', '随机生成数值', '随机生成',
        '0.1', 'Inches(英寸)', '0.1', 'Inches(英寸)',
        '0.1', 'Inches(英寸)', '0.1', 'Kilograms(公斤）',
        '保存与SKU一致', '数值清空', '同事提供', '同事提供',
        '同事提供', '同事提供', '同事提供',
        '同事提供', '是'
    ]
    for c, v in enumerate(defaults, 1):
        ws.cell(2, c).value = v

    # Data rows start from row 3
    for i, row in enumerate(data):
        r = i + 3
        ws.cell(r, 1).value = row['title']
        ws.cell(r, 2).value = row['desc']
        # 主图 → Col3, 所有附图 → Col4（换行分隔）
        ws.cell(r, 3).value = row['main_img']
        extra = row.get('extra_imgs', [])
        ws.cell(r, 4).value = '\n'.join(extra) if extra else ''
        # Cols 5-17: left as template defaults (manufacturer, package, MPN, etc.)
        for bi in range(5):
            ws.cell(r, 18 + bi).value = row.get('bullets', [''] * 5)[bi] if bi < len(row.get('bullets', [])) else ''
        ws.cell(r, 23).value = row.get('keywords', '')

    # Column widths (all 24 columns)
    widths = {1: 50, 2: 80, 3: 60, 4: 60, 5: 12, 6: 18, 7: 22, 8: 21, 9: 25,
              10: 22, 11: 27, 12: 25, 13: 23, 14: 23, 15: 30, 16: 18, 17: 13,
              18: 60, 19: 60, 20: 60, 21: 60, 22: 60, 23: 50, 24: 12}
    wrap_cols = {1, 2, 3, 4, 18, 19, 20, 21, 22, 23}  # text-heavy columns
    for col, w in widths.items():
        letter = openpyxl.utils.get_column_letter(col)
        ws.column_dimensions[letter].width = w
    # Text wrap for content columns
    from openpyxl.styles import Alignment
    wrap_align = Alignment(wrap_text=True, vertical='top')
    for r in range(1, ws.max_row + 1):
        for c in wrap_cols:
            ws.cell(r, c).alignment = wrap_align
    # Header row bold
    from openpyxl.styles import Font
    for c in range(1, 25):
        ws.cell(1, c).font = Font(bold=True)

    wb.save(output)
    print(f"完成! 保存: {output}", flush=True)
    return output


def main():
    if len(sys.argv) < 2:
        print("用法: uv run python scripts/process_amazon.py \"<采集表.xlsx>\"")
        sys.exit(1)

    tp = sys.argv[1]
    rid = new_request_id()
    _log.info("Amazon管道启动", request_id=rid, file=os.path.basename(tp))
    print(f"=== Amazon 采集表 → 回填表 === [rid={rid}]")
    print(f"输入: {tp}")

    if not _load_key():
        print("错误: 缺少 DMXAPI key，请在 keys.json 配置 dmx_key")
        sys.exit(1)

    wb = openpyxl.load_workbook(tp, data_only=True)
    ws = wb.active
    adapter = detect_adapter(ws)
    if not adapter or 'Amazon' not in adapter.name:
        print("错误: 无法识别为 Amazon 采集表格式")
        wb.close()
        sys.exit(1)

    print(f"表格格式: {adapter.name} | {ws.max_row - 1} 行")

    # Pipeline stages with error isolation + metrics
    metrics = PipelineMetrics()

    def _run(name, fn, *args):
        t0 = time.time()
        try:
            result = fn(*args)
            metrics.record_stage(name, time.time() - t0, 1)
            return result
        except Exception as e:
            _log.error(f"Amazon阶段 [{name}] 失败", error=str(e), exc_info=True)
            raise

    data = _run('读取', _stage_read, ws, adapter)
    wb.close()
    cache_path = os.path.splitext(tp)[0] + '_amz_cache.json'
    data, review_results = _run('图审', _stage_review_images, data, cache_path)
    data, gen_results = _run('图生图', _stage_gen_images, data, review_results)
    data = _run('标题优化', _stage_optimize_titles, data)
    data = _run('描述清洗', _stage_clean_descs, data)
    data = _run('Bullet+关键词', _stage_generate_bullets_keywords, data)
    output = _run('写回填表', _stage_write_output, data, tp)

    # Write metrics
    try:
        metrics_path = os.path.splitext(output)[0] + '_metrics.json'
        with open(metrics_path, 'w', encoding='utf-8') as f:
            json.dump(metrics.to_dict(), f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    print(f"输出: {output}")


if __name__ == '__main__':
    main()
