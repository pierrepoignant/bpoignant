"""X (Twitter) posting wrapper.

Mirrors the `mail` module: config comes from env vars (resolved at call time
so tests can patch), the public surface is tiny, and the whole thing is
dormant — a no-op that returns a clear reason — until credentials are set.

We post with the v2 endpoint `POST /2/tweets` using OAuth 1.0a user context,
which is the simplest scheme for posting to a single fixed account (no token
refresh). Signing is delegated to requests-oauthlib.

Required env vars (loaded via config/env_loader.py from bpoignant.json
locally or BPOIGNANT_SECRETS_JSON in production):
  BPOIGNANT_X__API_KEY         App API key (consumer key)
  BPOIGNANT_X__API_SECRET      App API secret (consumer secret)
  BPOIGNANT_X__ACCESS_TOKEN    Access token for the posting account
  BPOIGNANT_X__ACCESS_SECRET   Access token secret

Create these at https://developer.x.com → your app → Keys and tokens. The
app needs Read *and* Write permission, and the access token must be
regenerated after switching to Read+Write.
"""

import logging
import os
import re

import requests

log = logging.getLogger(__name__)

TWEETS_URL = 'https://api.twitter.com/2/tweets'
VERIFY_URL = 'https://api.twitter.com/1.1/account/verify_credentials.json'

TWEET_LIMIT = 280
# X wraps every link in a fixed-length t.co URL, so a link always costs this
# many characters regardless of its real length.
URL_WEIGHT = 23

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def _config():
    """Read X config from env. Resolved at call time so tests can patch."""
    return {
        'api_key': os.environ.get('BPOIGNANT_X__API_KEY', ''),
        'api_secret': os.environ.get('BPOIGNANT_X__API_SECRET', ''),
        'access_token': os.environ.get('BPOIGNANT_X__ACCESS_TOKEN', ''),
        'access_secret': os.environ.get('BPOIGNANT_X__ACCESS_SECRET', ''),
    }


def is_configured() -> bool:
    """True only when all four OAuth 1.0a credentials are present."""
    c = _config()
    return all([c['api_key'], c['api_secret'], c['access_token'], c['access_secret']])


def _plain(text: str) -> str:
    """Strip any stray HTML and collapse whitespace — summaries are usually
    plain text, but be defensive so no markup leaks into a tweet."""
    return _WS_RE.sub(' ', _TAG_RE.sub('', text or '')).strip()


def _truncate(s: str, n: int) -> str:
    """Trim to at most n characters, ending on a whole word with an ellipsis."""
    if n <= 0:
        return ''
    if len(s) <= n:
        return s
    cut = s[:n - 1].rstrip()
    # Prefer to break at the last space so we don't cut a word in half.
    sp = cut.rfind(' ')
    if sp >= n // 2:
        cut = cut[:sp].rstrip()
    return cut + '…'


def compose_article_tweet(title: str, summary: str, url: str, limit: int = TWEET_LIMIT) -> str:
    """Build a tweet for an article: title, then the summary (trimmed to fit),
    then the link on its own line. Always <= `limit` counted characters, where
    the link counts as URL_WEIGHT."""
    title = _plain(title)
    summary = _plain(summary)
    sep = '\n\n'

    # Fixed cost: link + the two blank-line separators around the body.
    fixed = URL_WEIGHT + 2 * len(sep)
    room_for_title_and_summary = limit - fixed

    if len(title) >= room_for_title_and_summary:
        # Title alone fills the budget — trim it and drop the summary.
        title = _truncate(title, limit - URL_WEIGHT - len(sep))
        return f"{title}{sep}{url}"

    room_for_summary = room_for_title_and_summary - len(title)
    if summary and room_for_summary > 1:
        summary = _truncate(summary, room_for_summary)
        return f"{title}{sep}{summary}{sep}{url}"
    return f"{title}{sep}{url}"


def verify_credentials():
    """Non-destructive credentials check. Calls the v1.1 verify_credentials
    endpoint and reads the ``x-access-level`` response header, which reports
    whether the access token is ``read`` or ``read-write`` — so we can confirm
    both that auth works *and* that posting is permitted, without tweeting.

    Returns (True, info) with keys ``screen_name`` and ``access_level`` on
    success, or (False, {'error': ...}) otherwise. Never raises."""
    cfg = _config()
    if not is_configured():
        return False, {'error': "X n'est pas configuré"}

    from requests_oauthlib import OAuth1
    auth = OAuth1(
        cfg['api_key'], cfg['api_secret'],
        cfg['access_token'], cfg['access_secret'],
    )
    try:
        resp = requests.get(VERIFY_URL, params={'skip_status': 'true'}, auth=auth, timeout=15)
    except requests.RequestException as exc:
        log.error("verify_credentials network error: %s", exc)
        return False, {'error': f"Erreur réseau : {exc}"}

    access_level = resp.headers.get('x-access-level', '')
    if resp.status_code == 200:
        data = resp.json()
        return True, {
            'screen_name': data.get('screen_name'),
            'name': data.get('name'),
            'access_level': access_level,
        }

    detail = resp.text[:200]
    try:
        errors = resp.json().get('errors') or []
        if errors:
            detail = errors[0].get('message', detail)
    except ValueError:
        pass
    return False, {'error': f"{resp.status_code} — {detail}", 'access_level': access_level}


def post_tweet(text: str):
    """Post `text` as a tweet. Returns (True, tweet_id) on success or
    (False, reason) otherwise — never raises, so callers can treat X as
    best-effort."""
    cfg = _config()
    if not is_configured():
        log.warning("post_tweet skipped — X credentials not set")
        return False, "X n'est pas configuré"

    from requests_oauthlib import OAuth1
    auth = OAuth1(
        cfg['api_key'], cfg['api_secret'],
        cfg['access_token'], cfg['access_secret'],
    )
    try:
        resp = requests.post(TWEETS_URL, json={'text': text}, auth=auth, timeout=15)
    except requests.RequestException as exc:
        log.error("post_tweet network error: %s", exc)
        return False, f"Erreur réseau : {exc}"

    if 200 <= resp.status_code < 300:
        tweet_id = (resp.json().get('data') or {}).get('id')
        return True, tweet_id

    log.error("post_tweet failed: status=%s body=%s", resp.status_code, resp.text[:300])
    detail = resp.text[:200]
    try:
        body = resp.json()
        detail = body.get('detail') or body.get('title') or detail
    except ValueError:
        pass
    return False, f"{resp.status_code} — {detail}"
