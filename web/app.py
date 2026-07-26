"""FastAPI app：上传/历史/详情/SSE/下载/设置。复用 process_ebay_tk 管道。"""
from contextlib import asynccontextmanager
import base64, binascii, errno, os, sys, json, uuid, shutil, time, re, secrets, zipfile, glob as _glob
from urllib.parse import quote
from scripts.pipeline_log import log as _log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Query
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from web import store, jobs
from web.review_report import (
    build_quality_score,
    build_review_data,
    build_review_report_csv,
)


@asynccontextmanager
async def _lifespan(_app):
    try:
        retention_days = int(os.environ.get('CROSSPILOT_RETENTION_DAYS', '7'))
    except ValueError:
        retention_days = 7
    if retention_days > 0:
        try:
            cleaned = store.cleanup_old_tasks(retention_days)
            if cleaned:
                print(f"[cleanup] 已清理 {cleaned} 个过期任务", flush=True)
        except Exception as exc:
            _log.warn("过期任务清理失败", error=str(exc))
    jobs.start_monitor()
    try:
        yield
    finally:
        jobs.stop_monitor(cancel_running=True)


app = FastAPI(title="CrossPilot", lifespan=_lifespan)

_ALLOWED_HOSTS = [
    item.strip()
    for item in os.environ.get(
        'CROSSPILOT_ALLOWED_HOSTS',
        '127.0.0.1,localhost,[::1],::1,testserver',
    ).split(',')
    if item.strip()
]
_ALLOWED_ORIGINS = {
    item.strip().rstrip('/')
    for item in os.environ.get('CROSSPILOT_ALLOWED_ORIGINS', '').split(',')
    if item.strip()
}
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

