"""Dashboard + Analytics routes."""
from fastapi import APIRouter
from web import store, jobs
from web.context import ctx
from web.routes._helpers import public_task

router = APIRouter(tags=["dashboard"])


@router.get("/api/dashboard")
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
        'max_upload_mb': ctx.max_upload_mb,
        'max_batch_files': ctx.max_batch_files,
        'recent': [
            public_task(t)
            for t in tasks[:5]
            if t['status'] in ('done', 'needs_review', 'running')
        ],
    }


@router.get("/api/analytics")
def analytics():
    return store.analytics_stats()
