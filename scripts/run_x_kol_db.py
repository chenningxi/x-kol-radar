#!/usr/bin/env python3
"""Database workflow: pipeline/init/import/profiles/assets/sync/review/export/apply/check/dispatch/status."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from x_kol_sync import database as db
from x_kol_sync import sync_x_kols as sync
from x_kol_sync.assets import AssetStore, media_budget
from x_kol_sync.avatars import repair_author_avatars
from x_kol_sync.verification import repair_author_verifications
import run_x_kol_dispatch as dispatch


def read(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []


def initialize(conn):
    for statement in (ROOT / 'db/schema.sql').read_text(encoding='utf-8').split(';'):
        if statement.strip():
            with conn.cursor() as c:
                c.execute(statement)
    conn.commit()


def load_profiles(conn, workspace, *, download_avatars=True):
    profiles = sync.load_profiles(workspace / 'data/x_profiles.md')
    with conn.cursor() as c:
        c.execute('SELECT handle,config FROM xkol_profiles')
        existing = {row['handle']: json.loads(row['config']) for row in c.fetchall()}
    store = AssetStore(workspace, budget=media_budget('avatar'))
    failed = 0
    # Give new profiles the image budget before older missing-avatar profiles.
    for p in sorted(profiles, key=lambda p: p['handle'].lower() in existing):
        config = dict(existing.get(p['handle'].lower(), {}), **p)
        if download_avatars:
            config['avatar'] = store.avatar(config)
            failed += int(config['avatar']['status'] != 'saved')
        with conn.cursor() as c:
            c.execute('INSERT INTO xkol_profiles(handle,config) VALUES(%s,%s) ON DUPLICATE KEY UPDATE config=%s',
                      (p['handle'].lower(), db.encode(config), db.encode(config)))
    return dict(profiles='updated', avatars_failed=failed)


def repair_assets(conn, workspace, *, limit=20):
    """Retry real authors' avatars and known tweet images, without fetching history."""
    result = repair_author_avatars(conn, workspace, limit=limit)
    result.update(repair_tweet_assets(conn, workspace, limit=limit))
    result['verification_retry'] = repair_author_verifications(conn, workspace, limit=limit)
    return result


def repair_tweet_assets(conn, workspace, *, limit=20):
    """Retry persisted partial media even when no new timeline rows are available."""
    store = AssetStore(workspace, budget=media_budget('retry'))
    result = dict(tweets_updated=0, images_failed=0, budget_exhausted=False)
    with conn.cursor() as c:
        c.execute("SELECT tweet_id,payload,revision FROM xkol_tweets WHERE "
                  "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.media_status'))='partial' OR "
                  "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.quoted_tweet.media_status'))='partial' "
                  'ORDER BY updated_at,tweet_id LIMIT %s', (limit,))
        tweets = c.fetchall()
    for item in tweets:
        # Leave rows not attempted this round untouched and first in the next
        # retry queue, rather than marking the whole page failed after timeout.
        if store.budget <= 0:
            result['budget_exhausted'] = True
            break
        row = store.enrich_tweet(json.loads(item['payload']))
        for part in (row, row.get('quoted_tweet') or {}):
            result['images_failed'] += sum(image['image']['status'] != 'saved' for image in part.get('media', []))
        with conn.cursor() as c:
            c.execute('UPDATE xkol_tweets SET payload=%s,revision=revision+1 WHERE tweet_id=%s AND revision=%s',
                      (db.encode(row), item['tweet_id'], item['revision']))
            if c.rowcount != 1:
                raise ValueError('Revision conflict while saving image metadata')
        conn.commit()
        result['tweets_updated'] += 1
    result['budget_exhausted'] = result['budget_exhausted'] or store.budget <= 0
    return result


def newest_cursor(existing, imported):
    """Keep the furthest numeric tweet cursor during a one-time JSONL migration."""
    values = [str(value).strip() for value in (existing, imported) if str(value or '').strip()]
    if not values:
        return None
    try:
        return str(max(int(value) for value in values))
    except ValueError:
        return values[0] if existing else values[-1]


