import subprocess

from scripts.pipeline_log import PipelineMetrics
from web import jobs, store
from web.review_report import (
    build_quality_score,
    build_review_data,
    build_review_report_csv,
)


def test_pipeline_metrics_includes_quality_summary():
    metrics = PipelineMetrics()
    metrics.set_quality_metrics({
        'passed': False,
        'issues': ['Bullet 重复', '关键词不足'],
        'truncated': True,
    })

    payload = metrics.to_dict()

    assert payload['quality']['passed'] is False
    assert payload['quality']['issue_count'] == 2
    assert payload['quality']['truncated'] is True


def test_pipeline_metrics_includes_cache_and_provider_attempts():
    metrics = PipelineMetrics()
    metrics.set_provider_metrics({
        'api_calls': 2,
        'api_errors': 1,
        'latency_s': 3.5,
        'http_attempts': 4,
        'http_errors': 2,
        'http_retries': 2,
        'http_status': {'429': 1, '503': 1, '200': 2},
        'circuit_open': 1,
        'by_operation': {},
    })
    metrics.set_cache_metrics({
        'title_translations': {'hits': 3, 'misses': 1},
        'desc_cleaned': {'hits': 1, 'misses': 1},
    })

    payload = metrics.to_dict()

    assert payload['http_attempts'] == 4
    assert payload['http_retries'] == 2
    assert payload['circuit_open'] == 1
    assert payload['cache']['hits'] == 4
    assert payload['cache']['misses'] == 2
    assert payload['cache']['hit_rate'] == 0.667


def test_pipeline_metrics_includes_concurrency_backoff():
    metrics = PipelineMetrics()
    metrics.set_concurrency_metrics({
        'review': {
            'initial_workers': 100,
            'final_workers': 25,
            'reductions': 2,
            'recoveries': 0,
            'failures': 30,
            'events': [{'reason': 'failure_rate'}],
        },
        'image_gen': {
            'initial_workers': 15,
            'final_workers': 15,
            'reductions': 0,
            'recoveries': 0,
            'failures': 0,
            'events': [],
        },
    })

    payload = metrics.to_dict()

    assert payload['concurrency']['reductions'] == 2
    assert payload['concurrency']['by_operation']['review']['final_workers'] == 25


def test_review_report_csv_extracts_issues_and_metrics():
    report = build_review_report_csv({
        'id': 'abc123',
        'filename': 'input.xlsx',
        'pipeline': 'amazon',
        'status': 'needs_review',
        'error': '输出存在质量问题',
        'stats': {
            'validation': {
                'passed': False,
                'issues': [
                    '第 3 行 Bullet 存在重复内容',
                    '第 4 行关键词需为 10 个有效搜索词',
                ],
            },
            'metrics': {
                'api_calls': 5,
                'api_success_rate': 0.8,
                'http_attempts': 7,
                'http_retries': 2,
                'circuit_open': 1,
                'quality': {'issue_count': 2},
                'cache': {'hit_rate': 0.5},
                'concurrency': {
                    'reductions': 1,
                    'by_operation': {
                        'amazon_review': {
                            'initial_workers': 100,
                            'final_workers': 50,
                            'reductions': 1,
                            'failures': 12,
                        },
                    },
                },
            },
        },
    })

    assert 'section,type,severity,row,field,message,value,action' in report
    assert 'validation,issue,review,3,Bullet' in report
    assert 'validation,issue,review,4,keywords' in report
    assert 'metrics,metric,info,,api_success_rate' in report
    assert 'concurrency,operation,warning,,amazon_review' in report


def test_review_data_includes_pipeline_audit_rows():
    data = build_review_data({
        'id': 'abc123',
        'filename': 'input.xlsx',
        'pipeline': 'amazon',
        'status': 'needs_review',
        'error': '输出存在质量问题',
        'stats': {
            'validation': {
                'passed': False,
                'issues': [],
                'audit': [{
                    'row': 1,
                    'stage': '标题优化',
                    'field': 'title',
                    'method': 'rule',
                    'reason': 'normalize_title',
                    'before': 'BMW Floor Mat',
                    'after': 'For BMW Floor Mat',
                }],
            },
            'metrics': {
                'quality': {'issue_count': 0},
                'concurrency': {'by_operation': {}},
            },
        },
    })

    audit = next(item for item in data['items'] if item['section'] == 'audit')
    assert data['summary']['audit_item_count'] == 1
    assert audit['type'] == 'change'
    assert audit['row'] == '1'
    assert audit['field'] == 'title'
    assert '标题优化' in audit['message']
    assert '原始: BMW Floor Mat' in audit['value']


