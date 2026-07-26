"""FastAPI app：上传/历史/详情/SSE/下载/设置。复用 process_ebay_tk 管道。"""
import os, sys, json, uuid, shutil, time, re, glob as _glob, datetime as _dt
from scripts.pipeline_log import log as _log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from web import store, jobs

app = FastAPI(title="CrossPilot")

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

jobs.start_monitor()


_WIN_RESERVED = {n.upper() for n in ['CON','PRN','AUX','NUL'] + [f'COM{i}' for i in range(1,10)] + [f'LPT{i}' for i in range(1,10)]}

def _safe_filename(filename):
    """过滤 path traversal 和危险字符。"""
    name = os.path.basename(filename or '')
    if not name.lower().endswith('.xlsx'):
        return None
    stem = os.path.splitext(name)[0].upper()
    if not name or stem in _WIN_RESERVED:
        return None
    return name
# 支持 PyInstaller 打包后 APPDATA 路径
DATA_DIR = os.environ.get('CROSSPILOT_DATA_DIR') or os.path.join(ROOT, 'data')
KEYS_PATH = os.environ.get('CROSSPILOT_KEYS_PATH') or os.path.join(ROOT, 'keys.json')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
KEYS_EXAMPLE = os.path.join(ROOT, 'keys.example.json') if os.path.exists(os.path.join(ROOT, 'keys.example.json')) else None


# ===== 仪表盘 =====
@app.get("/api/dashboard")
def dashboard():
    tasks = store.list_tasks(limit=50)
    today = _dt.date.today().isoformat()
    today_tasks = [t for t in tasks if t.get('created_at') and _dt.date.fromtimestamp(t['created_at']).isoformat() == today]
    done = [t for t in tasks if t['status'] == 'done']
    running = [t for t in tasks if t['status'] == 'running']
    total_reviewed = sum((t.get('stats') or {}).get('images_reviewed', 0) for t in done)
    total_watermarks = sum((t.get('stats') or {}).get('watermarks', 0) for t in done)
    total_generated = sum((t.get('stats') or {}).get('images_generated', 0) for t in done)
    return {
        'today_count': len(today_tasks),
        'running_count': len(running),
        'done_count': len(done),
        'total_reviewed': total_reviewed,
        'total_watermarks': total_watermarks,
        'total_generated': total_generated,
        'recent': [t for t in tasks[:5] if t['status'] in ('done', 'running')],
    }