def import_files(conn, workspace):
    """One-time compatibility migration; scheduled runtime never reads JSONL."""
    load_profiles(conn, workspace, download_avatars=False)
    rows = read(workspace / 'data/tweet_ledger.jsonl')
    added = sum(db.insert_tweet(conn, r) for r in rows)
    for r in read(workspace / 'data/feishu_send_ledger.jsonl'):
        db.record(conn, 'xkol_receipts', r)
    for r in read(workspace / 'data/sync_history.jsonl'):
        db.record(conn, 'xkol_sync_history', r)
    cursor_path = workspace / 'data/x_cursors.json'
    if cursor_path.exists():
        with conn.cursor() as c:
            for url, cursor in json.loads(cursor_path.read_text()).items():
                handle = url.rstrip('/').split('/')[-1].lower()
                c.execute('SELECT cursor_id FROM xkol_profiles WHERE handle=%s', (handle,))
                current = c.fetchone()
                if current:
                    c.execute('UPDATE xkol_profiles SET cursor_id=%s WHERE handle=%s',
                              (newest_cursor(current['cursor_id'], cursor), handle))
    conn.commit()
    return {'source_rows': len(rows), 'inserted': added, 'existing': len(rows)-added}


def collect(conn, workspace=ROOT):
    # Existing URLs are intentionally skipped by insert_tweet. Their images need
    # a separate retry before probing cursors, including quiet accounts and
    # rounds where the browser is unavailable. This uses the caller's same lock.
    asset_retry = repair_tweet_assets(conn, workspace)
    health, code = sync.run_doctor()
    if code:
        raise RuntimeError(db.encode(health))
    with conn.cursor() as c:
        c.execute('SELECT handle,config,cursor_id FROM xkol_profiles')
        profiles = c.fetchall()
    results = []
    store = AssetStore(workspace, budget=media_budget('download'))
    for item in profiles:
        p = json.loads(item['config'])
        if not p['enabled']:
            continue
        result = dict(run_id=__import__('uuid').uuid4().hex, synced_at=sync.utc_now(), profile_url=p['profile_url'], fetched=0, added=0, skipped=0, failed=0)
        try:
            probe = dict(p, limit=1)
            latest = sync.run_fetch(sync.resolve_fetch_script(), probe, False, 120)
            newest = max((int(r['id']) for r in latest), default=0)
            cursor = int(item['cursor_id'] or 0)
            if cursor and newest <= cursor and newest:
                result['mode'] = 'probe_unchanged'
            else:
                rows = sync.run_fetch(sync.resolve_fetch_script(), p, False, 120)
                result['fetched'] = len(rows)
                for row in rows:
                    row.setdefault('first_seen_at', sync.utc_now())
                    result['added'] += int(db.insert_tweet(conn, row, prepare=store.enrich_tweet))
                result['skipped'] = len(rows)-result['added']
                maximum = max([cursor] + [int(r['id']) for r in rows])
                with conn.cursor() as c:
                    c.execute('UPDATE xkol_profiles SET cursor_id=%s WHERE handle=%s', (str(maximum), item['handle']))
            result['status'] = 'ok'
            db.record(conn, 'xkol_sync_history', result)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            result.update(status='error', failed=1, added=0, error=str(exc))
            db.record(conn, 'xkol_sync_history', result)
            conn.commit()
            results.append(result)
            # Stop on external failures; no repeated X requests.
            break
        results.append(result)
    avatar_retry = (dict(skipped='sync_failed') if any(item.get('failed') for item in results)
                    else repair_author_avatars(conn, workspace))
    verification_retry = (dict(skipped='sync_failed') if any(item.get('failed') for item in results)
                          else repair_author_verifications(conn, workspace))
    return {'profiles': results, 'images_saved': store.saved, 'images_failed': store.failed,
            'asset_retry': asset_retry, 'avatar_retry': avatar_retry, 'verification_retry': verification_retry}


def apply_reviews(conn, path):
    return apply_review_updates(conn, read(path))


