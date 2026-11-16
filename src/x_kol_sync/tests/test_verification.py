import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))
from x_kol_sync import verification as v


class VerificationTests(unittest.TestCase):
    def status(self, handle, verified=True):
        return dict(handle=handle, verified=verified, checked_at=v.now(), source_url='https://x.com/' + handle)

    def setup_conn(self, rows, profiles=None):
        conn = mock.MagicMock(); cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        cursor.fetchall.side_effect = [profiles or [], [dict(tweet_id=str(i), payload=json.dumps(r), revision=4) for i, r in enumerate(rows)]]
        return conn, cursor

    def test_quote_verification_reads_once_and_preserves_content_without_monitoring_quote(self):
        row = dict(author='example', text='original', translation_zh='译文', unknown=3,
                   quoted_tweet=dict(author='source', text='quote', unknown_quote=4))
        conn, cursor = self.setup_conn([row, row], [dict(handle='example', config=json.dumps(dict(verification=self.status('example', False))))])
        with mock.patch.object(v, 'read_verification', side_effect=lambda handle, timeout: self.status(handle)) as read:
            result = v.repair_author_verifications(conn, Path.cwd())
        read.assert_called_once(); self.assertEqual(read.call_args.args[0], 'source')
        self.assertEqual(result['authors_remaining'], 0)
        updates = [c for c in cursor.execute.call_args_list if c.args[0].startswith('UPDATE xkol_tweets')]
        saved = json.loads(updates[-1].args[1][0])
        self.assertFalse(saved['author_verification']['verified'])
        self.assertTrue(saved['quoted_tweet']['author_verification']['verified'])
        self.assertEqual((saved['text'], saved['translation_zh'], saved['unknown'], saved['quoted_tweet']['unknown_quote']), ('original', '译文', 3, 4))
        self.assertFalse(any(c.args[0].startswith('INSERT') for c in cursor.execute.call_args_list))

    def test_wrong_author_and_unknown_failures_never_create_a_verified_badge(self):
        conn, cursor = self.setup_conn([dict(author='source', author_verification=self.status('example'))])
        with mock.patch.object(v, 'read_verification', side_effect=TimeoutError):
            result = v.repair_author_verifications(conn, Path.cwd())
        saved = json.loads(cursor.execute.call_args.args[1][0])['author_verification']
        self.assertEqual(saved['handle'], 'source'); self.assertNotIn('verified', saved)
        self.assertEqual(result['authors_failed'], 1)

    def test_refresh_failure_preserves_last_confirmed_snapshot(self):
        old = dict(self.status('example'), checked_at='2020-01-01T00:00:00+00:00')
        conn, cursor = self.setup_conn([dict(author='example', author_verification=old)])
        with mock.patch.object(v, 'read_verification', side_effect=TimeoutError):
            result = v.repair_author_verifications(conn, Path.cwd())
        saved = json.loads(cursor.execute.call_args.args[1][0])['author_verification']
        self.assertTrue(saved['verified']); self.assertEqual(saved['checked_at'], old['checked_at'])
        self.assertEqual(result['authors_remaining'], 1)

    def test_revision_conflict_rolls_back(self):
        conn, cursor = self.setup_conn([dict(author='example')]); cursor.rowcount = 0
        with mock.patch.object(v, 'read_verification', side_effect=lambda handle, timeout: self.status(handle)):
            with self.assertRaisesRegex(ValueError, 'Revision conflict'):
                v.repair_author_verifications(conn, Path.cwd())
        conn.rollback.assert_called_once()

    def test_reader_rejects_wrong_identity_and_nonboolean_verification(self):
        for raw in (dict(handle='other', verified=True), dict(handle='example', verified='true')):
            with mock.patch.object(v.shutil, 'which', return_value='/bin/example'), \
                 mock.patch.object(v.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(raw))):
                with self.assertRaises(v.AssetError): v.read_verification('example', 5)

    def test_reader_keeps_explicit_unverified_and_gold_badge_snapshot(self):
        for raw in (dict(handle='example', verified=False), dict(handle='example', verified=True, badge_type='gold', badge_color='rgb(15, 20, 25)'),
                    dict(handle='example', verified=True, badge_type='gray', badge_color='rgb(130, 154, 171)')):
            with mock.patch.object(v.shutil, 'which', return_value='/bin/example'), \
                 mock.patch.object(v.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(raw))):
                value = v.read_verification('example', 5)
            self.assertIs(value['verified'], raw['verified'])
            self.assertTrue(v.fresh(value))
            if raw['verified']: self.assertEqual(value['badge_type'], raw['badge_type'])

    def test_budget_deferred_authors_are_not_marked_false(self):
        conn, cursor = self.setup_conn([dict(author='example')])
        with mock.patch.object(v, 'media_budget', return_value=0), mock.patch.object(v, 'read_verification') as read:
            result = v.repair_author_verifications(conn, Path.cwd())
        read.assert_not_called(); self.assertTrue(result['budget_exhausted'])
        self.assertFalse(any(c.args[0].startswith('UPDATE') for c in cursor.execute.call_args_list))
