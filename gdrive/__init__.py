"""Read-only Google Drive access for importing article content.

Lets the admin pull the body of one of Bernard's Google Docs straight into the
article editor, instead of copy-pasting. We only ever *read* from Drive.

Credentials live in the database `config` table (see `settings/`), set through
the admin "Réglages" page:

    google_client_id       OAuth client id     (…apps.googleusercontent.com)
    google_client_secret   OAuth client secret
    google_refresh_token    obtained via the "Connecter" OAuth flow

For backward compatibility the matching `GOOGLE__CLIENT_ID` /
`GOOGLE__CLIENT_SECRET` / `GOOGLE__REFRESH_TOKEN` env vars are used as a
fallback when a value isn't set in the database.

Auth is the standard OAuth "authorization code" flow: the admin clicks
"Connecter", consents once as Bernard.Poignant@gmail.com, and Google returns a
long-lived **refresh token** (we request `access_type=offline` +
`prompt=consent`). That refresh token is stored and then exchanged for a
short-lived **access token** on every Drive call. No heavy Google SDK — the
whole thing is a handful of plain HTTPS calls via `requests`.
"""

import os
import re
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from settings.models import get_config, set_config, delete_config


AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
FILES_URL = 'https://www.googleapis.com/drive/v3/files'
DOC_MIME = 'application/vnd.google-apps.document'
SCOPE = 'https://www.googleapis.com/auth/drive.readonly'

# Database config keys (with their env-var fallbacks).
KEY_CLIENT_ID = 'google_client_id'
KEY_CLIENT_SECRET = 'google_client_secret'
KEY_REFRESH_TOKEN = 'google_refresh_token'

# Connection health, so an expired token is announced rather than discovered
# the next time someone tries to import a document.
KEY_ERROR = 'gdrive_error'
KEY_ERROR_AT = 'gdrive_error_at'
KEY_NOTIFIED_AT = 'gdrive_error_notified_at'

_TIMEOUT = 20


class GoogleDriveError(RuntimeError):
    """Raised when Drive can't be reached or answers with an error."""


class GoogleDriveAuthError(GoogleDriveError):
    """Raised when the problem is the connection itself — no refresh token, or
    one Google has revoked/expired. Callers use this to offer a reconnect link
    rather than a generic failure message; a network blip or a Drive 500 stays
    a plain GoogleDriveError, where reconnecting would fix nothing."""


# ─── Credentials ────────────────────────────────────────────

def _client_id():
    return (get_config(KEY_CLIENT_ID) or os.environ.get('GOOGLE__CLIENT_ID') or '').strip()


def _client_secret():
    return (get_config(KEY_CLIENT_SECRET) or os.environ.get('GOOGLE__CLIENT_SECRET') or '').strip()


def _refresh_token():
    return (get_config(KEY_REFRESH_TOKEN) or os.environ.get('GOOGLE__REFRESH_TOKEN') or '').strip()


def has_client_credentials():
    """True when the OAuth client id + secret are set — enough to start the
    "Connecter" flow (but not necessarily connected yet)."""
    return bool(_client_id() and _client_secret())


def is_connected():
    """True when a refresh token is stored — i.e. the OAuth flow has run."""
    return bool(_refresh_token())


def is_configured():
    """True when import can actually work: client credentials **and** a
    refresh token are all present. Gates the editor's import button."""
    return has_client_credentials() and is_connected()


def save_client_credentials(client_id, client_secret):
    set_config(KEY_CLIENT_ID, (client_id or '').strip())
    set_config(KEY_CLIENT_SECRET, (client_secret or '').strip())


def disconnect():
    """Forget the stored refresh token (client id/secret are kept)."""
    delete_config(KEY_REFRESH_TOKEN)


# ─── OAuth flow ─────────────────────────────────────────────

def authorization_url(redirect_uri, state):
    """Build the Google consent URL to redirect the admin to.

    `access_type=offline` + `prompt=consent` guarantee Google returns a
    refresh token every time, even on a repeat authorisation.
    """
    if not has_client_credentials():
        raise GoogleDriveError("Renseignez d'abord l'identifiant et le secret OAuth.")
    params = {
        'client_id': _client_id(),
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'state': state,
    }
    return f'{AUTH_URL}?{urlencode(params)}'


