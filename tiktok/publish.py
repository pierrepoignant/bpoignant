"""Publier sur TikTok par l'API, plutôt que depuis le téléphone.

Deux façons de poster, et le choix ne nous appartient qu'à moitié :

* **Publication directe** — le serveur publie, le clip paraît. Elle demande le
  scope `video.publish` *et* que l'application ait passé l'audit de TikTok :
  tant qu'elle ne l'a pas, tout ce qu'elle publie reste en visibilité privée,
  ce qui ne sert à rien. C'est `privacy_level_options` qui le dit, pas nous :
  une application non auditée ne se voit proposer que `SELF_ONLY`.
* **Dépôt en brouillon** — le serveur téléverse, TikTok prévient Bernard dans
  son application, il ouvre et publie. Pas d'audit à passer puisque c'est un
  humain qui appuie. Cinq dépôts en attente par tranche de vingt-quatre heures.

Le module choisit tout seul selon ce que l'autorisation a réellement accordé,
et un réglage permet de forcer le brouillon.

L'intérêt principal n'est pas d'économiser un transfert de fichier : c'est que
TikTok rend l'identifiant du post. L'enchaînement vers X et LinkedIn n'a plus à
attendre dix minutes puis à deviner que le post le plus récent est celui qu'on
vient de monter.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

from settings.models import get_config, set_config, delete_config

log = logging.getLogger(__name__)

AUTH_URL = 'https://www.tiktok.com/v2/auth/authorize/'
TOKEN_URL = 'https://open.tiktokapis.com/v2/oauth/token/'
API = 'https://open.tiktokapis.com/v2'

# `video.publish` publie, `video.upload` dépose en brouillon, `user.info.basic`
# donne le nom du compte connecté. On demande les trois : TikTok n'accorde que
# ce que l'application a fait approuver, et la réponse dit ce qui a été donné.
SCOPES = 'user.info.basic,video.upload,video.publish'

KEY_CLIENT_KEY = 'tiktok_client_key'
KEY_CLIENT_SECRET = 'tiktok_client_secret'
KEY_TOKEN = 'tiktok_access_token'
KEY_TOKEN_EXPIRES = 'tiktok_token_expires_at'
KEY_REFRESH = 'tiktok_refresh_token'
KEY_REFRESH_EXPIRES = 'tiktok_refresh_expires_at'
KEY_SCOPES = 'tiktok_granted_scopes'
KEY_OPEN_ID = 'tiktok_open_id'
KEY_USERNAME = 'tiktok_username'
# Le pseudo (@…), qui n'est pas le nom affiché : c'est lui qui compose l'URL
# d'un post, et les deux diffèrent presque toujours.
KEY_HANDLE = 'tiktok_handle'
KEY_FORCE_INBOX = 'tiktok_force_inbox'

_TIMEOUT = 30
# Bornes de découpe imposées par TikTok : cinq mégaoctets au moins par morceau,
# soixante-quatre au plus, mille morceaux au maximum, et le dernier morceau
# peut dépasser la taille annoncée (jusqu'à cent vingt-huit).
CHUNK_MIN = 5 * 1024 * 1024
CHUNK_MAX = 64 * 1024 * 1024
CHUNK_CIBLE = 10 * 1024 * 1024
CHUNKS_MAX = 1000

PRIVACY_LABELS = {
    'PUBLIC_TO_EVERYONE': 'Public',
    'MUTUAL_FOLLOW_FRIENDS': 'Amis (abonnement mutuel)',
    'FOLLOWER_OF_CREATOR': 'Abonnés',
    'SELF_ONLY': 'Privé (visible de Bernard seul)',
}


class TikTokPublishError(RuntimeError):
    pass


class TikTokAuthError(TikTokPublishError):
    pass


# ─── Réglages ───────────────────────────────────────────────

def _client_key():
    return (get_config(KEY_CLIENT_KEY) or '').strip()


def _client_secret():
    return (get_config(KEY_CLIENT_SECRET) or '').strip()


def has_client_credentials():
    return bool(_client_key() and _client_secret())


def save_settings(client_key, client_secret=None):
    set_config(KEY_CLIENT_KEY, (client_key or '').strip())
    # Un secret vide veut dire « garde celui d'avant » : le champ est un mot de
    # passe, il revient vide à chaque affichage.
    if (client_secret or '').strip():
        set_config(KEY_CLIENT_SECRET, client_secret.strip())


def username():
    return (get_config(KEY_USERNAME) or '').strip()


def granted_scopes():
    return [s for s in (get_config(KEY_SCOPES) or '').split(',') if s]


def force_inbox():
    return str(get_config(KEY_FORCE_INBOX) or '').strip() in ('1', 'true', 'True', 'on')


def set_force_inbox(actif):
    set_config(KEY_FORCE_INBOX, '1' if actif else '0')


def can_direct_post():
    return 'video.publish' in granted_scopes() and not force_inbox()


def mode():
    """'direct' ou 'brouillon' — ce que fera le prochain envoi."""
    return 'direct' if can_direct_post() else 'brouillon'


def is_connected():
    return bool((get_config(KEY_REFRESH) or '').strip())


def refresh_expires_at():
    brut = (get_config(KEY_REFRESH_EXPIRES) or '').strip()
    try:
        return datetime.fromisoformat(brut) if brut else None
    except ValueError:
        return None


def days_left():
    fin = refresh_expires_at()
    return None if fin is None else max(0, (fin - datetime.utcnow()).days)


def handle():
    return (get_config(KEY_HANDLE) or '').strip()


def disconnect():
    for cle in (KEY_TOKEN, KEY_TOKEN_EXPIRES, KEY_REFRESH, KEY_REFRESH_EXPIRES,
                KEY_SCOPES, KEY_OPEN_ID, KEY_USERNAME, KEY_HANDLE):
        delete_config(cle)


# ─── OAuth ──────────────────────────────────────────────────

def authorization_url(redirect_uri, state):
    if not has_client_credentials():
        raise TikTokPublishError("Renseignez d'abord la clé et le secret TikTok.")
    return AUTH_URL + '?' + urlencode({
        'client_key': _client_key(),
        'response_type': 'code',
        'scope': SCOPES,
        'redirect_uri': redirect_uri,
        'state': state,
    })


def _enregistrer_jetons(corps):
    """Store what the token endpoint returned, expiries included.

    The refresh token lasts a year and the access token a day, so both dates
    are kept: one decides when to refresh silently, the other when to warn
    that the connection has to be made again by hand.
    """
    jeton = (corps.get('access_token') or '').strip()
    rafraichir = (corps.get('refresh_token') or '').strip()
    if not jeton or not rafraichir:
        raise TikTokPublishError("Réponse TikTok sans jeton utilisable.")
    maintenant = datetime.utcnow()
    set_config(KEY_TOKEN, jeton)
    set_config(KEY_TOKEN_EXPIRES,
               (maintenant + timedelta(seconds=int(corps.get('expires_in') or 86400))
                ).isoformat(timespec='seconds'))
    set_config(KEY_REFRESH, rafraichir)
    set_config(KEY_REFRESH_EXPIRES,
               (maintenant + timedelta(seconds=int(corps.get('refresh_expires_in') or 365 * 86400))
                ).isoformat(timespec='seconds'))
    if corps.get('scope'):
        set_config(KEY_SCOPES, corps['scope'])
    if corps.get('open_id'):
        set_config(KEY_OPEN_ID, corps['open_id'])


def exchange_code(code, redirect_uri):
    if not code:
        raise TikTokPublishError("TikTok n'a renvoyé aucun code d'autorisation.")
    r = requests.post(TOKEN_URL, data={
        'client_key': _client_key(), 'client_secret': _client_secret(),
        'code': code, 'grant_type': 'authorization_code',
        'redirect_uri': redirect_uri,
    }, headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=_TIMEOUT)
    corps = _corps(r, "TikTok a refusé l'autorisation")
    _enregistrer_jetons(corps)
    # Le nom du compte, pour que l'écran dise à quoi il est relié, et le pseudo,
    # qui servira à composer l'adresse des posts.
    try:
        set_config(KEY_USERNAME, _user_info().get('display_name') or '')
    except TikTokPublishError:
        pass
    try:
        creator_info()
    except TikTokPublishError:
        pass


def _corps(reponse, defaut):
    """TikTok's error shape is in the body, not the status code.

    A refused post answers 200 with `error.code` set, so reading the status
    alone would report success on a failure.
    """
    try:
        corps = reponse.json() or {}
    except ValueError:
        raise TikTokPublishError(f"{defaut} ({reponse.status_code}) : {reponse.text[:160]}")
    err = corps.get('error') or {}
    # Deux formes d'erreur selon le service : les points d'accès OAuth mettent
    # une chaîne dans `error`, ceux de l'API un objet avec un `code`.
    if isinstance(err, str):
        if err and err != 'ok':
            message = (corps.get('error_description') or err).strip()
            if 'grant' in err or 'client' in err:
                raise TikTokAuthError(f"{defaut} : {message}")
            raise TikTokPublishError(f"{defaut} : {message}")
        err = {}
    code = err.get('code') if isinstance(err, dict) else None
    # `ok` est la valeur que TikTok renvoie quand tout va bien, dans le même
    # champ que les erreurs.
    if code and code != 'ok':
        message = (err.get('message') or '').strip() or code
        if code in ('access_token_invalid', 'scope_not_authorized',
                    'scope_permission_missed', 'invalid_grant'):
            raise TikTokAuthError(f"{defaut} : {message}")
        raise TikTokPublishError(f"{defaut} : {message}")
    if reponse.status_code >= 400:
        raise TikTokPublishError(f"{defaut} ({reponse.status_code}) : {reponse.text[:160]}")
    return corps.get('data') if 'data' in corps else corps


def _access_token():
    """A valid access token, refreshed when it has run out.

    The access token lives a day, the refresh token a year. Refreshing is
    silent; only the yearly expiry needs a human.
    """
    jeton = (get_config(KEY_TOKEN) or '').strip()
    brut = (get_config(KEY_TOKEN_EXPIRES) or '').strip()
    try:
        fin = datetime.fromisoformat(brut) if brut else None
    except ValueError:
        fin = None
    # Une minute de marge : un jeton qui expire pendant le téléversement ferait
    # échouer un envoi déjà à moitié fait.
    if jeton and fin and fin > datetime.utcnow() + timedelta(minutes=1):
        return jeton

    rafraichir = (get_config(KEY_REFRESH) or '').strip()
    if not rafraichir:
        raise TikTokAuthError("TikTok n'est pas connecté.")
    if not has_client_credentials():
        raise TikTokAuthError("Clé et secret TikTok absents.")
    r = requests.post(TOKEN_URL, data={
        'client_key': _client_key(), 'client_secret': _client_secret(),
        'grant_type': 'refresh_token', 'refresh_token': rafraichir,
    }, headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=_TIMEOUT)
    try:
        corps = _corps(r, "Autorisation TikTok refusée")
    except TikTokPublishError as exc:
        raise TikTokAuthError(f"{exc} Reconnectez le compte.") from exc
    _enregistrer_jetons(corps)
    return (get_config(KEY_TOKEN) or '').strip()


def _user_info():
    r = requests.get(f'{API}/user/info/',
                     params={'fields': 'open_id,display_name,avatar_url'},
                     headers={'Authorization': f'Bearer {_access_token()}'},
                     timeout=_TIMEOUT)
    return _corps(r, "Profil TikTok illisible").get('user') or {}


def verify_credentials():
    """Non-destructive check, for the settings page."""
    try:
        infos = _user_info()
        reste = days_left()
        suite = f" — autorisation valable encore {reste} jour(s)." if reste is not None else ''
        etat = ("publication directe" if can_direct_post()
                else "dépôt en brouillon (Bernard valide dans l'application)")
        return True, (f"Connecté à {infos.get('display_name') or 'ce compte'} · "
                      f"{etat}{suite}")
    except TikTokPublishError as exc:
        return False, str(exc)


# ─── Ce que le compte autorise ──────────────────────────────

def creator_info():
    """What TikTok says this account may do right now.

    Asked before every send, and not cached: TikTok requires the posting
    screen to show the account's *current* privacy options, and they change —
    a private account has no public option at all. It is also how we learn
    whether the app has passed its audit, since an unaudited one is only ever
    offered `SELF_ONLY`.
    """
    r = requests.post(f'{API}/post/publish/creator_info/query/',
                      headers={'Authorization': f'Bearer {_access_token()}',
                               'Content-Type': 'application/json; charset=UTF-8'},
                      timeout=_TIMEOUT)
    infos = _corps(r, "Informations du compte TikTok illisibles") or {}
    options = infos.get('privacy_level_options') or []
    if infos.get('creator_username'):
        set_config(KEY_HANDLE, infos['creator_username'])
    return {
        'username': infos.get('creator_username') or '',
        'nickname': infos.get('creator_nickname') or '',
        'privacy_options': options,
        'comment_disabled': bool(infos.get('comment_disabled')),
        'duet_disabled': bool(infos.get('duet_disabled')),
        'stitch_disabled': bool(infos.get('stitch_disabled')),
        'max_duration': infos.get('max_video_post_duration_sec'),
        # Sans option publique, publier directement ne produirait qu'un post
        # que personne ne verra : l'application n'a pas passé l'audit.
        'audite': 'PUBLIC_TO_EVERYONE' in options,
    }


# ─── Envoi ──────────────────────────────────────────────────

def _decoupe(taille):
    """(taille d'un morceau, nombre de morceaux) selon les règles de TikTok.

    TikTok divise la taille par celle d'un morceau : le dernier emporte le
    reste, et peut donc dépasser la taille annoncée. Un fichier plus petit que
    le minimum part d'un seul tenant.
    """
    if taille <= CHUNK_MIN:
        return taille, 1
    morceau = min(CHUNK_CIBLE, CHUNK_MAX)
    nombre = max(1, taille // morceau)
    if nombre > CHUNKS_MAX:
        nombre = CHUNKS_MAX
        morceau = min(CHUNK_MAX, max(CHUNK_MIN, taille // nombre))
        nombre = max(1, taille // morceau)
    return morceau, nombre


def _televerser(url, chemin, morceau, nombre, taille):
    """Send the bytes, chunk by chunk, in order.

    206 means « keep going », 201 means « that was the last one ». A 5xx is
    worth retrying: a few megabytes already sent should not be thrown away
    over one bad moment on the wire.
    """
    with open(chemin, 'rb') as fh:
        for i in range(nombre):
            debut = i * morceau
            fin = taille - 1 if i == nombre - 1 else debut + morceau - 1
            fh.seek(debut)
            donnees = fh.read(fin - debut + 1)
            for essai in range(3):
                r = requests.put(url, data=donnees, timeout=300, headers={
                    'Content-Type': 'video/mp4',
                    'Content-Length': str(len(donnees)),
                    'Content-Range': f'bytes {debut}-{fin}/{taille}',
                })
                if r.status_code in (200, 201, 206):
                    break
                if r.status_code < 500 or essai == 2:
                    raise TikTokPublishError(
                        f"Téléversement refusé ({r.status_code}) : {r.text[:160]}")
                time.sleep(2 * (essai + 1))


def post_video(path, title=None, privacy_level=None, disable_comment=False,
               disable_duet=False, disable_stitch=False, cover_ms=1000):
    """Send one clip to TikTok. Returns (ok, publish_id) or (False, message).

    Direct when the authorisation allows it and the app has been audited,
    otherwise into the creator's drafts — deciding here rather than asking the
    caller, because the answer depends on TikTok's mood, not on the caller's.
    """
    if not path or not os.path.exists(path):
        return False, "Le fichier du montage est introuvable."
    taille = os.path.getsize(path)
    if not taille:
        return False, "Le fichier du montage est vide."

    try:
        jeton = _access_token()
        direct = can_direct_post()
        if direct:
            infos = creator_info()
            if not infos['audite']:
                return False, (
                    "L'application n'a pas passé l'audit TikTok : une publication "
                    "directe resterait privée. Utilisez le dépôt en brouillon.")
            niveau = privacy_level or 'PUBLIC_TO_EVERYONE'
            if niveau not in infos['privacy_options']:
                return False, (f"Le compte n'accepte pas la visibilité « {niveau} » "
                               f"— proposées : {', '.join(infos['privacy_options'])}.")

        morceau, nombre = _decoupe(taille)
        source = {'source': 'FILE_UPLOAD', 'video_size': taille,
                  'chunk_size': morceau, 'total_chunk_count': nombre}
        if direct:
            url = f'{API}/post/publish/video/init/'
            corps = {
                'post_info': {
                    'title': (title or '')[:2200],
                    'privacy_level': niveau,
                    'disable_comment': bool(disable_comment),
                    'disable_duet': bool(disable_duet),
                    'disable_stitch': bool(disable_stitch),
                    'video_cover_timestamp_ms': int(cover_ms or 0),
                },
                'source_info': source,
            }
        else:
            # En brouillon, TikTok n'accepte aucun `post_info` : la légende et
            # la visibilité se choisissent dans l'application, au moment de
            # publier. Le texte reste donc à coller à la main.
            url = f'{API}/post/publish/inbox/video/init/'
            corps = {'source_info': source}

        r = requests.post(url, json=corps, timeout=_TIMEOUT, headers={
            'Authorization': f'Bearer {jeton}',
            'Content-Type': 'application/json; charset=UTF-8'})
        data = _corps(r, "TikTok a refusé l'envoi") or {}
        publish_id = data.get('publish_id')
        lien = data.get('upload_url')
        if not (publish_id and lien):
            return False, "TikTok n'a pas renvoyé d'adresse de téléversement."

        _televerser(lien, path, morceau, nombre, taille)
        return True, publish_id
    except TikTokPublishError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, f"Connexion à TikTok interrompue : {exc}"


ETATS = {
    'PROCESSING_UPLOAD': "TikTok traite la vidéo…",
    'PROCESSING_DOWNLOAD': "TikTok récupère la vidéo…",
    'SEND_TO_USER_INBOX': "Déposée dans les brouillons — à publier depuis l'application.",
    'PUBLISH_COMPLETE': "Publiée.",
    'FAILED': "Échec côté TikTok.",
}


def publish_status(publish_id):
    """Where a send has got to. Returns a dict, never raises for the caller."""
    try:
        r = requests.post(f'{API}/post/publish/status/fetch/',
                          json={'publish_id': publish_id}, timeout=_TIMEOUT,
                          headers={'Authorization': f'Bearer {_access_token()}',
                                   'Content-Type': 'application/json; charset=UTF-8'})
        data = _corps(r, "État de la publication illisible") or {}
    except (TikTokPublishError, requests.RequestException) as exc:
        return {'statut': None, 'detail': str(exc), 'post_ids': [], 'fini': False}

    statut = data.get('status') or ''
    # Le nom du champ porte la faute de frappe de la documentation ; la
    # corriger ici ne ferait que perdre la valeur.
    ids = data.get('publicaly_available_post_id') or data.get('publicly_available_post_id') or []
    detail = ETATS.get(statut, statut or 'inconnu')
    if statut == 'FAILED':
        detail = f"Échec côté TikTok : {data.get('fail_reason') or 'raison non précisée'}"
    return {
        'statut': statut,
        'detail': detail,
        'post_ids': [str(i) for i in ids],
        'fini': statut in ('PUBLISH_COMPLETE', 'SEND_TO_USER_INBOX', 'FAILED'),
    }


def post_url(post_id):
    nom = handle()
    return (f'https://www.tiktok.com/@{nom.lstrip("@")}/video/{post_id}'
            if nom and post_id else None)


# ─── Envoi d'un montage, suivi jusqu'au bout ────────────────

SUIVI_INTERVALLE = 10
SUIVI_DUREE_MAX = 15 * 60


def _noter(job_id, **champs):
    import video
    job = video.get_job(job_id) or {}
    etat = dict(job.get('tiktok') or {})
    etat.update(champs)
    video._set(job_id, tiktok=etat)
    return etat


def envoyer_job(app, job_id, titre=None, privacy_level=None,
                disable_comment=False, disable_duet=False, disable_stitch=False):
    """Send a finished montage to TikTok and follow it to the end.

    Runs in its own thread with an application context of its own: uploading
    ninety megabytes takes longer than a request should, and the token, the
    settings and the job file all need the application.

    What it writes on the job is what the page shows, so every step is
    recorded — including the failures, which are the ones worth reading.
    """
    import video

    def _travail():
        with app.app_context():
            job = video.get_job(job_id) or {}
            chemin = job.get('output')
            _noter(job_id, etat='envoi', detail="Téléversement vers TikTok…",
                   mode=mode(), demarre=datetime.utcnow().isoformat(timespec='seconds'),
                   post_ids=[], publish_id=None)
            ok, resultat = post_video(
                chemin, title=titre, privacy_level=privacy_level,
                disable_comment=disable_comment, disable_duet=disable_duet,
                disable_stitch=disable_stitch)
            if not ok:
                _noter(job_id, etat='echec', detail=resultat)
                return

            publish_id = resultat
            _noter(job_id, publish_id=publish_id, etat='attente',
                   detail="Envoyée — TikTok la traite…")

            debut = time.time()
            while time.time() - debut < SUIVI_DUREE_MAX:
                time.sleep(SUIVI_INTERVALLE)
                etat = publish_status(publish_id)
                if etat['statut'] == 'FAILED':
                    _noter(job_id, etat='echec', detail=etat['detail'])
                    return
                if etat['statut'] == 'SEND_TO_USER_INBOX':
                    _noter(job_id, etat='brouillon', detail=etat['detail'])
                    return
                if etat['statut'] == 'PUBLISH_COMPLETE':
                    ids = etat['post_ids']
                    _noter(job_id, etat='publie', post_ids=ids,
                           url=post_url(ids[0]) if ids else None,
                           detail=("Publiée." if ids else
                                   "Publiée — TikTok n'a pas encore rendu "
                                   "l'identifiant du post."))
                    return
                _noter(job_id, detail=etat['detail'])
            _noter(job_id, etat='inconnu',
                   detail="TikTok n'a pas répondu en quinze minutes — "
                          "vérifiez dans l'application.")

    threading.Thread(target=_travail, name=f'tiktok-envoi-{job_id}',
                     daemon=True).start()
