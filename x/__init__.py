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
ME_URL = 'https://api.twitter.com/2/users/me'
MENTIONS_URL = 'https://api.twitter.com/2/users/{user_id}/mentions'

# `GET /2/tweets` takes up to 100 ids per call, so metrics for the whole blog
# cost a single request.
METRICS_BATCH = 100
# Safety valve on the mentions timeline: with `since_id` a daily run normally
# needs one page, so anything beyond this is a backlog we'd rather cap than
# spend credits paginating through.
MAX_MENTION_PAGES = 5

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


class XError(RuntimeError):
    """Raised by the read endpoints when X can't be reached or answers with an
    error. The posting helpers keep their (ok, detail) contract instead — a
    failed share is best-effort, a failed sync is worth surfacing."""


def _auth():
    """OAuth 1.0a signer for the configured account."""
    from requests_oauthlib import OAuth1
    c = _config()
    return OAuth1(
        c['api_key'], c['api_secret'],
        c['access_token'], c['access_secret'],
    )


def _get(url, params):
    """GET an API v2 endpoint and return the decoded body, raising XError on
    anything that isn't a 2xx. 402 and 429 get their own message because they
    are the two an operator can actually act on."""
    if not is_configured():
        raise XError("X n'est pas configuré")
    try:
        resp = requests.get(url, params=params, auth=_auth(), timeout=20)
    except requests.RequestException as exc:
        raise XError(f"Erreur réseau : {exc}") from exc

    if 200 <= resp.status_code < 300:
        return resp.json()

    if resp.status_code == 402:
        raise XError("Crédits API X épuisés — rechargez dans la console développeur.")
    if resp.status_code == 429:
        raise XError("Limite de requêtes X atteinte — réessayez plus tard.")

    detail = resp.text[:200]
    try:
        body = resp.json()
        detail = body.get('detail') or body.get('title') or detail
    except ValueError:
        pass
    raise XError(f"{resp.status_code} — {detail}")


def account_id() -> str:
    """Numeric id of the authenticated account, needed by the mentions
    timeline. One cheap call; callers are expected to cache the result."""
    data = _get(ME_URL, {}).get('data') or {}
    user_id = data.get('id')
    if not user_id:
        raise XError("Réponse inattendue de /2/users/me.")
    return str(user_id)


def fetch_account() -> dict:
    """Account id, handle and follower figures in one call.

    Same endpoint as `account_id()` with `user.fields=public_metrics`, so the
    daily follower snapshot costs nothing extra beyond the call itself — and
    it hands back the id, which the caller can cache instead of asking again.
    """
    body = _get(ME_URL, {'user.fields': 'public_metrics,username,name'})
    data = body.get('data') or {}
    if not data.get('id'):
        raise XError("Réponse inattendue de /2/users/me.")
    m = data.get('public_metrics') or {}
    return {
        'id': str(data['id']),
        'username': data.get('username') or '',
        'name': data.get('name') or '',
        'followers': m.get('followers_count', 0),
        'following': m.get('following_count', 0),
        'tweets': m.get('tweet_count', 0),
        'listed': m.get('listed_count', 0),
    }


def fetch_metrics(tweet_ids):
    """Return {tweet_id: {likes, views, replies, retweets, quotes}} for the
    given ids, in batches of 100. Ids X no longer knows about (deleted tweets)
    are simply absent from the result rather than raising."""
    out = {}
    ids = [str(i) for i in tweet_ids if i]
    for start in range(0, len(ids), METRICS_BATCH):
        batch = ids[start:start + METRICS_BATCH]
        body = _get(TWEETS_URL, {
            'ids': ','.join(batch),
            'tweet.fields': 'public_metrics',
        })
        for item in (body.get('data') or []):
            m = item.get('public_metrics') or {}
            out[str(item['id'])] = {
                'likes': m.get('like_count', 0),
                # impression_count is only returned for the authenticated
                # account's own tweets — which is exactly what we ask for.
                'views': m.get('impression_count', 0),
                'replies': m.get('reply_count', 0),
                'retweets': m.get('retweet_count', 0),
                'quotes': m.get('quote_count', 0),
            }
        for err in (body.get('errors') or []):
            log.info("fetch_metrics: id absente (%s) — %s",
                     err.get('value'), err.get('detail', '')[:120])
    return out


