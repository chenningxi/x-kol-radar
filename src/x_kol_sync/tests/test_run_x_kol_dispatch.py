import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout


SCRIPT_PATH = pathlib.Path(__file__).parents[3] / "scripts" / "run_x_kol_dispatch.py"
POLICY_PATH = pathlib.Path(__file__).parents[3] / "prompts" / "translate_and_review.md"
SPEC = importlib.util.spec_from_file_location("run_x_kol_dispatch", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def row(url: str, classification: str | None, version: str | None = MODULE.CONTENT_POLICY_VERSION) -> dict:
    value = {
        "url": url,
        "profile": "https://x.com/example",
        "translation_status": "translated",
        "translation_zh": "译文",
    }
    if classification is not None:
        value["content_classification"] = classification
    if version is not None:
        value["content_policy_version"] = version
    return value


class ContentPolicyTests(unittest.TestCase):
    def test_sends_substantive_and_uncertain_rows(self):
        rows = [
            row("https://x.com/example/status/1", "substantive"),
            row("https://x.com/example/status/2", "uncertain"),
        ]

        to_send, to_skip, awaiting, awaiting_translation = MODULE.partition_dispatch_rows(rows, set())

        self.assertEqual(to_send, rows)
        self.assertEqual(to_skip, [])
        self.assertEqual(awaiting, [])
        self.assertEqual(awaiting_translation, [])

    def test_substantive_announcement_is_not_keyword_filtered(self):
        announcement = row("https://x.com/example/status/1", "substantive")
        announcement["text"] = "A new product launches today. Try it now."

        to_send, to_skip, awaiting, awaiting_translation = MODULE.partition_dispatch_rows(
            [announcement], set()
        )

        self.assertEqual(to_send, [announcement])
        self.assertEqual(to_skip, [])
        self.assertEqual(awaiting, [])
        self.assertEqual(awaiting_translation, [])

    def test_skips_only_confirmed_promotional_and_low_signal_rows(self):
        rows = [
            row("https://x.com/example/status/1", "promotional"),
            row("https://x.com/example/status/2", "low_signal"),
        ]

        to_send, to_skip, awaiting, awaiting_translation = MODULE.partition_dispatch_rows(rows, set())

        self.assertEqual(to_send, [])
        self.assertEqual(to_skip, rows)
        self.assertEqual(awaiting, [])
        self.assertEqual(awaiting_translation, [])

    def test_filtered_rows_do_not_require_translation(self):
        promotional = row("https://x.com/example/status/1", "promotional")
        promotional["translation_status"] = "pending"

        to_send, to_skip, awaiting, awaiting_translation = MODULE.partition_dispatch_rows(
            [promotional], set()
        )

        self.assertEqual(to_send, [])
        self.assertEqual(to_skip, [promotional])
        self.assertEqual(awaiting, [])
        self.assertEqual(awaiting_translation, [])

    def test_missing_unknown_or_old_policy_waits_for_review(self):
        rows = [
            row("https://x.com/example/status/1", None),
            row("https://x.com/example/status/2", "other"),
            row("https://x.com/example/status/3", "substantive", "v0"),
            row("https://x.com/example/status/4", "substantive", None),
        ]

        to_send, to_skip, awaiting, awaiting_translation = MODULE.partition_dispatch_rows(rows, set())

        self.assertEqual(to_send, [])
        self.assertEqual(to_skip, [])
        self.assertEqual(awaiting, rows)
        self.assertEqual(awaiting_translation, [])

    def test_untranslated_sendable_rows_wait_for_translation(self):
        untranslated = row("https://x.com/example/status/1", "substantive")
        untranslated["translation_status"] = "pending"

        to_send, to_skip, awaiting, awaiting_translation = MODULE.partition_dispatch_rows(
            [untranslated], set()
        )

        self.assertEqual(to_send, [])
        self.assertEqual(to_skip, [])
        self.assertEqual(awaiting, [])
        self.assertEqual(awaiting_translation, [untranslated])

    def test_previously_handled_rows_are_ignored(self):
        handled = row("https://x.com/example/status/2", "substantive")

        partitions = MODULE.partition_dispatch_rows(
            [handled],
            {handled["url"]},
        )

        self.assertEqual(partitions, ([], [], [], []))

    def test_skip_ledger_records_reason_without_chat_id(self):
        skipped = row("https://x.com/example/status/1", "promotional")
        skipped["content_reason"] = "只有试用号召，没有新增事实"
        with tempfile.TemporaryDirectory() as temporary:
            ledger = pathlib.Path(temporary) / "feishu_send_ledger.jsonl"
            with mock.patch.object(MODULE, "SEND_LEDGER", ledger):
                MODULE.append_skipped_rows([skipped])

            saved = json.loads(ledger.read_text(encoding="utf-8"))

        self.assertEqual(saved["status"], "skipped")
        self.assertEqual(saved["chat_id"], "")
        self.assertEqual(saved["content_classification"], "promotional")
        self.assertEqual(saved["content_policy_version"], MODULE.CONTENT_POLICY_VERSION)
        self.assertIn("试用号召", saved["content_reason"])

    def test_checked_in_policy_matches_dispatch_contract(self):
        policy = POLICY_PATH.read_text(encoding="utf-8")

        self.assertIn(f"策略 {MODULE.CONTENT_POLICY_VERSION}", policy)
        for classification in MODULE.SEND_CLASSIFICATIONS | MODULE.SKIP_CLASSIFICATIONS:
            self.assertIn(f"`{classification}`", policy)

    @mock.patch.object(MODULE, "read_rows")
    @mock.patch.object(MODULE.subprocess, "run")
    def test_sync_returns_without_reading_or_dispatching(self, run, read_rows):
        run.return_value = mock.Mock(returncode=0, stdout="sync complete\n", stderr="")

        code = MODULE.main(["sync"])

        self.assertEqual(code, 0)
        read_rows.assert_not_called()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][1], str(MODULE.CURSOR_RUNNER))


