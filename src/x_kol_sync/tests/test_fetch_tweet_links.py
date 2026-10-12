import importlib.util
import io
import json
import pathlib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).parents[1] / "fetch_tweet_links.py"
SPEC = importlib.util.spec_from_file_location("fetch_tweet_links", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class NormalizeProfileTests(unittest.TestCase):
    def test_accepts_profile_forms(self):
        cases = {
            "elonmusk": "elonmusk",
            "@elonmusk": "elonmusk",
            "https://x.com/elonmusk": "elonmusk",
            "https://twitter.com/elonmusk/": "elonmusk",
            "mobile.twitter.com/elonmusk?lang=en": "elonmusk",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(MODULE.normalize_profile(value), expected)

    def test_rejects_non_profiles(self):
        invalid = (
            "https://example.com/elonmusk",
            "https://x.com/home",
            "https://x.com/elonmusk/status/123",
            "https://x.com/invalid-handle",
            "",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    MODULE.normalize_profile(value)


class RecordPreparationTests(unittest.TestCase):
    def test_canonicalizes_deduplicates_and_excludes_retweets(self):
        items = [
            {
                "id": "123",
                "author": "Creator",
                "text": "first",
                "url": "https://twitter.com/Creator/status/123?s=20",
                "likes": 5,
            },
            {"id": "123", "author": "Creator", "url": "https://x.com/Creator/status/123"},
            {
                "id": "456",
                "author": "Creator",
                "url": "https://x.com/Creator/status/456",
                "is_retweet": True,
            },
            {"id": "789", "author": "Creator", "text": "third"},
        ]
        records = MODULE.prepare_records(items, "Creator", include_retweets=False)
        self.assertEqual([record["id"] for record in records], ["123", "789"])
        self.assertEqual(records[0]["url"], "https://x.com/creator/status/123")

    def test_deduplicates_author_case_and_host_variants(self):
        items = [
            {"url": "https://x.com/Creator/status/123"},
            {"url": "https://twitter.com/creator/status/123?s=20"},
        ]
        records = MODULE.prepare_records(items, "Creator", include_retweets=False)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["url"], "https://x.com/creator/status/123")
        self.assertEqual(records[0]["author"], "creator")

    def test_json_decoder_accepts_common_wrappers(self):
        payload = json.dumps({"rows": [{"id": "123", "author": "a"}]})
        self.assertEqual(MODULE.decode_opencli_output(payload)[0]["id"], "123")

    def test_text_output_is_one_url_per_line(self):
        rendered = MODULE.render_records(
            [{"url": "https://x.com/a/status/1"}, {"url": "https://x.com/a/status/2"}],
            "text",
        )
        self.assertEqual(rendered, "https://x.com/a/status/1\nhttps://x.com/a/status/2\n")


class CommandTests(unittest.TestCase):
    def test_builds_read_only_tweets_command(self):
        command = MODULE.build_opencli_command("opencli", "jack", 100, 2)
        self.assertEqual(
            command,
            [
                "opencli",
                "twitter",
                "tweets",
                "@jack",
                "--limit",
                "100",
                "--page-delay",
                "2",
                "-f",
                "json",
            ],
        )

    @mock.patch.object(MODULE.subprocess, "run")
    def test_empty_or_fully_filtered_fetch_is_successful(self, run):
        for payload in ([], [{"id": "1", "author": "a", "is_retweet": True}]):
            with self.subTest(payload=payload):
                run.return_value = mock.Mock(
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = MODULE.main(["a", "--format", "json"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout.getvalue()), [])
                self.assertIn("unique_links=0", stderr.getvalue())

    @mock.patch.object(MODULE.subprocess, "run")
    def test_nonempty_unrecognized_rows_remain_an_error(self, run):
        run.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps([{"text": "missing post identity"}]),
            stderr="",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = MODULE.main(["a", "--format", "json"])
        self.assertEqual(code, 6)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("No canonical post links", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
