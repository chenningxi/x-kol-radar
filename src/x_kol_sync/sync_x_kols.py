#!/usr/bin/env python3
"""Maintain a multi-profile X watchlist and append only unseen post URLs."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse


PROFILE_FIELDS = ("profile_url", "alias", "enabled", "limit", "include_retweets", "notes")
MARKDOWN_HEADERS = ("X 账号", "别名", "启用", "单次抓取上限", "包含转推", "备注")
LEGACY_MARKDOWN_HEADERS = ("X 账号", "启用", "单次抓取上限", "包含转推", "备注")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
STATUS_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/?#]+)/status/(\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
ALLOWED_HOSTS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
RESERVED_ROUTES = {
    "compose",
    "explore",
    "hashtag",
    "home",
    "i",
    "intent",
    "messages",
    "notifications",
    "search",
    "settings",
}
TRUE_VALUES = {"1", "true", "yes", "y", "on", "是", "启用"}
FALSE_VALUES = {"0", "false", "no", "n", "off", "", "否", "禁用"}


class SyncError(RuntimeError):
    """Expected operational error with a stable process exit code."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def workspace_paths(workspace: Path) -> dict[str, Path]:
    root = workspace.expanduser().resolve()
    data_dir = root / "data"
    return {
        "root": root,
        "data": data_dir,
        "profiles": data_dir / "x_profiles.md",
        "legacy_profiles": data_dir / "x_profiles.csv",
        "legacy_profiles_backup": data_dir / "x_profiles.csv.migrated.bak",
        "ledger": data_dir / "tweet_ledger.jsonl",
        "history": data_dir / "sync_history.jsonl",
        "lock": data_dir / ".x-kol-sync.lock",
    }


def normalize_profile(value: str) -> tuple[str, str]:
    raw = value.strip()
    if raw.startswith("@"):
        raw = raw[1:]
    if HANDLE_RE.fullmatch(raw):
        handle = raw
    else:
        candidate = raw
        if re.match(r"^(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/", candidate, re.I):
            candidate = "https://" + candidate
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
            raise SyncError("Only x.com or twitter.com profile URLs and valid handles are accepted")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise SyncError("The profile URL does not contain a handle")
        if len(parts) > 1:
            raise SyncError("A profile URL is required; status URLs and other routes are not accepted")
        handle = parts[0]
    if not HANDLE_RE.fullmatch(handle) or handle.lower() in RESERVED_ROUTES:
        raise SyncError(f"Invalid X profile handle: {handle}")
    return handle, f"https://x.com/{handle}"


def parse_bool(value: str, field: str, row_number: int) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise SyncError(f"Row {row_number}: {field} is not a valid boolean: {value!r}")


def validate_limit(value: int, field: str = "limit") -> int:
    if not 1 <= value <= 10_000:
        raise SyncError(f"{field} must be between 1 and 10000")
    return value


def initialize_workspace(workspace: Path) -> dict[str, Any]:
    paths = workspace_paths(workspace)
    paths["data"].mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    migration: dict[str, str] = {}
    if not paths["profiles"].exists():
        if paths["legacy_profiles"].is_file():
            if paths["legacy_profiles_backup"].exists():
                raise SyncError(
                    "Cannot migrate legacy watchlist because backup already exists: "
                    + str(paths["legacy_profiles_backup"])
                )
            profiles = load_legacy_profiles(paths["legacy_profiles"])
            write_profiles(paths["profiles"], profiles)
            os.replace(paths["legacy_profiles"], paths["legacy_profiles_backup"])
            migration = {
                "migrated_from": str(paths["legacy_profiles"]),
                "legacy_backup": str(paths["legacy_profiles_backup"]),
            }
        else:
            write_profiles(paths["profiles"], [])
        created.append(str(paths["profiles"]))
    for key in ("ledger", "history"):
        if not paths[key].exists():
            paths[key].touch()
            created.append(str(paths[key]))
    return {
        "status": "initialized",
        "workspace": str(paths["root"]),
        "created": created,
        **migration,
    }


def require_initialized(workspace: Path) -> dict[str, Path]:
    paths = workspace_paths(workspace)
    missing = [str(paths[key]) for key in ("profiles", "ledger", "history") if not paths[key].is_file()]
    if missing:
        raise SyncError("Workspace is not initialized; missing: " + ", ".join(missing))
    return paths


