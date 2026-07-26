"""SQLite tasks 表：状态机 + 进度。stdlib sqlite3，不上 ORM。
线程安全：每个线程独立连接（threading.local），避免多线程并发写冲突。"""
import sqlite3, json, os, time, threading, atexit

_DATA_DIR = os.environ.get('CROSSPILOT_DATA_DIR') or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
DB_PATH = os.path.join(_DATA_DIR, 'tasks.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 每个线程独立连接，避免多线程并发写冲突
_local = threading.local()


def _get_conn():
    """获取当前线程的 SQLite 连接（懒初始化）。"""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return conn


# 主线程初始化建表
def _init_db():
    conn = _get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS tasks(
      id TEXT PRIMARY KEY,
      filename TEXT,
      pipeline TEXT,
      status TEXT,           -- queued/running/done/needs_review/failed/cancelled
      stage TEXT,
      stage_index INTEGER,
      stage_total INTEGER,
      current INTEGER,
      total INTEGER,
      percent INTEGER,
      eta_s INTEGER,
      input_path TEXT,
      output_path TEXT,
      stats_json TEXT,       -- 水印数/生图数等汇总
      quality_score_value INTEGER,
      quality_grade TEXT,
      quality_score_json TEXT,
      error TEXT,
      created_at REAL,
      updated_at REAL
    )""")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if 'pipeline' not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN pipeline TEXT")
    if 'quality_score_value' not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN quality_score_value INTEGER")
    if 'quality_grade' not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN quality_grade TEXT")
    if 'quality_score_json' not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN quality_score_json TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_quality_score ON tasks(quality_score_value, created_at DESC)"
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_tasks_quality_score_sort
           ON tasks(
             CASE WHEN quality_score_value IS NULL THEN 1 ELSE 0 END,
             quality_score_value,
             created_at DESC
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_tasks_quality_score_sort_desc
           ON tasks(
             CASE WHEN quality_score_value IS NULL THEN 1 ELSE 0 END,
             quality_score_value DESC,
             created_at DESC
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_quality_grade ON tasks(quality_grade, created_at DESC)"
    )
    conn.commit()


_init_db()


def _row(r):
    if not r:
        return None
    d = dict(r)
    d.pop('stats_json', None)
    quality_score_json = d.pop('quality_score_json', None)
    d.pop('quality_score_value', None)
    d.pop('quality_grade', None)
    try:
        d['stats'] = json.loads(r['stats_json']) if r['stats_json'] else {}
    except Exception as e:
        print(f"[WARN] stats_json parse failed for task {r['id']}: {e}", file=__import__('sys').stderr)
        d['stats'] = {}
    try:
        d['quality_score'] = json.loads(quality_score_json) if quality_score_json else None
    except Exception as e:
        print(f"[WARN] quality_score_json parse failed for task {r['id']}: {e}", file=__import__('sys').stderr)
        d['quality_score'] = None
    if d['quality_score'] is None:
        d['quality_score'] = _build_quality_score(d)
    return d


def _build_quality_score(task):
    from web.review_report import build_quality_score

    return build_quality_score(task)


def _quality_fields(status, stats=None, stats_json=None, error=None):
    if stats is None:
        try:
            stats = json.loads(stats_json) if stats_json else {}
        except Exception:
            stats = {}
    payload = _build_quality_score({
        'status': status,
        'stats': stats if isinstance(stats, dict) else {},
        'error': error,
    })
    score = payload.get('score')
    score_value = int(score) if isinstance(score, (int, float)) else None
    return {
        'quality_score_value': score_value,
        'quality_grade': payload.get('grade'),
        'quality_score_json': json.dumps(payload, ensure_ascii=False),
    }


def create(job_id, filename, input_path, pipeline=None):
    now = time.time()
    quality = _quality_fields('queued')
    conn = _get_conn()
    conn.execute(
        """INSERT INTO tasks(
             id,filename,pipeline,status,input_path,
             quality_score_value,quality_grade,quality_score_json,
             created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            job_id, filename, pipeline, 'queued', input_path,
            quality['quality_score_value'],
            quality['quality_grade'],
            quality['quality_score_json'],
            now, now,
        ))
    conn.commit()
    return get(job_id)


