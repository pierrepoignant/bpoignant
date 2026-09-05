"""Read-only Gmail access, used to find the people Bernard actually
corresponds with.

Deliberately separate from `gdrive` even though both speak to Google with the
same OAuth client: the scopes are different, and a mailbox is not a document
folder. Drive keeps working if this is never connected, and revoking this does
not take Drive down with it.

Nothing here writes to the mailbox, and no message body is ever read — only
the From/To/Cc headers and the date. What comes back is a list of addresses to
*invite*, never to subscribe: someone who once wrote to Bernard has not asked
for his newsletter, and adding them without asking is both unlawful and the
fastest way to have the domain treated as a spammer.
"""

import logging
import re
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from urllib.parse import urlencode

import requests

from settings.models import get_config, set_config, delete_config

log = logging.getLogger(__name__)

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
API = 'https://gmail.googleapis.com/gmail/v1/users/me'

# Lecture seule, et rien d'autre : ni envoi, ni modification, ni suppression.
SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'

KEY_REFRESH_TOKEN = 'gmail_refresh_token'
KEY_ADDRESS = 'gmail_address'

_TIMEOUT = 30


class GmailError(RuntimeError):
    pass


class GmailAuthError(GmailError):
    pass


def _client():
    """The OAuth client is shared with Drive — same Google project, same
    console entry — so connecting Gmail needs no second set of credentials."""
    import gdrive
    return gdrive._client_id(), gdrive._client_secret()


def has_client_credentials():
    cid, secret = _client()
    return bool(cid and secret)


def refresh_token():
    return (get_config(KEY_REFRESH_TOKEN) or '').strip()


def address():
    return (get_config(KEY_ADDRESS) or '').strip()


def is_connected():
    return bool(refresh_token())


def disconnect():
    delete_config(KEY_REFRESH_TOKEN)
    delete_config(KEY_ADDRESS)


def authorization_url(redirect_uri, state):
    cid, _ = _client()
    if not cid:
        raise GmailError("Renseignez d'abord l'identifiant et le secret OAuth Google.")
    params = {
        'client_id': cid,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
    }
    return f'{AUTH_URL}?{urlencode(params)}'


def exchange_code(code, redirect_uri):
    cid, secret = _client()
    if not code:
        raise GmailError("Google n'a renvoyé aucun code d'autorisation.")
    resp = requests.post(TOKEN_URL, data={
        'client_id': cid, 'client_secret': secret, 'code': code,
        'grant_type': 'authorization_code', 'redirect_uri': redirect_uri,
    }, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GmailError(f"Google a refusé l'autorisation : {resp.text[:200]}")
    jeton = (resp.json() or {}).get('refresh_token')
    if not jeton:
        raise GmailError("Google n'a pas renvoyé de refresh token. Réessayez la connexion.")
    set_config(KEY_REFRESH_TOKEN, jeton.strip())
    try:
        set_config(KEY_ADDRESS, profile_address(_access_token()))
    except GmailError:
        pass


def _access_token():
    cid, secret = _client()
    jeton = refresh_token()
    if not (cid and secret and jeton):
        raise GmailAuthError("Gmail n'est pas connecté.")
    resp = requests.post(TOKEN_URL, data={
        'client_id': cid, 'client_secret': secret,
        'refresh_token': jeton, 'grant_type': 'refresh_token',
    }, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GmailAuthError(
            f"Autorisation Gmail refusée : {resp.text[:160]} Reconnectez la boîte.")
    tok = (resp.json() or {}).get('access_token')
    if not tok:
        raise GmailError("Réponse Google sans jeton d'accès.")
    return tok


def profile_address(token=None):
    token = token or _access_token()
    r = requests.get(f'{API}/profile', headers={'Authorization': f'Bearer {token}'},
                     timeout=_TIMEOUT)
    if r.status_code != 200:
        raise GmailError(f"Profil Gmail illisible ({r.status_code}).")
    return (r.json() or {}).get('emailAddress') or ''


# Adresses qui ne sont jamais des correspondants : services, robots, listes.
_IGNORE_RE = re.compile(
    r'(^|[.@_-])(no.?reply|ne.?pas.?repondre|notification|newsletter|mailer|'
    r'postmaster|bounce|donotreply|support|contact@|info@|admin@|abuse|'
    r'noreply|automated|do-not-reply)', re.I)
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _skippable(addr):
    if not addr or not _EMAIL_RE.match(addr):
        return True
    if _IGNORE_RE.search(addr):
        return True
    # Les adresses de suivi et de réponse automatique des grands services.
    return addr.lower().endswith(('.mailgun.org', '.sendgrid.net', 'google.com'))


def _messages(token, query, limit):
    """Message ids matching `query`, newest first."""
    ids, page = [], None
    while len(ids) < limit:
        params = {'q': query, 'maxResults': min(100, limit - len(ids))}
        if page:
            params['pageToken'] = page
        r = requests.get(f'{API}/messages', params=params,
                         headers={'Authorization': f'Bearer {token}'}, timeout=_TIMEOUT)
        if r.status_code != 200:
            raise GmailError(f"Lecture des messages impossible ({r.status_code}).")
        body = r.json() or {}
        ids.extend(m['id'] for m in (body.get('messages') or []))
        page = body.get('nextPageToken')
        if not page:
            break
    return ids[:limit]


def _headers(token, message_id):
    """From/To/Cc/Date of one message. `format=metadata` with an explicit
    header list means Google never sends us the body — we do not want it and
    should not receive it."""
    r = requests.get(
        f'{API}/messages/{message_id}',
        params=[('format', 'metadata')] + [('metadataHeaders', h)
                                           for h in ('From', 'To', 'Cc', 'Date')],
        headers={'Authorization': f'Bearer {token}'}, timeout=_TIMEOUT)
    if r.status_code != 200:
        return {}
    out = {}
    for h in ((r.json() or {}).get('payload') or {}).get('headers') or []:
        out[h.get('name', '').lower()] = h.get('value', '')
    return out


def recent_contacts(limit=100, scan=400):
    """The people Bernard has most recently written to or heard from.

    Walks the sent box and the inbox newest-first and keeps one entry per
    address, holding the most recent exchange and which way it went. `scan`
    caps how many messages are read: reaching a hundred distinct people takes
    more than a hundred messages, and the walk should not run away.

    Returns a list of dicts, newest exchange first.
    """
    token = _access_token()
    moi = (address() or profile_address(token)).lower()

    trouves = {}
    for requete, direction in (('in:sent', 'envoyé'), ('in:inbox', 'reçu')):
        for mid in _messages(token, requete, scan // 2):
            h = _headers(token, mid)
            if not h:
                continue
            try:
                quand = parsedate_to_datetime(h.get('date', '')).replace(tzinfo=None)
            except (TypeError, ValueError):
                quand = None

            if direction == 'envoyé':
                brut = f"{h.get('to', '')},{h.get('cc', '')}"
            else:
                brut = h.get('from', '')

            for nom, adresse in getaddresses([brut]):
                adresse = (adresse or '').strip().lower()
                if _skippable(adresse) or adresse == moi:
                    continue
                actuel = trouves.get(adresse)
                if actuel and actuel['last'] and quand and actuel['last'] >= quand:
                    continue
                trouves[adresse] = {
                    'email': adresse,
                    'name': (nom or '').strip().strip('"') or (actuel or {}).get('name') or '',
                    'last': quand,
                    'direction': direction,
                }
            if len(trouves) >= limit * 3:
                break

    contacts = sorted(trouves.values(),
                      key=lambda c: c['last'] or datetime.min, reverse=True)
    return contacts[:limit]
