"""RPA API v1 — 三个端点，零概念负担。"""
from __future__ import annotations

import os, uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse
from web import store, jobs

router = APIRouter(prefix="/api/v1", tags=["rpa"])


_health_cache = {'ts': 0, 'data': None}

@router.get("/health")
async def health():
    """API 健康检查 — 缓存 5 分钟，避免每次调用都 ping GPT。"""
    import time as _time
    global _health_cache
    now = _time.time()
    if _health_cache['data'] and (now - _health_cache['ts']) < 300:
        return _health_cache['data']

    from crosspilot.health import run_configured_health_check
    from crosspilot.config import load_config
    cfg = load_config()
    results = run_configured_health_check(cfg)
    data = {
        'status': 'ok' if all(r.ok for r in results) else 'degraded',
        'services': [
            {'name': r.name, 'ok': r.ok, 'latency_ms': int(r.latency_ms)}
            for r in results
        ],
    }
    _health_cache = {'ts': now, 'data': data}
    return data

MAX_UPLOAD_MB = int(os.environ.get('CROSSPILOT_MAX_UPLOAD_MB', '50'))


@router.post("/process")
async def process(file: UploadFile = File(...), mode: str = Form("full")):
    """提交文件处理任务。返回 task_id，RPA 用它轮询状态。"""
    # Validate
    if not file.filename:
        raise HTTPException(400, "文件名为空")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.json', '.xlsx', '.xls'):
        raise HTTPException(400, f"不支持的文件格式: {ext}，请使用 .json 或 .xlsx")

    # Save upload
    job_id = uuid.uuid4().hex[:12]
    data_dir = os.environ.get('CROSSPILOT_DATA_DIR', os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data'))
    uploads_dir = os.path.join(data_dir, 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)
    safe_name = _safe_filename(file.filename)
    input_path = os.path.join(uploads_dir, f'{job_id}_{safe_name}')

    content = await file.read()
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"文件超过 {MAX_UPLOAD_MB}MB 限制")
    with open(input_path, 'wb') as f:
        f.write(content)

    # Set text-only mode if requested
    if mode == 'text-only':
        os.environ['CROSSPILOT_TEXT_ONLY'] = '1'

    # Create task record (pipeline is auto-detected by enqueue)
    store.create(
        job_id=job_id,
        filename=file.filename,
        input_path=input_path,
    )

    # Enqueue for execution
    jobs.enqueue(job_id, input_path)

    return {
        'task_id': job_id,
        'status': 'queued',
        'filename': file.filename,
    }


@router.get("/process/{task_id}")
async def get_task(task_id: str):
    """查询任务状态。RPA 轮询这个接口直到 status=done 或 failed。"""
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")

    status_path = os.path.splitext(task['input_path'])[0] + '_status.json'
    progress = {}
    if os.path.exists(status_path):
        try:
            import json
            with open(status_path, encoding='utf-8') as f:
                progress = json.load(f)
        except Exception:
            pass

    return {
        'task_id': task_id,
        'status': task['status'],
        'filename': task['filename'],
        'pipeline': task['pipeline'] or '',
        'progress': progress.get('stage', ''),
        'percent': progress.get('percent', 0),
        'total_rows': progress.get('total', 0),
        'elapsed_seconds': progress.get('total_elapsed_s', 0),
        'error': task['error'] if task['status'] == 'failed' else None,
    }


@router.get("/process/{task_id}/download")
async def download(task_id: str):
    """下载清洗后的文件。"""
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if task['status'] not in ('done', 'needs_review'):
        raise HTTPException(400, f"任务未完成，当前状态: {task['status']}")

    output = task['output_path']
    if not output or not os.path.exists(output):
        raise HTTPException(404, "输出文件不存在")

    return FileResponse(output, filename=os.path.basename(output))


# ── helpers ────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    name = os.path.basename(name)
    keep = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ')
    return ''.join(c if c in keep else '_' for c in name)[:180]
