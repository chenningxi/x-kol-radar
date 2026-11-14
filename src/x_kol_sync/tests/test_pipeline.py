import io
import pathlib
import plistlib
import sys
import unittest
from contextlib import nullcontext, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
import run_x_kol_db as workflow
from x_kol_sync import deepseek_review


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = mock.MagicMock()
        self.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {'n': 0}
        self.status = dict(awaiting_review=0, awaiting_translation=0)

    def run_round(self, sync=None, review=None, send=None):
        calls = []
        def stage(name, outcome):
            def perform(*args, **kwargs):
                calls.append(name)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return perform
        with mock.patch.object(workflow, 'collect', side_effect=stage('sync', sync or {'profiles': []})), \
             mock.patch.object(deepseek_review, 'review_queue', side_effect=stage('review', review or {'failed': 0, 'remaining': 0})), \
             mock.patch.object(workflow, 'send', side_effect=stage('dispatch', send or {'sent': 1})), \
             mock.patch.object(workflow, 'queue_status', return_value=self.status):
            result = workflow.run_pipeline(self.conn, mock.Mock(), 'policy')
        return result, calls

    def test_stages_run_immediately_in_order(self):
        result, calls = self.run_round()
        self.assertEqual(calls, ['sync', 'review', 'dispatch'])
        self.assertTrue(result['ok'])

    def test_external_sync_failure_does_not_block_existing_database_queue(self):
        result, calls = self.run_round(sync=RuntimeError('browser unavailable'))
        self.assertEqual(calls, ['sync', 'review', 'dispatch'])
        self.assertFalse(result['ok'])
        self.conn.rollback.assert_called_once()

    def test_partial_sync_failure_still_reviews_and_sends(self):
        result, calls = self.run_round(sync={'profiles': [{'failed': 1}]})
        self.assertEqual(calls[-2:], ['review', 'dispatch'])
        self.assertFalse(result['ok'])

    def test_partial_translation_failure_still_sends_ready_rows(self):
        self.status['awaiting_translation'] = 1
        result, calls = self.run_round(review={'failed': 1, 'remaining': 1})
        self.assertEqual(calls[-1], 'dispatch')
        self.assertEqual(result['dispatch']['sent'], 1)
        self.assertFalse(result['ok'])

    def test_review_exception_does_not_block_previously_ready_rows(self):
        result, calls = self.run_round(review=RuntimeError('private credential must not appear'))
        self.assertEqual(calls[-1], 'dispatch')
        self.assertEqual(result['errors'], [{'stage': 'review', 'error_type': 'RuntimeError'}])
        self.assertFalse(result['ok'])

    def test_retries_persisted_batches_then_sends_new_ready_rows(self):
        self.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {'n': 2}
        result, calls = self.run_round()
        self.assertEqual(calls, ['sync', 'review', 'dispatch', 'dispatch', 'dispatch'])
        self.assertEqual(result['dispatch']['sent'], 3)

    def test_failed_dispatch_stops_retrying_in_the_same_round(self):
        result, calls = self.run_round(send=RuntimeError('temporary failure'))
        self.assertEqual(calls.count('dispatch'), 1)
        self.assertFalse(result['ok'])
        self.conn.rollback.assert_called_once()

    def test_standalone_sync_never_runs_review_or_send(self):
        with mock.patch.object(sys, 'argv', ['run_x_kol_db.py', 'sync']), \
             mock.patch.object(workflow.db, 'connect', return_value=self.conn), \
             mock.patch.object(workflow.db, 'task_lock', return_value=nullcontext()), \
             mock.patch.object(workflow, 'collect', return_value={'profiles': []}), \
             mock.patch.object(workflow, 'run_pipeline') as pipeline, \
             mock.patch.object(workflow, 'send') as send, redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(), 0)
        pipeline.assert_not_called()
        send.assert_not_called()

    def test_pipeline_holds_one_workflow_lock_for_the_complete_round(self):
        events = []
        class Lock:
            def __enter__(self):
                events.append('locked')
            def __exit__(self, *args):
                events.append('unlocked')
        def run(*args, **kwargs):
            self.assertEqual(events, ['locked'])
            events.append('pipeline')
            return {'ok': True}
        with mock.patch.object(sys, 'argv', ['run_x_kol_db.py', 'pipeline']), \
             mock.patch.object(workflow.db, 'connect', return_value=self.conn), \
             mock.patch.object(workflow.db, 'task_lock', return_value=Lock()) as lock, \
             mock.patch.object(deepseek_review, 'Client'), \
             mock.patch.object(workflow, 'run_pipeline', side_effect=run), redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.main(), 0)
        self.assertEqual(events, ['locked', 'pipeline', 'unlocked'])
        lock.assert_called_once_with(self.conn, 'workflow')

    def test_pipeline_template_runs_every_quarter_hour(self):
        with (ROOT / 'launchd/com.example.x-kol-pipeline.plist').open('rb') as file:
            config = plistlib.load(file)
        self.assertEqual(config['StartCalendarInterval'], [{'Minute': value} for value in (0, 15, 30, 45)])
        self.assertFalse(config['RunAtLoad'])
