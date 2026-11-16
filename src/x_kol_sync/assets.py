"""Public X images on disk; metadata stays in MySQL, never in local ledgers."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib import request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from . import __version__

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 20 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


class AssetError(RuntimeError):
    """An image could not be read or saved; tweet processing may continue."""


class AssetBudgetExceeded(AssetError):
    """The current round deferred an image because its time budget ran out."""


def media_budget(stage):
    """Separate bounded budgets for newly collected media and persisted retries."""
    name, default = {
        'download': ('X_KOL_MEDIA_DOWNLOAD_BUDGET_SECONDS', 300),
        'retry': ('X_KOL_MEDIA_RETRY_BUDGET_SECONDS', 180),
        'avatar': ('X_KOL_AVATAR_BUDGET_SECONDS', 180),
        'verification': ('X_KOL_VERIFICATION_BUDGET_SECONDS', 180),
    }[stage]
    try:
        value = int(os.getenv(name, '').strip() or default)
    except ValueError:
        raise ValueError(f'{name} must be an integer between 1 and 600') from None
    if not 1 <= value <= 600:
        raise ValueError(f'{name} must be an integer between 1 and 600')
    return value


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def image_url(source, kind):
    parsed = urlsplit(source)
    prefixes = ('/profile_images/', '/default_profile_images/') if kind == 'avatar' else (
        '/media/', '/ext_tw_video_thumb/', '/amplify_video_thumb/', '/tweet_video_thumb/')
    if (parsed.scheme != 'https' or parsed.hostname != 'pbs.twimg.com'
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not parsed.path.startswith(prefixes)):
        raise AssetError('Unsupported public image URL')
    path = parsed.path
    query = dict(parse_qsl(parsed.query))
    if kind == 'avatar':
        path = re.sub(r'_(normal|mini|bigger|\d+x\d+)(?=\.[^./]+$)', '_400x400', path)
    elif path.startswith('/media/'):
        query['name'] = 'orig'
    return urlunsplit(('https', 'pbs.twimg.com', path, urlencode(query), ''))


def image_format(data):
    if len(data) < 24:
        raise AssetError('Image response was truncated')
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg', 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png', 'image/png'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'gif', 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    raise AssetError('Response was not a supported image')


def read_avatar_source(handle, timeout):
    from .fetch_tweet_links import normalize_profile
    handle = normalize_profile(handle).lower()
    executable = shutil.which(os.getenv('X_KOL_OPENCLI', 'opencli'))
    node = shutil.which('node')
    if not executable or not node:
        raise AssetError('OpenCLI and Node.js are required for profile images')
    result = subprocess.run([node, str(ROOT / 'scripts/read_x_profile_avatar.mjs'), executable, handle],
                            capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise AssetError('Could not read profile image through OpenCLI')
    value = json.loads(result.stdout)
    if value.get('handle', '').lower() != handle or not isinstance(value.get('avatar_url'), str):
        raise AssetError('Profile image identity mismatch')
    return value['avatar_url']


class AssetStore:
    def __init__(self, workspace=ROOT, *, budget=60):
        workspace = Path(workspace).expanduser().resolve()
        configured = os.getenv('X_KOL_MEDIA_DIR', '').strip()
        self.root = Path(configured).expanduser() if configured else workspace / 'data/media'
        if not self.root.is_absolute():
            self.root = workspace / self.root
        self.root = self.root.resolve()
        self.budget = budget
        self.deadline = None
        self.opener = request.build_opener(NoRedirect())
        self.cached = {}
        self.saved = 0
        self.failed = 0

    def remaining(self):
        value = self.budget if self.deadline is None else self.deadline - time.monotonic()
        if value <= 0:
            raise AssetBudgetExceeded('Image time budget exhausted')
        return value

    @contextmanager
    def operation(self):
        # Charge image/browser work, not the time spent fetching timelines between images.
        outer = self.deadline is not None
        started = time.monotonic()
        if not outer:
            self.deadline = started + self.budget
        try:
            yield
        finally:
            if not outer:
                self.budget = max(0, self.budget - (time.monotonic() - started))
                self.deadline = None

    def available(self, asset):
        if not isinstance(asset, dict) or asset.get('status') != 'saved':
            return False
        relative = asset.get('local_path')
        if not isinstance(relative, str) or not relative:
            return False
        target = (self.root / relative).resolve()
        return self.root in target.parents and target.is_file() and target.stat().st_size > 0

    def save(self, source, kind):
        """One bounded, unauthenticated CDN request; atomically publish complete files."""
        with self.operation():
            return self._save(source, kind)

    def _save(self, source, kind):
        metadata = dict(source_url=source, status='failed', attempted_at=now())
        try:
            url = image_url(source, kind)
            if url in self.cached:
                return dict(self.cached[url], source_url=source)
            req = request.Request(url, headers={'User-Agent': f'x-kol-radar/{__version__}', 'Accept': 'image/*'})
            with self.opener.open(req, timeout=min(10, self.remaining())) as response:
                expected = int(response.headers.get('Content-Length') or 0)
                if expected > MAX_BYTES:
                    raise AssetError('Image exceeds size limit')
                parts = []
                size = 0
                while True:
                    self.remaining()
                    chunk = response.read(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    parts.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise AssetError('Image exceeds size limit')
                if expected and size != expected:
                    raise AssetError('Image response was truncated')
            data = b''.join(parts)
            extension, content_type = image_format(data)
            digest = hashlib.sha256(data).hexdigest()
            relative = Path('avatars' if kind == 'avatar' else 'images') / (digest + '.' + extension)
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as file:
                    temporary = Path(file.name)
                    file.write(data)
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            metadata.update(status='saved', download_url=url, local_path=relative.as_posix(),
                            sha256=digest, bytes=len(data), content_type=content_type, saved_at=now())
            self.cached[url] = dict(metadata)
        except Exception as exc:
            # Never persist response bodies, browser diagnostics or credentials.
            metadata['error_type'] = type(exc).__name__
        if metadata['status'] == 'saved':
            self.saved += 1
        else:
            self.failed += 1
        return metadata

    def avatar(self, profile):
        if self.available(profile.get('avatar')):
            return profile['avatar']
        with self.operation():
            return self._avatar(profile)

    def _avatar(self, profile):
        previous = profile.get('avatar') if isinstance(profile.get('avatar'), dict) else {}
        try:
            source = read_avatar_source(profile['handle'], min(45, self.remaining()))
            value = dict(previous, **self.save(source, 'avatar'))
            if value['status'] == 'saved':
                value.pop('error_type', None)
            return value
        except Exception as exc:
            return dict(previous, status='failed', error_type=type(exc).__name__, attempted_at=now())

    def enrich_tweet(self, row):
        """Keep media order and quote ownership; videos retain URLs and still posters only."""
        urls = row.get('media_urls')
        if isinstance(urls, list):
            posters = row.get('media_posters') or []
            previous = row.get('media') or []
            media = []
            for index, source in enumerate(urls):
                try:
                    parsed = urlsplit(source) if isinstance(source, str) else urlsplit('')
                    is_photo = parsed.hostname == 'pbs.twimg.com' and parsed.path.startswith('/media/')
                    is_video = parsed.hostname == 'video.twimg.com'
                except ValueError:
                    is_photo = is_video = False
                poster = posters[index] if index < len(posters) else None
                image_source = poster if is_video else source
                old = previous[index] if index < len(previous) else {}
                old = old if isinstance(old, dict) and old.get('url') == source else {}
                item = dict(old, url=source, type='photo' if is_photo else ('video' if is_video else 'unknown'), poster_url=poster)
                if old.get('url') == source and self.available(old.get('image')):
                    item['image'] = old['image']
                elif image_source:
                    old_image = old.get('image') if isinstance(old.get('image'), dict) else {}
                    item['image'] = dict(old_image, **self.save(image_source, 'image'))
                    if item['image']['status'] == 'saved':
                        item['image'].pop('error_type', None)
                else:
                    item['image'] = dict(status='unavailable')
                media.append(item)
            row['media'] = media
            row['media_status'] = ('complete' if all(item['image']['status'] == 'saved' for item in media)
                                   else 'partial')
            if not urls and row.get('has_media'):
                row['media_status'] = 'unavailable'
        elif row.get('has_media'):
            row['media_status'] = 'unavailable'
        quote = row.get('quoted_tweet')
        if isinstance(quote, dict):
            self.enrich_tweet(quote)
        return row
