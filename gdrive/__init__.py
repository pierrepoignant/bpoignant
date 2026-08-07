"""Read-only Google Drive access for importing article content.

Lets the admin pull the body of one of Bernard's Google Docs straight into the
article editor, instead of copy-pasting. We only ever *read* from Drive.

Auth is a one-time OAuth "installed app" consent that yields a long-lived
refresh token; the token is then exchanged for a short-lived access token on
each request. Credentials follow the app's `<SECTION>__<KEY>` env convention
(same as `ANTHROPIC__API_KEY` / `SEARCHAPI__API_KEY`):

    GOOGLE__CLIENT_ID       OAuth client id     (…apps.googleusercontent.com)
    GOOGLE__CLIENT_SECRET   OAuth client secret
    GOOGLE__REFRESH_TOKEN   refresh token for Bernard's account (drive.readonly)

When any of the three is missing the feature is simply "not configured": the
import button is hidden and the endpoints report it cleanly. No heavy Google
SDK — the Drive REST API is a couple of plain HTTPS calls via `requests`.
"""

import os

import requests
from bs4 import BeautifulSoup


TOKEN_URL = 'https://oauth2.googleapis.com/token'
FILES_URL = 'https://www.googleapis.com/drive/v3/files'
DOC_MIME = 'application/vnd.google-apps.document'

_TIMEOUT = 20


class GoogleDriveError(RuntimeError):
    """Raised when Drive can't be reached or answers with an error."""


def _creds():
    return (
        (os.environ.get('GOOGLE__CLIENT_ID') or '').strip(),
        (os.environ.get('GOOGLE__CLIENT_SECRET') or '').strip(),
        (os.environ.get('GOOGLE__REFRESH_TOKEN') or '').strip(),
    )


def is_configured():
    """True only when all three OAuth credentials are present."""
    return all(_creds())


def _access_token():
    client_id, client_secret, refresh_token = _creds()
    if not (client_id and client_secret and refresh_token):
        raise GoogleDriveError("Google Drive n'est pas configuré.")
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
        # Google returns e.g. {"error": "invalid_grant"} when the refresh token
        # has been revoked or expired — surface that so it can be renewed.
        detail = ''
        try:
            detail = resp.json().get('error_description') or resp.json().get('error') or ''
        except ValueError:
            detail = resp.text[:200]
        raise GoogleDriveError(f"Authentification Google refusée : {detail}".strip())
    token = resp.json().get('access_token')
    if not token:
        raise GoogleDriveError("Réponse Google sans jeton d'accès.")
    return token


def _auth_headers():
    return {'Authorization': f'Bearer {_access_token()}'}


def list_documents(query=None, limit=50):
    """Return Bernard's Google Docs, most-recently-modified first.

    Each item is ``{'id', 'name', 'modified'}``. ``query`` (optional) filters
    by name, case-insensitively, on Drive's side.
    """
    q = f"mimeType='{DOC_MIME}' and trashed=false"
    if query and query.strip():
        # Escape single quotes for the Drive query language.
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


def _error_detail(resp, what):
    try:
        msg = resp.json().get('error', {}).get('message', '')
    except (ValueError, AttributeError):
        msg = ''
    if resp.status_code in (401, 403):
        return f"Accès refusé à {what} (vérifiez les autorisations Google Drive)."
    if resp.status_code == 404:
        return f"Impossible de trouver {what}."
    return f"Erreur Google Drive ({resp.status_code}) sur {what}. {msg}".strip()
