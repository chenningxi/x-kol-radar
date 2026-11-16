"""Read-only queries for monitored profiles and captured tweet links."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import re

from . import database as db


def handle_argument(value):
    handle = value.removeprefix('@').lower()
    if not re.fullmatch(r'[a-z0-9_]{1,32}', handle):
        raise argparse.ArgumentTypeError('请使用博主账号（例如 example 或 @example）')
    return handle


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('请输入正整数') from None
    if number < 1:
        raise argparse.ArgumentTypeError('请输入正整数')
    return number


@contextmanager
def read_connection():
    connection = db.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute('START TRANSACTION READ ONLY')
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def profiles(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT p.handle,p.config,'
            '(SELECT COUNT(*) FROM xkol_tweets t WHERE t.profile_handle=p.handle) AS tweet_count '
            'FROM xkol_profiles p ORDER BY p.handle')
        rows = cursor.fetchall()
    result = []
    for row in rows:
        config = json.loads(row['config'])
        result.append(dict(handle=row['handle'], alias=config.get('alias', ''),
                           enabled=bool(config.get('enabled', False)), tweet_count=row['tweet_count']))
        if 'avatar' in config:
            result[-1]['avatar'] = config['avatar']
    return result


def tweet_page(connection, handle, page=1, page_size=20):
    if page < 1 or page_size < 1:
        raise ValueError('page and page_size must be positive')
    with connection.cursor() as cursor:
        cursor.execute('SELECT COUNT(*) AS total FROM xkol_tweets WHERE profile_handle=%s', (handle,))
        total = cursor.fetchone()['total']
        cursor.execute(
            'SELECT tweet_id,url,payload FROM xkol_tweets WHERE profile_handle=%s '
            'ORDER BY CAST(tweet_id AS UNSIGNED) DESC LIMIT %s OFFSET %s',
            (handle, page_size, (page - 1) * page_size))
        items = cursor.fetchall()
    for item in items:
        if 'payload' in item:
            item['payload'] = json.loads(item['payload'])
    return dict(handle=handle, page=page, page_size=page_size, total=total,
                total_pages=(total + page_size - 1) // page_size, items=items)
