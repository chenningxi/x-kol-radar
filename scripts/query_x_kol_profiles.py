#!/usr/bin/env python3
"""列出 MySQL 中的监控博主及已抓取推文数量（只读）。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from x_kol_sync import queries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    args = parser.parse_args(argv)
    with queries.read_connection() as connection:
        rows = queries.profiles(connection)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print('账号\t别名\t启用\t已抓取推文数')
        for row in rows:
            alias = ' '.join(str(row['alias']).split())
            print(f"@{row['handle']}\t{alias}\t{'是' if row['enabled'] else '否'}\t{row['tweet_count']}")
        print(f'共 {len(rows)} 位博主')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
