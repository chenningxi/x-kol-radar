import io
import json
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from x_kol_sync import queries
import query_x_kol_profiles
import query_x_kol_tweets


class QueryTests(unittest.TestCase):
    def test_json_queries_expose_image_metadata_without_downloads(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        avatar = {'status': 'saved', 'local_path': 'avatars/example.jpg'}
        cursor.fetchall.return_value = [dict(handle='example', config=json.dumps(
            dict(avatar=avatar)), tweet_count=1)]
        self.assertEqual(queries.profiles(connection)[0]['avatar'], avatar)
        payload = {'media': [{'image': {'status': 'saved', 'local_path': 'images/example.jpg'}}]}
        cursor.fetchone.return_value = {'total': 1}
        cursor.fetchall.return_value = [dict(tweet_id='1', url='https://x.com/example/status/1', payload=json.dumps(payload))]
        result = queries.tweet_page(connection, 'example')
        self.assertEqual(result['items'][0]['payload'], payload)
        connection.commit.assert_not_called()

    def test_read_connection_enforces_read_only_and_closes_on_failure(self):
        connection = mock.MagicMock()
        with mock.patch.object(queries.db, 'connect', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, 'query failed'):
                with queries.read_connection() as current:
                    self.assertIs(current, connection)
                    raise RuntimeError('query failed')
        connection.cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
            'START TRANSACTION READ ONLY')
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_profiles_preserve_disabled_and_zero_tweet_accounts(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [dict(handle='example', config=json.dumps(
            dict(alias='示例', enabled=False)), tweet_count=0)]
        self.assertEqual(queries.profiles(connection), [
            dict(handle='example', alias='示例', enabled=False, tweet_count=0)])
        self.assertTrue(cursor.execute.call_args.args[0].startswith('SELECT '))

    def test_pagination_filters_before_limit_and_uses_numeric_tweet_order(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = dict(total=45)
        cursor.fetchall.return_value = [dict(tweet_id='9', url='https://x.com/example/status/9')]
        result = queries.tweet_page(connection, 'example', page=3, page_size=20)
        self.assertEqual((result['total'], result['total_pages'], result['page']), (45, 3, 3))
        calls = cursor.execute.call_args_list
        self.assertEqual(calls[0].args[1], ('example',))
        self.assertEqual(calls[1].args[1], ('example', 20, 40))
        self.assertIn('WHERE profile_handle=%s', calls[1].args[0])
        self.assertIn('ORDER BY CAST(tweet_id AS UNSIGNED) DESC', calls[1].args[0])
        connection.commit.assert_not_called()

    def test_empty_or_out_of_range_page_keeps_total(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        for total, pages in ((0, 0), (21, 2)):
            cursor.fetchone.return_value = dict(total=total)
            result = queries.tweet_page(connection, 'example', page=99)
            self.assertEqual((result['total'], result['total_pages'], result['items']), (total, pages, []))

    def test_invalid_arguments_do_not_connect(self):
        with mock.patch.object(queries.db, 'connect') as connect, redirect_stderr(io.StringIO()):
            for args in (['example', '--page', '0'], ['example', '--page-size', '-1'],
                         ['example', '--page', 'abc'], ["example' OR 1=1"]):
                with self.assertRaises(SystemExit) as error:
                    query_x_kol_tweets.main(args)
                self.assertEqual(error.exception.code, 2)
        connect.assert_not_called()

    def test_tweet_cli_normalizes_handle_and_outputs_page_json(self):
        output = io.StringIO()
        page = dict(handle='example', page=2, page_size=10, total=0, total_pages=0, items=[])
        with mock.patch.object(queries, 'read_connection', return_value=nullcontext('connection')), \
             mock.patch.object(queries, 'tweet_page', return_value=page) as query, redirect_stdout(output):
            self.assertEqual(query_x_kol_tweets.main(['@Example', '--page', '2', '--page-size', '10', '--json']), 0)
        query.assert_called_once_with('connection', 'example', 2, 10)
        self.assertEqual(json.loads(output.getvalue()), page)

    def test_profile_cli_outputs_json(self):
        output = io.StringIO()
        rows = [dict(handle='example', alias='示例', enabled=True, tweet_count=3)]
        with mock.patch.object(queries, 'read_connection', return_value=nullcontext('connection')), \
             mock.patch.object(queries, 'profiles', return_value=rows), redirect_stdout(output):
            self.assertEqual(query_x_kol_profiles.main(['--json']), 0)
        self.assertEqual(json.loads(output.getvalue()), rows)
