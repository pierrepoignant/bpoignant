"""SendGrid wrapper.

We call the SendGrid v3 REST API directly with `requests` rather than the
official Python SDK to avoid an extra dependency. The whole surface is
one function — `send_email()` — that the rest of the app uses.

Required env vars (loaded via config/env_loader.py from bpoignant.json
locally or BPOIGNANT_SECRETS_JSON in production):
  SENDGRID__API_KEY      Twilio SendGrid API key
  MAIL_FROM_EMAIL        Verified sender (Single Sender or domain-auth address)
  MAIL_FROM_NAME         Display name shown in the recipient's inbox

Reply-To is fixed to bernard.poignant@gmail.com (see REPLY_TO below) so
replies always reach Bernard, regardless of environment configuration.
"""

import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)

SENDGRID_URL = 'https://api.sendgrid.com/v3/mail/send'

# Account-wide suppression groups we pull to suppress bad addresses. These are
# global to the SendGrid account (shared across every app that uses the same
# key), so a bounce seen by any app is honoured here too.
SUPPRESSION_ENDPOINTS = {
    'bounce':        'https://api.sendgrid.com/v3/suppression/bounces',
    'block':         'https://api.sendgrid.com/v3/suppression/blocks',
    'spamreport':    'https://api.sendgrid.com/v3/suppression/spam_reports',
    'invalid_email': 'https://api.sendgrid.com/v3/suppression/invalid_emails',
}

# Where replies to newsletter e-mails go. Hard-coded (not from the
# environment) so a stray MAIL_REPLY_TO can't redirect replies elsewhere.
REPLY_TO = 'bernard.poignant@gmail.com'


def _config():
    """Read mail config from env. Resolved at call time so tests can patch."""
    api_key = os.environ.get('SENDGRID__API_KEY', '')
    return {
        'api_key': api_key,
        'from_email': os.environ.get('MAIL_FROM_EMAIL', 'noreply@bernardpoignant.fr'),
        'from_name': os.environ.get('MAIL_FROM_NAME', 'Bernard Poignant'),
        'reply_to': REPLY_TO,
    }


def is_configured() -> bool:
    return bool(_config()['api_key'])


