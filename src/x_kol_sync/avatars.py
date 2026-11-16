"""Download actual tweet/quote authors' avatars without adding monitored profiles."""
from __future__ import annotations

import json
import re

from . import database as db
from .assets import AssetStore, media_budget


def author_handle(part):
    value = str(part.get('author') or '').lstrip('@').lower()
    return value if re.fullmatch(r'[a-z0-9_]{1,15}', value) else None


def parts(row):
    yield row
    if isinstance(row.get('quoted_tweet'), dict):
        yield row['quoted_tweet']


def repair_author_avatars(conn, workspace, *, limit=20):
    store = AssetStore(workspace, budget=media_budget('avatar'))
    with conn.cursor() as cursor:
        cursor.execute('SELECT handle,config FROM xkol_profiles ORDER BY handle')
        profiles = {r['handle'].lower(): json.loads(r['config']) for r in cursor.fetchall()}
        cursor.execute('SELECT tweet_id,payload,revision FROM xkol_tweets ORDER BY tweet_id')
        rows = [dict(r, row=json.loads(r['payload'])) for r in cursor.fetchall()]
    authors, known, previous = set(profiles), {}, {}

    def remember(handle, value):
        if not isinstance(value, dict):
            return
        if 'handle' in value and str(value['handle']).lower() != handle:
            return
        if store.available(value):
            known.setdefault(handle, dict(value, handle=handle))
        if value.get('attempted_at', '') >= previous.get(handle, {}).get('attempted_at', ''):
            previous[handle] = value

    for handle, config in profiles.items():
        remember(handle, config.get('avatar'))
    for item in rows:
        for part in parts(item['row']):
            handle = author_handle(part)
            if not handle:
                continue
            authors.add(handle)
            value = part.get('author_avatar')
            if isinstance(value, dict) and str(value.get('handle', '')).lower() == handle:
                remember(handle, value)

    updated = set()
    result = dict(profiles_attempted=0, profiles_failed=0, authors_attempted=0,
                  authors_failed=0, avatar_tweets_updated=0, avatars_budget_exhausted=False)

    def publish(handle, value, update_profile=False):
        if update_profile and handle in profiles:
            config = dict(profiles[handle], avatar=value)
            with conn.cursor() as cursor:
                cursor.execute('UPDATE xkol_profiles SET config=%s WHERE handle=%s', (db.encode(config), handle))
            profiles[handle] = config
        for item in rows:
            row = item['row']
            original = db.encode(row)
            for part in parts(row):
                if author_handle(part) != handle:
                    continue
                old = part.get('author_avatar')
                old = old if isinstance(old, dict) and str(old.get('handle', '')).lower() == handle else {}
                part['author_avatar'] = {**old, **value, 'handle': handle}
            if db.encode(row) == original:
                continue
            with conn.cursor() as cursor:
                cursor.execute('UPDATE xkol_tweets SET payload=%s,revision=revision+1 WHERE tweet_id=%s AND revision=%s',
                               (db.encode(row), item['tweet_id'], item['revision']))
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise ValueError('Revision conflict while saving author avatars')
            item['revision'] += 1
            updated.add(item['tweet_id'])
        conn.commit()

    # Reuse the same author's saved file across outer posts and all quote copies.
    for handle, value in known.items():
        publish(handle, value)
    missing = authors - known.keys()
    ordered = sorted(missing, key=lambda h: (bool(previous.get(h, {}).get('attempted_at')),
        previous.get(h, {}).get('attempted_at', ''), h not in profiles, h))
    for handle in ordered:
        if result['authors_attempted'] >= limit:
            break
        if store.budget <= 0:
            result['avatars_budget_exhausted'] = True
            break
        value = dict(store.avatar(dict(handle=handle, avatar=previous.get(handle, {}))), handle=handle)
        result['authors_attempted'] += 1
        result['authors_failed'] += int(value['status'] != 'saved')
        if handle in profiles:
            result['profiles_attempted'] += 1
            result['profiles_failed'] += int(value['status'] != 'saved')
        publish(handle, value, update_profile=True)
        if store.available(value):
            known[handle] = value
    result['avatar_tweets_updated'] = len(updated)
    result['authors_remaining'] = len(authors - known.keys())
    result['avatars_budget_exhausted'] = result['avatars_budget_exhausted'] or store.budget <= 0
    return result