VALID_TASK_FILTERS = {
    'all',
    'queued',
    'running',
    'done',
    'needs_review',
    'failed',
    'cancelled',
    'high_risk',
    'low_quality',
    'needs_sample',
    'usable',
}

QUALITY_TASK_FILTERS = {'low_quality', 'needs_sample', 'usable'}
VALID_TASK_SORTS = {'created_desc', 'created_asc', 'quality_asc', 'quality_desc'}


def normalize_task_filter(task_filter='all'):
    task_filter = str(task_filter or 'all')
    return task_filter if task_filter in VALID_TASK_FILTERS else 'all'


def is_quality_task_filter(task_filter='all'):
    return normalize_task_filter(task_filter) in QUALITY_TASK_FILTERS


def normalize_task_sort(task_sort='created_desc'):
    task_sort = str(task_sort or 'created_desc')
    return task_sort if task_sort in VALID_TASK_SORTS else 'created_desc'


def _json_int(path):
    return (
        "COALESCE(CAST(CASE WHEN json_valid(stats_json) "
        f"THEN json_extract(stats_json, '{path}') END AS INTEGER), 0)"
    )


def _json_array_len(path):
    return (
        "COALESCE(CASE WHEN json_valid(stats_json) "
        f"THEN json_array_length(stats_json, '{path}') END, 0)"
    )


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_QUALITY_GRADE_META = {
    'pass': {'label': '可用', 'severity': 'ok'},
    'sample': {'label': '抽检', 'severity': 'warn'},
    'review': {'label': '复核', 'severity': 'warn'},
    'critical': {'label': '高危', 'severity': 'danger'},
    'pending': {'label': '未完成', 'severity': 'info'},
    'cancelled': {'label': '已取消', 'severity': 'info'},
    'unknown': {'label': '未知', 'severity': 'info'},
}


def _quality_grade_item(grade, count):
    grade = grade or 'unknown'
    meta = _QUALITY_GRADE_META.get(grade, _QUALITY_GRADE_META['unknown'])
    return {
        'grade': grade,
        'label': meta['label'],
        'severity': meta['severity'],
        'count': int(count or 0),
    }


