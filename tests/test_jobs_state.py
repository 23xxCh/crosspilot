"""jobs.py 队列状态机测试 — enqueue, cancel, drain, subscribe, snapshot。"""
import pytest, os, tempfile, threading, json, time
from unittest.mock import patch, Mock, MagicMock


@pytest.fixture(autouse=True)
def reset_jobs_state(monkeypatch, tmp_path):
    """每个测试前重置 jobs 模块的全局状态。"""
    import web.jobs as j

    # 重置全局状态
    with j._lock:
        j._running.clear()
        j._starting.clear()
        j._pending.clear()
        j._draining.clear()
        j._monitor_stop.clear()

    # 设置临时数据目录
    d = str(tmp_path)
    monkeypatch.setattr(j, 'DATA_DIR', d)
    monkeypatch.setattr(j, 'MAX_WORKERS', 2)
    monkeypatch.setattr(j, 'MAX_PENDING', 3)

    # Mock store 调用
    monkeypatch.setattr(j.store, 'mark_failed', lambda *a, **kw: None)
    monkeypatch.setattr(j.store, 'mark_queued', lambda *a, **kw: None)
    monkeypatch.setattr(j.store, 'set_pipeline', lambda *a, **kw: None)
    monkeypatch.setattr(j.store, 'mark_running', lambda *a, **kw: None)
    monkeypatch.setattr(j.store, 'mark_cancelled', lambda *a, **kw: None)
    monkeypatch.setattr(j.store, 'delete', lambda *a, **kw: None)
    monkeypatch.setattr(j.store, 'get', lambda job_id: None)

    # Mock _start_task
    monkeypatch.setattr(j, '_start_task', lambda *a, **kw: None)

    # Mock _detect_pipeline
    monkeypatch.setattr(j, '_detect_pipeline', lambda path: 'amazon')

    # Create upload dir
    os.makedirs(os.path.join(d, 'uploads'), exist_ok=True)
    yield j


class TestEnqueue:
    """入队逻辑测试。"""

    def test_enqueue_starts_immediately_when_slots_free(self, reset_jobs_state):
        j = reset_jobs_state
        input_path = os.path.join(j.DATA_DIR, 'uploads', 'test.xlsx')
        with open(input_path, 'w') as f: f.write('dummy')

        result = j.enqueue('job001', input_path)

        assert result is True
        with j._lock:
            assert 'job001' not in j._running
            assert j._pending == []

    def test_enqueue_queues_when_workers_full(self, reset_jobs_state):
        j = reset_jobs_state
        input_path = os.path.join(j.DATA_DIR, 'uploads', 'test.xlsx')
        with open(input_path, 'w') as f: f.write('dummy')

        # Fill workers
        with j._lock:
            j._running['job001'] = {}
            j._running['job002'] = {}

        result = j.enqueue('job003', input_path)

        assert result is True
        with j._lock:
            assert len(j._pending) == 1
            assert j._pending[0][0] == 'job003'

    def test_enqueue_rejects_when_queue_full(self, reset_jobs_state):
        j = reset_jobs_state
        input_path = os.path.join(j.DATA_DIR, 'uploads', 'test.xlsx')
        with open(input_path, 'w') as f: f.write('dummy')
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(j, 'MAX_WORKERS', 1)

        # Fill worker + queue
        with j._lock:
            j._running['job001'] = {}
            j._pending.extend([('job002', input_path, 'amazon'),
                               ('job003', input_path, 'amazon'),
                               ('job004', input_path, 'amazon')])

        result = j.enqueue('job005', input_path)
        assert result is False

    def test_enqueue_rejects_duplicate(self, reset_jobs_state):
        j = reset_jobs_state
        input_path = os.path.join(j.DATA_DIR, 'uploads', 'test.xlsx')
        with open(input_path, 'w') as f: f.write('dummy')

        # First enqueue succeeds and job enters _running
        # (mock _start_task doesn't add to _running, so add manually)
        j.enqueue('job001', input_path)
        with j._lock:
            j._running['job001'] = {}

        # Second enqueue of same job fails
        result = j.enqueue('job001', input_path)
        assert result is False

    def test_enqueue_rejects_during_drain(self, reset_jobs_state):
        j = reset_jobs_state
        input_path = os.path.join(j.DATA_DIR, 'uploads', 'test.xlsx')
        with open(input_path, 'w') as f: f.write('dummy')

        j._draining.set()
        result = j.enqueue('job001', input_path)
        assert result is False


class TestCancel:
    """任务取消测试。"""

    def test_cancel_removes_from_pending(self, reset_jobs_state):
        j = reset_jobs_state
        input_path = os.path.join(j.DATA_DIR, 'uploads', 'test.xlsx')

        with j._lock:
            j._pending.append(('job001', input_path, 'amazon'))

        result = j.cancel('job001')

        assert result is True
        with j._lock:
            assert j._pending == []

    def test_cancel_terminates_running(self, reset_jobs_state):
        j = reset_jobs_state

        mock_proc = Mock()
        mock_proc.poll.return_value = None  # still running

        with j._lock:
            j._running['job001'] = {'proc': mock_proc, 'input_path': '/tmp/test.xlsx'}

        result = j.cancel('job001')

        assert result is True
        mock_proc.terminate.assert_called_once()

    def test_cannot_cancel_starting(self, reset_jobs_state):
        j = reset_jobs_state

        with j._lock:
            j._starting.add('job001')

        result = j.cancel('job001')
        assert result is False


class TestActive:
    """is_active 状态检测。"""

    def test_is_active_detects_all_states(self, reset_jobs_state):
        j = reset_jobs_state
        input_path = os.path.join(j.DATA_DIR, 'uploads', 'test.xlsx')

        assert j.is_active('nobody') is False

        with j._lock:
            j._running['job001'] = {}
        assert j.is_active('job001') is True

        with j._lock:
            j._starting.add('job002')
        assert j.is_active('job002') is True

        with j._lock:
            j._pending.append(('job003', input_path, 'amazon'))
        assert j.is_active('job003') is True


class TestSnapshot:
    """queue_snapshot 状态报告。"""

    def test_snapshot_counts_correctly(self, reset_jobs_state):
        j = reset_jobs_state

        with j._lock:
            j._running['job001'] = {}
            j._running['job002'] = {}
            j._starting.add('job003')
            j._pending.append(('job004', '/tmp/x.xlsx', 'amazon'))

        snap = j.queue_snapshot()

        assert snap['running_count'] == 3  # 2 running + 1 starting
        assert snap['queue_depth'] == 1
        assert snap['draining'] is False


class TestSSE:
    """SSE 订阅/发布。"""

    def test_subscribe_and_publish(self, reset_jobs_state):
        j = reset_jobs_state

        q = j.subscribe('job001')
        j._publish('job001', {'type': 'progress', 'data': {'stage': '标题优化'}})

        import queue
        event = q.get(timeout=1)
        assert event['type'] == 'progress'
        assert event['data']['stage'] == '标题优化'

    def test_unsubscribe_stops_events(self, reset_jobs_state):
        j = reset_jobs_state

        q = j.subscribe('job001')
        j.unsubscribe('job001', q)

        j._publish('job001', {'type': 'done'})

        import queue
        with pytest.raises(queue.Empty):
            q.get(timeout=0.1)
