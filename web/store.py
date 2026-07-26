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
      status TEXT,           -- queued/running/done/failed
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
      error TEXT,
      created_at REAL,
      updated_at REAL
    )""")
    conn.commit()


_init_db()


def _row(r):
    if not r:
        return None
    d = dict(r)
    d.pop('stats_json')
    try:
        d['stats'] = json.loads(r['stats_json']) if r['stats_json'] else {}
    except Exception as e:
        print(f"[WARN] stats_json parse failed for task {r['id']}: {e}", file=__import__('sys').stderr)
        d['stats'] = {}
    return d


def create(job_id, filename, input_path):
    now = time.time()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tasks(id,filename,status,input_path,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (job_id, filename, 'queued', input_path, now, now))
    conn.commit()
    return get(job_id)


def list_tasks(limit=100):
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def get(job_id):
    conn = _get_conn()
    return _row(conn.execute("SELECT * FROM tasks WHERE id=?", (job_id,)).fetchone())


_ALLOWED_COLUMNS = {'status', 'stage', 'stage_index', 'stage_total', 'current', 'total',
                     'percent', 'eta_s', 'error', 'output_path', 'stats', 'updated_at'}

def _set(job_id, **fields):
    """原子更新任务字段（仅允许白名单列名，防 SQL 注入）。"""
    conn = _get_conn()
    safe = {k: v for k, v in fields.items() if k in _ALLOWED_COLUMNS}
    keys = ','.join(f'{k}=?' for k in safe)
    conn.execute(f'UPDATE tasks SET {keys},updated_at=? WHERE id=?',
                 (*safe.values(), time.time(), job_id))
    conn.commit()


def update_progress(job_id, st):
    """st = 管道写的 _status.json 内容（dict）。"""
    _set(job_id, status='running', stage=st.get('stage', ''),
         stage_index=st.get('stage_index'), stage_total=st.get('stage_total'),
         current=st.get('current'), total=st.get('total'),
         percent=st.get('percent', 0), eta_s=st.get('eta_s'))


def mark_done(job_id, output_path, stats=None):
    _set(job_id, status='done', output_path=output_path, stage='完成',
         percent=100, eta_s=0, stats_json=json.dumps(stats or {}, ensure_ascii=False))


def mark_failed(job_id, error):
    _set(job_id, status='failed', error=error[:4000], stage='错误')


def delete(job_id):
    conn = _get_conn()
    conn.execute("DELETE FROM tasks WHERE id=?", (job_id,))
    conn.commit()


def close():
    """关闭当前线程的 SQLite 连接（在线程退出前调用，避免 fd 泄漏）。"""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None

atexit.register(close)
