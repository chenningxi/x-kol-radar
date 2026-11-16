// Read only the requested public profile avatar via OpenCLI Browser Bridge.
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const [, , executable, handle] = process.argv;
const verificationOnly = process.argv[4] === '--verification';
if (!executable || !/^[a-z0-9_]{1,15}$/.test(handle || '')) process.exit(2);
const session = 'xkol-avatar-' + randomUUID();
let page;
try {
  const entry = await fs.realpath(executable);
  const { Page } = await import(pathToFileURL(path.join(path.dirname(entry), 'browser/page.js')).href);
  page = new Page(session, undefined, undefined, 'background');
  const opened = JSON.parse(execFileSync(executable,
    ['browser', session, 'open', 'https://x.com/' + handle, '--window', 'background'], { encoding: 'utf8', timeout: 15000 }));
  if (!opened.page) throw Error('OpenCLI did not return a page identity');
  page.setActivePage(opened.page);
  let result;
  for (let attempt = 0; attempt < 20; attempt++) {
    result = await page.evaluate(({ expected, verificationOnly }) => {
      if (!['x.com', 'twitter.com'].includes(location.hostname)
          || location.pathname.replace(/\/$/, '').toLowerCase() !== '/' + expected) return null;
      const main = document.querySelector('[data-testid="primaryColumn"]');
      if (verificationOnly) {
        const identity = main?.querySelector('[data-testid="UserName"]');
        // The profile identity is outside tweet articles/recommendations and
        // must contain the exact requested handle before absence means false.
        if (!identity || identity.closest('article')
            || ![...identity.querySelectorAll('span')].some(s => s.textContent.trim().toLowerCase() === '@' + expected)) return null;
        const badge = identity.querySelector('[data-testid="icon-verified"]');
        return { handle: expected, verified: !!badge,
          badge_type: badge?.querySelector('stop[stop-color="#f4e72a"]') ? 'gold'
            : badge?.querySelector('path[fill="#829aab"]') ? 'gray' : (badge ? 'solid' : null),
          badge_color: badge ? getComputedStyle(badge.querySelector('path') || badge).fill : null };
      }
      const links = [...(main?.querySelectorAll('a[href]') || [])].filter(a => {
        const url = new URL(a.href, location.href);
        return url.hostname === location.hostname && ['/' + expected + '/photo', '/' + expected].includes(url.pathname.replace(/\/$/, '').toLowerCase());
      });
      // The profile header can be rendered before/after its photo link. An
      // avatar inside an exact link to this same account is also attributable;
      // never fall back to an arbitrary timeline or recommended-user image.
      for (const link of links.sort((a, b) => Number(!a.href.endsWith('/photo')) - Number(!b.href.endsWith('/photo')))) {
        for (const image of link.querySelectorAll('img')) {
          const source = image.currentSrc || image.src;
          if (!source) continue;
          const url = new URL(source);
          if (url.protocol === 'https:' && url.hostname === 'pbs.twimg.com'
              && /^\/(?:profile_images|default_profile_images)\//.test(url.pathname)) {
            return { handle: expected, avatar_url: source };
          }
        }
      }
      // Live profiles replace the photo link with a broadcast link. Its image
      // accessibility label still explicitly names the account that owns it.
      const owner = new RegExp('@' + expected + '(?![a-z0-9_])', 'i');
      for (const image of main?.querySelectorAll('img') || []) {
        if (!owner.test(image.alt || image.closest('[aria-label]')?.getAttribute('aria-label') || '')) continue;
        const source = image.currentSrc || image.src;
        if (!source) continue;
        const url = new URL(source);
        if (url.protocol === 'https:' && url.hostname === 'pbs.twimg.com'
            && /^\/(?:profile_images|default_profile_images)\//.test(url.pathname)) {
          return { handle: expected, avatar_url: source };
        }
      }
      return null;
    }, { expected: handle, verificationOnly });
    if (result?.data !== undefined && !result.handle) result = result.data;
    if (verificationOnly ? typeof result?.verified === 'boolean' : result?.avatar_url) break;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  if (verificationOnly ? typeof result?.verified !== 'boolean' : !result?.avatar_url) throw Error('Profile identity unavailable');
  process.stdout.write(JSON.stringify(result) + '\n');
} catch {
  process.stderr.write('Could not read the requested public profile avatar\n');
  process.exitCode = 1;
} finally {
  if (page) {
    try { execFileSync(executable, ['browser', session, 'close', '--window', 'background'], { stdio: 'ignore', timeout: 5000 }); }
    catch { /* Do not mask the source read outcome with a tab cleanup error. */ }
  }
}
