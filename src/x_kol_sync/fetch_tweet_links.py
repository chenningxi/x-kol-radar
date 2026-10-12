#!/usr/bin/env python3
"""Fetch canonical X post links through OpenCLI's logged-in browser adapter."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


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
OUTPUT_FIELDS = (
    "profile",
    "id",
    "author",
    "created_at",
    "text",
    "likes",
    "retweets",
    "replies",
    "views",
    "url",
)


class FetchError(RuntimeError):
    """Expected operational failure with a stable process exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def normalize_profile(value: str) -> str:
    """Return a validated X handle from a handle or profile URL."""
    raw = value.strip()
    if not raw:
        raise ValueError("Profile URL or handle is required")

    if raw.startswith("@"):
        raw = raw[1:]

    if HANDLE_RE.fullmatch(raw):
        if raw.lower() in RESERVED_ROUTES:
            raise ValueError(f"Reserved X route is not a profile: {raw}")
        return raw

    candidate = raw
    if re.match(r"^(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/", candidate, re.IGNORECASE):
        candidate = "https://" + candidate

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError("Only x.com or twitter.com profile URLs are accepted")

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("The URL does not contain a profile handle")
    if len(parts) > 1 and parts[1].lower() == "status":
        raise ValueError("A post URL was supplied; provide the creator profile URL")

    handle = parts[0]
    if not HANDLE_RE.fullmatch(handle) or handle.lower() in RESERVED_ROUTES:
        raise ValueError(f"Invalid X profile handle: {handle}")
    return handle


def build_opencli_command(
    opencli_bin: str,
    handle: str,
    limit: int,
    page_delay: int,
) -> list[str]:
    return [
        opencli_bin,
        "twitter",
        "tweets",
        f"@{handle}",
        "--limit",
        str(limit),
        "--page-delay",
        str(page_delay),
        "-f",
        "json",
    ]


def decode_opencli_output(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FetchError(f"OpenCLI returned invalid JSON: {exc}", 5) from exc

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = None
        for key in ("data", "rows", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        if items is None and any(key in payload for key in ("url", "id", "tweet_url")):
            items = [payload]
        if items is None:
            raise FetchError("OpenCLI JSON did not contain a recognizable result list", 5)
    else:
        raise FetchError("OpenCLI JSON must be an object or array", 5)

    records = [item for item in items if isinstance(item, dict)]
    if not records and items:
        raise FetchError("OpenCLI result rows were not JSON objects", 5)
    return records


def canonical_post_url(item: dict[str, Any], fallback_handle: str) -> tuple[str, str, str] | None:
    raw_url = str(item.get("url") or item.get("tweet_url") or "").strip()
    match = STATUS_URL_RE.match(raw_url)
    if match:
        author, post_id = match.groups()
        if not HANDLE_RE.fullmatch(author):
            return None
        author = author.lower()
        return f"https://x.com/{author}/status/{post_id}", author, post_id

    post_id = str(item.get("id") or item.get("tweet_id") or "").strip()
    author = str(item.get("author") or item.get("username") or fallback_handle).strip().lstrip("@")
    if post_id.isdigit() and HANDLE_RE.fullmatch(author):
        author = author.lower()
        return f"https://x.com/{author}/status/{post_id}", author, post_id
    return None


def prepare_records(
    items: Iterable[dict[str, Any]],
    handle: str,
    include_retweets: bool,
) -> list[dict[str, Any]]:
    profile = f"https://x.com/{handle}"
    seen: set[str] = set()
    records: list[dict[str, Any]] = []

    for item in items:
        if not include_retweets and bool(item.get("is_retweet")):
            continue
        canonical = canonical_post_url(item, handle)
        if canonical is None:
            continue
        url, author, post_id = canonical
        if url in seen:
            continue
        seen.add(url)
        records.append(
            {
                "profile": profile,
                "id": post_id,
                "author": author,
                "created_at": item.get("created_at") or "",
                "text": item.get("text") or "",
                "likes": item.get("likes") or 0,
                "retweets": item.get("retweets") or 0,
                "replies": item.get("replies") or 0,
                "views": item.get("views") or 0,
                "url": url,
            }
        )
    return records


def render_records(records: list[dict[str, Any]], output_format: str) -> str:
    if output_format == "text":
        return "\n".join(record["url"] for record in records) + ("\n" if records else "")
    if output_format == "json":
        return json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    if output_format == "jsonl":
        return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    if output_format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(records)
        return buffer.getvalue()
    raise ValueError(f"Unsupported output format: {output_format}")


def write_output(rendered: str, output_path: str | None, force: bool) -> None:
    if output_path is None:
        sys.stdout.write(rendered)
        return

    path = Path(output_path).expanduser()
    if path.exists() and not force:
        raise FetchError(f"Output already exists: {path}. Use --force only with user authorization.", 6)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    print(f"saved={path.resolve()}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch canonical post links from an X profile through OpenCLI."
    )
    parser.add_argument("profile", help="x.com/twitter.com profile URL, @handle, or handle")
    parser.add_argument("--limit", type=int, default=100, help="posts to request (1-10000)")
    parser.add_argument("--page-delay", type=int, default=2, help="seconds between pages (0-60)")
    parser.add_argument(
        "--format",
        choices=("text", "jsonl", "json", "csv"),
        default="jsonl",
        help="output data format",
    )
    parser.add_argument("--output", help="write data to this path instead of stdout")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    parser.add_argument("--include-retweets", action="store_true", help="keep reposts in the result")
    parser.add_argument("--dry-run", action="store_true", help="validate input and show the OpenCLI command")
    parser.add_argument(
        "--opencli-bin",
        default=os.environ.get("OPENCLI_BIN", "opencli"),
        help="OpenCLI executable name or path",
    )
    parser.add_argument("--timeout", type=int, default=300, help="OpenCLI timeout in seconds")
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= 10_000:
        parser.error("--limit must be between 1 and 10000")
    if not 0 <= args.page_delay <= 60:
        parser.error("--page-delay must be between 0 and 60")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        handle = normalize_profile(args.profile)
        command = build_opencli_command(args.opencli_bin, handle, args.limit, args.page_delay)

        if args.dry_run:
            sys.stdout.write(
                json.dumps(
                    {
                        "profile": f"https://x.com/{handle}",
                        "handle": handle,
                        "command": command,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            return 0

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
        except FileNotFoundError as exc:
            raise FetchError("OpenCLI executable was not found", 3) from exc
        except subprocess.TimeoutExpired as exc:
            raise FetchError(f"OpenCLI timed out after {args.timeout} seconds", 4) from exc

        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout).strip()
            if len(diagnostic) > 1200:
                diagnostic = diagnostic[:1200] + "..."
            raise FetchError(
                f"OpenCLI failed with exit code {completed.returncode}: {diagnostic or 'no diagnostic'}",
                4,
            )

        items = decode_opencli_output(completed.stdout)
        records = prepare_records(items, handle, args.include_retweets)
        eligible_items = (
            items
            if args.include_retweets
            else [item for item in items if not bool(item.get("is_retweet"))]
        )
        if eligible_items and not records:
            raise FetchError("No canonical post links were returned", 6)

        rendered = render_records(records, args.format)
        write_output(rendered, args.output, args.force)
        print(
            f"profile=https://x.com/{handle} unique_links={len(records)} include_retweets={str(args.include_retweets).lower()}",
            file=sys.stderr,
        )
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
