import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))

from x_kol_sync import avatars
from x_kol_sync.assets import AssetStore


class AuthorAvatarTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'avatars').mkdir()
        for handle in ('example', 'source'):
            (self.root / 'avatars' / (handle + '.png')).write_bytes(b'avatar-test-file')
        env = mock.patch.dict(os.environ, X_KOL_MEDIA_DIR=str(self.root))
        env.start(); self.addCleanup(env.stop)
        self.store = AssetStore(self.root)
        self.conn = mock.MagicMock()
        self.cursor = self.conn.cursor.return_value.__enter__.return_value
        self.cursor.rowcount = 1

    def image(self, handle):
        return dict(handle=handle, status='saved', local_path=f'avatars/{handle}.png')

    def rows(self, profiles, values):
        self.cursor.fetchall.side_effect = [profiles, [dict(tweet_id=str(i), payload=json.dumps(row), revision=4)
                                                      for i, row in enumerate(values, 1)]]

    def test_unmonitored_quote_author_downloads_once_and_never_becomes_a_profile(self):
        profile = dict(handle='example', config=json.dumps(dict(avatar=self.image('example'), unknown='keep')))
        row = dict(author='example', text='outer', translation_zh='译文', unknown={'keep': True},
                   quoted_tweet=dict(author='source', text='quote', unknown_quote='keep'))
        self.rows([profile], [row, row])
        with mock.patch.object(avatars, 'AssetStore', return_value=self.store), \
             mock.patch.object(self.store, 'avatar', return_value=self.image('source')) as read:
            result = avatars.repair_author_avatars(self.conn, self.root)
        read.assert_called_once()
        self.assertEqual(read.call_args.args[0]['handle'], 'source')
        self.assertEqual(result['authors_remaining'], 0)
        self.assertEqual(result['avatar_tweets_updated'], 2)
        updates = [call for call in self.cursor.execute.call_args_list if call.args[0].startswith('UPDATE')]
        self.assertTrue(all(call.args[0].startswith('UPDATE xkol_tweets') for call in updates))
        saved = json.loads(updates[-1].args[1][0])
        self.assertEqual(saved['author_avatar']['handle'], 'example')
        self.assertEqual(saved['quoted_tweet']['author_avatar']['handle'], 'source')
        self.assertEqual(saved['quoted_tweet']['unknown_quote'], 'keep')
        self.assertEqual(saved['translation_zh'], '译文')
        self.assertEqual(saved['unknown'], {'keep': True})
        self.assertFalse(any(call.args[0].startswith('INSERT') for call in self.cursor.execute.call_args_list))

    def test_expired_budget_leaves_unattempted_author_untouched(self):
        self.rows([], [dict(author='source', text='keep')])
        self.store.budget = 0
        with mock.patch.object(avatars, 'AssetStore', return_value=self.store), mock.patch.object(self.store, 'avatar') as read:
            result = avatars.repair_author_avatars(self.conn, self.root)
        read.assert_not_called()
        self.assertEqual(result['authors_remaining'], 1)
        self.assertTrue(result['avatars_budget_exhausted'])
        self.assertFalse(any(c.args[0].startswith('UPDATE') for c in self.cursor.execute.call_args_list))

    def test_wrong_author_avatar_is_not_reused_and_failure_does_not_borrow_another_author(self):
        self.rows([], [dict(author='source', author_avatar=self.image('example'))])
        with mock.patch.object(avatars, 'AssetStore', return_value=self.store), \
             mock.patch.object(self.store, 'avatar', return_value=dict(status='failed', error_type='AssetError')) as read:
            result = avatars.repair_author_avatars(self.conn, self.root)
        self.assertEqual(read.call_args.args[0]['handle'], 'source')
        self.assertEqual(result['authors_failed'], 1)
        saved = json.loads(self.cursor.execute.call_args.args[1][0])
        self.assertEqual(saved['author_avatar']['handle'], 'source')
        self.assertNotIn('local_path', saved['author_avatar'])

    def test_revision_conflict_rolls_back_avatar_update(self):
        self.rows([], [dict(author='source', text='keep')]); self.cursor.rowcount = 0
        with mock.patch.object(avatars, 'AssetStore', return_value=self.store), \
             mock.patch.object(self.store, 'avatar', return_value=self.image('source')):
            with self.assertRaisesRegex(ValueError, 'Revision conflict'):
                avatars.repair_author_avatars(self.conn, self.root)
        self.conn.rollback.assert_called_once()
