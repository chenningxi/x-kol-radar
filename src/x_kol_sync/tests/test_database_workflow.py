import importlib.util
import pathlib
import sys
import unittest
from unittest import mock
from contextlib import nullcontext, redirect_stdout
import io
import json

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from x_kol_sync import database
import run_x_kol_dispatch as dispatch
import run_x_kol_db as database_workflow


class DatabaseContractTests(unittest.TestCase):
    def test_dispatch_cannot_silently_ignore_review_dry_run(self):
        with mock.patch.object(sys, 'argv', ['run_x_kol_db.py', 'dispatch', '--dry-run']), \
             mock.patch.object(database, 'connect') as connect:
            with self.assertRaises(SystemExit) as exit:
                database_workflow.main()
            self.assertEqual(exit.exception.code, 2)
        connect.assert_not_called()

    def test_database_name_must_be_explicit(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'X_KOL_MYSQL_DATABASE'):
                database.connect()

    def test_check_reports_policy_version_without_writes_or_dispatch(self):
        connection = mock.MagicMock()
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['run_x_kol_db.py', 'check']), \
             mock.patch.object(database, 'connect', return_value=connection), \
             mock.patch.object(database, 'task_lock', return_value=nullcontext()), \
             mock.patch.object(database, 'tweets', return_value=[]), \
             mock.patch.object(database, 'sent_urls', return_value=set()), \
             mock.patch.object(database_workflow, 'send') as send, redirect_stdout(output):
            self.assertEqual(database_workflow.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['content_policy_version'], dispatch.CONTENT_POLICY_VERSION)
        connection.commit.assert_not_called()
        connection.cursor.assert_not_called()
        send.assert_not_called()

    def test_jsonl_migration_never_moves_database_cursor_backwards(self):
        self.assertEqual(database_workflow.newest_cursor('200', '100'), '200')
        self.assertEqual(database_workflow.newest_cursor('100', '200'), '200')
        self.assertEqual(database_workflow.newest_cursor(None, '200'), '200')

    def test_event_identity_is_stable_across_json_key_order(self):
        self.assertEqual(database.digest({'url':'a','status':'sent'}),
                         database.digest({'status':'sent','url':'a'}))

    def test_translated_author_does_not_bypass_untranslated_quote(self):
        row = dict(url='https://x.com/example/status/1', translation_status='translated',
                   content_classification='substantive', content_policy_version=dispatch.CONTENT_POLICY_VERSION,
                   quoted_tweet={'text':'foreign quote', 'translation_status':'pending'})
        send, skipped, review, translation = dispatch.partition_dispatch_rows([row], set())
        self.assertEqual(send, [])
        self.assertEqual(translation, [row])

    def test_imported_receipt_blocks_resending(self):
        row = dict(url='https://x.com/example/status/1', translation_status='translated',
                   content_classification='substantive', content_policy_version=dispatch.CONTENT_POLICY_VERSION)
        self.assertEqual(dispatch.partition_dispatch_rows([row], {row['url']}), ([], [], [], []))