def split_markdown_row(line: str, row_number: int) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        raise SyncError(f"Row {row_number}: expected a Markdown table row")

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in stripped[1:-1]:
        if escaped:
            if character == "n":
                current.append("\n")
            elif character in {"|", "\\"}:
                current.append(character)
            else:
                current.extend(("\\", character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def profile_value_from_markdown(cell: str) -> str:
    value = cell.strip()
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1].strip()
    link = re.fullmatch(r"\[[^\]]+\]\(([^)]+)\)", value)
    return link.group(1).strip() if link else value


def load_profiles(path: Path, enabled_only: bool = False) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    seen_handles: set[str] = set()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header_index: int | None = None
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        if tuple(split_markdown_row(line, index + 1)) in {MARKDOWN_HEADERS, LEGACY_MARKDOWN_HEADERS}:
            header_index = index
            break
    if header_index is None:
        raise SyncError("Watchlist is missing the required Markdown table header")
    if header_index + 1 >= len(lines):
        raise SyncError("Watchlist is missing the Markdown table separator row")
    separator = split_markdown_row(lines[header_index + 1], header_index + 2)
    header = tuple(split_markdown_row(lines[header_index], header_index + 1))
    expected_columns = len(header)
    if len(separator) != expected_columns or not all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in separator
    ):
        raise SyncError("Watchlist has an invalid Markdown table separator row")

    for index, line in enumerate(lines[header_index + 2 :], start=header_index + 3):
        if not line.strip() or not line.lstrip().startswith("|"):
            continue
        cells = split_markdown_row(line, index)
        if len(cells) != expected_columns:
            raise SyncError(
                f"Row {index}: expected {expected_columns} cells, found {len(cells)}"
            )
        if header == MARKDOWN_HEADERS:
            raw_profile, raw_alias, raw_enabled, raw_limit, raw_retweets, notes = cells
        else:
            raw_profile, raw_enabled, raw_limit, raw_retweets, notes = cells
            raw_alias = ""
        if not raw_profile:
            continue
        handle_name, canonical_url = normalize_profile(profile_value_from_markdown(raw_profile))
        identity = handle_name.lower()
        if identity in seen_handles:
            raise SyncError(f"Duplicate profile handle in watchlist at row {index}: {handle_name}")
        seen_handles.add(identity)
        try:
            limit = int(raw_limit) if raw_limit else 100
        except ValueError as exc:
            raise SyncError(f"Row {index}: limit is not an integer: {raw_limit!r}") from exc
        profile = {
            "profile_url": canonical_url,
            "handle": handle_name,
            "alias": raw_alias.strip() or handle_name,
            "enabled": parse_bool(raw_enabled, "enabled", index),
            "limit": validate_limit(limit, f"row {index} limit"),
            "include_retweets": parse_bool(raw_retweets, "include_retweets", index),
            "notes": notes,
        }
        if not enabled_only or profile["enabled"]:
            profiles.append(profile)
    return profiles


def load_legacy_profiles(path: Path) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    seen_handles: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not set(PROFILE_FIELDS).issubset(reader.fieldnames):
            legacy_fields = ("profile_url", "enabled", "limit", "include_retweets", "notes")
            if reader.fieldnames is None or not set(legacy_fields).issubset(reader.fieldnames):
                raise SyncError("Legacy watchlist must contain fields: " + ", ".join(PROFILE_FIELDS))
        for row_number, row in enumerate(reader, start=2):
            raw_profile = (row.get("profile_url") or "").strip()
            if not raw_profile:
                continue
            handle_name, canonical_url = normalize_profile(raw_profile)
            identity = handle_name.lower()
            if identity in seen_handles:
                raise SyncError(
                    f"Duplicate profile handle in legacy watchlist at row {row_number}: {handle_name}"
                )
            seen_handles.add(identity)
            raw_alias = (row.get("alias") or "").strip()
            raw_enabled = row.get("enabled") or ""
            raw_limit = (row.get("limit") or "").strip()
            # Accept the pre-alias five-column CSV shape.
            if raw_alias.lower() in TRUE_VALUES | FALSE_VALUES:
                raw_notes = row.get("notes") or ""
                row = {
                    "enabled": raw_alias,
                    "limit": row.get("enabled") or "",
                    "include_retweets": row.get("limit") or "",
                    "notes": row.get("include_retweets") or raw_notes,
                }
                raw_limit = (row.get("limit") or "").strip()
            try:
                limit = int(raw_limit) if raw_limit else 100
            except ValueError as exc:
                raise SyncError(
                    f"Legacy row {row_number}: limit is not an integer: {raw_limit!r}"
                ) from exc
            profiles.append(
                {
                    "profile_url": canonical_url,
                    "handle": handle_name,
                    "enabled": parse_bool(row.get("enabled") or "", "enabled", row_number),
                    "limit": validate_limit(limit, f"legacy row {row_number} limit"),
                    "include_retweets": parse_bool(
                        row.get("include_retweets") or "", "include_retweets", row_number
                    ),
                    "alias": raw_alias if raw_alias.lower() not in TRUE_VALUES | FALSE_VALUES else handle_name,
                    "notes": (row.get("notes") or "").strip(),
                }
            )
    return profiles


