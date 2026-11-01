#!/usr/bin/env python3
"""Explicit, resumable quote-context backfill; never sends or resets ledgers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "x_kol_sync"
sys.path.insert(0, str(SOURCE))
from fetch_tweet_links import canonical_post_url, decode_opencli_output, prepare_records


class RateLimitError(RuntimeError):
    """Stop the whole backfill on session rate limits instead of hammering X."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fetch(command: list[str], timeout: int) -> list[dict]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        diagnostic = (result.stderr or result.stdout).lower()
        if "429" in diagnostic or "rate-limited" in diagnostic:
            raise RateLimitError("OpenCLI session rate-limited (HTTP 429); stop and retry after cooldown")
        # Store codes, not arbitrary CLI output that could include private context.
        raise RuntimeError(f"OpenCLI read failed (exit {result.returncode})")
    return decode_opencli_output(result.stdout)


def quote_update(row: dict, item: dict) -> dict:
    expected = canonical_post_url(row, "")
    actual = canonical_post_url(item, "")
    if expected is None or actual is None or expected[0] != actual[0]:
        raise ValueError("Tweet detail identity did not match the ledger URL")
    if "quoted_tweet" not in item:
        raise ValueError("OpenCLI response lacks quoted_tweet support")
    normalized = prepare_records([item], actual[1], True)[0]
    patch = {"quote_lookup_status": "checked", "quote_lookup_updated_at": now()}
    if "quoted_tweet" in normalized:
        patch["quoted_tweet"] = normalized["quoted_tweet"]
        patch["quote_status"] = "available"
    elif "quote_status" in normalized:
        patch["quote_status"] = normalized["quote_status"]
    else:
        # Null means the provider did not supply a quote, not proof of no quote.
        patch["quote_status"] = "not_provided"
    return patch


def merge_patch(row: dict, patch: dict) -> dict:
    result = dict(row)
    old_quote = row.get("quoted_tweet")
    fresh_quote = patch.get("quoted_tweet")
    patch = dict(patch)
    if isinstance(old_quote, dict):
        if not isinstance(fresh_quote, dict):
            # Never discard already-known quote context on an incomplete read.
            patch["quote_status"] = row.get("quote_status", "available")
        elif old_quote.get("url") == fresh_quote.get("url"):
            merged_quote = {**old_quote, **fresh_quote}
            if old_quote.get("text") != fresh_quote.get("text"):
                for key in ("translation_status", "translation_zh", "source_language", "translation_updated_at", "translation_error"):
                    merged_quote.pop(key, None)
            patch["quoted_tweet"] = merged_quote
    result.update(patch)
    return result


def apply_updates(workspace: Path, updates: dict[str, dict]) -> tuple[int, str | None]:
    """Merge into the latest ledger under the existing sync lock, with backup/CAS."""
    if not updates:
        return 0, None
    ledger = workspace / "data/tweet_ledger.jsonl"
    lock = workspace / "data/.x-kol-sync.lock"
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        original = ledger.read_bytes()
        changed = 0
        lines = []
        for line in original.splitlines(keepends=True):
            if not line.strip():
                lines.append(line)
                continue
            row = json.loads(line)
            identity = canonical_post_url(row, "")
            patch = updates.get(identity[0]) if identity else None
            if patch is None:
                lines.append(line)
                continue
            merged = merge_patch(row, patch)
            if merged == row:
                lines.append(line)
                continue
            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
            lines.append(json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode() + ending)
            changed += 1
        if not changed:
            return 0, None
        backups = workspace / "logs/quote-backfill-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / f"tweet-ledger-{uuid.uuid4().hex}.jsonl"
        with backup.open("xb") as saved:
            saved.write(original)
            saved.flush()
            os.fsync(saved.fileno())
        descriptor, temporary = tempfile.mkstemp(prefix=".quote-backfill-", suffix=".tmp", dir=ledger.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(b"".join(lines))
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, ledger.stat().st_mode)
            if ledger.read_bytes() != original:
                raise RuntimeError("Ledger changed during merge; refusing to overwrite concurrent work")
            os.replace(temporary, ledger)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return changed, str(backup.relative_to(workspace))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true", help="explicitly fetch and merge quote details")
    parser.add_argument("--timeline-limit", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--request-delay", type=float, default=2)
    parser.add_argument("--opencli-bin", default=os.environ.get("X_KOL_OPENCLI", "opencli"))
    args = parser.parse_args(argv)
    if not 1 <= args.timeline_limit <= 10000 or args.timeout <= 0 or args.request_delay < 0:
        parser.error("limits/timeouts must be positive and delay must be nonnegative")
    workspace = args.workspace.resolve()
    candidates = [r for r in read_rows(workspace / "data/tweet_ledger.jsonl") if r.get("quote_lookup_status") != "checked"]
    print(json.dumps({"mode": "apply" if args.apply else "preview", "candidates": len(candidates)}, ensure_ascii=False), flush=True)
    if not args.apply or not candidates:
        return 0
    grouped: dict[str, list[dict]] = {}
    for row in candidates:
        grouped.setdefault(row["profile"], []).append(row)
    report = workspace / "data/quote_backfill_report.jsonl"
    totals = {"checked": 0, "quotes": 0, "failed": 0, "updated": 0}
    for group_index, (profile, rows) in enumerate(grouped.items(), 1):
        timeline = fetch([args.opencli_bin, "twitter", "tweets", profile.rstrip("/").rsplit("/", 1)[-1],
                          "--limit", str(args.timeline_limit), "-f", "json"], args.timeout)
        indexed = {}
        for item in timeline:
            identity = canonical_post_url(item, "")
            if identity:
                indexed[identity[0]] = item
        updates, results = {}, []
        for row in rows:
            identity = canonical_post_url(row, "")
            if identity is None:
                raise ValueError("Ledger contains an invalid tweet identity")
            url = identity[0]
            try:
                item = indexed.get(url)
                if item is None:
                    time.sleep(args.request_delay)
                    details = fetch([args.opencli_bin, "twitter", "thread", url, "--limit", "100", "-f", "json"], args.timeout)
                    item = next((r for r in details if canonical_post_url(r, "") == identity), None)
                    if item is None:
                        raise ValueError("Target post unavailable in thread response")
                patch = quote_update(row, item)
                updates[url] = patch
                totals["checked"] += 1
                totals["quotes"] += isinstance(patch.get("quoted_tweet"), dict)
                results.append({"url": url, "status": "checked", "quote_status": patch["quote_status"], "checked_at": patch["quote_lookup_updated_at"]})
            except RateLimitError:
                # Keep this group's successful results before stopping all requests.
                count, backup = apply_updates(workspace, updates)
                totals["updated"] += count
                with report.open("a", encoding="utf-8") as output:
                    for result in results:
                        output.write(json.dumps(result, ensure_ascii=False) + "\n")
                print(json.dumps({"status": "rate_limited", "backup": backup, **totals}), flush=True)
                raise
            except (ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                totals["failed"] += 1
                results.append({"url": url, "status": "failed", "error": str(error), "checked_at": now()})
        count, backup = apply_updates(workspace, updates)
        totals["updated"] += count
        with report.open("a", encoding="utf-8") as output:
            for result in results:
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps({"profile_group": group_index, "total_groups": len(grouped), "backup": backup, **totals}), flush=True)
        time.sleep(args.request_delay)
    print(json.dumps({"status": "partial-error" if totals["failed"] else "ok", **totals}), flush=True)
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
