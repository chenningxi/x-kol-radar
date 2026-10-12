import argparse
import csv
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).parents[1] / "sync_x_kols.py"
SPEC = importlib.util.spec_from_file_location("sync_x_kols", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ProfileTests(unittest.TestCase):
    def test_normalizes_profile_forms(self):
        cases = {
            "creator": ("creator", "https://x.com/creator"),
            "@creator": ("creator", "https://x.com/creator"),
            "https://twitter.com/Creator/": ("Creator", "https://x.com/Creator"),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(MODULE.normalize_profile(value), expected)

    def test_rejects_status_and_non_x_urls(self):
        for value in ("https://x.com/a/status/1", "https://example.com/a", "https://x.com/home"):
            with self.subTest(value=value):
                with self.assertRaises(MODULE.SyncError):
                    MODULE.normalize_profile(value)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self.temp.name)
        MODULE.initialize_workspace(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **overrides):
        values = {
            "workspace": self.workspace,
            "profile": "https://x.com/Creator",
            "limit": 100,
            "include_retweets": False,
            "disabled": False,
            "notes": "demo",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_init_is_non_destructive(self):
        paths = MODULE.workspace_paths(self.workspace)
        paths["ledger"].write_text('{"url":"https://x.com/a/status/1"}\n', encoding="utf-8")
        result = MODULE.initialize_workspace(self.workspace)
        self.assertEqual(result["created"], [])
        self.assertIn("status/1", paths["ledger"].read_text(encoding="utf-8"))

    def test_add_profile_is_idempotent(self):
        first = MODULE.add_profile(self.args())
        second = MODULE.add_profile(self.args(limit=500, notes="changed"))
        self.assertEqual(first["status"], "added")
        self.assertEqual(second["status"], "existing")
        profiles = MODULE.load_profiles(MODULE.workspace_paths(self.workspace)["profiles"])
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["limit"], 100)
        self.assertEqual(profiles[0]["notes"], "demo")

    def test_markdown_watchlist_is_readable_and_round_trips_notes(self):
        notes = "AI | tools\n重点"
        MODULE.add_profile(self.args(notes=notes))
        profiles_path = MODULE.workspace_paths(self.workspace)["profiles"]
        rendered = profiles_path.read_text(encoding="utf-8")
        self.assertIn("# X KOL 监控名单", rendered)
        self.assertIn("<https://x.com/Creator>", rendered)
        self.assertIn("是 | 100 | 否", rendered)
        self.assertIn(r"AI \| tools\n重点", rendered)
        profile = MODULE.load_profiles(profiles_path)[0]
        self.assertEqual(profile["notes"], notes)

    def test_set_profile_changes_only_explicit_fields(self):
        MODULE.add_profile(self.args())
        update = argparse.Namespace(
            workspace=self.workspace,
            profile="creator",
            enabled=False,
            limit=None,
            include_retweets=None,
            notes=None,
        )
        MODULE.set_profile(update)
        profile = MODULE.load_profiles(MODULE.workspace_paths(self.workspace)["profiles"])[0]
        self.assertFalse(profile["enabled"])
        self.assertEqual(profile["limit"], 100)

    def test_duplicate_handle_rows_are_rejected_case_insensitively(self):
        profiles_path = MODULE.workspace_paths(self.workspace)["profiles"]
        profiles_path.write_text(
            "# X KOL 监控名单\n\n"
            "| X 账号 | 启用 | 单次抓取上限 | 包含转推 | 备注 |\n"
            "|---|---:|---:|---:|---|\n"
            "| <https://x.com/Creator> | 是 | 100 | 否 | |\n"
            "| <https://x.com/creator> | 是 | 100 | 否 | |\n",
            encoding="utf-8",
        )
        with self.assertRaises(MODULE.SyncError):
            MODULE.load_profiles(profiles_path)

    def test_init_migrates_legacy_csv_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            paths = MODULE.workspace_paths(workspace)
            paths["data"].mkdir(parents=True)
            with paths["legacy_profiles"].open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(MODULE.PROFILE_FIELDS)
                writer.writerow(["https://x.com/Creator", "true", "250", "false", "legacy"])

            result = MODULE.initialize_workspace(workspace)

            self.assertEqual(result["migrated_from"], str(paths["legacy_profiles"]))
            self.assertFalse(paths["legacy_profiles"].exists())
            self.assertTrue(paths["legacy_profiles_backup"].is_file())
            profile = MODULE.load_profiles(paths["profiles"])[0]
            self.assertEqual(profile["profile_url"], "https://x.com/Creator")
            self.assertEqual(profile["limit"], 250)
            self.assertEqual(profile["notes"], "legacy")


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self.temp.name)
        MODULE.initialize_workspace(self.workspace)
        MODULE.add_profile(
            argparse.Namespace(
                workspace=self.workspace,
                profile="creator",
                limit=100,
                include_retweets=False,
                disabled=False,
                notes="",
            )
        )
    def tearDown(self):
        self.temp.cleanup()

    def sync_args(self):
        return argparse.Namespace(
            workspace=self.workspace,
            timeout=30,
            dry_run=False,
            skip_doctor=True,
        )

    @mock.patch.object(MODULE, "run_fetch")
    def test_appends_only_unseen_urls_across_runs(self, run_fetch):
        run_fetch.return_value = [
            {"id": "1", "url": "https://x.com/creator/status/1", "text": "one"},
            {"id": "2", "url": "https://x.com/creator/status/2", "text": "two"},
        ]
        first, first_code = MODULE.synchronize(self.sync_args())
        second, second_code = MODULE.synchronize(self.sync_args())
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first["totals"]["added"], 2)
        self.assertEqual(second["totals"]["added"], 0)
        self.assertEqual(second["totals"]["skipped"], 2)
        ledger = MODULE.workspace_paths(self.workspace)["ledger"].read_text(encoding="utf-8")
        self.assertEqual(len([line for line in ledger.splitlines() if line]), 2)

    @mock.patch.object(MODULE, "run_fetch")
    def test_existing_url_variants_are_deduplicated_without_rewriting_ledger(self, run_fetch):
        paths = MODULE.workspace_paths(self.workspace)
        paths["ledger"].write_text(
            '{"url":"https://twitter.com/Creator/status/1?s=20"}\n',
            encoding="utf-8",
        )
        run_fetch.return_value = [
            {"id": "1", "url": "https://x.com/creator/status/1", "text": "one"},
        ]
        payload, code = MODULE.synchronize(self.sync_args())
        self.assertEqual(code, 0)
        self.assertEqual(payload["totals"]["added"], 0)
        self.assertEqual(payload["totals"]["skipped"], 1)
        ledger_lines = [line for line in paths["ledger"].read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(ledger_lines), 1)

    @mock.patch.object(MODULE, "run_fetch")
    def test_new_urls_are_canonicalized_before_append(self, run_fetch):
        run_fetch.return_value = [
            {"id": "1", "url": "https://twitter.com/Creator/status/1?s=20", "text": "one"},
        ]
        payload, code = MODULE.synchronize(self.sync_args())
        self.assertEqual(code, 0)
        self.assertEqual(payload["totals"]["added"], 1)
        ledger_path = MODULE.workspace_paths(self.workspace)["ledger"]
        row = json.loads(ledger_path.read_text(encoding="utf-8").strip())
        self.assertEqual(row["url"], "https://x.com/creator/status/1")

    @mock.patch.object(MODULE, "run_fetch")
    def test_profile_failure_is_recorded(self, run_fetch):
        run_fetch.side_effect = MODULE.SyncError("bridge unavailable", 1)
        payload, code = MODULE.synchronize(self.sync_args())
        self.assertEqual(code, 1)
        self.assertEqual(payload["totals"]["failed"], 1)
        history_path = MODULE.workspace_paths(self.workspace)["history"]
        row = json.loads(history_path.read_text(encoding="utf-8").strip())
        self.assertEqual(row["status"], "error")
        self.assertIn("bridge unavailable", row["error"])

    @mock.patch.object(MODULE, "run_fetch")
    def test_empty_fetch_is_recorded_as_success(self, run_fetch):
        run_fetch.return_value = []
        payload, code = MODULE.synchronize(self.sync_args())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["totals"],
            {"fetched": 0, "added": 0, "skipped": 0, "failed": 0},
        )
        history_path = MODULE.workspace_paths(self.workspace)["history"]
        row = json.loads(history_path.read_text(encoding="utf-8").strip())
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["fetched"], 0)


if __name__ == "__main__":
    unittest.main()