def escape_markdown_cell(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("|", "\\|")


def render_profiles(profiles: list[dict[str, Any]]) -> str:
    lines = [
        "# X KOL 监控名单",
        "",
        "> 建议通过 skill 命令增删或修改账号；手动编辑时请保留表头和六列结构。",
        "",
        "| " + " | ".join(MARKDOWN_HEADERS) + " |",
        "|---|---|---:|---:|---:|---|",
    ]
    for profile in profiles:
        cells = (
            f"<{profile['profile_url']}>",
            escape_markdown_cell(profile.get("alias") or profile["handle"]),
            "是" if profile["enabled"] else "否",
            str(profile["limit"]),
            "是" if profile["include_retweets"] else "否",
            escape_markdown_cell(profile["notes"]),
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write_profiles(path: Path, profiles: list[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(render_profiles(profiles))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def add_profile(args: argparse.Namespace) -> dict[str, Any]:
    paths = require_initialized(args.workspace)
    profiles = load_profiles(paths["profiles"])
    handle_name, canonical_url = normalize_profile(args.profile)
    for profile in profiles:
        if profile["handle"].lower() == handle_name.lower():
            return {"status": "existing", "profile": profile}
    profile = {
        "profile_url": canonical_url,
        "handle": handle_name,
            "alias": getattr(args, "alias", None) or handle_name,
        "enabled": not args.disabled,
        "limit": validate_limit(args.limit),
        "include_retweets": args.include_retweets,
        "notes": args.notes or "",
    }
    profiles.append(profile)
    write_profiles(paths["profiles"], profiles)
    return {"status": "added", "profile": profile}


def set_profile(args: argparse.Namespace) -> dict[str, Any]:
    paths = require_initialized(args.workspace)
    profiles = load_profiles(paths["profiles"])
    handle_name, _ = normalize_profile(args.profile)
    if all(value is None for value in (getattr(args, "alias", None), args.enabled, args.limit, args.include_retweets, args.notes)):
        raise SyncError("set-profile requires at least one field to update")
    for profile in profiles:
        if profile["handle"].lower() != handle_name.lower():
            continue
        if getattr(args, "alias", None) is not None:
            profile["alias"] = args.alias
        if args.enabled is not None:
            profile["enabled"] = args.enabled
        if args.limit is not None:
            profile["limit"] = validate_limit(args.limit)
        if args.include_retweets is not None:
            profile["include_retweets"] = args.include_retweets
        if args.notes is not None:
            profile["notes"] = args.notes
        write_profiles(paths["profiles"], profiles)
        return {"status": "updated", "profile": profile}
    raise SyncError(f"Profile is not in the watchlist: {handle_name}")


def resolve_fetch_script() -> Path:
    bundled = Path(__file__).with_name("fetch_tweet_links.py").resolve()
    if bundled.is_file():
        return bundled
    raise SyncError(f"Bundled fetch wrapper is missing: {bundled}", 3)


def run_doctor() -> tuple[dict[str, Any], int]:
    checks: dict[str, Any] = {}
    try:
        resolved_fetch = resolve_fetch_script()
        checks["fetch_script"] = {"status": "ok", "path": str(resolved_fetch)}
    except SyncError as exc:
        checks["fetch_script"] = {"status": "missing", "error": str(exc)}
        return {"ready": False, "checks": checks}, 3

    opencli_bin = shutil.which("opencli")
    if opencli_bin is None:
        checks["opencli"] = {"status": "missing", "error": "opencli executable not found"}
        return {"ready": False, "checks": checks}, 3

    version = subprocess.run(
        [opencli_bin, "--version"], capture_output=True, text=True, check=False, timeout=30
    )
    checks["opencli"] = {
        "status": "ok" if version.returncode == 0 else "error",
        "path": opencli_bin,
        "version": (version.stdout or version.stderr).strip(),
    }
    try:
        doctor = subprocess.run(
            [opencli_bin, "doctor"], capture_output=True, text=True, check=False, timeout=60
        )
    except subprocess.TimeoutExpired:
        checks["browser_bridge"] = {"status": "error", "error": "opencli doctor timed out"}
        return {"ready": False, "checks": checks}, 3

    diagnostic = (doctor.stdout or doctor.stderr).strip()
    unhealthy = doctor.returncode != 0 or "[MISSING]" in diagnostic or "[FAIL]" in diagnostic
    checks["browser_bridge"] = {
        "status": "error" if unhealthy else "ok",
        "diagnostic": diagnostic,
    }
    return {"ready": not unhealthy, "checks": checks}, 3 if unhealthy else 0


def canonical_ledger_url(value: str) -> str:
    """Normalize X status URL variants while preserving unknown legacy keys."""
    stripped = value.strip()
    match = STATUS_URL_RE.match(stripped)
    if match is None:
        return stripped
    author, post_id = match.groups()
    return f"https://x.com/{author.lower()}/status/{post_id}"


def load_known_urls(path: Path) -> set[str]:
    known: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SyncError(f"Ledger row {line_number} is invalid JSON: {exc}") from exc
            url = record.get("url") if isinstance(record, dict) else None
            if not isinstance(url, str) or not url.strip():
                raise SyncError(f"Ledger row {line_number} has no valid url")
            known.add(canonical_ledger_url(url))
    return known


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def sync_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("Another sync is already running for this workspace", 7) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "started_at": utc_now()}))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_fetch(
    fetch_script: Path,
    profile: dict[str, Any],
    dry_run: bool,
    timeout: int,
) -> list[dict[str, Any]] | dict[str, Any]:
    command = [
        sys.executable,
        str(fetch_script),
        profile["profile_url"],
        "--limit",
        str(profile["limit"]),
        "--format",
        "json",
        "--timeout",
        str(timeout),
    ]
    if profile["include_retweets"]:
        command.append("--include-retweets")
    if dry_run:
        command.append("--dry-run")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout + 30,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip()
        if len(diagnostic) > 1200:
            diagnostic = diagnostic[:1200] + "..."
        raise SyncError(
            f"Fetch failed with exit code {completed.returncode}: {diagnostic or 'no diagnostic'}",
            1,
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Fetch wrapper returned invalid JSON: {exc}", 1) from exc
    if dry_run:
        if not isinstance(payload, dict):
            raise SyncError("Fetch wrapper dry-run did not return a JSON object", 1)
        return payload
    if not isinstance(payload, list):
        raise SyncError("Fetch wrapper did not return a JSON array", 1)
    return [item for item in payload if isinstance(item, dict)]


def synchronize(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    paths = require_initialized(args.workspace)
    profiles = load_profiles(paths["profiles"], enabled_only=True)
    fetch_script = resolve_fetch_script()
    if not profiles:
        return {
            "status": "ok",
            "workspace": str(paths["root"]),
            "message": "No enabled profiles",
            "profiles": [],
            "totals": {"fetched": 0, "added": 0, "skipped": 0, "failed": 0},
        }, 0

    if args.dry_run:
        previews = []
        for profile in profiles:
            previews.append(
                {
                    "profile_url": profile["profile_url"],
                    "requested_limit": profile["limit"],
                    "include_retweets": profile["include_retweets"],
                    "fetch": run_fetch(fetch_script, profile, True, args.timeout),
                }
            )
        return {"status": "dry-run", "workspace": str(paths["root"]), "profiles": previews}, 0

    if not args.skip_doctor:
        doctor, doctor_code = run_doctor()
        if doctor_code:
            raise SyncError("Runtime is not ready: " + json.dumps(doctor, ensure_ascii=False), 3)

    with sync_lock(paths["lock"]):
        known_urls = load_known_urls(paths["ledger"])
        run_seen = set(known_urls)
        new_records: list[dict[str, Any]] = []
        history_records: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        run_id = str(uuid.uuid4())
        synced_at = utc_now()

        for profile in profiles:
            result = {
                "profile_url": profile["profile_url"],
                "requested_limit": profile["limit"],
                "include_retweets": profile["include_retweets"],
                "status": "ok",
                "fetched": 0,
                "added": 0,
                "skipped": 0,
                "error": "",
            }
            try:
                fetched = run_fetch(fetch_script, profile, False, args.timeout)
                assert isinstance(fetched, list)
                result["fetched"] = len(fetched)
                for item in fetched:
                    url = item.get("url")
                    if not isinstance(url, str) or not url.strip():
                        continue
                    url = canonical_ledger_url(url)
                    if url in run_seen:
                        continue
                    run_seen.add(url)
                    record = dict(item)
                    record["url"] = url
                    record["first_seen_at"] = synced_at
                    new_records.append(record)
                    result["added"] += 1
                result["skipped"] = result["fetched"] - result["added"]
            except (SyncError, subprocess.TimeoutExpired) as exc:
                result["status"] = "error"
                result["error"] = str(exc)
            results.append(result)
            history_records.append({"run_id": run_id, "synced_at": synced_at, **result})

        append_jsonl(paths["ledger"], new_records)
        append_jsonl(paths["history"], history_records)

    totals = {
        "fetched": sum(result["fetched"] for result in results),
        "added": sum(result["added"] for result in results),
        "skipped": sum(result["skipped"] for result in results),
        "failed": sum(result["status"] == "error" for result in results),
    }
    return {
        "status": "partial-error" if totals["failed"] else "ok",
        "run_id": run_id,
        "synced_at": synced_at,
        "workspace": str(paths["root"]),
        "profiles": results,
        "totals": totals,
        "ledger_unique_before": len(known_urls),
        "ledger_unique_after": len(run_seen),
    }, 1 if totals["failed"] else 0


def read_history(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SyncError(f"History row {line_number} is invalid JSON: {exc}") from exc
            if isinstance(record, dict):
                records.append(record)
    return records


def status(workspace: Path) -> dict[str, Any]:
    paths = require_initialized(workspace)
    profiles = load_profiles(paths["profiles"])
    known_urls = load_known_urls(paths["ledger"])
    history = read_history(paths["history"])
    last_run_id = history[-1].get("run_id") if history else None
    last_results = [row for row in history if row.get("run_id") == last_run_id] if last_run_id else []
    return {
        "workspace": str(paths["root"]),
        "profiles": {"configured": len(profiles), "enabled": sum(p["enabled"] for p in profiles)},
        "ledger_unique_urls": len(known_urls),
        "last_sync_at": last_results[-1].get("synced_at") if last_results else None,
        "last_run_id": last_run_id,
        "last_results": last_results,
    }


def add_common_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, required=True, help="Business data workspace")


def parse_optional_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incrementally sync recent X posts for multiple creators")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create missing workspace state files")
    add_common_workspace(init_parser)

    add_parser = subparsers.add_parser("add-profile", help="Idempotently add one X profile")
    add_common_workspace(add_parser)
    add_parser.add_argument("profile")
    add_parser.add_argument("--limit", type=int, default=100)
    add_parser.add_argument("--include-retweets", action="store_true")
    add_parser.add_argument("--disabled", action="store_true")
    add_parser.add_argument("--notes")
    add_parser.add_argument("--alias", default="")

    set_parser = subparsers.add_parser("set-profile", help="Update an existing profile")
    add_common_workspace(set_parser)
    set_parser.add_argument("profile")
    set_parser.add_argument("--enabled", type=parse_optional_bool)
    set_parser.add_argument("--limit", type=int)
    set_parser.add_argument("--include-retweets", type=parse_optional_bool)
    set_parser.add_argument("--notes")
    set_parser.add_argument("--alias")

    list_parser = subparsers.add_parser("list-profiles", help="List configured profiles")
    add_common_workspace(list_parser)

    doctor_parser = subparsers.add_parser("doctor", help="Check bundled fetch wrapper and OpenCLI runtime")
    add_common_workspace(doctor_parser)

    sync_parser = subparsers.add_parser("sync", help="Run incremental synchronization")
    add_common_workspace(sync_parser)
    sync_parser.add_argument("--timeout", type=int, default=300)
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--skip-doctor", action="store_true", help=argparse.SUPPRESS)

    status_parser = subparsers.add_parser("status", help="Summarize workspace state")
    add_common_workspace(status_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            emit(initialize_workspace(args.workspace))
            return 0
        if args.command == "add-profile":
            emit(add_profile(args))
            return 0
        if args.command == "set-profile":
            emit(set_profile(args))
            return 0
        if args.command == "list-profiles":
            paths = require_initialized(args.workspace)
            emit(load_profiles(paths["profiles"]))
            return 0
        if args.command == "doctor":
            require_initialized(args.workspace)
            payload, code = run_doctor()
            emit(payload)
            return code
        if args.command == "sync":
            if args.timeout < 1:
                raise SyncError("timeout must be positive")
            payload, code = synchronize(args)
            emit(payload)
            return code
        if args.command == "status":
            emit(status(args.workspace))
            return 0
        raise SyncError(f"Unsupported command: {args.command}")
    except SyncError as exc:
        emit({"status": "error", "error": str(exc), "exit_code": exc.exit_code})
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
