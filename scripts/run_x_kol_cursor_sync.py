#!/usr/bin/env python3
"""Run the existing sync only when a watched profile's latest post ID advances."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import os
from pathlib import Path


WORKSPACE = Path(os.environ.get("X_KOL_WORKSPACE", Path.cwd()))
SYNC = WORKSPACE / "src/x_kol_sync/sync_x_kols.py"
CURSORS = WORKSPACE / "data/x_cursors.json"
PYTHON = os.environ.get("X_KOL_PYTHON", sys.executable)
OPENCLI = os.environ.get("X_KOL_OPENCLI", "opencli")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=WORKSPACE, text=True, capture_output=True, check=False)


def profiles() -> list[str]:
    result = run([PYTHON, str(SYNC), "list-profiles", "--workspace", str(WORKSPACE)])
    if result.returncode:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    rows = json.loads(result.stdout)
    return [row["profile_url"] for row in rows if row.get("enabled")]


def probe(profile_url: str) -> int | None:
    handle = profile_url.rstrip("/").rsplit("/", 1)[-1]
    result = run([OPENCLI, "twitter", "tweets", f"@{handle}", "--limit", "1", "-f", "json"])
    if result.returncode:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    rows = json.loads(result.stdout)
    ids = [int(row["id"]) for row in rows if str(row.get("id", "")).isdigit()]
    return max(ids) if ids else None


def main() -> int:
    doctor = run([PYTHON, str(SYNC), "doctor", "--workspace", str(WORKSPACE)])
    print(doctor.stdout, end="")
    if doctor.returncode:
        print(doctor.stderr, file=sys.stderr, end="")
        return doctor.returncode

    try:
        cursors = json.loads(CURSORS.read_text(encoding="utf-8")) if CURSORS.exists() else {}
    except json.JSONDecodeError:
        cursors = {}

    watched = profiles()
    changed = False
    latest: dict[str, int] = {}
    for profile_url in watched:
        value = probe(profile_url)
        if value is not None:
            latest[profile_url] = value
            if str(cursors.get(profile_url, "")) != str(value):
                changed = True

    if not changed and watched and cursors:
        print(json.dumps({"status": "ok", "mode": "cursor-probe", "profiles": watched}, ensure_ascii=False))
        return 0

    sync = run([PYTHON, str(SYNC), "sync", "--workspace", str(WORKSPACE)])
    print(sync.stdout, end="")
    if sync.returncode:
        print(sync.stderr, file=sys.stderr, end="")
        return sync.returncode

    # Advance cursors only after the full sync succeeds.
    ledger = WORKSPACE / "data/tweet_ledger.jsonl"
    for line in ledger.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        profile_url = row.get("profile")
        post_id = row.get("id")
        if profile_url in watched and str(post_id).isdigit():
            cursors[profile_url] = max(int(cursors.get(profile_url, 0)), int(post_id))
    CURSORS.write_text(json.dumps(cursors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