def exchange_code(code, redirect_uri):
    """Exchange the OAuth `code` from the callback for tokens and store the
    refresh token. Returns nothing; raises `GoogleDriveError` on failure."""
    if not code:
        raise GoogleDriveError("Google n'a renvoyé aucun code d'autorisation.")
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                'code': code,
                'client_id': _client_id(),
                'client_secret': _client_secret(),
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GoogleDriveError(f"Connexion à Google impossible : {exc}") from exc
    if resp.status_code != 200:
        raise GoogleDriveError(_oauth_error(resp))
    refresh_token = resp.json().get('refresh_token')
    if not refresh_token:
        # Should not happen with prompt=consent, but guard anyway.
        raise GoogleDriveError(
            "Google n'a pas renvoyé de refresh token. Réessayez la connexion."
        )
    set_config(KEY_REFRESH_TOKEN, refresh_token.strip())


def connection_error():
    """The stored failure message while the connection is broken, else None.
    Read on every admin page, so it must stay a plain config lookup."""
    return (get_config(KEY_ERROR) or '').strip() or None


def record_success():
    """Clear a previously recorded failure. A no-op when nothing was wrong, so
    the happy path doesn't write to the database on every Drive call."""
    if not connection_error():
        return
    for key in (KEY_ERROR, KEY_ERROR_AT, KEY_NOTIFIED_AT):
        delete_config(key)


def record_auth_failure(message):
    """Remember that the connection is broken and e-mail the admins — once per
    outage, not once per attempt: a failure surfaces on every page that touches
    Drive, and Bernard should not get twenty identical messages."""
    from datetime import datetime

    first_time = not connection_error()
    set_config(KEY_ERROR, str(message)[:500])
    if first_time:
        set_config(KEY_ERROR_AT, datetime.utcnow().isoformat(timespec='seconds'))
        _notify_admins(message)


def _notify_admins(message):
    from datetime import datetime
    from flask import current_app, has_request_context, render_template, url_for

    try:
        from auth.models import User
        from mail import is_configured as mail_is_configured, send_email
        if not mail_is_configured():
            return
        recipients = User.query.filter(User.is_admin == True,  # noqa: E712
                                       User.email.isnot(None)).all()
        if not recipients:
            return

        # The cron health check has no request context; url_for needs one.
        if has_request_context():
            ctx = None
        else:
            base = os.environ.get('SITE_BASE_URL', 'https://bernardpoignant.fr')
            ctx = current_app.test_request_context(base_url=base)
            ctx.push()
        try:
            settings_url = url_for('admin_settings.index', _external=True)
            site_url = url_for('articles.public_list', _external=True)
            for admin in recipients:
                html = render_template(
                    'email/gdrive_alert.html',
                    message=message, settings_url=settings_url, site_url=site_url,
                    site_name=current_app.config['SITE_NAME'],
                    site_tagline=current_app.config['SITE_TAGLINE'],
                )
                send_email(to_email=admin.email, to_name=admin.username,
                           subject="Google Drive déconnecté — reconnexion nécessaire",
                           html=html, categories=['gdrive-alert'])
        finally:
            if ctx is not None:
                ctx.pop()
        set_config(KEY_NOTIFIED_AT, datetime.utcnow().isoformat(timespec='seconds'))
    except Exception as exc:
        current_app.logger.warning(f"gdrive alert failed: {exc}")


def health_check():
    """Verify the stored refresh token still works. Returns (ok, message).

    Records the outcome, so a scheduled run both detects the problem and
    triggers the alert.
    """
    if not has_client_credentials():
        return False, "Identifiants OAuth Google absents."
    if not is_connected():
        return False, "Google Drive n'est pas connecté."
    try:
        _access_token()
    except GoogleDriveAuthError as exc:
        return False, str(exc)
    except GoogleDriveError as exc:
        # A network blip is not an expired token — don't cry wolf.
        return True, f"Vérification impossible ({exc})"
    return True, "Connexion valide."


