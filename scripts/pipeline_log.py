"""结构化日志 + 请求追踪 + 指标。
用法:
  from scripts.pipeline_log import log, new_request_id, PipelineMetrics
  rid = new_request_id()
  log.info("开始", request_id=rid)
  metrics = PipelineMetrics()
  metrics.record_stage("review", 12.5, 100)
"""
import sys, os, time, json, uuid, traceback as _tb


_LOG_DIR = os.path.join(os.environ.get('CROSSPILOT_DATA_DIR',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')))
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_PATH = os.path.join(_LOG_DIR, 'server.log')

# Thread-local request ID
import threading as _threading
_rid_local = _threading.local()

def new_request_id():
    """Generate a new request ID for this pipeline run (short UUID)."""
    rid = uuid.uuid4().hex[:12]
    _rid_local.id = rid
    return rid

def _get_request_id():
    return getattr(_rid_local, 'id', '-')


class Logger:
    def __init__(self):
        self._file = sys.stderr
        self._logfile = _LOG_PATH

    def _emit(self, level, msg, **kwargs):
        entry = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'level': level,
            'msg': msg,
            'request_id': kwargs.pop('request_id', _get_request_id()),
        }
        if kwargs:
            entry['context'] = {k: str(v)[:200] for k, v in kwargs.items()}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        print(line, file=self._file, flush=True)
        try:
            with open(self._logfile, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def info(self, msg, **kwargs):
        self._emit('INFO', msg, **kwargs)

    def warn(self, msg, **kwargs):
        self._emit('WARN', msg, **kwargs)

    def error(self, msg, exc_info=False, **kwargs):
        if exc_info:
            kwargs['traceback'] = _tb.format_exc()[-500:]
        self._emit('ERROR', msg, **kwargs)


log = Logger()


class PipelineMetrics:
    """Track stage durations, API calls, success rates for a pipeline run."""

    def __init__(self):
        self.stages = {}       # stage_name -> {duration_s, item_count, success_count}
        self.api_calls = 0
        self.api_errors = 0
        self.t_start = time.time()

    def record_stage(self, name, duration_s, item_count, success_count=None):
        self.stages[name] = {
            'duration_s': round(duration_s, 1),
            'items': item_count,
            'success': success_count if success_count is not None else item_count,
        }

    def record_api(self, success=True):
        self.api_calls += 1
        if not success:
            self.api_errors += 1

    def to_dict(self):
        return {
            'total_elapsed_s': round(time.time() - self.t_start, 1),
            'stages': self.stages,
            'api_calls': self.api_calls,
            'api_errors': self.api_errors,
            'api_success_rate': round(
                1 - self.api_errors / max(self.api_calls, 1), 3),
        }