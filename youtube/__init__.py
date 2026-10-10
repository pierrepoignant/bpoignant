"""Publier les clips sur YouTube (en Shorts), par l'API Data v3.

Le compte Google est le même que pour Drive et Gmail — même projet, même
client OAuth — mais l'autorisation d'« téléverser sur YouTube » est un scope à
part, donc un jeton à part : connecter YouTube ne touche pas Drive, et le
révoquer ne prive pas le site de ses imports.

Une vidéo verticale et courte devient automatiquement un Short côté YouTube ;
on n'a rien de spécial à faire, sinon envoyer le fichier avec un titre et une
description. Un envoi coûte 1 600 unités de quota sur les 10 000 journalières :
six par jour, bien plus que le rythme de Bernard.

Le jeton de rafraîchissement expire au bout de sept jours tant que l'écran de
consentement du projet Google reste en mode « test » ; il faut publier
l'application pour qu'il tienne (comme pour Gmail).
"""

import logging
import os
from datetime import datetime, timedelta

import requests

from settings.models import get_config, set_config, delete_config

log = logging.getLogger(__name__)

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
UPLOAD_URL = 'https://www.googleapis.com/upload/youtube/v3/videos'
CHANNELS_URL = 'https://www.googleapis.com/youtube/v3/channels'

# Téléverser, et rien d'autre : ni lire, ni modifier, ni supprimer la chaîne.
SCOPE = 'https://www.googleapis.com/auth/youtube.upload'
# Catégorie « News & Politics » — la plus juste pour des chroniques d'actualité.
CATEGORY_NEWS = '25'

KEY_REFRESH = 'youtube_refresh_token'
KEY_CHANNEL = 'youtube_channel_title'
KEY_PRIVACY = 'youtube_privacy'          # public / unlisted / private
KEY_AUTH_ERROR = 'youtube_auth_error'

_TIMEOUT = 30


class YouTubeError(RuntimeError):
    pass


class YouTubeAuthError(YouTubeError):
    pass


def _client():
    """Le client OAuth est partagé avec Drive — même projet Google."""
    import gdrive
    return gdrive._client_id(), gdrive._client_secret()


def has_client_credentials():
    cid, secret = _client()
    return bool(cid and secret)


def refresh_token():
    return (get_config(KEY_REFRESH) or '').strip()


def is_connected():
    return bool(refresh_token())


def channel_title():
    return (get_config(KEY_CHANNEL) or '').strip()


def privacy():
    return (get_config(KEY_PRIVACY) or 'public').strip()


def set_privacy(value):
    set_config(KEY_PRIVACY, value if value in ('public', 'unlisted', 'private') else 'public')


def auth_error():
    return (get_config(KEY_AUTH_ERROR) or '').strip()


def _note_auth_error(message):
    try:
        if (get_config(KEY_AUTH_ERROR) or '') != message:
            set_config(KEY_AUTH_ERROR, message)
    except Exception:
        log.exception("YouTube : impossible de noter la panne d'autorisation")


def _clear_auth_error():
    try:
        if get_config(KEY_AUTH_ERROR):
            delete_config(KEY_AUTH_ERROR)
    except Exception:
        log.exception("YouTube : impossible d'effacer la panne d'autorisation")


def disconnect():
    for cle in (KEY_REFRESH, KEY_CHANNEL):
        delete_config(cle)
    _clear_auth_error()


# ─── OAuth ──────────────────────────────────────────────────

def authorization_url(redirect_uri, state):
    from urllib.parse import urlencode
    cid, _ = _client()
    if not cid:
        raise YouTubeError("Renseignez d'abord l'identifiant et le secret OAuth Google (voir Réglages).")
    params = {
        'client_id': cid,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
        'include_granted_scopes': 'true',
    }
    return f'{AUTH_URL}?{urlencode(params)}'