def _access_token():
    client_id, client_secret, refresh_token = _client_id(), _client_secret(), _refresh_token()
    if not (client_id and client_secret and refresh_token):
        raise GoogleDriveAuthError("Google Drive n'est pas connecté.")
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                'client_id': client_id,
                'client_secret': client_secret,
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token',
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GoogleDriveError(f"Connexion à Google impossible : {exc}") from exc
    if resp.status_code != 200:
        # Only a genuinely dead credential earns the banner and the alert.
        # Google answers this endpoint with 429 when rate-limited and 5xx when
        # it is having a bad day, and treating those as a revoked token raised
        # a permanent "reconnect Google Drive" over a passing hiccup — the
        # banner then stayed up until the next successful Drive call, which
        # only happens when someone opens the import dialog.
        message = _oauth_error(resp)
        if _is_dead_credential(resp):
            message += " Reconnectez Google Drive."
            record_auth_failure(message)
            raise GoogleDriveAuthError(message)
        raise GoogleDriveError(message)
    token = resp.json().get('access_token')
    if not token:
        raise GoogleDriveError("Réponse Google sans jeton d'accès.")
    record_success()
    return token


# Les codes OAuth qui veulent dire « ce jeton ne reviendra pas » : jeton révoqué
# ou expiré, client supprimé, autorisation retirée. Tout le reste est passager.
DEAD_CREDENTIAL_ERRORS = {
    'invalid_grant', 'invalid_client', 'unauthorized_client', 'invalid_request',
}


def _is_dead_credential(resp):
    """True when Google says the credential itself is finished, rather than
    that it is busy or broken for the moment."""
    if resp.status_code >= 500 or resp.status_code == 429:
        return False
    try:
        code = (resp.json().get('error') or '').strip().lower()
    except ValueError:
        # Pas de JSON : sur un 4xx c'est assez inhabituel pour mériter l'alerte,
        # sur le reste on ne conclut rien.
        return 400 <= resp.status_code < 500
    return code in DEAD_CREDENTIAL_ERRORS


def _oauth_error(resp):
    try:
        body = resp.json()
        detail = body.get('error_description') or body.get('error') or ''
    except ValueError:
        detail = resp.text[:200]
    return f"Authentification Google refusée : {detail}".strip()


def _auth_headers():
    return {'Authorization': f'Bearer {_access_token()}'}


# ─── Drive reads ────────────────────────────────────────────