# ===== 批量上传 =====
@app.post("/api/upload/batch")
async def upload_batch(files: list[UploadFile] = File(...)):
    results = []
    for file in files:
        if not file.filename or not file.filename.lower().endswith('.xlsx'):
            results.append({'filename': file.filename, 'error': '非 .xlsx 文件'})
            continue
        safe_name = _safe_filename(file.filename)
        if not safe_name:
            results.append({'filename': file.filename, 'error': '文件名无效'})
            continue
        job_id = uuid.uuid4().hex[:12]
        job_dir = os.path.join(UPLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        in_path = os.path.join(job_dir, safe_name)
        with open(in_path, 'wb') as f:
            shutil.copyfileobj(file.file, f)
        store.create(job_id, safe_name, in_path)
        jobs.enqueue(job_id, in_path)
        results.append({'job_id': job_id, 'filename': file.filename})
    return {'results': results}


# ===== 任务每行水印/生图明细 =====
@app.get("/api/tasks/{job_id}/rows")
def task_rows(job_id: str):
    t = store.get(job_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    input_path = t.get('input_path', '')
    cache_path = os.path.splitext(input_path)[0] + '_cache.json'
    review = {}
    generated = {}
    try:
        with open(cache_path, encoding='utf-8') as f:
            c = json.load(f)
        review = c.get('review_results', {})
        generated = c.get('gen_results', {})
    except Exception as e:
        print(f"[WARN] cache read failed: {e}", file=sys.stderr)

    rows = []
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
            main_url = str(ws.cell(r, main_col).value or '').strip()
            if main_url and main_url.startswith('http'):
                img_urls.append(('main', main_url))
            for ac in att_cols:
                au = str(ws.cell(r, ac).value or '').strip()
                if au and au.startswith('http'):
                    img_urls.append(('attachment', au))
            if variant_col:
                vu = str(ws.cell(r, variant_col).value or '').strip()
                if vu and vu.startswith('http'):
                    img_urls.append(('variant', vu))
            if size_col:
                su = str(ws.cell(r, size_col).value or '').strip()
                if su and su.startswith('http'):
                    img_urls.append(('size', su))

            # 统计该行水印/生图数
            wm_count = sum(1 for _, u in img_urls if review.get(u) is True)
            gen_count = sum(1 for _, u in img_urls if u in generated)
            # 找第一张有水印的主图
            wm_main = None
            if main_url and review.get(main_url) is True:
                wm_main = main_url
            gen_main = generated.get(main_url) if main_url else None

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
def get_stages():
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
    return {'version': __version__, 'update': update}


# ===== 单文件上传 =====
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith('.xlsx'):
        raise HTTPException(400, "只接受 .xlsx")
    safe_name = _safe_filename(file.filename)
    if not safe_name:
        raise HTTPException(400, "文件名无效")
    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    in_path = os.path.join(job_dir, safe_name)
    with open(in_path, 'wb') as f:
        shutil.copyfileobj(file.file, f)
    store.create(job_id, safe_name, in_path)
    jobs.enqueue(job_id, in_path)
    return {'job_id': job_id}


@app.get("/api/tasks")
def list_tasks():
    return {'tasks': store.list_tasks()}


def _sync_progress(job_id, input_path):
    """从 status.json 同步真实进度到 SQLite（公开 helper，task_detail/task_events 复用）。"""
    if not input_path:
        return None
    st = jobs.read_status(input_path)
    if st:
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
    # 附加 cache.json 明细
    t['cache'] = _read_cache_detail(t['input_path'])
    return t


def _read_cache_detail(input_path):
    if not input_path:
        return None
    try:
        with open(os.path.splitext(input_path)[0] + '_cache.json', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        _log.warn("cache detail read failed", path=input_path, error=str(e))
        return None


@app.get("/api/tasks/{job_id}/events")
def task_events(job_id: str):
    q = jobs.subscribe(job_id)
    def gen():
        _SSE_MAX_S = 1800  # 最多 30 分钟，防止卡死时无限循环
        _started = time.time()
        try:
            while time.time() - _started < _SSE_MAX_S:
                t = store.get(job_id)
                if not t:
                    break
                if t.get('status') != 'running':
                    yield f"data: {json.dumps({'type': t['status'], 'data': t}, ensure_ascii=False)}\n\n"
                    break
                st = jobs.read_status(t['input_path'])
                if st:
                    if t.get('created_at'):
                        st['total_elapsed_s'] = max(0, int(time.time() - t['created_at']))
                    store.update_progress(job_id, st)
                    yield f"data: {json.dumps({'type':'progress','data':st}, ensure_ascii=False)}\n\n"
                try:
                    from queue import Empty
                    ev = q.get(timeout=3)
                except Empty:
                    ev = None
                if ev:
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get('type') in ('done', 'failed'):
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
    # 防路径遍历：确保输出文件在 uploads 目录内
    real_out = os.path.realpath(t['output_path'])
    real_uploads = os.path.realpath(UPLOAD_DIR)
    if not real_out.startswith(real_uploads + os.sep):
        raise HTTPException(403, "非法文件路径")
    return FileResponse(t['output_path'], filename=os.path.basename(t['output_path']))


@app.delete("/api/tasks/{job_id}")
def delete_task(job_id: str):
    # 校验 job_id 格式（仅十六进制，12 字符），防止 path traversal
    if not job_id or not re.match(r'^[0-9a-f]{12}$', job_id):
        raise HTTPException(400, "无效的任务 ID")
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    # 二次校验：解析后的真实路径必须在 UPLOAD_DIR 内
    real_dir = os.path.realpath(job_dir)
    real_upload = os.path.realpath(UPLOAD_DIR) + os.sep
    if not (real_dir + os.sep).startswith(real_upload):
        raise HTTPException(400, "路径非法")
    shutil.rmtree(job_dir, ignore_errors=True)
    store.delete(job_id)
    return {'ok': True}


@app.get("/api/settings")
def get_settings():
    """回显 key 掩码，不回明文。"""
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
        'dmx_key_set': bool(k.get('dmx_key')),
        'agnes_key_set': bool(k.get('agnes_key')),
    }


@app.post("/api/settings")
async def save_settings(payload: dict):
    new = {}
    try:
        with open(KEYS_PATH, encoding='utf-8') as f:
            new = json.load(f)
    except Exception as e:
        _log.warn("settings 读取失败", path=KEYS_PATH, error=str(e))
        new = {}
    if 'dmx_key' in payload and payload['dmx_key']:
        new['dmx_key'] = payload['dmx_key']
    if 'agnes_key' in payload and payload['agnes_key']:
        new['agnes_key'] = payload['agnes_key']
    with open(KEYS_PATH, 'w', encoding='utf-8') as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
    return {'ok': True}


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), 'static'),
                          html=True), name="static")