def send_email(
    to_email: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    to_name: Optional[str] = None,
    categories=None,
) -> bool:
    """Send one email via SendGrid. Returns True on 2xx, False otherwise.

    Returns False (and logs) if SendGrid isn't configured, rather than
    raising — callers don't need to check is_configured() first.

    `categories` tags the message (e.g. ['newsletter', 'article-42']) so its
    opens/clicks can later be pulled per-category from the Stats API.
    """
    cfg = _config()
    if not cfg['api_key']:
        log.warning("send_email skipped — SENDGRID__API_KEY not set (to=%s, subject=%s)", to_email, subject)
        return False

    to_entry = {'email': to_email}
    if to_name:
        to_entry['name'] = to_name

    body = {
        'personalizations': [{'to': [to_entry], 'subject': subject}],
        'from': {'email': cfg['from_email'], 'name': cfg['from_name']},
        'content': [
            {'type': 'text/plain', 'value': text or _html_to_text(html)},
            {'type': 'text/html',  'value': html},
        ],
    }
    if categories:
        # SendGrid caps categories at 10 and 255 chars each.
        body['categories'] = [str(c)[:255] for c in categories][:10]
    if cfg['reply_to']:
        body['reply_to'] = {'email': cfg['reply_to']}

    try:
        resp = requests.post(
            SENDGRID_URL,
            json=body,
            headers={'Authorization': f"Bearer {cfg['api_key']}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        log.error("send_email network error (to=%s): %s", to_email, exc)
        return False

    if 200 <= resp.status_code < 300:
        return True

    log.error(
        "send_email failed: status=%s to=%s body=%s",
        resp.status_code, to_email, resp.text[:500],
    )
    return False


def fetch_suppressions(kinds=None, start_time=None):
    """Pull suppressed addresses from SendGrid's account-wide suppression
    lists (bounces / blocks / spam reports / invalid emails).

    Returns a dict ``{email: {'kind', 'reason', 'created'}}``. The first kind
    that lists an address wins (bounce/block before spamreport/invalid).
    ``start_time`` (unix seconds) limits to entries created since then — handy
    for incremental syncs. Returns ``{}`` when SendGrid isn't configured.
    """
    cfg = _config()
    if not cfg['api_key']:
        return {}

    kinds = kinds or list(SUPPRESSION_ENDPOINTS)
    headers = {'Authorization': f"Bearer {cfg['api_key']}"}
    out = {}
    for kind in kinds:
        url = SUPPRESSION_ENDPOINTS.get(kind)
        if not url:
            continue
        offset, page = 0, 500
        while True:
            params = {'limit': page, 'offset': offset}
            if start_time:
                params['start_time'] = int(start_time)
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=20)
            except requests.RequestException as exc:
                log.error("fetch_suppressions network error (%s): %s", kind, exc)
                break
            if resp.status_code != 200:
                log.error("fetch_suppressions failed (%s): status=%s body=%s",
                          kind, resp.status_code, resp.text[:300])
                break
            batch = resp.json() or []
            for row in batch:
                addr = (row.get('email') or '').strip().lower()
                if not addr or addr in out:
                    continue
                out[addr] = {
                    'kind': kind,
                    'reason': row.get('reason') or kind,
                    'created': row.get('created'),
                }
            if len(batch) < page:
                break
            offset += page
    return out


MAX_STATS_CATEGORIES = 10   # hard limit of /v3/categories/stats
CATEGORY_STATS_URL = 'https://api.sendgrid.com/v3/categories/stats'


def fetch_category_stats(category, start_date, end_date=None):
    """Pull aggregated open/click stats for one SendGrid category (e.g.
    ``article-42``) between ``start_date`` and ``end_date`` (``date`` objects
    or ``YYYY-MM-DD`` strings; end defaults to today).

    Returns a dict with summed ``delivered / opens / unique_opens / clicks /
    unique_clicks / bounces / spam_reports`` plus derived ``open_rate`` and
    ``click_rate`` (unique / delivered, as fractions), and a per-day ``series``.
    Returns ``None`` when SendGrid isn't configured or the call fails, so the
    caller can degrade gracefully.

    Note: requires Open & Click Tracking to be enabled in SendGrid, and only
    reflects mail sent *with* this category (i.e. going forward).
    """
    cfg = _config()
    if not cfg['api_key']:
        return None

    def _fmt(d):
        return d if isinstance(d, str) else d.strftime('%Y-%m-%d')

    params = {
        'start_date': _fmt(start_date),
        'categories': category,
        'aggregated_by': 'day',
    }
    if end_date is not None:
        params['end_date'] = _fmt(end_date)

    try:
        resp = requests.get(
            CATEGORY_STATS_URL,
            headers={'Authorization': f"Bearer {cfg['api_key']}"},
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        log.error("fetch_category_stats network error (%s): %s", category, exc)
        return None
    if resp.status_code != 200:
        log.error("fetch_category_stats failed (%s): status=%s body=%s",
                  category, resp.status_code, resp.text[:300])
        return None

    keys = ('delivered', 'opens', 'unique_opens', 'clicks', 'unique_clicks',
            'bounces', 'spam_reports', 'requests', 'unsubscribes')
    totals = {k: 0 for k in keys}
    series = []
    for day in resp.json() or []:
        m = {}
        for stat in day.get('stats', []):
            m = stat.get('metrics', {}) or {}
            break
        for k in keys:
            totals[k] += int(m.get(k, 0) or 0)
        series.append({
            'date': day.get('date'),
            'delivered': int(m.get('delivered', 0) or 0),
            'unique_opens': int(m.get('unique_opens', 0) or 0),
            'unique_clicks': int(m.get('unique_clicks', 0) or 0),
        })

    delivered = totals['delivered'] or 0
    totals['open_rate'] = (totals['unique_opens'] / delivered) if delivered else 0.0
    totals['click_rate'] = (totals['unique_clicks'] / delivered) if delivered else 0.0
    totals['series'] = series
    return totals


def fetch_known_categories(prefix=None):
    """Names of the categories SendGrid actually knows about.

    Worth one call because the stats endpoint 404s the *entire* request when a
    single unknown category is included — and every article mailed before the
    category tags existed is unknown. Filtering up front avoids both the error
    and the retry-splitting that used to work around it.
    """
    cfg = _config()
    if not cfg['api_key']:
        return None
    params = {'limit': 500}
    if prefix:
        params['category'] = prefix
    try:
        resp = requests.get(
            'https://api.sendgrid.com/v3/categories',
            headers={'Authorization': f"Bearer {cfg['api_key']}"},
            params=params, timeout=20,
        )
    except requests.RequestException as exc:
        log.error("fetch_known_categories network error: %s", exc)
        return None
    if resp.status_code != 200:
        log.error("fetch_known_categories failed: status=%s", resp.status_code)
        return None
    return {c.get('category') for c in (resp.json() or []) if c.get('category')}


def fetch_multi_category_stats(categories, start_date, end_date=None,
                               aggregated_by='day'):
    """Stats for several categories in one request.

    `/v3/categories/stats` accepts a repeated `categories` parameter and
    returns, per period, one entry per category — so the whole per-article
    table costs one call instead of one call per article.

    Returns {category: {'delivered', 'unique_opens', 'unique_clicks', 'opens',
    'clicks', 'series': [{'date', ...}]}} or None when SendGrid is off or the
    call fails, so callers can degrade rather than break.
    """
    cfg = _config()
    if not cfg['api_key'] or not categories:
        return None

    # The endpoint rejects more than 10 categories per request, so split and
    # merge. Still far cheaper than one call per article.
    categories = list(categories)
    if len(categories) > MAX_STATS_CATEGORIES:
        merged = {}
        for i in range(0, len(categories), MAX_STATS_CATEGORIES):
            chunk = fetch_multi_category_stats(
                categories[i:i + MAX_STATS_CATEGORIES],
                start_date, end_date, aggregated_by)
            if chunk is None:
                return None
            merged.update(chunk)
        return merged

    def _fmt(d):
        return d if isinstance(d, str) else d.strftime('%Y-%m-%d')

    params = [('start_date', _fmt(start_date)), ('aggregated_by', aggregated_by)]
    if end_date is not None:
        params.append(('end_date', _fmt(end_date)))
    params += [('categories', c) for c in categories]

    try:
        resp = requests.get(
            CATEGORY_STATS_URL,
            headers={'Authorization': f"Bearer {cfg['api_key']}"},
            params=params,
            timeout=30,
        )
    except requests.RequestException as exc:
        log.error("fetch_multi_category_stats network error: %s", exc)
        return None
    if resp.status_code != 200:
        log.error("fetch_multi_category_stats failed: status=%s body=%s",
                  resp.status_code, resp.text[:300])
        return None

    keys = ('delivered', 'opens', 'unique_opens', 'clicks', 'unique_clicks',
            'requests', 'bounces', 'spam_reports', 'unsubscribes')
    out = {c: {k: 0 for k in keys} | {'series': []} for c in categories}
    for period in resp.json() or []:
        for stat in period.get('stats', []):
            name = stat.get('name')
            if name not in out:
                continue
            m = stat.get('metrics', {}) or {}
            for k in keys:
                out[name][k] += int(m.get(k, 0) or 0)
            out[name]['series'].append({
                'date': period.get('date'),
                'delivered': int(m.get('delivered', 0) or 0),
                'unique_opens': int(m.get('unique_opens', 0) or 0),
                'unique_clicks': int(m.get('unique_clicks', 0) or 0),
            })
    for c, t in out.items():
        d = t['delivered'] or 0
        t['open_rate'] = (t['unique_opens'] / d) if d else 0.0
        t['click_rate'] = (t['unique_clicks'] / d) if d else 0.0
    return out


def _html_to_text(html: str) -> str:
    """Crude HTML-to-plain fallback so we always send a multipart message.
    Good enough for our hand-written templates."""
    import re
    # Drop <style>…</style> first
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.S | re.I)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'</p>', '\n\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    # Collapse whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
