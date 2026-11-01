import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock


ROOT = pathlib.Path(__file__).parents[3]
SCRIPT = ROOT / "scripts/test_x_kol_quote_message.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("quote_message_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class IsolatedQuoteMessageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self.temporary.name)
        self.sample = self.workspace / "sample.json"
        self.sample.write_bytes((ROOT / "examples/quoted_tweet.json").read_bytes())

    def tearDown(self):
        self.temporary.cleanup()

    def args(self):
        return ["--sample-file", str(self.sample), "--workspace", str(self.workspace)]

    @mock.patch.object(MODULE.subprocess, "run")
    def test_default_is_preview_only(self, run):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(MODULE.main(self.args()), 0)
        self.assertIn("非正式推送", output.getvalue())
        self.assertIn("引用推文", output.getvalue())
        run.assert_not_called()
        self.assertFalse((self.workspace / "data").exists())

    @mock.patch.dict(MODULE.os.environ, {"X_KOL_FEISHU_TEST_CHAT_ID": "", "X_KOL_FEISHU_TEST_USER_ID": "", "X_KOL_FEISHU_CHAT_ID": "oc_production"})
    @mock.patch.object(MODULE.subprocess, "run")
    def test_send_never_falls_back_to_production(self, run):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.main(self.args() + ["--send", "--profile", "example-profile"])
        run.assert_not_called()

    @mock.patch.object(MODULE.subprocess, "run")
    def test_success_is_separate_and_deduplicated(self, run):
        run.return_value = mock.Mock(returncode=0, stdout=json.dumps({"ok": True, "data": {"message_id": "om_example"}}), stderr="")
        (self.workspace / "data").mkdir()
        production = self.workspace / "data/feishu_send_ledger.jsonl"
        production.write_text("unchanged")
        args = self.args() + ["--send", "--profile", "example-profile", "--test-chat-id", "oc_test", "--test-user-id", ""]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(MODULE.main(args), 0)
            self.assertEqual(MODULE.main(args), 0)
        self.assertEqual(production.read_text(), "unchanged")
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("oc_test", command)
        self.assertEqual(command[command.index("--as") + 1], "bot")
        receipts = MODULE.read_rows(self.workspace / "data/feishu_quote_test_ledger.jsonl")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["message_id"], "om_example")

    @mock.patch.object(MODULE.subprocess, "run")
    def test_missing_receipt_is_not_success(self, run):
        run.return_value = mock.Mock(returncode=0, stdout='{"ok":true,"data":{}}', stderr="")
        args = self.args() + ["--send", "--profile", "example-profile", "--test-user-id", "ou_test", "--test-chat-id", ""]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(MODULE.main(args), 1)
        receipts = MODULE.read_rows(self.workspace / "data/feishu_quote_test_ledger.jsonl")
        self.assertEqual(receipts[0]["status"], "failed")
