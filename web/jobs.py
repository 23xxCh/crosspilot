"""后台任务队列：每任务一个独立 python 子进程跑 process_ebay_tk._main。

为什么多进程不用多线程：_DASHBOARD_HOOK 是模块级全局，多线程下多任务互相覆盖。
每任务独立子进程，全局各管各的，真并发。

实现用 subprocess.Popen 调独立 runner 脚本（不用 multiprocessing.spawn——spawn 要
pickle target 函数，子进程重新 import 整个 jobs 模块，在 uvicorn 下容易静默失败）。
"""
import atexit, os, sys, json, threading, queue as _q, time, subprocess, glob, hashlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from web import store  # noqa: E402
DATA_DIR = os.environ.get('CROSSPILOT_DATA_DIR') or os.path.join(ROOT, 'data')

# 单个 openpyxl 任务可能占用数倍文件大小的内存，默认最多两个管道并行。
MAX_WORKERS = max(1, min(int(os.environ.get('CROSSPILOT_MAX_WORKERS', '2')), 4))
MAX_PENDING = 10  # 等待队列上限，防止内存暴涨

# job_id -> {'proc': Popen, 'input_path': str}
_running = {}
_starting = set()
_lock = threading.Lock()
_monitor_stop = threading.Event()
_draining = threading.Event()
_instance_lock_file = None

# ===== SSE 总线 =====
_subscribers = {}
_sub_lock = threading.Lock()


def subscribe(job_id):
    q = _q.Queue()
    with _sub_lock:
        _subscribers.setdefault(job_id, []).append(q)
    return q


def unsubscribe(job_id, q):
    with _sub_lock:
        if job_id in _subscribers and q in _subscribers[job_id]:
            _subscribers[job_id].remove(q)


def _publish(job_id, event):
    with _sub_lock:
        for q in _subscribers.get(job_id, []):
            q.put(event)


def status_json_path(input_path):
    """任务状态文件路径（公开，供 app.py 复用）。"""
    return os.path.splitext(input_path)[0] + '_status.json'


