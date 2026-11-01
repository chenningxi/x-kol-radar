#!/usr/bin/env python3
"""Cursor-gated X sync plus reviewed, idempotent Feishu dispatch."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import subprocess
import sys
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
CONTENT_POLICY_VERSION = "v2"
TRANSLATION_READY = {"translated", "not_needed", "no_translation_needed"}
SEND_CLASSIFICATIONS = {"substantive", "uncertain"}
SKIP_CLASSIFICATIONS = {"promotional", "low_signal"}


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


def partition_dispatch_rows(
    rows: list[dict],
    sent_urls: set[str],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Split unsent rows into send, skip, review, and translation queues."""
    to_send: list[dict] = []
    to_skip: list[dict] = []
    awaiting_review: list[dict] = []
    awaiting_translation: list[dict] = []

    for row in rows:
        if row.get("url") in sent_urls:
            continue

        classification = str(row.get("content_classification") or "").strip().lower()
        policy_version = str(row.get("content_policy_version") or "").strip().lower()
        if policy_version != CONTENT_POLICY_VERSION:
            awaiting_review.append(row)
        elif classification in SKIP_CLASSIFICATIONS:
            to_skip.append(row)
        elif classification in SEND_CLASSIFICATIONS:
            quote = row.get("quoted_tweet")
            quote_ready = (
                not isinstance(quote, dict)
                or not quote.get("text")
                or quote.get("translation_status") in TRANSLATION_READY
            )
            if row.get("translation_status") in TRANSLATION_READY and quote_ready:
                to_send.append(row)
            else:
                awaiting_translation.append(row)
        else:
            awaiting_review.append(row)

    return to_send, to_skip, awaiting_review, awaiting_translation


def append_skipped_rows(rows: list[dict]) -> None:
    if not rows:
        return
    with SEND_LEDGER.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({
                "sent_at": now(),
                "url": row.get("url"),
                "profile": row.get("profile"),
                "status": "skipped",
                "message_id": None,
                "chat_id": "",
                "error": "",
                "content_classification": row.get("content_classification"),
                "content_reason": row.get("content_reason", ""),
                "content_policy_version": row.get("content_policy_version"),
            }, ensure_ascii=False, separators=(",", ":")) + "\n")


def display_text(row: dict) -> str:
    value = row.get("translation_zh") if row.get("translation_status") == "translated" else row.get("text")
    text = " ".join(html.unescape(str(value or "")).split())
    if len(text) > 500:
        text = text[:500] + "…"
    return text or ("（无文字正文，含媒体）" if row.get("has_media") else "（无文字正文）")


def author_label(row: dict, alias_map: dict[str, str]) -> str:
    author = str(row.get("author") or "").lstrip("@")
    normalized_aliases = {key.rstrip("/").lower(): value for key, value in alias_map.items()}
    name = normalized_aliases.get(f"https://x.com/{author.lower()}") or row.get("name")
    if not name and row.get("profile"):
        name = alias_map.get(row["profile"])
    if author:
        return f"{name}（@{author}）" if name else f"@{author}"
    return str(name or "未知作者")


def build_message(rows: list[dict], alias_map: dict[str, str]) -> str:
    lines = [f"X KOL 新增推文 · {datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')}", ""]
    for row in rows:
        lines.extend([
            "━━━━━━━━━━━━",
            f"【{author_label(row, alias_map)}】",
            f"发布时间：{published_at(row)}",
            "",
            "作者发言（中文翻译）：" if row.get("translation_status") == "translated" else "作者发言：",
            display_text(row),
        ])
        quote = row.get("quoted_tweet")
        if isinstance(quote, dict):
            lines.extend([
                "",
                f"引用推文｜{author_label(quote, alias_map)}",
                f"引用发布时间：{published_at(quote)}",
                "引用内容（中文翻译）：" if quote.get("translation_status") == "translated" else "引用内容：",
                display_text(quote),
                f"引用链接：{quote.get('url') or '不可用'}",
            ])
        elif row.get("quote_status") == "unavailable":
            lines.extend(["", "引用推文：详情不可用（可能已删除、受限或未返回）"])
        lines.extend(["", f"原文链接：{row.get('url')}"])
    lines.append("━━━━━━━━━━━━")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("sync", "dispatch", "check"), nargs="?", default="sync")
    args = parser.parse_args(argv)

    if args.mode == "sync":
        sync = subprocess.run([PYTHON, str(CURSOR_RUNNER)], cwd=WORKSPACE, text=True, capture_output=True, check=False)
        sys.stdout.write(sync.stdout)
        if sync.returncode:
            sys.stderr.write(sync.stderr)
            return sync.returncode
        return 0

    after = read_rows(LEDGER)
    sent = read_rows(SEND_LEDGER)
    sent_urls = {
        row.get("url") for row in sent
        if row.get("status") in {"sent", "skipped"}
    }

    pending, skipped, awaiting_review, awaiting_translation = partition_dispatch_rows(after, sent_urls)
    if args.mode == "check":
        print(json.dumps({
            "content_policy_version": CONTENT_POLICY_VERSION,
            "send_ready": len(pending),
            "skip_ready": len(skipped),
            "awaiting_review": len(awaiting_review),
            "awaiting_translation": len(awaiting_translation),
        }, ensure_ascii=False))
        return 1 if awaiting_review or awaiting_translation else 0
    append_skipped_rows(skipped)
    if skipped:
        print(f"content_policy_skipped={len(skipped)}")
    if awaiting_review:
        print(f"content_policy_awaiting_review={len(awaiting_review)}")
    if awaiting_translation:
        print(f"content_policy_awaiting_translation={len(awaiting_translation)}")
    if not pending:
        return 0

    if not PROFILE or not CHAT_ID:
        print(
            "Missing X_KOL_FEISHU_PROFILE or X_KOL_FEISHU_CHAT_ID; "
            "cannot dispatch to Feishu.",
            file=sys.stderr,
        )
        return 2

    message = build_message(pending, aliases())
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
