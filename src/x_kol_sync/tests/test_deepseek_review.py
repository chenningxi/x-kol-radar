import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
from x_kol_sync import deepseek_review as review


def row(**extra):
    return dict(id='123', text='An important launch', author='example', **extra)


def result(**extra):
    return dict(id='123', content_classification='substantive', content_reason='包含重要发布信息',
                translation_status='translated', source_language='en', translation_zh='重要发布', **extra)


class ReviewValidationTests(unittest.TestCase):
    def test_policy_version_mismatch_stops_before_processing(self):
        with self.assertRaisesRegex(review.ReviewError, 'version mismatch'):
            review.load_policy(ROOT, 'v999')

    def test_preserves_valid_translation_and_ignores_untrusted_fields(self):
        original = row(translation_status='translated', translation_zh='保留已有译文')
        answer = result()
        answer.update(revision=99, text='overwrite', content_policy_version='invented')
        update = review.validated_update(original, 4, answer, 'v2')
        self.assertNotIn('translation_zh', update)
        self.assertNotIn('text', update)
        self.assertEqual(update['revision'], 4)
        self.assertEqual(update['content_policy_version'], 'v2')

    def test_both_authors_are_translated_independently(self):
        original = row(quoted_tweet=dict(id='99', text='A new spacecraft', author='source'))
        answer = result(quoted_tweet=dict(id='99', translation_status='translated', source_language='en',
                                         translation_zh='新飞船'))
        update = review.validated_update(original, 1, answer, 'v2')
        self.assertEqual(update['translation_zh'], '重要发布')
        self.assertEqual(update['quoted_tweet']['translation_zh'], '新飞船')

    def test_missing_or_mismatched_quote_translation_is_rejected(self):
        original = row(quoted_tweet=dict(id='99', text='Foreign quote'))
        for quote in (None, {}, dict(id='wrong')):
            with self.subTest(quote=quote), self.assertRaises(review.ReviewError):
                review.validated_update(original, 1, result(quoted_tweet=quote), 'v2')

    def test_links_and_emoji_are_normalized_without_fabricated_translation(self):
        original = dict(id='123', text='👍 https://t.co/example',
                        quoted_tweet=dict(id='99', text='🎉'))
        update = review.validated_update(original, 1, result(), 'v2')
        for part in (update, update['quoted_tweet']):
            self.assertEqual(part['translation_status'], 'no_translation_needed')
            self.assertEqual(part['source_language'], 'und')
            self.assertIsNone(part['translation_error'])

    def test_foreign_text_cannot_claim_no_translation_needed(self):
        answer = result()
        answer.update(translation_status='no_translation_needed', source_language='zh')
        with self.assertRaises(review.ReviewError):
            review.validated_update(row(), 1, answer, 'v2')

    def test_promotional_content_preserves_all_translation_fields(self):
        answer = result()
        answer['content_classification'] = 'promotional'
        update = review.validated_update(row(), 1, answer, 'v2')
        self.assertFalse(any(key.startswith('translation_') for key in update))

    def test_unknown_classification_or_wrong_identity_is_rejected(self):
        for patch in (dict(content_classification='other'), dict(id='456'), dict(content_reason='')):
            answer = result()
            answer.update(patch)
            with self.subTest(patch=patch), self.assertRaises(review.ReviewError):
                review.validated_update(row(), 1, answer, 'v2')

    def test_model_input_omits_unknown_and_private_metadata(self):
        value = review.model_input(row(private_data='hidden', chat_id='hidden', revision=99))
        self.assertNotIn('private_data', value)
        self.assertNotIn('chat_id', value)
        self.assertNotIn('revision', value)


class DeepSeekClientTests(unittest.TestCase):
    def client(self):
        with mock.patch.dict(os.environ, DEEPSEEK_API_KEY='test-key'):
            client = review.Client()
        client.opener = mock.MagicMock()
        return client

    def response(self, content, finish='stop'):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(dict(
            choices=[dict(finish_reason=finish, message=dict(content=content))])).encode()
        return response

    def test_request_goes_directly_to_deepseek_and_uses_json_output(self):
        client = self.client()
        client.opener.open.return_value = self.response(json.dumps(result()))
        update = client.review(row(), 1, 'policy', 'v2', time.monotonic() + 10)
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.deepseek.com/chat/completions')
        payload = json.loads(req.data)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(update['content_policy_version'], 'v2')

    @mock.patch.object(review.time, 'sleep')
    def test_blank_or_truncated_json_is_retried_then_rejected(self, sleep):
        for content, finish in (('   ', 'stop'), (json.dumps(result()), 'length')):
            client = self.client()
            client.opener.open.return_value = self.response(content, finish)
            with self.assertRaises(review.ReviewError):
                client.review(row(), 1, 'policy', 'v2', time.monotonic() + 10)
            self.assertEqual(client.opener.open.call_count, 3)

    def test_auth_failure_is_not_retried_or_exposed(self):
        client = self.client()
        client.opener.open.side_effect = HTTPError(review.ENDPOINT, 401, 'test-key', {}, io.BytesIO(b'test-key'))
        with self.assertRaises(review.ServiceError) as error:
            client.review(row(), 1, 'policy', 'v2', time.monotonic() + 10)
        self.assertNotIn('test-key', str(error.exception))
        self.assertEqual(client.opener.open.call_count, 1)

    @mock.patch.object(review.time, 'sleep')
    def test_rate_limit_is_retried(self, sleep):
        client = self.client()
        client.opener.open.side_effect = [HTTPError(review.ENDPOINT, 429, '', {}, io.BytesIO()),
                                          self.response(json.dumps(result()))]
        client.review(row(), 1, 'policy', 'v2', time.monotonic() + 10)
        self.assertEqual(client.opener.open.call_count, 2)

    def test_missing_key_stops_immediately(self):
        with mock.patch.dict(os.environ, DEEPSEEK_API_KEY=''), self.assertRaises(review.ServiceError):
            review.Client()

    def test_redirect_is_not_followed(self):
        self.assertIsNone(review.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))
