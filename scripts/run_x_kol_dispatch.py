#!/usr/bin/env python3
"""Cursor-gated X sync plus idempotent Feishu dispatch ledger."""

from __future__ import annotations

import hashlib
import json
import argparse
import subprocess
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path(os.environ.get("X_KOL_WORKSPACE", Path.cwd()))
CURSOR_RUNNER = WORKSPACE / "scripts/run_x_kol_cursor_sync.py"
LEDGER = WORKSPACE / "data/tweet_ledger.jsonl"
SEND_LEDGER = WORKSPACE / "data/feishu_send_ledger.jsonl"
CHAT_ID = os.environ.get("X_KOL_FEISHU_CHAT_ID", "")
PROFILE = os.environ.get("X_KOL_FEISHU_PROFILE", "")
PYTHON = os.environ.get("X_KOL_PYTHON", sys.executable)
WATCHLIST = WORKSPACE / "data/x_profiles.md"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def published_at(row: dict) -> str:
    value = str(row.get("created_at") or "").strip()
    if not value:
        return "未知时间"
    try:
        parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def aliases() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in WATCHLIST.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|---") or "X 账号" in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        profile = cells[0].strip("<>")
        result[profile] = cells[1] or profile.rsplit("/", 1)[-1]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("sync", "dispatch"), nargs="?", default="sync")
    args = parser.parse_args()

    if args.mode == "sync":
        sync = subprocess.run([PYTHON, str(CURSOR_RUNNER)], cwd=WORKSPACE, text=True, capture_output=True, check=False)
        sys.stdout.write(sync.stdout)
        if sync.returncode:
            sys.stderr.write(sync.stderr)
            return sync.returncode

    after = read_rows(LEDGER)
    sent = read_rows(SEND_LEDGER)
    sent_urls = {
        row.get("url") for row in sent
        if row.get("status") in {"sent", "skipped"}
    }
    pending = [
        row for row in after
        if row.get("url") not in sent_urls
        and row.get("translation_status") in {
            "translated",
            "not_needed",
            "no_translation_needed",
        }
    ]
    if not pending:
        return 0

    if not PROFILE or not CHAT_ID:
        print(
            "Missing X_KOL_FEISHU_PROFILE or X_KOL_FEISHU_CHAT_ID; "
            "cannot dispatch to Feishu.",
            file=sys.stderr,
        )
        return 2

    alias_map = aliases()
    lines = [f"X KOL 新增推文 · {datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')}", ""]
    for row in pending:
        text_value = row.get("translation_zh") if row.get("translation_status") == "translated" else row.get("text")
        text = " ".join(str(text_value or "").split())
        if len(text) > 500:
            text = text[:500] + "…"
        alias = alias_map.get(row.get("profile"), row.get("author", ""))
        lines.extend([
            "━━━━━━━━━━━━",
            f"【{alias}】",
            f"发布时间：{published_at(row)}",
            "",
        ])
        if row.get("translation_status") == "translated":
            lines.append("中文翻译：")
        lines.extend([text or "(无正文)", "", f"原文链接：{row.get('url')}"])
    lines.append("━━━━━━━━━━━━")
    message = "\n".join(lines)
    batch_key = hashlib.sha256("\n".join(sorted(row["url"] for row in pending)).encode()).hexdigest()[:40]
    command = [
        "lark-cli", "--profile", PROFILE, "im", "+messages-send",
        "--as", "bot", "--chat-id", CHAT_ID, "--text", message,
        "--idempotency-key", f"x-kol-{batch_key}", "--format", "json",
    ]
    result = subprocess.run(command, cwd=WORKSPACE, text=True, capture_output=True, check=False)
    raw = (result.stdout or result.stderr).strip()
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        response = {"raw": raw}
    status = "sent" if result.returncode == 0 and response.get("ok") is True else "failed"
    message_id = ((response.get("data") or {}).get("message_id") if isinstance(response, dict) else None)
    with SEND_LEDGER.open("a", encoding="utf-8") as handle:
        for row in pending:
            handle.write(json.dumps({
                "sent_at": now(), "url": row.get("url"), "profile": row.get("profile"),
                "status": status, "message_id": message_id, "chat_id": CHAT_ID,
                "error": "" if status == "sent" else raw[:1200],
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
    if result.returncode:
        sys.stderr.write(raw + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
