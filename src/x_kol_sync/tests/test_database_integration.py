"""Opt-in integration tests: all application tables are connection-local temporary tables."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from x_kol_sync import database as db
from x_kol_sync import deepseek_review
import run_x_kol_db as workflow


@unittest.skipUnless(os.getenv('X_KOL_RUN_DB_TESTS') == '1', 'Set X_KOL_RUN_DB_TESTS=1 for MySQL/Redis tests')
class DatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        schema = (ROOT / 'db/schema.sql').read_text(encoding='utf-8')
        for statement in schema.split(';'):
            if statement.strip():
                with self.conn.cursor() as cursor:
                    cursor.execute(statement.replace('CREATE TABLE IF NOT EXISTS', 'CREATE TEMPORARY TABLE'))
        self.conn.commit()

    def row(self, tweet_id='1'):
        return dict(id=tweet_id, url=f'https://x.com/example/status/{tweet_id}',
                    profile='https://x.com/example', author='example', text='An important update',
                    translation_status='translated', translation_zh='重要更新',
                    content_policy_version=workflow.dispatch.CONTENT_POLICY_VERSION,
                    content_classification='substantive', unknown={'preserved': True})

    def apply(self, updates):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'updates.jsonl'
            path.write_text('\n'.join(db.encode(row) for row in updates), encoding='utf-8')
            return workflow.apply_reviews(self.conn, path)

    def test_insert_deduplicates_and_receipt_prevents_resend(self):
        row = self.row()
        self.assertTrue(db.insert_tweet(self.conn, row))
        self.assertFalse(db.insert_tweet(self.conn, row))
        db.record(self.conn, 'xkol_receipts', {'url': row['url'], 'status': 'sent'})
        self.conn.commit()
        self.assertEqual(len(db.tweets(self.conn)), 1)
        self.assertEqual(workflow.queues(self.conn), ([], [], [], []))

    def test_review_preserves_original_unknown_fields_and_quote(self):
        row = self.row()
        row['quoted_tweet'] = dict(id='99', text='Source text', unknown='keep')
        db.insert_tweet(self.conn, row)
        self.conn.commit()
        self.apply([dict(id='1', revision=1, text='must not overwrite', translation_zh='新的译文',
                         quoted_tweet=dict(id='99', text='must not overwrite',
                                           translation_status='translated', translation_zh='引用译文'))])
        saved, revision = db.tweets(self.conn)[0]
        self.assertEqual(revision, 2)
        self.assertEqual(saved['text'], row['text'])
        self.assertEqual(saved['unknown'], row['unknown'])
        self.assertEqual(saved['quoted_tweet']['text'], 'Source text')
        self.assertEqual(saved['quoted_tweet']['unknown'], 'keep')
        self.assertEqual(saved['quoted_tweet']['translation_zh'], '引用译文')

    def test_revision_conflict_rolls_back_whole_batch(self):
        for tweet_id in ('1', '2'):
            db.insert_tweet(self.conn, self.row(tweet_id))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'Revision conflict'):
            try:
                self.apply([dict(id='1', revision=1, translation_zh='changed'),
                            dict(id='2', revision=999, translation_zh='conflict')])
            except ValueError:
                self.conn.rollback()
                raise
        self.assertTrue(all(revision == 1 and row['translation_zh'] == '重要更新'
                            for row, revision in db.tweets(self.conn)))

    def test_failed_send_retries_persisted_batch_and_idempotency_key(self):
        row = self.row()
        db.insert_tweet(self.conn, row)
        self.conn.commit()
        with mock.patch.dict(os.environ, X_KOL_FEISHU_PROFILE='test-profile',
                             X_KOL_FEISHU_CHAT_ID='oc_test_chat_id'), mock.patch('subprocess.run') as run:
            run.return_value = mock.Mock(returncode=1, stdout='{"ok":false}', stderr='test failure')
            with self.assertRaisesRegex(RuntimeError, 'batch retained'):
                workflow.send(self.conn)
            first_command = run.call_args.args[0]
            self.assertEqual(db.sent_urls(self.conn), set())
            self.conn.rollback()
            run.return_value = mock.Mock(returncode=0, stdout=json.dumps(dict(ok=True, data=dict(message_id='test-message'))), stderr='')
            self.assertEqual(workflow.send(self.conn), {'sent': 1})
            self.assertEqual(run.call_args.args[0], first_command)
            self.assertEqual(db.sent_urls(self.conn), {row['url']})
            workflow.send(self.conn)
            self.assertEqual(run.call_count, 2)

    def test_dispatch_reports_skipped_rows_alongside_successful_sends(self):
        db.insert_tweet(self.conn, self.row('1'))
        filtered = self.row('2')
        filtered['content_classification'] = 'low_signal'
        db.insert_tweet(self.conn, filtered)
        self.conn.commit()
        with mock.patch.dict(os.environ, X_KOL_FEISHU_PROFILE='test-profile', X_KOL_FEISHU_CHAT_ID='oc_test_chat_id'), \
             mock.patch('subprocess.run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"ok":true,"data":{"message_id":"test"}}', stderr='')
            self.assertEqual(workflow.send(self.conn), {'sent': 1, 'skipped': 1})
        self.assertEqual(db.sent_urls(self.conn), {self.row('1')['url'], self.row('2')['url']})

    def test_redis_and_mysql_lock_reject_reentry_and_release(self):
        with mock.patch.dict(os.environ, X_KOL_REDIS_PREFIX='xkol:test:' + uuid.uuid4().hex):
            with db.task_lock(self.conn, 'workflow'):
                with self.assertRaises(RuntimeError):
                    with db.task_lock(self.conn, 'workflow'):
                        self.fail('Nested task acquired the same lock')
            with db.task_lock(self.conn, 'workflow'):
                pass

    def test_api_review_uses_revision_checks_and_skips_terminal_records(self):
        for tweet_id in ('1', '2'):
            value = self.row(tweet_id)
            value.pop('content_policy_version')
            value['translation_status'] = 'pending'
            db.insert_tweet(self.conn, value)
        db.record(self.conn, 'xkol_receipts', dict(url=self.row('2')['url'], status='sent'))
        self.conn.commit()
        client = mock.Mock()
        dry = deepseek_review.review_queue(self.conn, None, '', workflow.dispatch,
                                          workflow.apply_review_updates, dry_run=True)
        self.assertEqual(dry['pending'], 1)
        self.assertEqual(db.tweets(self.conn)[0][1], 1)
        # Use an unready source so the validator requires a fresh translation.
        source = self.row()
        source['translation_status'] = 'pending'
        client.review.return_value = deepseek_review.validated_update(
            source, 1, dict(id='1', content_classification='substantive', content_reason='重要新进展',
                            translation_status='translated', source_language='en', translation_zh='译文'), 'v2')
        result = deepseek_review.review_queue(self.conn, client, '', workflow.dispatch,
                                             workflow.apply_review_updates)
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(client.review.call_count, 1)
        saved = {row['id']: (row, revision) for row, revision in db.tweets(self.conn)}
        self.assertEqual(saved['1'][1], 2)
        self.assertEqual(saved['1'][0]['unknown'], {'preserved': True})
        self.assertEqual(saved['2'][1], 1)

    def test_pipeline_delivers_successful_rows_while_failed_rows_wait_and_do_not_duplicate(self):
        def collect(conn):
            for tweet_id in ('1', '2'):
                value = self.row(tweet_id)
                value.pop('content_policy_version')
                value['translation_status'] = 'pending'
                db.insert_tweet(conn, value)
            conn.commit()
            return {'profiles': [{'failed': 0}]}
        def review(row, revision, policy, version, deadline):
            if row['id'] == '2':
                raise deepseek_review.ReviewError('invalid response')
            return deepseek_review.validated_update(row, revision, dict(
                id='1', content_classification='substantive', content_reason='重要新进展',
                translation_status='translated', source_language='en', translation_zh='新译文'), version)
        client = mock.Mock()
        client.review.side_effect = review
        with mock.patch.object(workflow, 'collect', side_effect=collect), \
             mock.patch.dict(os.environ, X_KOL_FEISHU_PROFILE='test-profile', X_KOL_FEISHU_CHAT_ID='oc_test_chat_id'), \
             mock.patch('subprocess.run') as send:
            send.return_value = mock.Mock(returncode=0, stdout='{"ok":true,"data":{"message_id":"test"}}', stderr='')
            first = workflow.run_pipeline(self.conn, client, 'policy')
            self.assertFalse(first['ok'])
            self.assertEqual(first['dispatch']['sent'], 1)
            self.assertEqual(first['queue']['awaiting_review'], 1)
            self.assertEqual(db.sent_urls(self.conn), {self.row('1')['url']})
            second = workflow.run_pipeline(self.conn, client, 'policy')
            self.assertEqual(second['dispatch']['sent'], 0)
            self.assertEqual(send.call_count, 1)
        saved = {row['id']: row for row, _ in db.tweets(self.conn)}
        self.assertEqual(saved['2']['translation_status'], 'failed')
        self.assertIn('translation_updated_at', saved['2'])

    def test_new_reviews_are_not_starved_by_a_batch_of_old_failed_records(self):
        for tweet_id in ('1', '2'):
            value = self.row(tweet_id)
            value.pop('content_policy_version')
            value['translation_status'] = 'failed' if tweet_id == '1' else 'pending'
            if tweet_id == '1':
                value['translation_error'] = 'old failure'
            db.insert_tweet(self.conn, value)
        self.conn.commit()
        client = mock.Mock()
        client.review.return_value = deepseek_review.validated_update(
            self.row('2'), 1, dict(id='2', content_classification='low_signal', content_reason='极短反应'), 'v2')
        summary = deepseek_review.review_queue(self.conn, client, '', workflow.dispatch,
                                              workflow.apply_review_updates, limit=1)
        self.assertEqual(client.review.call_args.args[0]['id'], '2')
        self.assertEqual(summary['updated'], 1)
        self.assertEqual(summary['remaining'], 1)
