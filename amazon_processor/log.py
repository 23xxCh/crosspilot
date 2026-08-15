"""结构化日志 + 请求追踪 + 指标。
用法:
  from amazon_processor.log import log, new_request_id, PipelineMetrics
  rid = new_request_id()
  log.info("开始", request_id=rid)
  metrics = PipelineMetrics()
  metrics.record_stage("review", 12.5, 100)
"""
import sys, os, time, json, uuid, traceback as _tb


_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '.runtime',
    'logs',
)
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_PATH = os.path.join(_LOG_DIR, 'server.log')
_MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_LOG_FILES = 3

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

    def _rotate(self):
        """日志超过 _MAX_LOG_SIZE 时轮转，保留最近 _MAX_LOG_FILES 个。"""
        try:
            if os.path.exists(self._logfile) and os.path.getsize(self._logfile) > _MAX_LOG_SIZE:
                for i in range(_MAX_LOG_FILES - 1, 0, -1):
                    src = f'{self._logfile}.{i}' if i > 1 else self._logfile
                    dst = f'{self._logfile}.{i + 1}'
                    if os.path.exists(src):
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.rename(src, dst)
                # 重命名当前文件
                os.rename(self._logfile, f'{self._logfile}.1')
        except OSError:
            pass

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
            self._rotate()
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
        self.api_latency_s = 0.0
        self.api_by_operation = {}
        self.http_attempts = 0
        self.http_errors = 0
        self.http_retries = 0
        self.rate_wait_s = 0.0
        self.http_status = {}
        self.circuit_open = 0
        self.fallback_attempts = 0
        self.fallback_successes = 0
        self.fallback_failures = 0
        self.fallback_routes = {}
        self.cache = {}
        self.concurrency = {}
        self.quality = {}
        self.image_remediation = {}
        self.t_start = time.time()

    def record_stage(self, name, duration_s, item_count, success_count=None):
        success = success_count if success_count is not None else item_count
        self.stages[name] = {
            'duration_s': round(duration_s, 1),
            'items': item_count,
            'success': success,
            'items_per_s': round(item_count / max(duration_s, 0.001), 1),
            'success_rate': (
                round(success / item_count, 3) if item_count else None
            ),
        }

    def record_api(self, success=True):
        self.api_calls += 1
        if not success:
            self.api_errors += 1

    def set_provider_metrics(self, metrics):
        """Attach a provider snapshot collected by CompositeProvider."""
        if not isinstance(metrics, dict):
            return
        self.api_calls = int(metrics.get('api_calls') or 0)
        self.api_errors = int(metrics.get('api_errors') or 0)
        self.api_latency_s = float(metrics.get('latency_s') or 0)
        self.api_by_operation = metrics.get('by_operation') or {}
        self.http_attempts = int(metrics.get('http_attempts') or 0)
        self.http_errors = int(metrics.get('http_errors') or 0)
        self.http_retries = int(metrics.get('http_retries') or 0)
        self.rate_wait_s = float(metrics.get('rate_wait_s') or 0)
        self.http_status = metrics.get('http_status') or {}
        self.circuit_open = int(metrics.get('circuit_open') or 0)
        self.fallback_attempts = int(
            metrics.get('fallback_attempts') or 0
        )
        self.fallback_successes = int(
            metrics.get('fallback_successes') or 0
        )
        self.fallback_failures = int(
            metrics.get('fallback_failures') or 0
        )
        self.fallback_routes = {
            str(route): dict(values)
            for route, values in (
                metrics.get('fallback_routes') or {}
            ).items()
            if isinstance(values, dict)
        }

    def set_cache_metrics(self, metrics):
        """Attach cache hit/miss evidence collected by pipeline stages."""
        if not isinstance(metrics, dict):
            return
        normalized = {}
        total_hits = total_misses = 0
        for name, values in metrics.items():
            if not isinstance(values, dict):
                continue
            hits = int(values.get('hits') or 0)
            misses = int(values.get('misses') or 0)
            total = hits + misses
            total_hits += hits
            total_misses += misses
            normalized[name] = {
                'hits': hits,
                'misses': misses,
                'hit_rate': round(hits / total, 3) if total else None,
            }
        total = total_hits + total_misses
        self.cache = {
            'hits': total_hits,
            'misses': total_misses,
            'hit_rate': round(total_hits / total, 3) if total else None,
            'by_stage': normalized,
        }

    def set_concurrency_metrics(self, metrics):
        """Attach adaptive concurrency/backoff evidence."""
        if not isinstance(metrics, dict):
            return
        normalized = {}
        reductions = recoveries = failures = 0
        for name, values in metrics.items():
            if not isinstance(values, dict):
                continue
            item = {
                'items': int(values.get('items') or 0),
                'attempted_items': int(values.get('attempted_items') or values.get('items') or 0),
                'attempts': int(values.get('attempts') or 1),
                'initial_workers': int(values.get('initial_workers') or 0),
                'final_workers': int(values.get('final_workers') or 0),
                'min_workers': int(values.get('min_workers') or 0),
                'reductions': int(values.get('reductions') or 0),
                'recoveries': int(values.get('recoveries') or 0),
                'failures': int(values.get('failures') or 0),
                'backoff_total_s': float(values.get('backoff_total_s') or 0),
                'events': list(values.get('events') or [])[:10],
            }
            reductions += item['reductions']
            recoveries += item['recoveries']
            failures += item['failures']
            normalized[name] = item
        self.concurrency = {
            'reductions': reductions,
            'recoveries': recoveries,
            'failures': failures,
            'by_operation': normalized,
        }

    def set_quality_metrics(self, validation):
        """Attach final validation evidence for the UI metrics card."""
        if not isinstance(validation, dict):
            return
        issues = validation.get('issues') or []
        self.quality = {
            'passed': bool(validation.get('passed')),
            'issue_count': len(issues) if isinstance(issues, list) else 0,
            'truncated': bool(validation.get('truncated')),
        }

    def set_image_remediation_metrics(self, metrics):
        """Attach pre-generation routing evidence."""
        if not isinstance(metrics, dict):
            return
        self.image_remediation = {
            key: int(metrics.get(key) or 0)
            for key in (
                'reviewed',
                'flagged',
                'clean_retained',
                'unknown_retained',
                'attachment_reviewed',
                'attachment_flagged',
                'attachment_deleted',
                'generated_main',
                'generated_variant',
                'failed_main',
                'failed_variant',
                'generation_url_checked',
                'generation_url_valid',
                'generation_url_invalid',
            )
        }

    def to_dict(self):
        return {
            'total_elapsed_s': round(time.time() - self.t_start, 1),
            'stages': self.stages,
            'api_calls': self.api_calls,
            'api_errors': self.api_errors,
            'api_success_rate': (
                round(1 - self.api_errors / self.api_calls, 3)
                if self.api_calls else None
            ),
            'api_latency_s': round(self.api_latency_s, 3),
            'api_by_operation': self.api_by_operation,
            'http_attempts': self.http_attempts,
            'http_errors': self.http_errors,
            'http_retries': self.http_retries,
            'rate_wait_s': round(self.rate_wait_s, 3),
            'http_status': self.http_status,
            'circuit_open': self.circuit_open,
            'fallback_attempts': self.fallback_attempts,
            'fallback_successes': self.fallback_successes,
            'fallback_failures': self.fallback_failures,
            'fallback_routes': self.fallback_routes,
            'cache': self.cache,
            'concurrency': self.concurrency,
            'quality': self.quality,
            'image_remediation': self.image_remediation,
        }
