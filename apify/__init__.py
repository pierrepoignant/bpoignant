"""Apify integration — scraping TikTok data.

TikTok has no public API for reading a personal account's figures, so the
numbers are collected by running an Apify actor. Credentials live in the
database `config` table, set from the admin "Réglages" page, exactly like the
Google Drive ones — so the token can be changed without a deploy, and it never
sits in the repository.

Env vars of the same name are honoured as a fallback, which keeps the module
usable from a script or a cron before anything has been entered in the UI.
"""

import os

import requests

from settings.models import get_config, set_config, delete_config

API_BASE = 'https://api.apify.com/v2'

# Database config keys (with their env-var fallbacks).
KEY_TOKEN = 'apify_token'
KEY_ACTOR = 'apify_tiktok_actor'
KEY_PROFILE = 'apify_tiktok_profile'

# The usual community TikTok actor. Configurable because actors get renamed,
# deprecated, or swapped for a cheaper one, and none of that should need a
# code change.
DEFAULT_ACTOR = 'clockworks~tiktok-scraper'

_TIMEOUT = 30
# An actor run is not instant; this is the ceiling for the synchronous call
# that starts a run and waits for its dataset.
_RUN_TIMEOUT = 300


class ApifyError(RuntimeError):
    """Raised when Apify can't be reached or answers with an error."""


# ── Configuration ────────────────────────────────────────────

def token():
    return (get_config(KEY_TOKEN) or os.environ.get('APIFY__TOKEN') or '').strip()


def actor():
    return (get_config(KEY_ACTOR) or os.environ.get('APIFY__TIKTOK_ACTOR')
            or DEFAULT_ACTOR).strip()


def profile():
    """The TikTok handle to scrape, without the @."""
    return (get_config(KEY_PROFILE) or os.environ.get('APIFY__TIKTOK_PROFILE')
            or '').strip().lstrip('@')


def is_configured():
    """A token is the only hard requirement — the actor has a default and the
    profile is only needed for profile-wide scrapes."""
    return bool(token())


def save_settings(api_token, tiktok_actor, tiktok_profile):
    """Store what was entered. A blank token means "keep the existing one", so
    the page can be re-submitted without the secret being echoed back into it."""
    api_token = (api_token or '').strip()
    if api_token:
        set_config(KEY_TOKEN, api_token)
    set_config(KEY_ACTOR, (tiktok_actor or '').strip() or DEFAULT_ACTOR)
    set_config(KEY_PROFILE, (tiktok_profile or '').strip().lstrip('@'))


def disconnect():
    """Forget the token; the actor and profile are harmless and stay."""
    delete_config(KEY_TOKEN)


# ── API ──────────────────────────────────────────────────────

def _get(path, **params):
    if not is_configured():
        raise ApifyError("Apify n'est pas configuré (jeton manquant).")
    params['token'] = token()
    try:
        resp = requests.get(f'{API_BASE}{path}', params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise ApifyError(f"Connexion à Apify impossible : {exc}") from exc
    if resp.status_code == 401:
        raise ApifyError("Jeton Apify refusé (401). Vérifiez-le dans Réglages.")
    if resp.status_code != 200:
        raise ApifyError(f"Apify a répondu {resp.status_code} : {resp.text[:200]}")
    return resp.json()


def account_info():
    """Who the token belongs to, and what plan. Used by the "Tester" button:
    it proves the token works without spending a run."""
    data = _get('/users/me').get('data') or {}
    return {
        'username': data.get('username'),
        'plan': (data.get('plan') or {}).get('id'),
        'monthly_usage_usd': (data.get('plan') or {}).get('monthlyUsageUsd'),
    }


def run_actor(payload, actor_id=None, timeout=_RUN_TIMEOUT):
    """Run the actor and return its dataset items.

    Uses run-sync-get-dataset-items, which starts a run and waits: one HTTP
    call instead of start / poll / fetch. Actor runs cost credits, so this is
    never called to check configuration — see `account_info`.
    """
    if not is_configured():
        raise ApifyError("Apify n'est pas configuré (jeton manquant).")
    aid = (actor_id or actor()).replace('/', '~')
    try:
        resp = requests.post(
            f'{API_BASE}/acts/{aid}/run-sync-get-dataset-items',
            params={'token': token()}, json=payload, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ApifyError(f"Exécution Apify impossible : {exc}") from exc
    if resp.status_code == 404:
        raise ApifyError(f"Acteur introuvable : {aid}. Vérifiez son identifiant dans Réglages.")
    if resp.status_code >= 400:
        raise ApifyError(f"Apify a répondu {resp.status_code} : {resp.text[:200]}")
    try:
        items = resp.json()
    except ValueError:
        raise ApifyError("Réponse Apify illisible.")
    return items if isinstance(items, list) else []


def scrape_profile(handle=None, limit=30):
    """Recent videos for a TikTok profile."""
    handle = (handle or profile()).lstrip('@')
    if not handle:
        raise ApifyError("Aucun profil TikTok renseigné dans Réglages.")
    return run_actor({'profiles': [handle], 'resultsPerPage': limit,
                      'shouldDownloadVideos': False,
                      'shouldDownloadCovers': False})


def scrape_posts(urls):
    """Figures for specific TikTok post URLs."""
    urls = [u for u in (urls or []) if u]
    if not urls:
        return []
    return run_actor({'postURLs': urls, 'shouldDownloadVideos': False,
                      'shouldDownloadCovers': False})


def normalise(item):
    """Flatten one actor result into the handful of fields we care about.

    Actors change their output shape between versions, so every field is read
    defensively and a missing one becomes None rather than raising — a renamed
    key should cost one empty column, not the whole sync.
    """
    stats = item.get('stats') or item.get('statistics') or {}

    def pick(*names):
        for n in names:
            if item.get(n) is not None:
                return item.get(n)
            if stats.get(n) is not None:
                return stats.get(n)
        return None

    return {
        'url': pick('webVideoUrl', 'postPage', 'url'),
        'id': pick('id', 'videoId'),
        'text': pick('text', 'desc'),
        'created_at': pick('createTimeISO', 'createTime'),
        'views': pick('playCount', 'views'),
        'likes': pick('diggCount', 'likes'),
        'comments': pick('commentCount', 'comments'),
        'shares': pick('shareCount', 'shares'),
        'author': (item.get('authorMeta') or {}).get('name') or pick('authorName'),
    }