class QuoteDispatchTests(unittest.TestCase):
    def example(self):
        path = pathlib.Path(__file__).parents[3] / "examples" / "quoted_tweet.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_both_authors_and_translations_are_separate_in_message(self):
        example = self.example()
        message = MODULE.build_message([example], {"https://x.com/example_author": "示例作者"})

        self.assertIn("【示例作者（@example_author）】", message)
        self.assertIn("作者发言（中文翻译）：\n这个进展很重要。", message)
        self.assertIn("引用推文｜示例团队（@example_source）", message)
        self.assertIn("引用内容（中文翻译）：\n我们今天发布了新产品，下个月开始交付。", message)
        self.assertIn("引用链接：https://x.com/example_source/status/100", message)
        self.assertIn("原文链接：https://x.com/example_author/status/101", message)
        self.assertNotIn("This is an important", message)

    def test_quote_translation_failure_delays_sending(self):
        example = self.example()
        example["quoted_tweet"]["translation_status"] = "failed"

        send, skip, review, translate = MODULE.partition_dispatch_rows([example], set())

        self.assertEqual((send, skip, review), ([], [], []))
        self.assertEqual(translate, [example])

    def test_emoji_comment_preserves_important_quote_in_message(self):
        example = self.example()
        example["text"] = "🚀"
        example["translation_status"] = "no_translation_needed"
        example["source_language"] = "und"

        send, skip, review, translate = MODULE.partition_dispatch_rows([example], set())
        message = MODULE.build_message(send, {})

        self.assertEqual(send, [example])
        self.assertEqual((skip, review, translate), ([], [], []))
        self.assertIn("作者发言：\n🚀", message)
        self.assertIn("我们今天发布了新产品，下个月开始交付。", message)

    def test_quote_without_words_does_not_require_translation(self):
        example = self.example()
        example["quoted_tweet"]["text"] = ""
        example["quoted_tweet"].pop("translation_status")

        send, skip, review, translate = MODULE.partition_dispatch_rows([example], set())

        self.assertEqual(send, [example])
        self.assertEqual((skip, review, translate), ([], [], []))

    def test_unavailable_quote_is_explicit_and_does_not_block_sending(self):
        example = self.example()
        example.pop("quoted_tweet")
        example["quote_status"] = "unavailable"

        send, _, _, translate = MODULE.partition_dispatch_rows([example], set())
        message = MODULE.build_message(send, {})

        self.assertEqual(send, [example])
        self.assertEqual(translate, [])
        self.assertIn("引用推文：详情不可用", message)

    def test_legacy_plain_tweet_does_not_invent_quote(self):
        plain = row("https://x.com/example/status/1", "substantive")
        plain["author"] = "example"
        message = MODULE.build_message([plain], {})

        self.assertIn("作者发言", message)
        self.assertNotIn("引用推文", message)

    def test_unicode_entities_and_media_only_quote_render(self):
        example = self.example()
        quote = example["quoted_tweet"]
        quote["translation_status"] = "no_translation_needed"
        quote["text"] = "A &amp; B"
        self.assertIn("A & B", MODULE.build_message([example], {}))
        quote["text"] = ""
        quote["has_media"] = True
        self.assertIn("（无文字正文，含媒体）", MODULE.build_message([example], {}))

    @mock.patch.object(MODULE.subprocess, "run")
    def test_dispatch_sends_quote_once_and_registers_filtered_row_once(self, run):
        example = self.example()
        skipped = row("https://x.com/example/status/102", "promotional")
        run.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps({"ok": True, "data": {"message_id": "om_example"}}),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            ledger = workspace / "tweet_ledger.jsonl"
            send_ledger = workspace / "feishu_send_ledger.jsonl"
            ledger.write_text(
                "\n".join(json.dumps(r) for r in [example, skipped]) + "\n", encoding="utf-8"
            )
            with mock.patch.multiple(
                MODULE, LEDGER=ledger, SEND_LEDGER=send_ledger, WORKSPACE=workspace,
                PROFILE="example-profile", CHAT_ID="oc_example",
            ), mock.patch.object(MODULE, "aliases", return_value={}):
                self.assertEqual(MODULE.main(["dispatch"]), 0)
                self.assertEqual(MODULE.main(["dispatch"]), 0)
            saved = MODULE.read_rows(send_ledger)

        run.assert_called_once()
        command = run.call_args.args[0]
        message = command[command.index("--text") + 1]
        self.assertIn("引用推文｜示例团队（@example_source）", message)
        self.assertNotIn("status/102", message)
        self.assertEqual([r["status"] for r in saved], ["skipped", "sent"])
        self.assertEqual(saved[1]["url"], example["url"])