def test_quality_score_combines_quality_and_stability_signals():
    score = build_quality_score({
        'status': 'needs_review',
        'stats': {
            'validation': {
                'passed': False,
                'issues': ['第 1 行 Bullet 存在重复内容'],
                'audit': [{
                    'row': 1,
                    'stage': 'Bullet+关键词',
                    'field': 'Bullet',
                    'method': 'fallback',
                    'severity': 'warning',
                }],
            },
            'metrics': {
                'api_success_rate': 0.8,
                'http_retries': 2,
                'circuit_open': 1,
                'quality': {'issue_count': 0},
                'concurrency': {'reductions': 1, 'by_operation': {}},
            },
        },
    })

    assert score['score'] < 75
    assert score['grade'] in {'review', 'critical'}
    assert any(reason['code'] == 'validation_issues' for reason in score['reasons'])
    assert any(reason['code'] == 'http_retries' for reason in score['reasons'])
    assert any(reason['code'] == 'audit_warnings' for reason in score['reasons'])


def test_quality_score_marks_clean_done_task_as_usable():
    score = build_quality_score({
        'status': 'done',
        'stats': {
            'validation': {'passed': True, 'issues': []},
            'metrics': {
                'api_success_rate': 1.0,
                'http_retries': 0,
                'circuit_open': 0,
                'quality': {'issue_count': 0},
                'concurrency': {'reductions': 0, 'by_operation': {}},
            },
        },
    })

    assert score['score'] == 100
    assert score['grade'] == 'pass'


def test_read_stats_includes_pipeline_metrics(monkeypatch):
    monkeypatch.setattr(jobs, 'read_cache', lambda _path: {
        'review_results': {'a': True, 'b': False},
        'gen_results': {'a': 'generated-a'},
    })
    monkeypatch.setattr(jobs, 'read_status', lambda _path: {
        'metrics': {
            'api_calls': 2,
            'api_errors': 0,
            'api_success_rate': 1.0,
            'stages': {},
        },
    })

    stats = jobs._read_stats('input.xlsx')

    assert stats['images_reviewed'] == 2
    assert stats['metrics']['api_calls'] == 2


def test_enqueue_rejected_while_draining(tmp_path, monkeypatch):
    source = tmp_path / 'input.xlsx'
    source.write_bytes(b'placeholder')
    store.create('draining-job', 'input.xlsx', str(source))
    monkeypatch.setattr(jobs, '_detect_pipeline', lambda _path: 'ebay')

    jobs.begin_drain()
    try:
        assert jobs.enqueue('draining-job', str(source)) is False
        assert store.get('draining-job')['status'] == 'failed'
    finally:
        jobs.cancel_drain()
        store.delete('draining-job')


def test_cancel_kills_process_that_ignores_terminate():
    class StubbornProcess:
        def __init__(self):
            self.terminated = False
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired('worker', timeout)
            return -9

        def kill(self):
            self.killed = True

    process = StubbornProcess()
    with jobs._lock:
        jobs._running['cancel-job'] = {
            'proc': process,
            'input_path': 'unused.xlsx',
            '_err_fd': None,
        }
    try:
        assert jobs.cancel('cancel-job') is True
        assert process.terminated is True
        assert process.killed is True
    finally:
        with jobs._lock:
            jobs._running.pop('cancel-job', None)


def test_frozen_worker_command_uses_current_executable(monkeypatch):
    monkeypatch.setattr(jobs.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(jobs.sys, 'executable', r'C:\CrossPilot\CrossPilot.exe')

    assert jobs._py_cmd() == [r'C:\CrossPilot\CrossPilot.exe', '--run-job']


def test_monitor_preserves_needs_review_terminal_state(tmp_path, monkeypatch):
    class FinishedProcess:
        def poll(self):
            return 0

    source = tmp_path / 'amazon.xlsx'
    output = tmp_path / 'amazon_回填.xlsx'
    source.write_bytes(b'input')
    output.write_bytes(b'output')
    store.create('review-job', source.name, str(source), pipeline='amazon')
    store.mark_running('review-job')
    monkeypatch.setattr(
        jobs,
        'read_status',
        lambda _path: {
            'status': 'needs_review',
            'stage': '待人工复核',
            'output': str(output),
            'validation': {
                'passed': False,
                'issues': ['Bullet 不足 5 条'],
            },
        },
    )
    monkeypatch.setattr(jobs, '_find_output', lambda *_args, **_kwargs: str(output))
    monkeypatch.setattr(jobs, '_read_stats', lambda _path: {})
    with jobs._lock:
        jobs._running['review-job'] = {
            'proc': FinishedProcess(),
            'input_path': str(source),
            '_err_fd': None,
            'started_at': 0,
        }

    try:
        jobs._monitor_tick({})
        task = store.get('review-job')
        assert task['status'] == 'needs_review'
        assert task['output_path'] == str(output)
        assert task['stats']['validation']['passed'] is False
    finally:
        with jobs._lock:
            jobs._running.pop('review-job', None)
        store.delete('review-job')