def list_documents(query=None, limit=50):
    """Return Bernard's Google Docs, most-recently-modified first.

    Each item is ``{'id', 'name', 'modified'}``. ``query`` (optional) filters
    by name, case-insensitively, on Drive's side.
    """
    q = f"mimeType='{DOC_MIME}' and trashed=false"
    if query and query.strip():
        # Escape backslashes then single quotes for the Drive query language.
        safe = query.strip().replace('\\', '\\\\').replace("'", "\\'")
        q += f" and name contains '{safe}'"
    params = {
        'q': q,
        'orderBy': 'modifiedTime desc',
        'pageSize': max(1, min(int(limit or 50), 100)),
        'fields': 'files(id,name,modifiedTime)',
        'spaces': 'drive',
    }
    try:
        resp = requests.get(FILES_URL, headers=_auth_headers(), params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise GoogleDriveError(f"Connexion à Google Drive impossible : {exc}") from exc
    if resp.status_code != 200:
        raise GoogleDriveError(_error_detail(resp, "la liste des documents"))
    files = resp.json().get('files', [])
    return [
        {'id': f['id'], 'name': f.get('name', 'Sans titre'), 'modified': f.get('modifiedTime', '')}
        for f in files
    ]


def get_document(file_id):
    """Fetch one Google Doc, exported as HTML.

    Returns ``{'id', 'name', 'html'}`` where ``html`` is the *inner* body of
    Google's HTML export (head/style stripped) — ready to be run through the
    article HTML cleaner and dropped into the editor.
    """
    if not file_id:
        raise GoogleDriveError("Aucun document sélectionné.")
    headers = _auth_headers()

    # Title first (the export doesn't carry a reliable title).
    try:
        meta = requests.get(
            f'{FILES_URL}/{file_id}',
            headers=headers,
            params={'fields': 'id,name,mimeType'},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GoogleDriveError(f"Connexion à Google Drive impossible : {exc}") from exc
    if meta.status_code != 200:
        raise GoogleDriveError(_error_detail(meta, "le document"))
    info = meta.json()
    if info.get('mimeType') != DOC_MIME:
        raise GoogleDriveError("Ce fichier n'est pas un Google Doc.")
    name = info.get('name', 'Sans titre')

    try:
        export = requests.get(
            f'{FILES_URL}/{file_id}/export',
            headers=headers,
            params={'mimeType': 'text/html'},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GoogleDriveError(f"Connexion à Google Drive impossible : {exc}") from exc
    if export.status_code != 200:
        raise GoogleDriveError(_error_detail(export, "le contenu du document"))

    return {'id': file_id, 'name': name, 'html': _body_inner_html(export.text)}


def _body_inner_html(html):
    """Pull the inner HTML of <body> out of Google's full-document export,
    dropping <head>, <style>, <script> etc. so their CSS text can't leak in
    when the downstream bleach pass strips those tags."""
    soup = BeautifulSoup(html or '', 'html.parser')
    for tag in soup(['style', 'script', 'head', 'meta', 'title', 'link']):
        tag.decompose()
    body = soup.body
    if body is not None:
        return body.decode_contents()
    return str(soup)


# ─── Import boilerplate stripping ───────────────────────────
#
# Bernard's docs usually open with the article title on the first line and end
# with two lines: "Bernard Poignant" and a date. On import we lift the title
# out and drop those trailing lines so they don't end up inside the body.

# Author lines to strip (normalised, lower-case). "par bernard poignant" and a
# bare "bernard poignant" both match.
_AUTHOR_LINES = {'bernard poignant'}

_MONTHS_FR = (
    r'(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|'
    r'septembre|octobre|novembre|décembre|decembre)'
)
_DATE_PATTERNS = [
    re.compile(r'^(le\s+)?\d{1,2}(er)?\s+' + _MONTHS_FR + r'\s+\d{4}$', re.I),  # 9 août 2026
    re.compile(r'^' + _MONTHS_FR + r'\s+\d{4}$', re.I),                          # août 2026
    re.compile(r'^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$'),                          # 09/08/2026
    re.compile(r'^\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}$'),                            # 2026-08-09
]


def _norm(text):
    return re.sub(r'\s+', ' ', (text or '')).strip().rstrip('.').strip()


def _is_author_line(text):
    t = _norm(text).lower().strip('.,')
    if t.startswith('par '):
        t = t[4:].strip()
    return t in _AUTHOR_LINES


def _is_date_line(text):
    return any(p.match(_norm(text)) for p in _DATE_PATTERNS)


def _looks_like_title(el, text, doc_name):
    if el.name in ('h1', 'h2', 'h3'):
        return True
    if doc_name and _norm(text).lower() == _norm(doc_name).lower():
        return True
    # A short opening line with no terminal sentence punctuation reads as a
    # title rather than a sentence of the article.
    if len(text) <= 100 and not re.search(r'[.!?:]$', text.strip()):
        return True
    return False


def strip_boilerplate(html, doc_name):
    """Given cleaned article HTML and the Google Doc's name, lift out the
    title line and remove trailing author/date lines.

    Returns ``(title, body_html)``: ``title`` is the in-document first line
    when it looks like a title, otherwise the doc name; ``body_html`` is the
    HTML with the detected title and any trailing author/date lines removed.
    """
    soup = BeautifulSoup(html or '', 'html.parser')
    blocks = [b for b in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
              if b.get_text(strip=True)]
    if not blocks:
        return (doc_name or '', str(soup))

    detected_title = None
    first = blocks[0]
    first_text = first.get_text(' ', strip=True)
    if _looks_like_title(first, first_text, doc_name):
        detected_title = first_text
        first.decompose()
        blocks = blocks[1:]

    # Trailing author/date lines, in either order — up to 3 blocks from the end.
    removed = 0
    while blocks and removed < 3:
        last = blocks[-1]
        text = last.get_text(' ', strip=True)
        if _is_author_line(text) or _is_date_line(text):
            last.decompose()
            blocks = blocks[:-1]
            removed += 1
        else:
            break

    return (detected_title or doc_name or '', str(soup))


def _error_detail(resp, what):
    try:
        msg = resp.json().get('error', {}).get('message', '')
    except (ValueError, AttributeError):
        msg = ''
    if resp.status_code in (401, 403):
        return f"Accès refusé à {what} (reconnectez Google Drive ou vérifiez les autorisations)."
    if resp.status_code == 404:
        return f"Impossible de trouver {what}."
    return f"Erreur Google Drive ({resp.status_code}) sur {what}. {msg}".strip()