class ReadOnlyCheckTests(unittest.TestCase):
    @mock.patch.object(MODULE.subprocess, "run")
    def test_check_reports_current_policy_and_never_mutates_or_sends(self, run):
        ready = row("https://x.com/example/status/1", "substantive")
        filtered = row("https://x.com/example/status/2", "low_signal")
        old_policy = row("https://x.com/example/status/3", "substantive", "v1")
        unfinished_quote = row("https://x.com/example/status/4", "uncertain")
        unfinished_quote["quoted_tweet"] = {"text": "New product", "translation_status": "failed"}
        with tempfile.TemporaryDirectory() as temporary:
            ledger = pathlib.Path(temporary) / "tweet_ledger.jsonl"
            send_ledger = pathlib.Path(temporary) / "feishu_send_ledger.jsonl"
            ledger.write_text("\n".join(json.dumps(r) for r in [ready, filtered, old_policy, unfinished_quote]) + "\n")
            send_ledger.write_text("")
            before = (ledger.read_bytes(), send_ledger.read_bytes())
            output = io.StringIO()
            with mock.patch.multiple(MODULE, LEDGER=ledger, SEND_LEDGER=send_ledger), redirect_stdout(output):
                code = MODULE.main(["check"])
            self.assertEqual((ledger.read_bytes(), send_ledger.read_bytes()), before)

        run.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "content_policy_version": MODULE.CONTENT_POLICY_VERSION,
            "send_ready": 1, "skip_ready": 1, "awaiting_review": 1, "awaiting_translation": 1,
        })

    @mock.patch.object(MODULE, "append_skipped_rows")
    @mock.patch.object(MODULE, "read_rows")
    def test_check_returns_success_only_when_review_and_translation_complete(self, read_rows, append):
        read_rows.side_effect = [[row("https://x.com/example/status/1", "substantive")], []]
        with redirect_stdout(io.StringIO()):
            code = MODULE.main(["check"])

        self.assertEqual(code, 0)
        append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
