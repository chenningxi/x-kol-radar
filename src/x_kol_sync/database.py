"""MySQL source of truth; Redis locks are isolated by database and namespace."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def connect():
    database_name = os.getenv('X_KOL_MYSQL_DATABASE', '').strip()
    if not database_name:
        raise ValueError('Set X_KOL_MYSQL_DATABASE in the environment before running database commands')
    import pymysql
    return pymysql.connect(
        host=os.getenv('X_KOL_MYSQL_HOST', '127.0.0.1'),
        port=int(os.getenv('X_KOL_MYSQL_PORT', '3306')),
        user=os.getenv('X_KOL_MYSQL_USER', 'root'),
        password=os.getenv('X_KOL_MYSQL_PASSWORD', ''),
        database=database_name,
        charset='utf8mb4', autocommit=False,
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=10,
        init_command="SET time_zone = '+00:00'",
    )


@contextmanager
def task_lock(connection, name):
    """MySQL session lock remains authoritative even if the Redis lease expires."""
    import redis
    namespace = os.getenv('X_KOL_REDIS_PREFIX', 'xkol:radar')
    cache = redis.Redis(
        host=os.getenv('X_KOL_REDIS_HOST', '127.0.0.1'),
        port=int(os.getenv('X_KOL_REDIS_PORT', '6379')),
        db=int(os.getenv('X_KOL_REDIS_DB', '0')),
        password=os.getenv('X_KOL_REDIS_PASSWORD') or None,
        socket_connect_timeout=5, socket_timeout=5,
    )
    key = namespace + ':' + connection.db.decode() + ':' + name
    lease = cache.lock(key, timeout=3600, blocking=False)
    if not lease.acquire(blocking=False):
        raise RuntimeError('Another task holds ' + key)
    acquired = False
    sql_key = hashlib.sha256(key.encode()).hexdigest()
    try:
        with connection.cursor() as c:
            c.execute('SELECT GET_LOCK(%s,0) AS acquired', (sql_key,))
            acquired = c.fetchone()['acquired'] == 1
        if not acquired:
            raise RuntimeError('Another database task is running')
        yield
    finally:
        if acquired:
            with connection.cursor() as c:
                c.execute('SELECT RELEASE_LOCK(%s)', (sql_key,))
        try:
            lease.release()
        except redis.exceptions.LockNotOwnedError:
            pass


def tweets(connection):
    with connection.cursor() as c:
        c.execute('SELECT payload,revision FROM xkol_tweets ORDER BY first_seen_at,tweet_id')
        return [(json.loads(r['payload']), r['revision']) for r in c.fetchall()]


def insert_tweet(connection, row):
    from .sync_x_kols import canonical_ledger_url
    value = dict(row)
    value['url'] = canonical_ledger_url(value['url'])
    with connection.cursor() as c:
        c.execute('SELECT tweet_id FROM xkol_tweets WHERE tweet_id=%s OR url=%s',
                  (str(value['id']), value['url']))
        if c.fetchone():
            return False
        c.execute('INSERT INTO xkol_tweets(tweet_id,url,profile_handle,payload,translation_status,content_policy_version) VALUES(%s,%s,%s,%s,%s,%s)',
                  (str(value['id']), value['url'], value.get('profile','').rstrip('/').split('/')[-1].lower(), encode(value), value.get('translation_status','pending'), value.get('content_policy_version')))
    return True


def record(connection, table, row):
    if table not in {'xkol_receipts', 'xkol_sync_history'}:
        raise ValueError('Invalid event table')
    with connection.cursor() as c:
        if table == 'xkol_receipts':
            from .sync_x_kols import canonical_ledger_url
            c.execute('INSERT IGNORE INTO xkol_receipts(event_key,tweet_url,status,payload) VALUES(%s,%s,%s,%s)',
                      (digest(row), canonical_ledger_url(row['url']), row['status'], encode(row)))
        else:
            c.execute('INSERT IGNORE INTO xkol_sync_history(event_key,payload) VALUES(%s,%s)', (digest(row), encode(row)))


def sent_urls(connection):
    with connection.cursor() as c:
        c.execute("SELECT DISTINCT tweet_url FROM xkol_receipts WHERE status IN ('sent','skipped')")
        return {r['tweet_url'] for r in c.fetchall()}