def read_status(input_path):
    """读取任务状态 JSON（公开，供 app.py 复用）。"""
    try:
        with open(status_json_path(input_path), encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # 文件不存在或损坏
        return None


def _read_stats(input_path):
    c = read_cache(input_path)
    rev = c.get('review_results', {})
    gen = c.get('gen_results', {})
    stats = {
        'images_reviewed': len(rev),
        'watermarks': sum(1 for v in rev.values() if v is True),
        'images_generated': len(gen),
    }
    status = read_status(input_path)
    if isinstance(status, dict) and isinstance(status.get('metrics'), dict):
        stats['metrics'] = status['metrics']
    return stats


def _ebay_cache_path(input_path):
    try:
        with open(input_path, 'rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()[:16] \
                if hasattr(hashlib, 'file_digest') else hashlib.sha256(f.read()).hexdigest()[:16]
        return os.path.join(DATA_DIR, 'cache', f'{digest}.json')
    except OSError:
        return ''


def cache_paths(input_path):
    base = os.path.splitext(input_path)[0]
    return [
        base + '_amz_cache.json',
        _ebay_cache_path(input_path),
        base + '_cache.json',
    ]


def read_cache(input_path):
    for cache_path in cache_paths(input_path):
        if not cache_path or not os.path.exists(cache_path):
            continue
        try:
            with open(cache_path, encoding='utf-8') as f:
                return json.load(f) or {}
        except Exception as e:
            print(f"[WARN] cache read failed: {e}", file=sys.stderr)
    return {}


def clear_cache(input_path):
    removed = 0
    for cache_path in cache_paths(input_path):
        if cache_path and os.path.exists(cache_path):
            try:
                os.remove(cache_path)
                removed += 1
            except OSError as e:
                print(f"[WARN] cache delete failed: {e}", file=sys.stderr)
    return removed


def _read_stderr_tail(input_path):
    """读取子进程 stderr 最后几行，提取有用错误信息。"""
    err_file = os.path.splitext(input_path)[0] + '_child_stderr.txt'
    try:
        with open(err_file, encoding='utf-8', errors='replace') as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        # 优先取最后一行含 Error/Traceback/异常 的行
        for line in reversed(lines):
            if any(kw in line for kw in ('Error', 'Traceback', '异常', '❌', '错误')):
                return line[-200:]
        return lines[-1][-200:] if lines else ''
    except Exception:
        return ''


def _find_output(input_path, started_at=None):
    """查找 XLSX/JSON 管道输出，支持时间戳防覆盖命名。"""
    st = read_status(input_path)
    status_output = st.get('output') if isinstance(st, dict) else None
    if status_output and os.path.exists(status_output):
        return status_output

    base, ext = os.path.splitext(input_path)
    candidates = []
    for suffix in ['_cleaned.xlsx', '_回填.xlsx', '_回填.json']:
        std = base + suffix
        if os.path.exists(std):
            candidates.append(std)
    patterns = [base + '_cleaned_*' + ext, base + '_回填_*' + ext]
    if ext.lower() == '.json' and '采集表' in os.path.basename(base):
        refill_base = os.path.join(
            os.path.dirname(base),
            os.path.basename(base).replace('采集表', '回填表', 1),
        )
        patterns.extend([refill_base + '.json', refill_base + '_*.json'])
    for pat in patterns:
        candidates.extend(glob.glob(pat))
    if started_at is not None:
        recent = [p for p in candidates if os.path.getmtime(p) >= started_at - 2]
        if recent:
            candidates = recent
    return max(candidates, key=os.path.getmtime) if candidates else None


# ===== 监控线程：轮询所有在跑子进程，推进度 + 探活 =====
def _py_cmd():
    """返回开发模式或 PyInstaller 模式的独立任务进程命令。"""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--run-job']
    return ['uv', 'run', 'python', '-u', os.path.join(ROOT, 'web', 'runner.py')]

def _monitor_tick(last_snap):
    done_ids = []
    with _lock:
        items = list(_running.items())

    for job_id, info in items:
        if info.get('cancelled'):
            done_ids.append(job_id)
            last_snap.pop(job_id, None)
            continue

        st = read_status(info['input_path'])
        if st and st != last_snap.get(job_id):
            last_snap[job_id] = st
            if st.get('status') not in ('done', 'needs_review', 'failed', 'cancelled'):
                store.update_progress(job_id, st)
                _publish(job_id, {'type': 'progress', 'data': st})

        rc = info['proc'].poll()
        if rc is None:
            continue

        out = _find_output(info['input_path'], info.get('started_at'))
        if rc == 0:
            if out:
                stats = _read_stats(info['input_path'])
                if isinstance(st, dict) and st.get('status') == 'needs_review':
                    validation = st.get('validation') or {
                        'passed': False,
                        'issues': ['管道要求人工复核'],
                    }
                    stats['validation'] = validation
                    message = st.get('error') or '输出存在质量问题，请人工复核'
                    store.mark_needs_review(job_id, out, stats, message)
                    _publish(
                        job_id,
                        {
                            'type': 'needs_review',
                            'data': {
                                'output': out,
                                'stats': stats,
                                'error': message,
                            },
                        },
                    )
                else:
                    store.mark_done(job_id, out, stats)
                    _publish(job_id, {'type': 'done', 'data': {'output': out, 'stats': stats}})
            else:
                store.mark_failed(job_id, "进程成功退出但未生成输出文件")
                _publish(job_id, {'type': 'failed', 'data': {'error': 'no output file'}})
        else:
            err_detail = _read_stderr_tail(info['input_path'])
            err_msg = f"子进程退出码 {rc}"
            if err_detail:
                err_msg += f" — {err_detail}"
            store.mark_failed(job_id, err_msg)
            _publish(job_id, {'type': 'failed', 'data': {'error': err_msg}})
        done_ids.append(job_id)
        last_snap.pop(job_id, None)

    if not done_ids:
        return

    # 只在锁内更新队列状态；真正启动任务必须在锁外，避免重复获取 _lock。
    to_start = []
    with _lock:
        for jid in done_ids:
            info = _running.pop(jid, None)
            if info and info.get('_err_fd'):
                try:
                    info['_err_fd'].close()
                except Exception:
                    pass
        available = max(0, MAX_WORKERS - len(_running) - len(_starting))
        while _pending and len(to_start) < available:
            item = _pending.pop(0)
            _starting.add(item[0])
            to_start.append(item)

    for job_id, input_path, pipeline in to_start:
        try:
            _start_task(job_id, input_path, pipeline)
        except Exception as e:
            store.mark_failed(job_id, f"任务启动失败: {type(e).__name__}: {e}")
            _publish(job_id, {'type': 'failed', 'data': {'error': str(e)}})


def _monitor():
    last_snap = {}
    while not _monitor_stop.is_set():
        try:
            _monitor_tick(last_snap)
        except Exception as e:
            print(f"[jobs] monitor error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        _monitor_stop.wait(1)


_thread = None


def _acquire_instance_lock(timeout=10):
    """确保同一数据目录只有一个队列管理进程，避免多 worker 互相抢任务。"""
    global _instance_lock_file
    if _instance_lock_file is not None:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, 'queue-manager.lock')
    lock_file = open(path, 'a+b')
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b'0')
        lock_file.flush()
    deadline = time.time() + timeout
    while True:
        try:
            lock_file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _instance_lock_file = lock_file
            return
        except OSError:
            if time.time() >= deadline:
                lock_file.close()
                raise RuntimeError(
                    "CrossPilot 同一数据目录只允许一个 Web 进程；请勿使用多个 uvicorn worker"
                )
            time.sleep(0.1)


def _release_instance_lock():
    global _instance_lock_file
    lock_file = _instance_lock_file
    _instance_lock_file = None
    if lock_file is None:
        return
    try:
        lock_file.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def start_monitor():
    global _thread
    if _thread and _thread.is_alive():
        return
    _acquire_instance_lock()
    _monitor_stop.clear()
    _draining.clear()
    recovered = store.recover_incomplete_tasks()
    if recovered:
        print(f"[jobs] 已恢复 {recovered} 个中断任务为可重试状态", flush=True)
    _thread = threading.Thread(target=_monitor, daemon=True)
    _thread.start()


def begin_drain():
    """停止接收新任务，但继续跑完当前队列。"""
    with _lock:
        _draining.set()


def cancel_drain():
    with _lock:
        _draining.clear()


def wait_until_idle(timeout=None):
    deadline = time.time() + timeout if timeout is not None else None
    while True:
        with _lock:
            idle = not _running and not _starting and not _pending
        if idle:
            return True
        if deadline is not None and time.time() >= deadline:
            return False
        time.sleep(0.25)


def stop_monitor(cancel_running=True):
    """停止队列管理器；普通关机时终止子进程并保留可重试状态。"""
    global _thread
    _draining.set()
    if cancel_running:
        with _lock:
            pending_ids = [item[0] for item in _pending]
            _pending.clear()
            running_ids = list(_running)
        for job_id in pending_ids:
            store.mark_failed(job_id, "服务已关闭，排队任务未执行，请重试")
        for job_id in running_ids:
            if cancel(job_id):
                store.mark_failed(job_id, "服务已关闭，任务已安全终止，请重试")
    _monitor_stop.set()
    if _thread and _thread.is_alive() and _thread is not threading.current_thread():
        _thread.join(timeout=6)
    _thread = None
    with _lock:
        for info in _running.values():
            err_fd = info.get('_err_fd')
            if err_fd:
                try:
                    err_fd.close()
                except OSError:
                    pass
        _running.clear()
        _starting.clear()
    _release_instance_lock()


def _detect_pipeline(input_path: str) -> str | None:
    """检测 XLSX/JSON 类型，返回 'ebay'、'amazon' 或 None。"""
    wb = None
    try:
        if input_path.lower().endswith('.json'):
            from scripts.services.amazon_json import load_columnar_json
            max_rows = max(
                1, int(os.environ.get('CROSSPILOT_MAX_ROWS', '10000'))
            )
            load_columnar_json(input_path, max_rows=max_rows)
            return 'amazon'

        import openpyxl
        from scripts.adapters import detect_adapter
        from scripts.adapters.amazon_tk import AmazonTkAdapter
        wb = openpyxl.load_workbook(input_path, data_only=True)
        adapter = detect_adapter(wb.active)
        if adapter is AmazonTkAdapter:
            return 'amazon'
        return 'ebay' if adapter else None
    except Exception as e:
        print(f"[jobs] 表格类型检测失败: {e}", file=sys.stderr, flush=True)
        return None
    finally:
        if wb is not None:
            wb.close()


def enqueue(job_id, input_path):
    """启动任务：自动识别表格格式，路由到对应管道(eBay/Amazon)。
    所有运行模式共用同一并发/排队上限。"""
    if _draining.is_set():
        store.mark_failed(job_id, "系统正在安全关闭或更新，请稍后重试")
        return False
    pipeline = _detect_pipeline(input_path)
    if pipeline is None:
        store.mark_failed(job_id, "无法识别表格格式，请使用受支持的 eBay/Amazon 模板")
        return False
    store.set_pipeline(job_id, pipeline)
    print(f"[jobs] {job_id}: 检测到 {pipeline} 管道", flush=True)

    should_start = False
    with _lock:
        if _draining.is_set():
            store.mark_failed(job_id, "系统正在安全关闭或更新，请稍后重试")
            return False
        if job_id in _running or job_id in _starting or any(item[0] == job_id for item in _pending):
            return False
        if len(_running) + len(_starting) >= MAX_WORKERS:
            if len(_pending) >= MAX_PENDING:
                store.mark_failed(job_id, "队列已满（最多排队 10 个），请稍后再试")
                return False
            _pending.append((job_id, input_path, pipeline))
            store.mark_queued(job_id)
        else:
            _starting.add(job_id)
            should_start = True

    if should_start:
        try:
            _start_task(job_id, input_path, pipeline)
        except Exception as e:
            message = f"任务启动失败: {type(e).__name__}: {e}"
            store.mark_failed(job_id, message)
            _publish(job_id, {'type': 'failed', 'data': {'error': message}})
            return False
        finally:
            with _lock:
                _starting.discard(job_id)
    return True


def _start_task(job_id, input_path, pipeline):
    try:
        _start_dev_proc(job_id, input_path, pipeline)
    finally:
        with _lock:
            _starting.discard(job_id)


_pending = []  # (job_id, input_path, pipeline) 等待队列


def _start_dev_proc(job_id, input_path, pipeline='ebay'):
    err_file = os.path.splitext(input_path)[0] + '_child_stderr.txt'
    ef = open(err_file, 'w', encoding='utf-8')
    store.mark_running(job_id)
    started_at = time.time()
    try:
        proc = subprocess.Popen(_py_cmd() + [pipeline, input_path],
                                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=ef)
    except Exception as e:
        ef.close()
        store.mark_failed(job_id, f"任务启动失败: {type(e).__name__}: {e}")
        raise
    with _lock:
        _running[job_id] = {
            'proc': proc,
            'input_path': input_path,
            '_err_fd': ef,
            'pipeline': pipeline,
            'started_at': started_at,
        }
        _starting.discard(job_id)


def is_active(job_id):
    with _lock:
        return (
            job_id in _running
            or job_id in _starting
            or any(item[0] == job_id for item in _pending)
        )


def cancel(job_id):
    """取消排队任务或终止独立子进程任务。"""
    proc = None
    err_fd = None
    with _lock:
        if job_id in _starting:
            return False
        for idx, item in enumerate(_pending):
            if item[0] == job_id:
                _pending.pop(idx)
                return True
        info = _running.get(job_id)
        if not info:
            return True
        info['cancelled'] = True
        proc = info.get('proc')
        err_fd = info.get('_err_fd')

    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if err_fd:
        try:
            err_fd.close()
        except Exception:
            pass
    return True


def is_busy():
    with _lock:
        return len(_running)


def queue_snapshot():
    with _lock:
        return {
            'running_count': len(_running) + len(_starting),
            'queue_depth': len(_pending),
            'draining': _draining.is_set(),
        }


atexit.register(_release_instance_lock)