def apply_review_updates(conn, updates):
    """Apply file-based or API-generated updates through the same revision checks."""
    allowed = {'translation_status','source_language','translation_zh','translation_error','translation_updated_at',
               'content_classification','content_reason','content_policy_version','content_reviewed_at'}
    # Validate the complete batch before committing any changes.
    for item in updates:
        with conn.cursor() as c:
            c.execute('SELECT payload,revision FROM xkol_tweets WHERE tweet_id=%s FOR UPDATE', (str(item['id']),))
            current = c.fetchone()
            if not current or current['revision'] != item['revision']:
                raise ValueError('Revision conflict for ' + str(item['id']))
            row = json.loads(current['payload'])
            for key in allowed:
                if key in item:
                    row[key] = item[key]
            if isinstance(item.get('quoted_tweet'), dict):
                quote = row.get('quoted_tweet')
                if not isinstance(quote, dict) or str(quote.get('id')) != str(item['quoted_tweet'].get('id')):
                    raise ValueError('Quote identity mismatch')
                quote.update({k:v for k,v in item['quoted_tweet'].items() if k in allowed})
            if row.get('translation_status') == 'translated' and not row.get('translation_zh'):
                raise ValueError('Missing translation_zh')
            quote = row.get('quoted_tweet')
            if isinstance(quote, dict) and quote.get('translation_status') == 'translated' and not quote.get('translation_zh'):
                raise ValueError('Missing quoted translation_zh')
            c.execute('UPDATE xkol_tweets SET payload=%s,translation_status=%s,content_policy_version=%s,revision=revision+1 WHERE tweet_id=%s',
                      (db.encode(row), row.get('translation_status','pending'), row.get('content_policy_version'), str(item['id'])))
    conn.commit()
    return {'updated':len(updates)}


def queues(conn):
    return dispatch.partition_dispatch_rows([r for r,v in db.tweets(conn)], db.sent_urls(conn))


def queue_status(conn):
    ready, skipped, review, translation = queues(conn)
    return dict(content_policy_version=dispatch.CONTENT_POLICY_VERSION,
                tweets=len(db.tweets(conn)), terminal_urls=len(db.sent_urls(conn)),
                send_ready=len(ready), skip_ready=len(skipped),
                awaiting_review=len(review), awaiting_translation=len(translation))


def send(conn):
    # Persist each batch before contacting Feishu; retries retain its idempotency key.
    skipped_count = 0
    with conn.cursor() as c:
        c.execute("SELECT batch_key,payload FROM xkol_batches WHERE status='pending' ORDER BY updated_at LIMIT 1")
        batch = c.fetchone()
    if not batch:
        ready, skipped, review, translation = queues(conn)
        skipped_count = len(skipped)
        for row in skipped:
            db.record(conn, 'xkol_receipts', dict(url=row['url'], status='skipped', reason=row.get('content_reason'), sent_at=sync.utc_now()))
        if not ready:
            conn.commit()
            return {'ready':0, 'skipped':len(skipped), 'awaiting_review':len(review), 'awaiting_translation':len(translation)}
        with conn.cursor() as c:
            c.execute('SELECT config FROM xkol_profiles')
            aliases = {p['profile_url']: p['alias'] for p in (json.loads(x['config']) for x in c.fetchall())}
        payload = dict(rows=ready, message=dispatch.build_message(ready, aliases), chat_id=os.environ['X_KOL_FEISHU_CHAT_ID'], profile=os.environ['X_KOL_FEISHU_PROFILE'])
        batch = dict(batch_key=db.digest(payload), payload=db.encode(payload))
        with conn.cursor() as c:
            c.execute('INSERT INTO xkol_batches(batch_key,payload) VALUES(%s,%s)', (batch['batch_key'], batch['payload']))
        conn.commit()
    payload = json.loads(batch['payload'])
    import subprocess
    response = subprocess.run(['lark-cli','--profile',payload['profile'],'im','+messages-send','--as','bot','--chat-id',payload['chat_id'],'--text',payload['message'],'--idempotency-key','xkol-'+batch['batch_key'][:40],'--format','json'], text=True,capture_output=True,timeout=120)
    result = json.loads(response.stdout or '{}')
    if response.returncode or result.get('ok') is not True:
        raise RuntimeError('Feishu send failed; batch retained for retry: ' + (response.stderr or response.stdout)[:500])
    for row in payload['rows']:
        db.record(conn, 'xkol_receipts', dict(url=row['url'], status='sent', chat_id=payload['chat_id'], message_id=(result.get('data') or {}).get('message_id'), sent_at=sync.utc_now()))
    with conn.cursor() as c:
        c.execute("UPDATE xkol_batches SET status='sent' WHERE batch_key=%s", (batch['batch_key'],))
    conn.commit()
    result = {'sent':len(payload['rows'])}
    if skipped_count:
        result['skipped'] = skipped_count
    return result


