import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[3] / "scripts/backfill_x_kol_quotes.py"
SPEC = importlib.util.spec_from_file_location("backfill_quotes", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BackfillTests(unittest.TestCase):
    def row(self):
        return {"id": "101", "author": "example", "url": "https://x.com/example/status/101",
                "text": "original", "translation_zh": "原文", "content_policy_version": "v2",
                "custom": {"keep": True}}

    def test_detail_identity_must_match(self):
        with self.assertRaises(ValueError):
            MODULE.quote_update(self.row(), {"id": "100", "author": "other", "quoted_tweet": None})

    def test_null_quote_does_not_claim_no_quote(self):
        patch = MODULE.quote_update(self.row(), {**self.row(), "quoted_tweet": None})
        self.assertEqual(patch["quote_status"], "not_provided")
        self.assertNotIn("quoted_tweet", patch)

    @mock.patch.object(MODULE.subprocess, "run")
    def test_rate_limit_is_distinct_and_stops_further_reads(self, run):
        run.return_value = mock.Mock(returncode=1, stdout="", stderr="HTTP 429: rate-limited")
        with self.assertRaises(MODULE.RateLimitError):
            MODULE.fetch(["opencli", "twitter", "tweets", "example"], 30)
        run.assert_called_once()

    def test_existing_quote_translation_and_unknown_fields_are_preserved(self):
        row = self.row()
        row["quoted_tweet"] = {"url": "https://x.com/source/status/100", "text": "Launch",
                               "translation_zh": "发布", "translation_status": "translated", "custom": 1}
        patch = {"quoted_tweet": {"url": "https://x.com/source/status/100", "text": "Launch", "name": "Team"}}
        result = MODULE.merge_patch(row, patch)
        self.assertEqual(result["quoted_tweet"]["translation_zh"], "发布")
        self.assertEqual(result["quoted_tweet"]["custom"], 1)
        self.assertEqual(result["translation_zh"], row["translation_zh"])

    def test_incomplete_read_does_not_remove_known_quote(self):
        row = {**self.row(), "quoted_tweet": {"text": "Launch"}, "quote_status": "available"}
        result = MODULE.merge_patch(row, {"quote_status": "not_provided"})
        self.assertEqual(result["quoted_tweet"], row["quoted_tweet"])
        self.assertEqual(result["quote_status"], "available")

    def test_atomic_merge_backs_up_and_preserves_order_and_other_ledgers(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            (workspace / "data").mkdir()
            ledger = workspace / "data/tweet_ledger.jsonl"
            original = (json.dumps(self.row()) + '\n\n{"url":"https://x.com/example/status/102","unknown":42}\n').encode()
            ledger.write_bytes(original)
            send = workspace / "data/feishu_send_ledger.jsonl"
            send.write_text('{"url":"https://x.com/example/status/101","status":"sent"}\n')
            send_before = send.read_bytes()
            count, backup = MODULE.apply_updates(workspace, {
                self.row()["url"]: {"quoted_tweet": {"text": "Launch"}, "quote_status": "available"}
            })
            saved = ledger.read_bytes().splitlines()
            self.assertEqual(count, 1)
            self.assertEqual((workspace / backup).read_bytes(), original)
            self.assertEqual(saved[1], b"")
            self.assertEqual(saved[2], original.splitlines()[2])
            result = json.loads(saved[0])
            for key, value in self.row().items():
                self.assertEqual(result[key], value)
            self.assertEqual(send.read_bytes(), send_before)

    @mock.patch.object(MODULE, "fetch")
    def test_preview_does_not_fetch_or_write(self, fetch):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            (workspace / "data").mkdir()
            ledger = workspace / "data/tweet_ledger.jsonl"
            original = json.dumps(self.row()) + "\n"
            ledger.write_text(original)
            self.assertEqual(MODULE.main(["--workspace", str(workspace)]), 0)
            self.assertEqual(ledger.read_text(), original)
        fetch.assert_not_called()