_SECURITY_HEADERS = {
    'Content-Security-Policy': (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; connect-src 'self'; font-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    'X-Frame-Options': 'DENY',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
}


def _secure_response(response):
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response

@app.middleware("http")
async def _optional_basic_auth(request, call_next):
    """设置 CROSSPILOT_AUTH_PASSWORD 后，为整个 Web 应用启用 HTTP Basic。"""
    expected_password = os.environ.get('CROSSPILOT_AUTH_PASSWORD', '')
    if expected_password:
        expected_user = os.environ.get('CROSSPILOT_AUTH_USER', 'crosspilot')
        auth = request.headers.get('Authorization', '')
        valid = False
        if auth.startswith('Basic '):
            try:
                user, password = base64.b64decode(
                    auth[6:], validate=True
                ).decode('utf-8').split(':', 1)
                valid = (
                    secrets.compare_digest(user, expected_user)
                    and secrets.compare_digest(password, expected_password)
                )
            except (binascii.Error, ValueError, UnicodeDecodeError):
                pass
        if not valid:
            return JSONResponse(
                {'detail': 'Authentication required'},
                status_code=401,
                headers={'WWW-Authenticate': 'Basic realm="CrossPilot"'},
            )
    return await call_next(request)

# 强制禁止浏览器缓存静态文件（HTML/JS 更新后前端立刻生效）
@app.middleware("http")
async def _no_cache_static(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path.endswith(('.html', '.js', '.css')) or path == '/' or path == '':
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
    return resp


@app.middleware("http")
async def _browser_security(request: Request, call_next):
    """阻止跨站写操作，并为本地管理界面补齐浏览器安全边界。"""
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        origin = (request.headers.get('Origin') or '').rstrip('/')
        fetch_site = (request.headers.get('Sec-Fetch-Site') or '').lower()
        host = request.headers.get('Host', '')
        allowed_origins = {f'http://{host}', f'https://{host}'} | _ALLOWED_ORIGINS
        if fetch_site == 'cross-site' or (origin and origin not in allowed_origins):
            return _secure_response(
                JSONResponse({'detail': 'Cross-site request blocked'}, status_code=403)
            )
        if origin and request.headers.get('X-CrossPilot-Request') != '1':
            return _secure_response(
                JSONResponse(
                    {'detail': 'Missing same-origin request marker'},
                    status_code=403,
                )
            )

    response = await call_next(request)
    return _secure_response(response)

_WIN_RESERVED = {n.upper() for n in ['CON','PRN','AUX','NUL'] + [f'COM{i}' for i in range(1,10)] + [f'LPT{i}' for i in range(1,10)]}

def _safe_filename(filename):
    """过滤 path traversal 和危险字符。"""
    name = os.path.basename(filename or '')
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .')
    extension = os.path.splitext(name)[1].lower()
    if extension not in {'.xlsx', '.json'}:
        return None
    stem = os.path.splitext(name)[0].rstrip(' .')
    if not name or not stem or stem.split('.')[0].upper() in _WIN_RESERVED:
        return None
    return stem[:180] + extension
# 支持 PyInstaller 打包后 APPDATA 路径
DATA_DIR = os.environ.get('CROSSPILOT_DATA_DIR') or os.path.join(ROOT, 'data')
KEYS_PATH = os.environ.get('CROSSPILOT_KEYS_PATH') or os.path.join(ROOT, 'keys.json')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
KEYS_EXAMPLE = os.path.join(ROOT, 'keys.example.json') if os.path.exists(os.path.join(ROOT, 'keys.example.json')) else None


# ===== 仪表盘 =====
@app.get("/api/dashboard")
def dashboard():
    stats = store.dashboard_stats()
    tasks = store.list_tasks(limit=50)
    image_stats = store.aggregate_image_stats()
    queue_stats = jobs.queue_snapshot()
    return {
        **stats,
        **queue_stats,
        'total_reviewed': image_stats['images_reviewed'],
        'total_watermarks': image_stats['watermarks'],
        'total_generated': image_stats['images_generated'],
        'max_upload_mb': MAX_UPLOAD_SIZE // 1024 // 1024,
        'max_batch_files': MAX_BATCH_FILES,
        'recent': [
            _public_task(t)
            for t in tasks[:5]
            if t['status'] in ('done', 'needs_review', 'running')
        ],
    }


# ===== 批量上传 =====
MAX_UPLOAD_SIZE = max(
    1, int(os.environ.get('CROSSPILOT_MAX_UPLOAD_MB', '50'))
) * 1024 * 1024
MAX_BATCH_FILES = max(
    1, min(int(os.environ.get('CROSSPILOT_MAX_BATCH_FILES', '20')), 100)
)

_PUBLIC_TASK_FIELDS = (
    'id', 'filename', 'pipeline', 'status', 'stage', 'stage_index',
    'stage_total', 'current', 'total', 'percent', 'eta_s', 'error',
    'created_at', 'updated_at', 'stats', 'total_elapsed_s', 'quality_score',
)
_PUBLIC_PROGRESS_FIELDS = (
    'status', 'stage', 'stage_index', 'stage_total', 'current', 'total',
    'percent', 'eta_s', 'error', 'total_elapsed_s', 'updated_at',
)


def _public_task(task):
    """只向浏览器返回展示字段，隐藏本地路径和内部缓存。"""
    public = {
        key: task.get(key)
        for key in _PUBLIC_TASK_FIELDS
        if key in task
    }
    if not public.get('quality_score'):
        public['quality_score'] = build_quality_score(public)
    return public


def _public_progress(progress):
    return {
        key: progress.get(key)
        for key in _PUBLIC_PROGRESS_FIELDS
        if key in progress
    }


def _review_value(value, limit=240):
    """Short display-only value for review context; never include local paths."""
    if isinstance(value, (list, tuple)):
        text = ' | '.join(str(item or '').strip() for item in value if str(item or '').strip())
    else:
        text = str(value or '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _first_image(value):
    if isinstance(value, list):
        values = value
    else:
        values = str(value or '').replace('\r', '').split('\n')
    for item in values:
        text = str(item or '').strip()
        if text.startswith(('http://', 'https://')):
            return text
    return ''


def _review_field(current):
    current = _review_value(current)
    return {'current': current} if current else {}


def _read_amazon_json_review_rows(path, *, output=False):
    from scripts.services.amazon_json import load_columnar_json
    payload = load_columnar_json(path)
    titles = payload.get('产品标题', [])
    descriptions = payload.get('产品描述', [])
    product_images = payload.get('产品图片链接', [])
    rows = []
    for index, title in enumerate(titles, start=1):
        fields = {
            'title': _review_field(title),
            'description': _review_field(descriptions[index - 1] if index - 1 < len(descriptions) else ''),
            'main_image': _review_field(_first_image(product_images[index - 1] if index - 1 < len(product_images) else '')),
        }
        if output:
            bullets = [
                (payload.get(f'Bullet Point{bullet_index}', []) or [''])[index - 1]
                if index - 1 < len(payload.get(f'Bullet Point{bullet_index}', []))
                else ''
                for bullet_index in range(1, 6)
            ]
            fields['Bullet'] = _review_field([bullet for bullet in bullets if bullet])
            keywords = payload.get('关键词信息', [])
            fields['keywords'] = _review_field(keywords[index - 1] if index - 1 < len(keywords) else '')
        rows.append({
            'data_row': index,
            'row': index,
            'output_row' if output else 'source_row': index,
            'fields': {key: value for key, value in fields.items() if value},
        })
    return rows


def _read_source_xlsx_review_rows(path):
    import openpyxl
    from scripts.adapters import detect_adapter

    wb = None
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        adapter = detect_adapter(wb.active)
        ws = wb[adapter.sheet_name] if adapter and adapter.sheet_name and adapter.sheet_name in wb.sheetnames else wb.active
        cols = adapter.cols if adapter else {}
        rows = []
        for sheet_row in range(2, ws.max_row + 1):
            fields = {
                'title': _review_field(ws.cell(sheet_row, cols.get('title', 2)).value),
                'description': _review_field(ws.cell(sheet_row, cols.get('desc', 3)).value),
            }
            main_col = cols.get('main_image')
            if main_col:
                fields['main_image'] = _review_field(_first_image(ws.cell(sheet_row, main_col).value))
            rows.append({
                'data_row': sheet_row - 1,
                'row': sheet_row,
                'source_row': sheet_row,
                'fields': {key: value for key, value in fields.items() if value},
            })
        return rows
    finally:
        if wb is not None:
            wb.close()


def _read_output_xlsx_review_rows(path):
    import openpyxl

    wb = None
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        ws = wb.active
        rows = []
        for sheet_row in range(3, ws.max_row + 1):
            bullets = [ws.cell(sheet_row, col).value for col in range(18, 23)]
            fields = {
                'title': _review_field(ws.cell(sheet_row, 1).value),
                'description': _review_field(ws.cell(sheet_row, 2).value),
                'main_image': _review_field(_first_image(ws.cell(sheet_row, 3).value)),
                'Bullet': _review_field([bullet for bullet in bullets if bullet]),
                'keywords': _review_field(ws.cell(sheet_row, 23).value),
            }
            rows.append({
                'data_row': sheet_row - 2,
                'row': sheet_row,
                'output_row': sheet_row,
                'fields': {key: value for key, value in fields.items() if value},
            })
        return rows
    finally:
        if wb is not None:
            wb.close()


def _merge_review_context(source_rows, output_rows):
    merged = {}
    for item in source_rows or []:
        data_row = item.get('data_row')
        context = merged.setdefault(data_row, {'data_row': data_row, 'fields': {}})
        context['source_row'] = item.get('source_row') or item.get('row')
        context.setdefault('row', item.get('row'))
        for field, value in (item.get('fields') or {}).items():
            current = _review_value(value.get('current') if isinstance(value, dict) else value)
            if current:
                context['fields'].setdefault(field, {})['original'] = current
    for item in output_rows or []:
        data_row = item.get('data_row')
        context = merged.setdefault(data_row, {'data_row': data_row, 'fields': {}})
        context['output_row'] = item.get('output_row') or item.get('row')
        context['row'] = item.get('row')
        for field, value in (item.get('fields') or {}).items():
            current = _review_value(value.get('current') if isinstance(value, dict) else value)
            if current:
                context['fields'].setdefault(field, {})['processed'] = current
    for context in merged.values():
        title_field = context.get('fields', {}).get('title', {})
        context['title'] = title_field.get('processed') or title_field.get('original') or ''
    return [merged[key] for key in sorted(key for key in merged if key is not None)]


def _review_context_rows(task):
    """Best-effort row context for review UI/CSV; all filesystem details stay server-side."""
    if task.get('pipeline') != 'amazon':
        return []
    input_path = task.get('input_path') or ''
    output_path = task.get('output_path') or ''
    source_rows = []
    output_rows = []
    try:
        if input_path and os.path.exists(input_path):
            if input_path.lower().endswith('.json'):
                source_rows = _read_amazon_json_review_rows(input_path)
            elif input_path.lower().endswith('.xlsx'):
                source_rows = _read_source_xlsx_review_rows(input_path)
    except Exception as exc:
        _log.warn('复核上下文读取输入失败', error=str(exc))
    try:
        if output_path and os.path.exists(output_path):
            if output_path.lower().endswith('.json'):
                output_rows = _read_amazon_json_review_rows(output_path, output=True)
            elif output_path.lower().endswith('.xlsx'):
                output_rows = _read_output_xlsx_review_rows(output_path)
    except Exception as exc:
        _log.warn('复核上下文读取输出失败', error=str(exc))
    return _merge_review_context(source_rows, output_rows)


async def _save_upload(file: UploadFile, destination: str):
    """流式保存并验证 XLSX 或 Amazon 列式 JSON。"""
    written = 0
    try:
        with open(destination, 'wb') as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_SIZE:
                    raise ValueError(f'文件超过 {MAX_UPLOAD_SIZE // 1024 // 1024}MB 上限')
                out.write(chunk)
    except Exception:
        try:
            os.remove(destination)
        except OSError:
            pass
        raise
    extension = os.path.splitext(destination)[1].lower()
    validation_error = None
    if written == 0:
        validation_error = '文件为空'
    elif extension == '.xlsx' and not zipfile.is_zipfile(destination):
        validation_error = '文件不是有效的 .xlsx 工作簿'
    elif extension == '.json':
        try:
            from scripts.services.amazon_json import load_columnar_json
            max_rows = max(
                1, int(os.environ.get('CROSSPILOT_MAX_ROWS', '10000'))
            )
            load_columnar_json(destination, max_rows=max_rows)
        except ValueError as exc:
            validation_error = str(exc)

    if validation_error:
        try:
            os.remove(destination)
        except OSError:
            pass
        raise ValueError(validation_error)
    return written


@app.post("/api/upload/batch")
async def upload_batch(files: list[UploadFile] = File(...)):
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(413, f'单次最多上传 {MAX_BATCH_FILES} 个文件')
    results = []
    for file in files:
        extension = os.path.splitext(file.filename or '')[1].lower()
        if extension not in {'.xlsx', '.json'}:
            results.append({
                'filename': file.filename,
                'error': '只接受 .xlsx 或 Amazon .json 文件',
            })
            continue
        safe_name = _safe_filename(file.filename)
        if not safe_name:
            results.append({'filename': file.filename, 'error': '文件名无效'})
            continue
        job_id = uuid.uuid4().hex[:12]
        job_dir = os.path.join(UPLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        in_path = os.path.join(job_dir, safe_name)
        try:
            await _save_upload(file, in_path)
        except ValueError as e:
            shutil.rmtree(job_dir, ignore_errors=True)
            results.append({'filename': file.filename, 'error': str(e)})
            continue
        except OSError as e:
            shutil.rmtree(job_dir, ignore_errors=True)
            results.append({'filename': file.filename, 'error': f'文件保存失败: {e}'})
            continue
        store.create(job_id, safe_name, in_path)
        if jobs.enqueue(job_id, in_path):
            results.append({'job_id': job_id, 'filename': file.filename})
        else:
            state = store.get(job_id) or {}
            results.append({
                'job_id': job_id,
                'filename': file.filename,
                'error': state.get('error') or '任务无法入队',
            })
    return {'results': results, **jobs.queue_snapshot()}


# ===== 任务每行水印/生图明细 =====
@app.get("/api/tasks/{job_id}/rows")
def task_rows(job_id: str):
    t = store.get(job_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    input_path = t.get('input_path', '')
    c = jobs.read_cache(input_path)
    review = c.get('review_results', {})
    generated = c.get('gen_results', {})

    rows = []
    wb = None
    try:
        import openpyxl
        from scripts.adapters import detect_adapter
        wb = openpyxl.load_workbook(input_path, data_only=True)
        adapter = detect_adapter(wb.active)
        ws = wb[adapter.sheet_name] if adapter and adapter.sheet_name and adapter.sheet_name in wb.sheetnames else wb.active
        cols = adapter.cols if adapter else {
            'title': 2, 'main_image': 18, 'attachments': list(range(19, 27)),
            'variant': 29, 'size_image': 28,
        }
        title_col = cols['title']
        main_col = cols['main_image']
        att_cols = cols.get('attachments', [])
        variant_col = cols.get('variant')
        size_col = cols.get('size_image')

        for r in range(2, ws.max_row + 1):
            title = str(ws.cell(r, title_col).value or '').strip()
            # 收集该行所有图片 URL
            img_urls = []
            main_raw = str(ws.cell(r, main_col).value or '').strip()
            main_values = [
                u.strip() for u in main_raw.replace('\r', '').split('\n')
                if u.strip().startswith('http')
            ]
            main_url = main_values[0] if main_values else ''
            if main_url:
                img_urls.append(('main', main_url))
            img_urls.extend(('attachment', u) for u in main_values[1:])
            for ac in att_cols:
                au = str(ws.cell(r, ac).value or '').strip()
                if au and au.startswith('http'):
                    img_urls.append(('attachment', au))
            if variant_col:
                variant_raw = str(ws.cell(r, variant_col).value or '').strip()
                variant_values = [
                    u.strip() for u in variant_raw.replace('\r', '').split('\n')
                    if u.strip().startswith('http')
                ]
                img_urls.extend(('variant', u) for u in variant_values)
            if size_col:
                su = str(ws.cell(r, size_col).value or '').strip()
                if su and su.startswith('http'):
                    img_urls.append(('size', su))

            # 统计该行水印/生图数
            wm_count = sum(1 for _, u in img_urls if review.get(u) is True)
            def _generated_url(kind, url):
                return generated.get(url) or generated.get(f'{kind}:{url}')

            gen_count = sum(1 for kind, u in img_urls if _generated_url(kind, u))
            # 找第一张有水印的主图
            wm_main = None
            if main_url and review.get(main_url) is True:
                wm_main = main_url
            gen_main = _generated_url('main', main_url) if main_url else None

            rows.append({
                'row': r - 1,          # 数据行号（不含表头）
                'title': title[:120],  # 截断，避免太长
                'main_image': main_url,
                'wm_main': wm_main,
                'gen_main': gen_main,
                'image_count': len(img_urls),
                'watermark_count': wm_count,
                'generated_count': gen_count,
                'all_clean': wm_count == 0,
            })
    except Exception as e:
        print(f"[WARN] xlsx row parse failed: {e}", file=sys.stderr)
    finally:
        if wb is not None:
            wb.close()
    return {'rows': rows, 'total': len(rows), 'filename': t['filename']}


# ===== 模板列表（来源适配器注册表） =====
@app.get("/api/templates")
def list_templates():
    adir = os.path.join(ROOT, 'scripts', 'adapters')
    tmpl = []
    try:
        for f in sorted(_glob.glob(os.path.join(adir, '*.py'))):
            if f.endswith('__init__.py') or f.endswith('base.py'):
                continue
            name = os.path.splitext(os.path.basename(f))[0]
            tmpl.append({'id': name, 'name': name.replace('_', '→').upper()})
    except Exception as e:
        print(f"[WARN] template scan failed: {e}", file=sys.stderr)
    return {'templates': tmpl}


# ===== 管道阶段（前端动态读取，避免硬编码不同步） =====
@app.get("/api/stages")
def get_stages(pipeline: str = 'ebay'):
    if pipeline == 'amazon':
        from scripts.process_amazon import AMAZON_STAGES
        return {"stages": AMAZON_STAGES}
    from scripts.process_ebay_tk import StatusReporter
    return {"stages": StatusReporter.STAGES}


# ===== 版本 & 更新 =====
@app.get("/api/version")
def get_version():
    from web import __version__
    try:
        from web.updater import check_for_update
        update = check_for_update()
    except Exception as e:
        print(f"[WARN] update check failed: {e}", file=sys.stderr)
        update = None
    public_update = {'version': update['version']} if update else None
    return {'version': __version__, 'update': public_update}


# ===== 单文件上传 =====
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    extension = os.path.splitext(file.filename or '')[1].lower()
    if extension not in {'.xlsx', '.json'}:
        raise HTTPException(400, "只接受 .xlsx 或 Amazon .json")
    safe_name = _safe_filename(file.filename)
    if not safe_name:
        raise HTTPException(400, "文件名无效")
    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    in_path = os.path.join(job_dir, safe_name)
    try:
        await _save_upload(file, in_path)
    except ValueError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        status = 413 if '超过' in str(e) else 400
        raise HTTPException(status, str(e))
    except OSError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, f"文件保存失败: {e}")
    store.create(job_id, safe_name, in_path)
    if not jobs.enqueue(job_id, in_path):
        state = store.get(job_id) or {}
        raise HTTPException(503, state.get('error') or "任务无法入队")
    return {'job_id': job_id}


@app.get("/api/analytics")
def analytics():
    return store.analytics_stats()


@app.get("/api/tasks")
def list_tasks(
        page: int = 1,
        limit: int = 20,
        task_filter: str = Query('all', alias='filter'),
        sort: str = 'created_desc',
):
    page = max(1, page)  # clamp: page=0/-1 等同 page=1
    limit = max(1, min(limit, 100))  # clamp: 1-100
    task_filter = store.normalize_task_filter(task_filter)
    task_sort = store.normalize_task_sort(sort)
    offset = (page - 1) * limit
    total = store.count_tasks(task_filter=task_filter)
    tasks = [
        _public_task(task)
        for task in store.list_tasks(
            limit=limit,
            offset=offset,
            task_filter=task_filter,
            task_sort=task_sort,
        )
    ]
    return {
        'tasks': tasks,
        'total': total,
        'page': page,
        'filter': task_filter,
        'sort': task_sort,
        'pages': max(1, (total + limit - 1) // limit),
    }


def _sync_progress(job_id, input_path):
    """从 status.json 同步真实进度到 SQLite（公开 helper，task_detail/task_events 复用）。"""
    if not input_path:
        return None
    st = jobs.read_status(input_path)
    if st and st.get('status') not in ('done', 'needs_review', 'failed', 'cancelled'):
        store.update_progress(job_id, st)
    return st


@app.get("/api/tasks/{job_id}")
def task_detail(job_id: str):
    t = store.get(job_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    # 同步 status.json → SQLite（hook 只在里程碑触发）
    if t.get('status') == 'running':
        st = _sync_progress(job_id, t.get('input_path'))
        if st:
            t.update({k: st.get(k) for k in ['stage','stage_index','stage_total','current','total','percent','eta_s'] if k in st})
        # 用 DB 创建时间实时计算总耗时
        if t.get('created_at'):
            t['total_elapsed_s'] = max(0, int(time.time() - t['created_at']))
    return _public_task(t)


@app.get("/api/tasks/{job_id}/events")
def task_events(job_id: str):
    q = jobs.subscribe(job_id)
    def gen():
        _SSE_MAX_S = 1800  # 最多 30 分钟，防止卡死时无限循环
        _started = time.time()
        last_status = None
        try:
            while time.time() - _started < _SSE_MAX_S:
                t = store.get(job_id)
                if not t:
                    break
                status = t.get('status')
                if status in ('done', 'needs_review', 'failed', 'cancelled'):
                    event = {'type': t['status'], 'data': _public_task(t)}
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    break
                if status == 'queued':
                    if last_status != 'queued':
                        event = {'type': 'queued', 'data': _public_task(t)}
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    last_status = status
                else:
                    st = jobs.read_status(t['input_path'])
                    if not st:
                        st = t
                    if t.get('created_at'):
                        st['total_elapsed_s'] = max(0, int(time.time() - t['created_at']))
                    if st.get('status') not in ('done', 'needs_review', 'failed', 'cancelled'):
                        store.update_progress(job_id, st)
                    event = {'type': 'progress', 'data': _public_progress(st)}
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    last_status = status
                try:
                    from queue import Empty
                    ev = q.get(timeout=3)
                except Empty:
                    ev = None
                if ev:
                    event_type = ev.get('type')
                    event_data = ev.get('data') if isinstance(ev.get('data'), dict) else {}
                    if event_type in ('done', 'needs_review', 'failed', 'cancelled'):
                        current = store.get(job_id)
                        event_data = _public_task(current) if current else {}
                    else:
                        event_data = _public_progress(event_data)
                    event = {'type': event_type, 'data': event_data}
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event_type in ('done', 'needs_review', 'failed', 'cancelled'):
                        break
        finally:
            jobs.unsubscribe(job_id, q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.get("/api/tasks/{job_id}/download")
def download(job_id: str):
    t = store.get(job_id)
    if not t or not t.get('output_path') or not os.path.exists(t['output_path']):
        raise HTTPException(404, "输出文件不存在")
    # 防路径遍历：解析真实路径并校验，使用已解析路径避免 TOCTOU
    real_out = os.path.realpath(t['output_path'])
    real_uploads = os.path.realpath(UPLOAD_DIR)
    if not real_out.startswith(real_uploads + os.sep):
        raise HTTPException(403, "非法文件路径")
    output_extension = os.path.splitext(real_out)[1].lower()
    if t.get('pipeline') == 'amazon':
        if output_extension == '.json':
            stem = os.path.splitext(t.get('filename', 'output'))[0]
            download_name = (
                stem.replace('采集表', '回填表', 1)
                if '采集表' in stem
                else stem + '_回填'
            ) + '.json'
            return FileResponse(real_out, filename=download_name)
        suffix = '_回填.xlsx'
    else:
        suffix = '_cleaned.xlsx'
    download_name = os.path.splitext(t.get('filename', 'output'))[0] + suffix
    return FileResponse(real_out, filename=download_name)


@app.get("/api/tasks/{job_id}/review-report")
def review_report(job_id: str):
    t = store.get(job_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    public_task = _public_task(t)
    report = '\ufeff' + build_review_report_csv(
        public_task,
        row_contexts=_review_context_rows(t),
    )
    stem = os.path.splitext(t.get('filename', 'output'))[0] or 'output'
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem).strip(' .') or 'output'
    download_name = stem[:120] + '_复核报告.csv'
    return StreamingResponse(
        iter([report.encode('utf-8')]),
        media_type='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': (
                "attachment; filename*=UTF-8''" + quote(download_name)
            ),
        },
    )


@app.get("/api/tasks/{job_id}/review-data")
def review_data(job_id: str):
    t = store.get(job_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return build_review_data(
        _public_task(t),
        row_contexts=_review_context_rows(t),
    )


@app.post("/api/tasks/{job_id}/retry")
def retry_task(job_id: str, fresh: bool = False):
    t = store.get(job_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    input_path = t.get('input_path', '')
    if not input_path or not os.path.exists(input_path):
        raise HTTPException(404, "原始文件已删除，无法重试")
    if jobs.is_active(job_id):
        raise HTTPException(409, "任务仍在运行或排队中，不能重复入队")
    # 重置状态
    store.prepare_retry(job_id)
    if fresh:
        jobs.clear_cache(input_path)
    try:
        os.remove(jobs.status_json_path(input_path))
    except OSError:
        pass
    # 重新入队
    if not jobs.enqueue(job_id, input_path):
        state = store.get(job_id) or {}
        raise HTTPException(409, state.get('error') or "任务无法入队")
    return {
        'job_id': job_id,
        'status': 'queued',
        'mode': 'fresh' if fresh else 'resume',
    }


@app.post("/api/tasks/{job_id}/cancel")
def cancel_task(job_id: str):
    task = store.get(job_id)
    if not task:
        raise HTTPException(404, '任务不存在')
    if not jobs.is_active(job_id):
        raise HTTPException(409, '任务当前未在运行或排队')
    if not jobs.cancel(job_id):
        raise HTTPException(409, '任务正在启动，暂时无法安全取消')
    store.mark_cancelled(job_id)
    return _public_task(store.get(job_id))


@app.delete("/api/tasks/{job_id}")
def delete_task(job_id: str):
    # 校验 job_id 格式（仅十六进制，12 字符），防止 path traversal
    if not job_id or not re.match(r'^[0-9a-f]{12}$', job_id):
        raise HTTPException(400, "无效的任务 ID")
    if not store.get(job_id):
        raise HTTPException(404, "任务不存在")
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    # 二次校验：解析后的真实路径必须在 UPLOAD_DIR 内
    real_dir = os.path.realpath(job_dir)
    real_upload = os.path.realpath(UPLOAD_DIR) + os.sep
    if not (real_dir + os.sep).startswith(real_upload):
        raise HTTPException(400, "路径非法")
    if jobs.is_active(job_id) and not jobs.cancel(job_id):
        raise HTTPException(409, "该任务正在进程内处理，暂时无法安全删除")
    if os.path.exists(job_dir):
        try:
            shutil.rmtree(job_dir)
        except OSError as e:
            raise HTTPException(409, f"任务文件暂时无法删除: {e}")
    store.delete(job_id)
    return {'ok': True}


@app.get("/api/settings")
def get_settings():
    """回显 key 配置状态，不回传明文。"""
    try:
        with open(KEYS_PATH, encoding='utf-8') as f:
            k = json.load(f)
    except Exception as e:
        _log.warn("settings 读取失败", path=KEYS_PATH, error=str(e))
        try:
            with open(KEYS_EXAMPLE, encoding='utf-8') as f:
                k = json.load(f)
        except Exception as e2:
            _log.warn("settings example 读取失败", error=str(e2))
            k = {}
    return {
        # 新的 provider 配置格式
        'text_provider': k.get('text_provider', 'deepseek'),
        'vision_provider': k.get('vision_provider', 'agnes'),
        'image_gen_provider': k.get('image_gen_provider', 'agnes'),
        # key 配置状态
        'deepseek_key_set': bool(k.get('deepseek_key')),
        'agnes_key_set': bool(k.get('agnes_key')),
    }


@app.post("/api/settings")
async def save_settings(payload: dict):
    """保存当前实现真正支持的 provider 配置。"""
    new = {}
    try:
        with open(KEYS_PATH, encoding='utf-8') as f:
            new = json.load(f)
    except Exception as e:
        _log.warn("settings 读取失败", path=KEYS_PATH, error=str(e))
        new = {}

    provider_options = {
        'text_provider': {'deepseek', 'agnes'},
        'vision_provider': {'agnes'},
        'image_gen_provider': {'agnes'},
    }
    for field, allowed in provider_options.items():
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or value not in allowed:
            choices = ' / '.join(sorted(allowed))
            raise HTTPException(400, f"{field} 不支持 {value!r}，可选: {choices}")
        new[field] = value

    # 处理 API keys
    for field in ('deepseek_key', 'agnes_key'):
        value = payload.get(field)
        if value is None or value == '':
            continue
        if not isinstance(value, str) or len(value) > 4096:
            raise HTTPException(400, f"{field} 格式无效")
        new[field] = value.strip()

    os.makedirs(os.path.dirname(KEYS_PATH), exist_ok=True)
    temp_path = KEYS_PATH + f'.{uuid.uuid4().hex}.tmp'
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(temp_path, KEYS_PATH)
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EBUSY, errno.EXDEV):
            raise
        # 单文件 Docker bind mount 不支持 rename，退化为受控原地写入。
        with open(KEYS_PATH, 'w', encoding='utf-8') as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.remove(temp_path)
        except OSError:
            pass
    try:
        os.chmod(KEYS_PATH, 0o600)
    except OSError:
        pass
    for module_name in (
        'pipelines.ebay_shared', 'scripts.pipelines.ebay_shared',
        'process_amazon', 'scripts.process_amazon',
    ):
        module = sys.modules.get(module_name)
        if module and hasattr(module, 'reload_credentials'):
            module.reload_credentials()
    return {'ok': True}


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), 'static'),
                          html=True), name="static")
