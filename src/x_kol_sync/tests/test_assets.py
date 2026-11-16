import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from x_kol_sync import assets, database
import run_x_kol_db as workflow

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jY9kAAAAASUVORK5CYII=')
PHOTO = 'https://pbs.twimg.com/media/example.jpg'
AVATAR = 'https://pbs.twimg.com/profile_images/123/example_normal.jpg'


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = mock.patch.dict('os.environ', {'X_KOL_MEDIA_DIR': self.folder.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = assets.AssetStore()

    def response(self, data=PNG, headers=None):
        value = io.BytesIO(data)
        value.headers = headers or {}
        return value

    def test_media_budgets_default_to_five_and_three_minutes(self):
        with mock.patch.dict('os.environ', X_KOL_MEDIA_DOWNLOAD_BUDGET_SECONDS='',
                             X_KOL_MEDIA_RETRY_BUDGET_SECONDS=''):
            self.assertEqual(assets.media_budget('download'), 300)
            self.assertEqual(assets.media_budget('retry'), 180)

    def test_media_budgets_are_independently_configurable_and_bounded(self):
        with mock.patch.dict('os.environ', X_KOL_MEDIA_DOWNLOAD_BUDGET_SECONDS='420',
                             X_KOL_MEDIA_RETRY_BUDGET_SECONDS='240'):
            self.assertEqual(assets.media_budget('download'), 420)
            self.assertEqual(assets.media_budget('retry'), 240)
        for value in ('0', '-1', '601', 'invalid', 'inf', '3.5'):
            with mock.patch.dict('os.environ', X_KOL_MEDIA_RETRY_BUDGET_SECONDS=value):
                with self.assertRaisesRegex(ValueError, 'X_KOL_MEDIA_RETRY_BUDGET_SECONDS'):
                    assets.media_budget('retry')

    def test_retry_worker_receives_configured_budget(self):
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        with mock.patch.dict('os.environ', X_KOL_MEDIA_RETRY_BUDGET_SECONDS='240'), \
             mock.patch.object(workflow, 'AssetStore', return_value=self.store) as constructor:
            workflow.repair_tweet_assets(conn, Path(self.folder.name))
        constructor.assert_called_once_with(Path(self.folder.name), budget=240)

    def test_download_requests_original_and_records_complete_file(self):
        with mock.patch.object(self.store.opener, 'open', return_value=self.response()) as get:
            result = self.store.save(PHOTO, 'image')
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(result['source_url'], PHOTO)
        self.assertEqual(result['content_type'], 'image/png')
        self.assertIn('name=orig', get.call_args.args[0].full_url)
        self.assertEqual((self.store.root / result['local_path']).read_bytes(), PNG)
        self.assertFalse(Path(result['local_path']).is_absolute())
        self.assertEqual(list(self.store.root.rglob('*'))[-1].suffix, '.png')

    def test_avatar_upgrade_and_reuse_skip_browser_and_network(self):
        self.assertIn('_400x400.jpg', assets.image_url(AVATAR, 'avatar'))
        self.assertIn('_400x400.jpg', assets.image_url(AVATAR.replace('_normal', '_200x200'), 'avatar'))
        with mock.patch.object(assets, 'read_avatar_source', return_value=AVATAR) as read, \
             mock.patch.object(self.store.opener, 'open', return_value=self.response()) as get:
            first = self.store.avatar({'handle': 'example'})
            second = self.store.avatar({'handle': 'example', 'avatar': first})
        self.assertEqual(first, second)
        read.assert_called_once()
        get.assert_called_once()

    def test_untrusted_urls_never_reach_network(self):
        with mock.patch.object(self.store.opener, 'open') as get:
            for url in ('http://pbs.twimg.com/media/a.jpg', 'https://example.com/a.jpg',
                        'https://pbs.twimg.com@127.0.0.1/media/a.jpg',
                        'https://pbs.twimg.com:444/media/a.jpg', 'file:///etc/passwd'):
                self.assertEqual(self.store.save(url, 'image')['status'], 'failed')
        get.assert_not_called()

    def test_html_truncation_and_oversize_leave_no_partial_files(self):
        cases = [(b'<html>not an image</html>', {}),
                 (PNG, {'Content-Length': str(len(PNG) + 1)}),
                 (PNG, {'Content-Length': str(assets.MAX_BYTES + 1)})]
        for data, headers in cases:
            with mock.patch.object(self.store.opener, 'open', return_value=self.response(data, headers)):
                result = self.store.save(PHOTO, 'image')
            self.assertEqual(result['status'], 'failed')
            self.assertNotIn('local_path', result)
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_timeout_is_sanitized_and_does_not_erase_text(self):
        row = {'id': '1', 'text': 'keep', 'unknown': True, 'media_urls': [PHOTO]}
        with mock.patch.object(self.store.opener, 'open', side_effect=TimeoutError('private diagnostic')):
            self.store.enrich_tweet(row)
        self.assertEqual((row['text'], row['unknown'], row['media_status']), ('keep', True, 'partial'))
        self.assertNotIn('private diagnostic', json.dumps(row))

    def test_budget_exhaustion_does_not_start_browser_or_downloads(self):
        self.store.deadline = 0
        with mock.patch.object(assets, 'read_avatar_source') as read, \
             mock.patch.object(self.store.opener, 'open') as get:
            self.assertEqual(self.store.avatar({'handle': 'example'})['status'], 'failed')
            failure = self.store.save(PHOTO, 'image')
            self.assertEqual(failure['status'], 'failed')
            self.assertEqual(failure['error_type'], 'AssetBudgetExceeded')
        read.assert_not_called()
        get.assert_not_called()

    def test_retry_budget_leaves_unattempted_rows_untouched(self):
        conn = mock.MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        cursor.fetchall.return_value = [dict(tweet_id=str(i), revision=4,
            payload=json.dumps({'id': str(i), 'media_urls': [PHOTO], 'unknown': 'keep'})) for i in (1, 2)]
        self.store.budget = 1
        def enrich(row):
            self.store.budget = 0
            row.update(media_status='complete', media=[])
            return row
        with mock.patch.object(workflow, 'AssetStore', return_value=self.store), \
             mock.patch.object(self.store, 'enrich_tweet', side_effect=enrich) as retry:
            result = workflow.repair_tweet_assets(conn, Path(self.folder.name))
        self.assertEqual(result['tweets_updated'], 1)
        self.assertTrue(result['budget_exhausted'])
        retry.assert_called_once()
        updates = [c for c in cursor.execute.call_args_list if c.args[0].startswith('UPDATE')]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].args[1][1:], ('1', 4))
        self.assertEqual(json.loads(updates[0].args[1][0])['unknown'], 'keep')

    def test_sync_retries_partial_media_even_when_profile_cursor_is_unchanged(self):
        conn = mock.MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [dict(handle='example', cursor_id='123', config=json.dumps(
            dict(profile_url='https://x.com/example', enabled=True, limit=30)))]
        retried = dict(tweets_updated=1, images_failed=0, budget_exhausted=False)
        with mock.patch.object(workflow, 'repair_tweet_assets', return_value=retried) as repair, \
             mock.patch.object(workflow.sync, 'run_doctor', return_value=({}, 0)), \
             mock.patch.object(workflow.sync, 'run_fetch', return_value=[{'id': '123'}]) as fetch, \
             mock.patch.dict('os.environ', X_KOL_MEDIA_DOWNLOAD_BUDGET_SECONDS='420'), \
             mock.patch.object(workflow, 'AssetStore', return_value=self.store) as constructor, \
             mock.patch.object(workflow, 'repair_author_avatars', return_value={'authors_attempted': 0}), \
             mock.patch.object(workflow, 'repair_author_verifications', return_value={'authors_attempted': 0}):
            result = workflow.collect(conn, Path(self.folder.name))
        repair.assert_called_once_with(conn, Path(self.folder.name))
        constructor.assert_called_once_with(Path(self.folder.name), budget=420)
        fetch.assert_called_once()
        self.assertEqual(result['profiles'][0]['mode'], 'probe_unchanged')
        self.assertEqual(result['asset_retry'], retried)

    def test_timeline_wait_does_not_consume_image_budget(self):
        clock = [0]
        with mock.patch.object(assets.time, 'monotonic', side_effect=lambda: clock[0]):
            store = assets.AssetStore(budget=10)
            clock[0] = 600
            with mock.patch.object(store.opener, 'open', return_value=self.response()):
                self.assertEqual(store.save(PHOTO, 'image')['status'], 'saved')

    def test_retry_preserves_unknown_media_fields_and_clears_old_error(self):
        row = {'media_urls': [PHOTO], 'media': [{'url': PHOTO, 'custom': {'keep': True},
               'image': {'status': 'failed', 'error_type': 'TimeoutError', 'custom_image': 'keep'}}]}
        with mock.patch.object(self.store, 'save', return_value={'status': 'saved', 'local_path': 'images/example.png'}):
            self.store.enrich_tweet(row)
        self.assertEqual(row['media'][0]['custom'], {'keep': True})
        self.assertEqual(row['media'][0]['image']['custom_image'], 'keep')
        self.assertNotIn('error_type', row['media'][0]['image'])

    def test_quote_images_and_video_poster_keep_their_ownership(self):
        row = {'media_urls': [PHOTO], 'quoted_tweet': {'id': '2', 'author': 'source',
               'media_urls': ['https://video.twimg.com/example.mp4'],
               'media_posters': ['https://pbs.twimg.com/ext_tw_video_thumb/example.jpg']}}
        with mock.patch.object(self.store, 'save', return_value={'status': 'saved'}) as save:
            self.store.enrich_tweet(row)
        self.assertEqual(row['media'][0]['type'], 'photo')
        self.assertEqual(row['quoted_tweet']['media'][0]['type'], 'video')
        self.assertEqual(row['quoted_tweet']['author'], 'source')
        self.assertEqual(save.call_args_list, [mock.call(PHOTO, 'image'), mock.call(
            'https://pbs.twimg.com/ext_tw_video_thumb/example.jpg', 'image')])

    def test_missing_upstream_urls_are_not_reported_as_saved(self):
        row = self.store.enrich_tweet({'has_media': True})
        self.assertEqual(row['media_status'], 'unavailable')
        self.assertNotIn('media', row)
        empty = self.store.enrich_tweet({'has_media': True, 'media_urls': []})
        self.assertEqual(empty['media_status'], 'unavailable')

    def test_relative_workspace_is_not_joined_twice(self):
        with mock.patch.dict('os.environ', {'X_KOL_MEDIA_DIR': ''}):
            store = assets.AssetStore('example-workspace')
        self.assertEqual(store.root, (Path.cwd() / 'example-workspace/data/media').resolve())

    def test_avatar_paths_cannot_escape_cache(self):
        self.assertFalse(self.store.available({'status': 'saved', 'local_path': '../outside.jpg'}))

    def test_duplicate_tweet_skips_image_download_and_preserves_payload(self):
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {'tweet_id': '1'}
        prepare = mock.Mock()
        self.assertFalse(database.insert_tweet(conn, {'id': '1', 'url': 'https://x.com/example/status/1'}, prepare=prepare))
        prepare.assert_not_called()

    def test_profile_reload_preserves_unknown_metadata_and_avatar(self):
        conn = mock.MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        avatar = {'status': 'saved', 'local_path': 'avatars/example.png'}
        cursor.fetchall.return_value = [{'handle': 'example', 'config': json.dumps(
            {'handle': 'example', 'unknown': {'keep': True}, 'avatar': avatar})}]
        with mock.patch.object(workflow.sync, 'load_profiles', return_value=[
                {'handle': 'example', 'alias': 'new alias'}]), \
             mock.patch.object(workflow.AssetStore, 'avatar', return_value=avatar):
            workflow.load_profiles(conn, Path(self.folder.name))
        saved = json.loads(cursor.execute.call_args.args[1][1])
        self.assertEqual(saved['unknown'], {'keep': True})
        self.assertEqual(saved['avatar'], avatar)
        self.assertEqual(saved['alias'], 'new alias')

    def test_migration_does_not_read_x_for_avatars(self):
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        with mock.patch.object(workflow.sync, 'load_profiles', return_value=[{'handle': 'example'}]), \
             mock.patch.object(workflow.AssetStore, 'avatar') as avatar:
            workflow.load_profiles(conn, Path(self.folder.name), download_avatars=False)
        avatar.assert_not_called()

    def test_avatar_reader_rejects_mismatched_identity(self):
        with mock.patch.object(assets.shutil, 'which', return_value='/placeholder/bin'), \
             mock.patch.object(assets.subprocess, 'run', return_value=mock.Mock(
                 returncode=0, stdout=json.dumps({'handle': 'other', 'avatar_url': AVATAR}))):
            with self.assertRaises(assets.AssetError):
                assets.read_avatar_source('example', 10)
