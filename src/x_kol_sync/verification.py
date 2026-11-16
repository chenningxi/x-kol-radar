"""Read actual author verification snapshots; never infer a badge from an avatar."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess

from . import database as db
from .assets import AssetStore, AssetError, ROOT, media_budget, now
from .avatars import author_handle, parts


def valid(value, handle):
    return (isinstance(value, dict) and value.get('handle') == handle
            and type(value.get('verified')) is bool and bool(value.get('checked_at'))
            and value.get('source_url') == 'https://x.com/' + handle)


def fresh(value):
    try:
        # Gradient badges inherit body text color; older snapshots without their
        # SVG type must be read again before that color can be displayed.
        if value.get('verified') and value.get('badge_color') in {'rgb(15, 20, 25)', 'rgb(231, 233, 234)'} and value.get('badge_type') != 'gold':
            return False
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['checked_at'])).total_seconds()
        return 0 <= age < 86400
    except (ValueError, TypeError, KeyError):
        return False


def read_verification(handle, timeout):
    executable, node = shutil.which(os.getenv('X_KOL_OPENCLI', 'opencli')), shutil.which('node')
    if not executable or not node:
        raise AssetError('OpenCLI and Node.js are required for author verification')
    result = subprocess.run([node, str(ROOT / 'scripts/read_x_profile_avatar.mjs'), executable,
                             handle, '--verification'], capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise AssetError('Could not read author verification through OpenCLI')
    raw = json.loads(result.stdout)
    if raw.get('handle') != handle or type(raw.get('verified')) is not bool:
        raise AssetError('Verification identity mismatch')
    value = dict(handle=handle, verified=raw['verified'], checked_at=now(), source_url='https://x.com/' + handle)
    if raw.get('badge_type') in {'gold', 'gray', 'solid'}:
        value['badge_type'] = raw['badge_type']
    color = raw.get('badge_color')
    match = re.fullmatch(r'rgb\((\d{1,3}), (\d{1,3}), (\d{1,3})\)', color or '')
    if raw['verified'] and match and all(int(n) <= 255 for n in match.groups()):
        value['badge_color'] = color
    return value


def repair_author_verifications(conn, workspace, *, limit=20):
    """Caller holds the workflow lock. Only optional author metadata is changed."""
    store = AssetStore(workspace, budget=media_budget('verification'))
    with conn.cursor() as cursor:
        cursor.execute('SELECT handle,config FROM xkol_profiles ORDER BY handle')
        profiles = {r['handle'].lower(): json.loads(r['config']) for r in cursor.fetchall()}
        cursor.execute('SELECT tweet_id,payload,revision FROM xkol_tweets ORDER BY tweet_id')
        rows = [dict(r, row=json.loads(r['payload'])) for r in cursor.fetchall()]
    authors, known, attempts = set(profiles), {}, {}

    def remember(handle, value):
        if not isinstance(value, dict) or value.get('handle') != handle:
            return
        attempts[handle] = max(attempts.get(handle, ''), value.get('attempted_at', value.get('checked_at', '')))
        if valid(value, handle) and (handle not in known or value['checked_at'] > known[handle]['checked_at']):
            known[handle] = value

    for handle, config in profiles.items():
        remember(handle, config.get('verification'))
    for item in rows:
        for part in parts(item['row']):
            handle = author_handle(part)
            if handle:
                authors.add(handle)
                remember(handle, part.get('author_verification'))

    updated = set()

    def publish(handle, value):
        if handle in profiles and profiles[handle].get('verification') != value:
            old = profiles[handle].get('verification')
            old = old if isinstance(old, dict) and old.get('handle') == handle else {}
            config = dict(profiles[handle], verification={**old, **value})
            with conn.cursor() as cursor:
                cursor.execute('UPDATE xkol_profiles SET config=%s WHERE handle=%s', (db.encode(config), handle))
            profiles[handle] = config
        for item in rows:
            row = item['row']; original = db.encode(row)
            for part in parts(row):
                if author_handle(part) == handle:
                    old = part.get('author_verification')
                    old = old if isinstance(old, dict) and old.get('handle') == handle else {}
                    part['author_verification'] = {**old, **value}
            if db.encode(row) == original:
                continue
            with conn.cursor() as cursor:
                cursor.execute('UPDATE xkol_tweets SET payload=%s,revision=revision+1 WHERE tweet_id=%s AND revision=%s',
                               (db.encode(row), item['tweet_id'], item['revision']))
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise ValueError('Revision conflict while saving author verification')
            item['revision'] += 1
            updated.add(item['tweet_id'])
        conn.commit()

    for handle, value in known.items():
        publish(handle, value)
    pending = {h for h in authors if h not in known or not fresh(known[h])}
    result = dict(authors_attempted=0, authors_failed=0, tweets_updated=0, budget_exhausted=False)
    for handle in sorted(pending, key=lambda h: (bool(attempts.get(h)), attempts.get(h, ''), h)):
        if result['authors_attempted'] >= limit or store.budget <= 0:
            break
        result['authors_attempted'] += 1
        try:
            with store.operation():
                value = read_verification(handle, min(45, store.remaining()))
        except Exception as exc:
            # Keep an earlier confirmed snapshot on a transient read failure.
            value = dict(known.get(handle, {}), handle=handle, attempted_at=now(), error_type=type(exc).__name__)
            result['authors_failed'] += 1
        else:
            value.update(attempted_at=value['checked_at'], error_type=None)
            known[handle] = value
            pending.remove(handle)
        publish(handle, value)
    result.update(tweets_updated=len(updated), authors_remaining=len(pending), budget_exhausted=store.budget <= 0)
    return result
