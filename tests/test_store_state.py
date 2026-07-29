"""store.py 状态转换测试 — queued → running → done/failed/cancelled。"""
import pytest, os, tempfile, json


@pytest.fixture(scope='module')
def store_data_dir(tmp_path_factory):
    """所有 store 测试共享同一个临时目录（避免 Windows SQLite 锁问题）。"""
    d = tmp_path_factory.mktemp('crosspilot_store_test')
    os.environ['CROSSPILOT_DATA_DIR'] = str(d)
    import web.store as store_module
    store_module._DATA_DIR = str(d)
    store_module.DB_PATH = os.path.join(str(d), 'tasks.db')
    store_module._local = __import__('threading').local()
    store_module._init_db()
    return d


class TestStoreStateTransitions:
    """状态转换合法性验证。"""

    def test_create_sets_queued(self, store_data_dir):
        from web import store
        job_id = 'st_create'
        input_path = os.path.join(str(store_data_dir), 'test.xlsx')
        with open(input_path, 'w') as f:
            f.write('dummy')

        store.create(job_id, 'test.xlsx', input_path, pipeline='amazon')
        task = store.get(job_id)

        assert task is not None
        assert task['status'] == 'queued'
        assert task['pipeline'] == 'amazon'

    def test_mark_running_updates_status(self, store_data_dir):
        from web import store
        store.create('st_run', 'test.xlsx', '/tmp/test.xlsx')
        store.mark_running('st_run', 'title-opt')

        task = store.get('st_run')
        assert task['status'] == 'running'
        assert task['stage'] == 'title-opt'

    def test_mark_done_persists_output(self, store_data_dir):
        from web import store
        store.create('st_done', 'test.xlsx', '/tmp/test.xlsx')
        store.mark_done('st_done', '/tmp/output.xlsx')

        task = store.get('st_done')
        assert task['status'] == 'done'
        assert task['output_path'] == '/tmp/output.xlsx'
        assert task['percent'] == 100

    def test_mark_needs_review_persists_output(self, store_data_dir):
        from web import store
        store.create('st_review', 'test.xlsx', '/tmp/test.xlsx')
        store.mark_needs_review('st_review', '/tmp/output.xlsx',
                                message='quality issues')

        task = store.get('st_review')
        assert task['status'] == 'needs_review'
        assert task['output_path'] == '/tmp/output.xlsx'

    def test_mark_failed_stores_error(self, store_data_dir):
        from web import store
        store.create('st_fail', 'test.xlsx', '/tmp/test.xlsx')
        store.mark_failed('st_fail', 'API key invalid')

        task = store.get('st_fail')
        assert task['status'] == 'failed'
        assert 'API key invalid' in (task['error'] or '')

    def test_mark_cancelled_stops_task(self, store_data_dir):
        from web import store
        store.create('st_cancel', 'test.xlsx', '/tmp/test.xlsx')
        store.mark_cancelled('st_cancel')

        task = store.get('st_cancel')
        assert task['status'] == 'cancelled'

    def test_active_only_prevents_overwrite(self, store_data_dir):
        from web import store
        store.create('st_active', 'test.xlsx', '/tmp/test.xlsx')
        store.mark_done('st_active', '/tmp/output.xlsx')

        store._set('st_active', active_only=True, status='cancelled')
        task = store.get('st_active')
        assert task['status'] == 'done'

    def test_late_progress_cannot_overwrite_done(self, store_data_dir):
        from web import store
        store.create('st_late', 'test.xlsx', '/tmp/test.xlsx')
        store.mark_done('st_late', '/tmp/output.xlsx')

        store.update_progress('st_late', {
            'status': 'running', 'stage': 'titles', 'percent': 50,
        })
        task = store.get('st_late')
        assert task['status'] == 'done'
        assert task['percent'] == 100


class TestStoreConcurrency:
    def test_concurrent_creates_no_data_loss(self, store_data_dir):
        from web import store
        import threading

        errors = []
        ids = set()

        def create_one(i):
            try:
                job_id = f'conc_{i:04d}'
                store.create(job_id, f'test_{i}.xlsx', f'/tmp/test_{i}.xlsx')
                ids.add(job_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_one, args=(i,))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f'Create errors: {errors}'
        for job_id in ids:
            task = store.get(job_id)
            assert task is not None, f'{job_id} not found'
            assert task['status'] == 'queued'