def fetch_mentions(user_id, since_id=None):
    """Return (replies, newest_id) from the mentions timeline.

    A reply to one of Bernard's tweets mentions him by construction, so this
    one endpoint surfaces new replies across every tweet whatever its age —
    unlike `search/recent`, which is per-conversation and only reaches back
    seven days. `since_id` keeps each run to the handful that arrived since
    the last one.

    Each reply is a dict with id, conversation_id (the root tweet, i.e. our
    `x_post_id`), author_id, author_username, author_name, text, created_at.
    `newest_id` is what to pass as `since_id` next time — None when nothing
    new came back, so the caller leaves its cursor alone.
    """
    params = {
        'max_results': 100,
        'tweet.fields': 'created_at,conversation_id,author_id',
        'expansions': 'author_id',
        'user.fields': 'username,name',
    }
    if since_id:
        params['since_id'] = str(since_id)

    # Cold start (no cursor): take the most recent page only. Paginating back
    # through years of mentions costs a call per page to read replies that
    # pre-date every article tweet — the cursor we set from this run makes
    # every later run cheap. With a cursor we do allow catching up, for the
    # rare day that draws more than one page of replies.
    max_pages = MAX_MENTION_PAGES if since_id else 1

    replies = []
    newest_id = None
    token = None
    for _ in range(max_pages):
        page_params = dict(params)
        if token:
            page_params['pagination_token'] = token
        body = _get(MENTIONS_URL.format(user_id=user_id), page_params)

        users = {u['id']: u for u in ((body.get('includes') or {}).get('users') or [])}
        for item in (body.get('data') or []):
            author = users.get(item.get('author_id')) or {}
            replies.append({
                'id': str(item['id']),
                'conversation_id': str(item.get('conversation_id') or ''),
                'author_id': str(item.get('author_id') or ''),
                'author_username': author.get('username') or '',
                'author_name': author.get('name') or '',
                'text': item.get('text') or '',
                'created_at': item.get('created_at') or '',
            })

        meta = body.get('meta') or {}
        # `newest_id` is per page; the first page holds the newest of the run.
        newest_id = newest_id or meta.get('newest_id')
        token = meta.get('next_token')
        if not token:
            break
    else:
        if since_id:
            log.warning("fetch_mentions: arrêt à %s pages, backlog restant",
                        MAX_MENTION_PAGES)

    return replies, newest_id


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


def share_url(url, source='x'):
    """Append a UTM source to a link being shared on X.

    Two purposes. It attributes traffic, so X shows up distinctly in the
    referrer stats instead of blending into direct visits. And it gives X a
    URL it has not crawled before: X caches card metadata per exact URL, with
    no way to force a refresh since the Card Validator was retired, so a page
    whose tags were broken when X first saw it would otherwise keep serving a
    broken card for about a week.

    Safe for SEO: base.html emits rel="canonical" from request.base_url, which
    drops the query string, so search engines still see one URL. Analytics logs
    request.path, so page-view counts don't fragment either.
    """
    if not url:
        return url
    separator = '&' if '?' in url else '?'
    return f"{url}{separator}utm_source={source}"


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


def delete_tweet(tweet_id):
    """Delete a tweet we posted. Returns (True, None) on success or
    (False, reason) — same best-effort contract as post_tweet."""
    cfg = _config()
    if not is_configured():
        return False, "X n'est pas configuré"

    from requests_oauthlib import OAuth1
    auth = OAuth1(
        cfg['api_key'], cfg['api_secret'],
        cfg['access_token'], cfg['access_secret'],
    )
    try:
        resp = requests.delete(f"{TWEETS_URL}/{tweet_id}", auth=auth, timeout=15)
    except requests.RequestException as exc:
        log.error("delete_tweet network error: %s", exc)
        return False, f"Erreur réseau : {exc}"

    if 200 <= resp.status_code < 300:
        return True, None

    log.error("delete_tweet failed: status=%s body=%s", resp.status_code, resp.text[:300])
    detail = resp.text[:200]
    try:
        body = resp.json()
        detail = body.get('detail') or body.get('title') or detail
    except ValueError:
        pass
    return False, f"{resp.status_code} — {detail}"


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