def run_pipeline(conn, client, policy, *, limit=20, workspace=ROOT):
    """One locked round: collect, review, dispatch; committed progress survives failures."""
    from x_kol_sync.deepseek_review import review_queue
    result = {'errors': []}

    def stage(name, operation):
        try:
            value = operation()
        except Exception as exc:
            # Earlier stages commit their progress; only discard the current transaction.
            conn.rollback()
            # Do not echo API response bodies, credentials or recipient configuration.
            result['errors'].append(dict(stage=name, error_type=type(exc).__name__))
            return None
        result[name] = value
        return value

    synced = stage('sync', lambda: collect(conn, workspace))
    reviewed = stage('review', lambda: review_queue(
        conn, client, policy, dispatch, apply_review_updates, limit=limit))

    def dispatch_ready():
        # Retry persisted batches first, then send this round's newly reviewed rows.
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM xkol_batches WHERE status='pending'")
            pending_batches = cursor.fetchone()['n']
        outcomes = []
        for _ in range(pending_batches + 1):
            outcome = send(conn)
            outcomes.append(outcome)
            if not outcome.get('sent'):
                break
        return dict(sent=sum(item.get('sent', 0) for item in outcomes),
                    skipped=sum(item.get('skipped', 0) for item in outcomes))

    stage('dispatch', dispatch_ready)
    result['queue'] = queue_status(conn)
    sync_failed = synced is None or any(row.get('failed') for row in synced.get('profiles', []))
    review_failed = reviewed is None or reviewed.get('failed') or reviewed.get('remaining')
    result['ok'] = not bool(result['errors'] or sync_failed or review_failed
                            or result['queue']['awaiting_review'] or result['queue']['awaiting_translation'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init','import','profiles','assets','sync','status','export','apply','check','dispatch','review','pipeline'])
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--file', type=Path)
    parser.add_argument('--limit', type=int, default=20, help='maximum review records, or authors and tweets per assets run')
    parser.add_argument('--dry-run', action='store_true', help='review queue counts only; no API calls or writes')
    args = parser.parse_args()
    if args.dry_run and args.command != 'review':
        parser.error('--dry-run is only supported with review')
    if args.limit < 1:
        parser.error('--limit must be positive')
    if args.command in {'review', 'pipeline'}:
        from x_kol_sync.deepseek_review import Client, load_policy
        policy = load_policy(ROOT, dispatch.CONTENT_POLICY_VERSION)
        client = None if args.dry_run else Client()
    conn = db.connect()
    try:
        with db.task_lock(conn, 'workflow'):
            if args.command == 'init':
                initialize(conn)
                result = {'schema':'ready'}
            elif args.command == 'import':
                result = import_files(conn, args.workspace)
            elif args.command == 'profiles':
                result = load_profiles(conn, args.workspace)
                conn.commit()
            elif args.command == 'assets':
                result = repair_assets(conn, args.workspace, limit=args.limit)
            elif args.command == 'sync':
                result = collect(conn, args.workspace)
            elif args.command == 'pipeline':
                result = run_pipeline(conn, client, policy, limit=args.limit, workspace=args.workspace)
            elif args.command == 'apply':
                if not args.file: parser.error('--file required')
                result = apply_reviews(conn, args.file)
            elif args.command == 'dispatch':
                result = send(conn)
            elif args.command == 'review':
                from x_kol_sync.deepseek_review import review_queue
                result = review_queue(conn, client, policy, dispatch, apply_review_updates,
                                      limit=args.limit, dry_run=args.dry_run)
            elif args.command == 'export':
                terminal = db.sent_urls(conn)
                for row, revision in db.tweets(conn):
                    if row['url'] not in terminal:
                        print(db.encode(dict(row, revision=revision)))
                return 0
            else:
                result = queue_status(conn)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.command == 'check':
                return int(bool(result['awaiting_review'] or result['awaiting_translation']))
            if args.command == 'review':
                return int(bool(result['failed'] or result['remaining']))
            if args.command == 'pipeline':
                return int(not result['ok'])
            if args.command == 'assets':
                return int(bool(result['profiles_failed'] or result['images_failed'] or result.get('authors_failed')
                                or result['verification_retry']['authors_failed']))
            return int(any(r.get('failed') for r in result.get('profiles', []))) if isinstance(result.get('profiles'),list) else 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
