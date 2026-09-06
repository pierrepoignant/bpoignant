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
import time
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


def _explain(resp, defaut):
    """Google's own words rather than a bare status code.

    A 403 here is almost always "the Gmail API is not enabled in this project",
    which is a two-click fix — but only if the message reaches the person
    looking at the screen.
    """
    try:
        err = (resp.json() or {}).get('error') or {}
    except ValueError:
        return f"{defaut} ({resp.status_code})"
    message = (err.get('message') or '').strip()
    if not message:
        return f"{defaut} ({resp.status_code})"
    if (err.get('status') == 'PERMISSION_DENIED'
            and 'has not been used in project' in message):
        lien = ''
        for d in err.get('details') or []:
            lien = ((d.get('metadata') or {}).get('activationUrl')) or lien
        return ("L'API Gmail n'est pas activée dans le projet Google du site. "
                "Activez-la dans la console, puis réessayez"
                + (f" : {lien}" if lien else '.'))
    return f"{defaut} : {message}"


def profile_address(token=None):
    """The connected mailbox's own address, cached once known.

    It is fetched through the same backoff as everything else: this is the
    first call of an import, and failing it on a momentary quota limit aborts
    the run before it has read a single message. The result is stored, because
    the address does not change and the connection may well have been made
    before the API was enabled — which is exactly when the first attempt to
    read it failed.
    """
    token = token or _access_token()
    r = _get(f'{API}/profile', token, None)
    if r.status_code != 200:
        raise GmailError(_explain(r, "Profil Gmail illisible"))
    adresse = (r.json() or {}).get('emailAddress') or ''
    if adresse and adresse != address():
        set_config(KEY_ADDRESS, adresse)
    return adresse


# Adresses qui ne sont jamais des correspondants : services, robots, listes.
_IGNORE_RE = re.compile(
    r'(^|[.@_+-])(no.?reply|ne.?pas.?repondre|notification|newsletter|mailer|'
    r'postmaster|bounce|donotreply|support|contact@|info@|admin@|abuse|'
    r'noreply|automated|do-not-reply|invoice|facture|billing|receipt|'
    r'statements?|alerte?s?|nepasrepondre)', re.I)
# Services dont aucune adresse n'est un correspondant.
_IGNORE_DOMAINS = ('stripe.com', 'paypal.com', 'notify.', 'mailchimp.com',
                   'substack.com', 'eventbrite.com', 'doctolib.fr')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _skippable(addr):
    if not addr or not _EMAIL_RE.match(addr):
        return True
    if _IGNORE_RE.search(addr):
        return True
    bas = addr.lower()
    domaine = bas.rsplit('@', 1)[-1]
    if any(domaine == d or domaine.endswith('.' + d) or d.endswith('.') and domaine.startswith(d)
           for d in _IGNORE_DOMAINS):
        return True
    # Les adresses de suivi et de réponse automatique des grands services.
    return bas.endswith(('.mailgun.org', '.sendgrid.net', 'google.com'))


def _get(url, token, params=None, tentatives=5):
    """GET with backoff on Gmail's rate limiter.

    Each message read costs quota units, and a few hundred of them in a row
    trips the per-minute allowance. Google answers 429 and expects the caller
    to wait; failing the whole import over a limit that clears in seconds
    would be throwing away several minutes of work.
    """
    attente = 2
    for essai in range(tentatives):
        r = requests.get(url, params=params,
                         headers={'Authorization': f'Bearer {token}'}, timeout=_TIMEOUT)
        if r.status_code != 429 and not (r.status_code == 403 and 'ratelimit' in r.text.lower()
                                         or 'Quota exceeded' in r.text):
            return r
        if essai == tentatives - 1:
            return r
        log.info('Gmail: quota atteint, pause de %ss', attente)
        time.sleep(attente)
        attente *= 2
    return r


def _messages(token, query, limit):
    """Message ids matching `query`, newest first."""
    ids, page = [], None
    while len(ids) < limit:
        params = {'q': query, 'maxResults': min(100, limit - len(ids))}
        if page:
            params['pageToken'] = page
        r = _get(f'{API}/messages', token, params)
        if r.status_code != 200:
            raise GmailError(_explain(r, "Lecture des messages impossible"))
        body = r.json() or {}
        ids.extend(m['id'] for m in (body.get('messages') or []))
        page = body.get('nextPageToken')
        if not page:
            break
    return ids[:limit]


# Gmail compte son quota à la minute : c'est la cadence qui le fait sauter, pas
# le volume total. Une pause courte entre deux lectures coûte quelques minutes
# sur un gros balayage et évite de passer son temps en marche arrière.
PAUSE_ENTRE_LECTURES = 0.06


def _headers(token, message_id):
    """From/To/Cc/Date of one message. `format=metadata` with an explicit
    header list means Google never sends us the body — we do not want it and
    should not receive it."""
    r = _get(f'{API}/messages/{message_id}', token,
             [('format', 'metadata')] + [('metadataHeaders', h)
                                         for h in ('From', 'To', 'Cc', 'Date')])
    if r.status_code != 200:
        return {}
    out = {}
    for h in ((r.json() or {}).get('payload') or {}).get('headers') or []:
        out[h.get('name', '').lower()] = h.get('value', '')
    return out


def recent_contacts(limit=100, scan=600, before=None):
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
    # `to:me` plutôt que `in:inbox` : Bernard archive tout, sa boîte de
    # réception ne contient qu'une poignée de messages, et chercher là ne
    # trouvait presque aucun correspondant entrant. `-in:chats` écarte les
    # conversations Hangouts, qui ne sont pas du courrier.
    # `before` fait reculer la fenêtre : sans lui, chaque passe repart des
    # messages les plus récents et relit exactement les mêmes.
    borne = f' before:{before:%Y/%m/%d}' if before else ''
    for requete, direction in ((f'in:sent{borne}', 'envoyé'),
                               (f'to:me -in:chats -in:sent{borne}', 'reçu')):
        for mid in _messages(token, requete, scan // 2):
            h = _headers(token, mid)
            time.sleep(PAUSE_ENTRE_LECTURES)
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