def _quality_reason_summary(conn, cutoff, limit=2000):
    """Aggregate recent quality-score reasons without scanning unbounded history."""
    rows = conn.execute(
        """SELECT quality_score_json
           FROM tasks
           WHERE quality_score_json IS NOT NULL
             AND created_at > ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    reasons = {}
    for row in rows:
        try:
            payload = json.loads(row['quality_score_json'])
        except Exception:
            continue
        for reason in payload.get('reasons') or []:
            if not isinstance(reason, dict):
                continue
            code = str(reason.get('code') or 'unknown')
            item = reasons.setdefault(code, {
                'code': code,
                'label': str(reason.get('label') or code),
                'count': 0,
                'points': 0,
            })
            item['count'] += max(1, int(_num(reason.get('count'), 1)))
            item['points'] += int(_num(reason.get('points'), 0))
    return sorted(
        reasons.values(),
        key=lambda item: (-item['points'], -item['count'], item['code']),
    )[:8]


def _quality_analytics(conn, cutoff):
    score_row = conn.execute(
        """SELECT
             COUNT(quality_score_value) AS scored,
             AVG(quality_score_value) AS avg_score,
             SUM(CASE WHEN quality_grade='pass' THEN 1 ELSE 0 END) AS usable,
             SUM(CASE WHEN quality_grade='sample' THEN 1 ELSE 0 END) AS needs_sample,
             SUM(CASE WHEN quality_grade IN ('review', 'critical') THEN 1 ELSE 0 END) AS low_quality
           FROM tasks"""
    ).fetchone()
    scored = int(score_row['scored'] or 0)
    usable = int(score_row['usable'] or 0)
    needs_sample = int(score_row['needs_sample'] or 0)
    low_quality = int(score_row['low_quality'] or 0)
    distribution_rows = conn.execute(
        """SELECT COALESCE(quality_grade, 'unknown') AS grade, COUNT(*) AS count
           FROM tasks
           GROUP BY COALESCE(quality_grade, 'unknown')"""
    ).fetchall()
    distribution_counts = {
        row['grade'] or 'unknown': int(row['count'] or 0)
        for row in distribution_rows
    }
    ordered_grades = ['pass', 'sample', 'review', 'critical', 'pending', 'cancelled', 'unknown']
    distribution = [
        _quality_grade_item(grade, distribution_counts[grade])
        for grade in ordered_grades
        if distribution_counts.get(grade)
    ]
    for grade, count in sorted(distribution_counts.items()):
        if grade not in ordered_grades and count:
            distribution.append(_quality_grade_item(grade, count))

    daily_rows = conn.execute(
        """SELECT date(created_at, 'unixepoch', 'localtime') AS day,
                  COUNT(quality_score_value) AS scored,
                  ROUND(AVG(quality_score_value), 1) AS avg_score,
                  SUM(CASE WHEN quality_grade IN ('review', 'critical') THEN 1 ELSE 0 END) AS low_quality,
                  SUM(CASE WHEN quality_grade='sample' THEN 1 ELSE 0 END) AS needs_sample,
                  SUM(CASE WHEN quality_grade='pass' THEN 1 ELSE 0 END) AS usable
           FROM tasks
           WHERE created_at > ?
           GROUP BY day ORDER BY day""",
        (cutoff,),
    ).fetchall()

    return {
        'average_score': round(score_row['avg_score'], 1) if score_row['avg_score'] is not None else None,
        'scored_count': scored,
        'usable_count': usable,
        'needs_sample_count': needs_sample,
        'low_quality_count': low_quality,
        'low_quality_rate': round(low_quality / max(scored, 1) * 100, 1),
        'review_pressure_count': needs_sample + low_quality,
        'review_pressure_rate': round((needs_sample + low_quality) / max(scored, 1) * 100, 1),
        'distribution': distribution,
        'top_reasons': _quality_reason_summary(conn, cutoff),
        'daily_trends': [
            {
                'day': row['day'],
                'scored': int(row['scored'] or 0),
                'avg_score': row['avg_score'],
                'low_quality': int(row['low_quality'] or 0),
                'needs_sample': int(row['needs_sample'] or 0),
                'usable': int(row['usable'] or 0),
            }
            for row in daily_rows
        ],
    }


def _task_filter_where(task_filter='all'):
    task_filter = normalize_task_filter(task_filter)
    if task_filter == 'all':
        return '', ()
    if task_filter == 'low_quality':
        return "WHERE quality_grade IN ('review', 'critical')", ()
    if task_filter == 'needs_sample':
        return "WHERE quality_grade = 'sample'", ()
    if task_filter == 'usable':
        return "WHERE quality_grade = 'pass'", ()
    if task_filter == 'high_risk':
        return (
            f"""WHERE status IN ('needs_review', 'failed')
               OR quality_grade IN ('review', 'critical')
               OR {_json_array_len('$.validation.issues')} > 0
               OR {_json_int('$.metrics.quality.issue_count')} > 0
               OR {_json_int('$.metrics.http_retries')} > 0
               OR {_json_int('$.metrics.circuit_open')} > 0
               OR {_json_int('$.metrics.concurrency.reductions')} > 0""",
            (),
        )
    return 'WHERE status = ?', (task_filter,)


def _task_order_by(task_sort='created_desc'):
    task_sort = normalize_task_sort(task_sort)
    if task_sort == 'created_asc':
        return 'ORDER BY created_at ASC'
    if task_sort == 'quality_asc':
        return (
            "ORDER BY CASE WHEN quality_score_value IS NULL THEN 1 ELSE 0 END, "
            "quality_score_value ASC, created_at DESC"
        )
    if task_sort == 'quality_desc':
        return (
            "ORDER BY CASE WHEN quality_score_value IS NULL THEN 1 ELSE 0 END, "
            "quality_score_value DESC, created_at DESC"
        )
    return 'ORDER BY created_at DESC'


def list_tasks(limit=100, offset=0, task_filter='all', task_sort='created_desc'):
    conn = _get_conn()
    where, params = _task_filter_where(task_filter)
    order_by = _task_order_by(task_sort)
    rows = conn.execute(
        f"SELECT * FROM tasks {where} {order_by} LIMIT ? OFFSET ?",
        (*params, limit, offset)
    ).fetchall()
    return [_row(r) for r in rows]


def count_tasks(task_filter='all'):
    conn = _get_conn()
    where, params = _task_filter_where(task_filter)
    row = conn.execute(f"SELECT COUNT(*) FROM tasks {where}", params).fetchone()
    return row[0] if row else 0


def analytics_stats():
    """分析面板聚合数据：30 天趋势、平台分布、成功率、耗时分布。"""
    conn = _get_conn()
    cutoff = time.time() - 30 * 86400
    # 30 天每日趋势
    daily = conn.execute("""
        SELECT date(created_at, 'unixepoch', 'localtime') as day,
               COUNT(*) as total,
               SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
               SUM(CASE WHEN status='needs_review' THEN 1 ELSE 0 END) as needs_review,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
        FROM tasks
        WHERE created_at > ?
        GROUP BY day ORDER BY day
    """, (cutoff,)).fetchall()
    daily_trends = [
        {
            'day': d[0],
            'total': d[1],
            'done': d[2],
            'needs_review': d[3],
            'failed': d[4],
        }
        for d in daily
    ]

    # 平台分布优先使用识别后的 pipeline，旧记录才从文件名推断。
    ebay = conn.execute(
        """SELECT COUNT(*) FROM tasks
           WHERE pipeline='ebay'
              OR (pipeline IS NULL AND lower(filename) NOT LIKE '%amazon%')"""
    ).fetchone()[0]
    amazon = conn.execute(
        """SELECT COUNT(*) FROM tasks
           WHERE pipeline='amazon'
              OR (pipeline IS NULL AND lower(filename) LIKE '%amazon%')"""
    ).fetchone()[0]
    platform = [{'name': 'eBay', 'count': ebay}, {'name': 'Amazon', 'count': amazon}]

    # 成功率
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    needs_review = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='needs_review'"
    ).fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'").fetchone()[0]
    success_rate = round(done / max(total, 1) * 100, 1)

    # 平均耗时（done 任务）
    avg_row = conn.execute(
        "SELECT AVG(updated_at - created_at) FROM tasks WHERE status='done' AND updated_at > created_at"
    ).fetchone()
    avg_duration = round(avg_row[0], 1) if avg_row[0] else 0

    # 水印/生图总计
    watermarks = conn.execute(
        f"SELECT SUM({_json_int('$.watermarks')}) FROM tasks WHERE status='done'"
    ).fetchone()[0] or 0
    generated = conn.execute(
        f"SELECT SUM({_json_int('$.images_generated')}) FROM tasks WHERE status='done'"
    ).fetchone()[0] or 0
    reviewed = conn.execute(
        f"SELECT SUM({_json_int('$.images_reviewed')}) FROM tasks WHERE status='done'"
    ).fetchone()[0] or 0
    watermark_rate = round(watermarks / max(reviewed, 1) * 100, 1)
    gen_rate = round(generated / max(watermarks, 1) * 100, 1)

    return {
        'daily_trends': daily_trends,
        'platform': platform,
        'total': total, 'done': done, 'needs_review': needs_review, 'failed': failed,
        'success_rate': success_rate,
        'avg_duration': avg_duration,
        'reviewed': reviewed, 'watermarks': watermarks, 'generated': generated,
        'watermark_rate': watermark_rate, 'gen_rate': gen_rate,
        'quality': _quality_analytics(conn, cutoff),
    }


def cleanup_old_tasks(days=7):
    """删除已完成或失败的旧任务及上传目录（默认 7 天前）。"""
    conn = _get_conn()
    cutoff = time.time() - days * 86400
    old = conn.execute(
        "SELECT id, input_path FROM tasks WHERE status IN ('done', 'needs_review', 'failed', 'cancelled') AND updated_at < ?",
        (cutoff,)
    ).fetchall()
    cleaned = 0
    for job_id, input_path in old:
        can_delete_record = True
        if input_path:
            job_dir = os.path.realpath(os.path.dirname(input_path))
            uploads_dir = os.path.realpath(os.path.join(_DATA_DIR, 'uploads'))
            if not (job_dir + os.sep).startswith(uploads_dir + os.sep):
                can_delete_record = False
                print(f"[WARN] skip cleanup outside uploads: {job_dir}", file=__import__('sys').stderr)
            elif os.path.exists(job_dir):
                try:
                    import shutil
                    shutil.rmtree(job_dir)
                except OSError as e:
                    can_delete_record = False
                    print(f"[WARN] cleanup failed for {job_dir}: {e}", file=__import__('sys').stderr)
        if can_delete_record:
            conn.execute("DELETE FROM tasks WHERE id = ?", (job_id,))
            cleaned += 1
    conn.commit()
    return cleaned


def dashboard_stats():
    """聚合仪表盘统计（全量，不依赖分页 limit）。"""
    conn = _get_conn()
    today = time.strftime('%Y-%m-%d')
    today_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE date(created_at, 'unixepoch', 'localtime') = ?", (today,)
    ).fetchone()[0]
    running = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
    ).fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'done'"
    ).fetchone()[0]
    needs_review = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'needs_review'"
    ).fetchone()[0]
    return {
        'today_count': today_count,
        'running_count': running,
        'done_count': done,
        'needs_review_count': needs_review,
    }


def get(job_id):
    conn = _get_conn()
    return _row(conn.execute("SELECT * FROM tasks WHERE id=?", (job_id,)).fetchone())


_ALLOWED_COLUMNS = {'status', 'stage', 'stage_index', 'stage_total', 'current', 'total',
                     'percent', 'eta_s', 'error', 'output_path', 'stats_json', 'pipeline',
                     'quality_score_value', 'quality_grade', 'quality_score_json'}

def _set(job_id, *, active_only=False, **fields):
    """原子更新任务字段（仅允许白名单列名，防 SQL 注入）。"""
    conn = _get_conn()
    safe = {k: v for k, v in fields.items() if k in _ALLOWED_COLUMNS}
    if not safe:
        return
    keys = ','.join(f'{k}=?' for k in safe)
    where = "id=?"
    if active_only:
        where += " AND status NOT IN ('done', 'needs_review', 'failed', 'cancelled')"
    conn.execute(f'UPDATE tasks SET {keys},updated_at=? WHERE {where}',
                 (*safe.values(), time.time(), job_id))
    conn.commit()


def update_progress(job_id, st):
    """st = 管道写的 _status.json 内容（dict）。"""
    _set(job_id, active_only=True,
         status=st.get('status', 'running'), stage=st.get('stage', ''),
         stage_index=st.get('stage_index'), stage_total=st.get('stage_total'),
         current=st.get('current'), total=st.get('total'),
         percent=st.get('percent', 0), eta_s=st.get('eta_s'),
         error=st.get('error'))


def mark_queued(job_id, stage='排队等待'):
    _set(
        job_id,
        status='queued',
        stage=stage,
        percent=0,
        eta_s=0,
        error=None,
        **_quality_fields('queued'),
    )


def prepare_retry(job_id):
    _set(
        job_id,
        status='queued',
        stage='排队等待',
        stage_index=0,
        stage_total=0,
        current=0,
        total=0,
        percent=0,
        eta_s=0,
        error=None,
        output_path=None,
        stats_json=None,
        **_quality_fields('queued'),
    )


def mark_running(job_id, stage='准备处理'):
    _set(
        job_id,
        status='running',
        stage=stage,
        percent=0,
        eta_s=0,
        error=None,
        **_quality_fields('running'),
    )


def set_pipeline(job_id, pipeline):
    _set(job_id, pipeline=pipeline)


def mark_done(job_id, output_path, stats=None):
    stats_json = json.dumps(stats or {}, ensure_ascii=False)
    _set(job_id, status='done', output_path=output_path, stage='完成',
         percent=100, eta_s=0, stats_json=stats_json,
         **_quality_fields('done', stats=stats))


def mark_needs_review(job_id, output_path, stats=None, message='输出存在质量问题，请人工复核'):
    stats_json = json.dumps(stats or {}, ensure_ascii=False)
    _set(
        job_id,
        status='needs_review',
        output_path=output_path,
        stage='待人工复核',
        percent=100,
        eta_s=0,
        error=message[:4000],
        stats_json=stats_json,
        **_quality_fields('needs_review', stats=stats, error=message),
    )


def mark_failed(job_id, error):
    _set(
        job_id,
        status='failed',
        error=error[:4000],
        stage='错误',
        **_quality_fields('failed', error=error),
    )


def mark_cancelled(job_id, message='用户已取消，可从缓存继续处理'):
    _set(
        job_id,
        status='cancelled',
        error=message[:4000],
        stage='已取消',
        **_quality_fields('cancelled', error=message),
    )


def recover_incomplete_tasks():
    """服务重启后，将无法继续跟踪的内存任务转为失败，允许用户安全重试。"""
    conn = _get_conn()
    now = time.time()
    quality = _quality_fields('failed', error='服务已重启，原任务执行状态已丢失，请重试')
    cur = conn.execute(
        """UPDATE tasks
           SET status='failed', stage='错误',
               error='服务已重启，原任务执行状态已丢失，请重试',
               quality_score_value=?, quality_grade=?, quality_score_json=?,
               updated_at=?
           WHERE status IN ('queued', 'running')""",
        (
            quality['quality_score_value'],
            quality['quality_grade'],
            quality['quality_score_json'],
            now,
        ),
    )
    conn.commit()
    return cur.rowcount


def aggregate_image_stats():
    conn = _get_conn()
    row = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN json_valid(stats_json) THEN CAST(json_extract(stats_json, '$.images_reviewed') AS INTEGER) ELSE 0 END), 0),
          COALESCE(SUM(CASE WHEN json_valid(stats_json) THEN CAST(json_extract(stats_json, '$.watermarks') AS INTEGER) ELSE 0 END), 0),
          COALESCE(SUM(CASE WHEN json_valid(stats_json) THEN CAST(json_extract(stats_json, '$.images_generated') AS INTEGER) ELSE 0 END), 0)
        FROM tasks WHERE status='done'
    """).fetchone()
    return {
        'images_reviewed': row[0],
        'watermarks': row[1],
        'images_generated': row[2],
    }


def delete(job_id):
    conn = _get_conn()
    conn.execute("DELETE FROM tasks WHERE id=?", (job_id,))
    conn.commit()


def backfill_missing_quality_scores(limit=1000):
    """Populate quality index columns for old rows created before scoring existed."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, status, stats_json, error
           FROM tasks
           WHERE quality_score_json IS NULL OR quality_grade IS NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    for row in rows:
        quality = _quality_fields(
            row['status'],
            stats_json=row['stats_json'],
            error=row['error'],
        )
        conn.execute(
            """UPDATE tasks
               SET quality_score_value=?, quality_grade=?, quality_score_json=?
               WHERE id=?""",
            (
                quality['quality_score_value'],
                quality['quality_grade'],
                quality['quality_score_json'],
                row['id'],
            ),
        )
    if rows:
        conn.commit()
    return len(rows)


def close():
    """关闭当前线程的 SQLite 连接（在线程退出前调用，避免 fd 泄漏）。"""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None

try:
    backfill_missing_quality_scores()
except Exception as exc:
    print(f"[WARN] quality score backfill failed: {exc}", file=__import__('sys').stderr)

atexit.register(close)
