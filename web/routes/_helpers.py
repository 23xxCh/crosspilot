"""共享辅助函数 — 从 app.py 迁移，供路由模块和 app.py 共同使用。"""
from __future__ import annotations

from web.review_report import build_quality_score

_PUBLIC_TASK_FIELDS = (
    'id', 'filename', 'pipeline', 'status', 'stage', 'stage_index',
    'stage_total', 'current', 'total', 'percent', 'eta_s', 'error',
    'created_at', 'updated_at', 'stats', 'total_elapsed_s', 'quality_score',
)
_PUBLIC_PROGRESS_FIELDS = (
    'status', 'stage', 'stage_index', 'stage_total', 'current', 'total',
    'percent', 'eta_s', 'error', 'total_elapsed_s', 'updated_at',
)


def public_task(task):
    """只向浏览器返回展示字段，隐藏本地路径和内部缓存。"""
    public = {
        key: task.get(key)
        for key in _PUBLIC_TASK_FIELDS
        if key in task
    }
    if not public.get('quality_score'):
        public['quality_score'] = build_quality_score(public)
    return public


def public_progress(progress):
    return {
        key: progress.get(key)
        for key in _PUBLIC_PROGRESS_FIELDS
        if key in progress
    }


def sync_progress(job_id, input_path):
    """从 status.json 同步真实进度到 SQLite（task_detail/task_events 复用）。"""
    from web import store, jobs
    if not input_path:
        return None
    st = jobs.read_status(input_path)
    if st and st.get('status') not in ('done', 'needs_review', 'failed', 'cancelled'):
        store.update_progress(job_id, st)
    return st