def exchange_code(code, redirect_uri):
    cid, secret = _client()
    if not code:
        raise YouTubeError("Google n'a renvoyé aucun code d'autorisation.")
    resp = requests.post(TOKEN_URL, data={
        'client_id': cid, 'client_secret': secret, 'code': code,
        'grant_type': 'authorization_code', 'redirect_uri': redirect_uri,
    }, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise YouTubeError(f"Google a refusé l'autorisation : {resp.text[:200]}")
    jeton = (resp.json() or {}).get('refresh_token')
    if not jeton:
        raise YouTubeError("Google n'a pas renvoyé de refresh token. Réessayez la connexion.")
    set_config(KEY_REFRESH, jeton.strip())
    _clear_auth_error()
    try:
        set_config(KEY_CHANNEL, _channel_title(_access_token()))
    except YouTubeError:
        pass


def _access_token():
    cid, secret = _client()
    jeton = refresh_token()
    if not (cid and secret and jeton):
        raise YouTubeAuthError("YouTube n'est pas connecté.")
    resp = requests.post(TOKEN_URL, data={
        'client_id': cid, 'client_secret': secret,
        'refresh_token': jeton, 'grant_type': 'refresh_token',
    }, timeout=_TIMEOUT)
    if resp.status_code != 200:
        brut = resp.text[:200]
        message = ("L'autorisation YouTube a expiré ou a été révoquée par Google."
                   if 'invalid_grant' in brut else
                   f"Autorisation YouTube refusée : {brut[:160]}")
        _note_auth_error(message)
        raise YouTubeAuthError(message + " Reconnectez la chaîne.")
    tok = (resp.json() or {}).get('access_token')
    if not tok:
        raise YouTubeError("Réponse Google sans jeton d'accès.")
    _clear_auth_error()
    return tok


def _channel_title(token):
    r = requests.get(CHANNELS_URL, params={'part': 'snippet', 'mine': 'true'},
                     headers={'Authorization': f'Bearer {token}'}, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise YouTubeError(f"Chaîne YouTube illisible : {r.text[:160]}")
    items = (r.json() or {}).get('items') or []
    if not items:
        raise YouTubeError("Ce compte Google n'a pas de chaîne YouTube.")
    return ((items[0].get('snippet') or {}).get('title') or '')[:200]


def verify_credentials():
    """Non-destructive check for the settings page."""
    try:
        titre = _channel_title(_access_token())
        return True, f"Connecté à la chaîne « {titre or 'sans nom'} »."
    except YouTubeError as exc:
        return False, str(exc)


def check_auth(force=False):
    """Why the stored authorisation is refused, or '' — cached, like Gmail's."""
    import time
    if not is_connected():
        return ''
    deja = auth_error()
    if deja:
        return deja
    if not force and time.time() - _CHECK[0] < 600:
        return ''
    try:
        _access_token()
    except YouTubeAuthError as exc:
        return auth_error() or str(exc)
    except Exception:
        return ''
    _CHECK[0] = time.time()
    return ''


_CHECK = [0.0]


# ─── Publication ────────────────────────────────────────────

def _description(titre, caption):
    """Le texte sous la vidéo. On ajoute #Shorts, qui aide YouTube à la ranger
    comme un Short, et une signature courte."""
    corps = (caption or '').strip()
    pieds = "\n\nUne chronique de Bernard Poignant · bernardpoignant.fr\n#Shorts"
    # YouTube coupe à 5 000 caractères ; on garde de la marge pour le pied.
    return (corps[:4800] + pieds).strip()


def upload(path, title, caption=None, privacy_status=None, made_for_kids=False):
    """Envoyer un fichier sur YouTube. Renvoie (ok, video_id_ou_erreur).

    Téléversement « resumable » : un POST ouvre la session et renvoie l'adresse
    d'envoi dans l'en-tête Location, puis un PUT pousse les octets. En un seul
    morceau — nos clips font quelques dizaines de mégaoctets, bien en deçà de
    ce qu'une requête encaisse — ce qui évite de gérer la reprise par plages.
    """
    if not path or not os.path.exists(path):
        return False, "Le fichier du montage est introuvable."
    taille = os.path.getsize(path)
    if not taille:
        return False, "Le fichier du montage est vide."
    try:
        token = _access_token()
    except YouTubeError as exc:
        return False, str(exc)

    corps = {
        'snippet': {
            'title': (title or 'Chronique')[:100],
            'description': _description(title, caption),
            'categoryId': CATEGORY_NEWS,
        },
        'status': {
            'privacyStatus': privacy_status or privacy(),
            'selfDeclaredMadeForKids': bool(made_for_kids),
        },
    }
    try:
        ouverture = requests.post(
            UPLOAD_URL,
            params={'uploadType': 'resumable', 'part': 'snippet,status'},
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json; charset=UTF-8',
                     'X-Upload-Content-Type': 'video/mp4',
                     'X-Upload-Content-Length': str(taille)},
            json=corps, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        return False, f"Connexion à YouTube interrompue : {exc}"
    if ouverture.status_code not in (200, 201):
        return False, _explain(ouverture, "ouverture de l'envoi refusée")
    lien = ouverture.headers.get('Location')
    if not lien:
        return False, "YouTube n'a pas renvoyé d'adresse d'envoi."

    try:
        with open(path, 'rb') as fh:
            envoi = requests.put(
                lien, data=fh, timeout=600,
                headers={'Content-Type': 'video/mp4', 'Content-Length': str(taille)})
    except requests.RequestException as exc:
        return False, f"Envoi à YouTube interrompu : {exc}"
    if envoi.status_code not in (200, 201):
        return False, _explain(envoi, "envoi refusé")
    vid = (envoi.json() or {}).get('id')
    if not vid:
        return False, "YouTube n'a pas renvoyé d'identifiant de vidéo."
    return True, vid


def _explain(resp, defaut):
    try:
        err = (resp.json() or {}).get('error') or {}
    except ValueError:
        return f"{defaut} ({resp.status_code})"
    message = (err.get('message') or '').strip()
    # Le quota journalier épuisé : un cas fréquent et réparable (il revient le
    # lendemain), qui mérite d'être nommé plutôt que noyé.
    if resp.status_code == 403 and 'quota' in message.lower():
        return "Quota YouTube du jour épuisé — réessayez demain, ou demandez un quota plus large à Google."
    return f"{defaut} : {message or resp.status_code}"


def video_url(video_id):
    return f'https://www.youtube.com/watch?v={video_id}' if video_id else None
