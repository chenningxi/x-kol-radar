#!/usr/bin/env python3
"""按博主分页列出 MySQL 中已抓取的推文链接（只读，最新在前）。"""
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
    parser.add_argument('handle', type=queries.handle_argument, help='博主账号，例如 example 或 @example')
    parser.add_argument('--page', type=queries.positive_integer, default=1, help='页码，从 1 开始（默认 1）')
    parser.add_argument('--page-size', type=queries.positive_integer, default=20, help='每页条数（默认 20）')
    parser.add_argument('--json', action='store_true', help='输出 JSON，含总数和分页信息')
    args = parser.parse_args(argv)
    with queries.read_connection() as connection:
        result = queries.tweet_page(connection, args.handle, args.page, args.page_size)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"@{result['handle']}：共 {result['total']} 条 / {result['total_pages']} 页，"
              f"当前第 {result['page']} 页，每页 {result['page_size']} 条")
        start = (result['page'] - 1) * result['page_size'] + 1
        for number, row in enumerate(result['items'], start):
            print(f"{number}\t{row['url']}")
        if not result['items']:
            print('本页无记录。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
