#!/usr/bin/env python3
"""Preview/send one quote sample to an explicit test recipient, not dispatch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from run_x_kol_dispatch import TRANSLATION_READY, build_message, now, read_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-file", type=Path, required=True, help="JSON object containing one translated quote sample")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--send", action="store_true", help="send only after the test recipient is authorized")
    parser.add_argument("--profile", default=os.environ.get("X_KOL_FEISHU_PROFILE", ""))
    parser.add_argument("--test-chat-id", default=os.environ.get("X_KOL_FEISHU_TEST_CHAT_ID", ""))
    parser.add_argument("--test-user-id", default=os.environ.get("X_KOL_FEISHU_TEST_USER_ID", ""))
    args = parser.parse_args(argv)
    sample = json.loads(args.sample_file.read_text(encoding="utf-8"))
    quote = sample.get("quoted_tweet")
    if not isinstance(quote, dict) or not quote.get("url"):
        parser.error("sample must contain a quoted_tweet with a URL")
    message = "X KOL 引用展示测试（非正式推送）\n" + build_message([sample], {}).split("\n", 1)[1]
    if not args.send:
        print(message)
        return 0
    if not args.profile or bool(args.test_chat_id) == bool(args.test_user_id):
        parser.error("sending requires a profile and exactly one explicit test recipient")
    if sample.get("translation_status") not in TRANSLATION_READY or quote.get("text") and quote.get("translation_status") not in TRANSLATION_READY:
        parser.error("author and quote translations must be complete before sending")
    flag, recipient = ("--chat-id", args.test_chat_id) if args.test_chat_id else ("--user-id", args.test_user_id)
    key = "xkol-quote-test-" + hashlib.sha256((args.profile + flag + recipient + message).encode()).hexdigest()[:32]
    # This independent ledger never changes normal send/skipped state.
    receipt_path = args.workspace / "data/feishu_quote_test_ledger.jsonl"
    if any(r.get("key") == key and r.get("status") == "sent" for r in read_rows(receipt_path)):
        print(json.dumps({"status": "already_tested"}))
        return 0
    command = ["lark-cli", "--profile", args.profile, "im", "+messages-send", "--as", "bot",
               flag, recipient, "--text", message, "--idempotency-key", key, "--format", "json"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    try:
        response = json.loads(result.stdout or result.stderr)
    except json.JSONDecodeError:
        response = {}
    data = response.get("data") if isinstance(response, dict) else None
    message_id = data.get("message_id") if isinstance(data, dict) else None
    success = result.returncode == 0 and isinstance(response, dict) and response.get("ok") is True and isinstance(message_id, str) and message_id.startswith("om_")
    receipt = {"key": key, "sent_at": now(), "url": sample.get("url"), "quote_url": quote["url"],
               "status": "sent" if success else "failed", "message_id": message_id,
               "recipient": recipient, "exit_code": result.returncode}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    print(json.dumps({"status": receipt["status"], "message_id": message_id}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
