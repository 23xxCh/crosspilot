"""后台任务队列：每任务一个独立 python 子进程跑 process_ebay_tk._main。

为什么多进程不用多线程：_DASHBOARD_HOOK 是模块级全局，多线程下多任务互相覆盖。
每任务独立子进程，全局各管各的，真并发。

实现用 subprocess.Popen 调独立 runner 脚本（不用 multiprocessing.spawn——spawn 要
pickle target 函数，子进程重新 import 整个 jobs 模块，在 uvicorn 下容易静默失败）。
"""
import os, sys, json, threading, queue as _q, time, subprocess, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from web import store  # noqa: E402

# ponytail: 并发数。开太多撞 DMXAPI 限流（图审并发100是单任务内的事）
MAX_WORKERS = min(os.cpu_count() or 2, 4)

# job_id -> {'proc': Popen, 'input_path': str}
_running = {}
_lock = threading.Lock()

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
    cache = os.path.splitext(input_path)[0] + '_cache.json'
    try:
        with open(cache, encoding='utf-8') as f:
            c = json.load(f)
        rev = c.get('review_results', {})
        gen = c.get('gen_results', {})
        return {
            'images_reviewed': len(rev),
            'watermarks': sum(1 for v in rev.values() if v is True),
            'images_generated': len(gen),
        }
    except Exception as e:
        print(f"[WARN] cache read failed: {e}", file=sys.stderr)
        return {}


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


def _find_output(input_path):
    """查找管道输出，支持标准命名和时间戳防覆盖命名（_cleaned_HHMMSS.xlsx）。"""
    base, ext = os.path.splitext(input_path)
    # 标准命名
    std = base + '_cleaned.xlsx'
    if os.path.exists(std):
        return std
    # 时间戳防覆盖命名（取最新）
    pattern = base + '_cleaned_*' + ext
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


# ===== 监控线程：轮询所有在跑子进程，推进度 + 探活 =====
def _py_cmd():
    """Dev 模式：uv run python runner.py"""
    return ['uv', 'run', 'python', '-u', os.path.join(ROOT, 'web', 'runner.py')]

def _monitor():
    last_snap = {}
    while True:
        done_ids = []
        with _lock:
            items = list(_running.items())
        for job_id, info in items:
            # frozen 模式：检查线程存活
            if info.get('frozen'):
                alive = info['thread'].is_alive()
                if not alive:
                    # 线程自己调了 mark_done/mark_failed，这里只清理 _running
                    done_ids.append(job_id)
                    last_snap.pop(job_id, None)
                else:
                    st = read_status(info['input_path'])
                    if st and st != last_snap.get(job_id):
                        last_snap[job_id] = st
                        store.update_progress(job_id, st)
                        _publish(job_id, {'type': 'progress', 'data': st})
                continue

            # dev 模式：poll 子进程
            st = read_status(info['input_path'])
            prev = last_snap.get(job_id)
            if st and st != prev:
                last_snap[job_id] = st
                store.update_progress(job_id, st)
                _publish(job_id, {'type': 'progress', 'data': st})
            rc = info['proc'].poll()
            if rc is not None:
                out = _find_output(info['input_path'])
                if rc == 0:
                    if out:
                        stats = _read_stats(info['input_path'])
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
        if done_ids:
            with _lock:
                for jid in done_ids:
                    _running.pop(jid, None)
                # 从等待队列补位
                while _pending and len(_running) < MAX_WORKERS:
                    next_job = _pending.pop(0)
                    _start_dev_proc(*next_job)
        time.sleep(1)


_thread = None


def start_monitor():
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_monitor, daemon=True)
    _thread.start()


def enqueue(job_id, input_path):
    """启动任务：PyInstaller exe 模式用线程(process隔离不了- exe 重入会启动完整服务器),
    开发模式用子进程(uv run runner.py 保持隔离). 管道是同步的, 线程足够."""
    if getattr(sys, 'frozen', False):
        import threading
        def _run():
            import process_ebay_tk as p
            try:
                out = p._main(input_path)  # 捕获返回的真实输出路径
                if not out or not os.path.exists(out):
                    out = _find_output(input_path)  # fallback
                stats = _read_stats(input_path)
                store.mark_done(job_id, out, stats)
                _publish(job_id, {'type': 'done', 'data': {'output': out, 'stats': stats}})
            except SystemExit as se:
                # _main 在缺 key/错误率高时 sys.exit(1)
                store.mark_failed(job_id, f"SystemExit: {se}")
                _publish(job_id, {'type': 'failed', 'data': {'error': str(se)}})
            except Exception as e:
                store.mark_failed(job_id, f"{type(e).__name__}: {e}")
                _publish(job_id, {'type': 'failed', 'data': {'error': str(e)}})
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        # 记录线程引用，monitor 通过 is_alive 探活
        with _lock:
            _running[job_id] = {'thread': t, 'input_path': input_path, 'frozen': True}
        return

    # Dev mode: 有槽位直接起，没槽位入等待队列
    with _lock:
        if len(_running) >= MAX_WORKERS:
            _pending.append((job_id, input_path))
            store.update_progress(job_id, {'stage': '排队等待', 'percent': 0})
            return
    _start_dev_proc(job_id, input_path)


_pending = []  # (job_id, input_path) 等待队列


def _start_dev_proc(job_id, input_path):
    err_file = os.path.splitext(input_path)[0] + '_child_stderr.txt'
    ef = open(err_file, 'w')
    proc = subprocess.Popen(_py_cmd() + [input_path],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=ef)
    with _lock:
        _running[job_id] = {'proc': proc, 'input_path': input_path, '_err_fd': ef}


def is_busy():
    with _lock:
        return len(_running)
