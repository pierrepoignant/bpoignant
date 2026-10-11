"""Publier les clips sur la Page Facebook et en Reels Instagram, par le Graph API.

Meta ne permet pas de publier sur un profil Facebook personnel — l'API a été
retirée en 2018, sans remplacement. On publie donc sur une Page Facebook et sur
un compte Instagram professionnel rattaché à cette Page : les deux passent par
le même Graph API et le même jeton.

Le jeton : on échange le code contre un jeton utilisateur courte durée, puis
contre un longue durée (soixante jours), et on lit depuis celui-ci le jeton de
la Page — ce jeton de Page n'expire pas tant que Bernard ne change pas son mot
de passe ni ne retire l'accès. C'est lui qu'on garde et qu'on utilise pour tout.

Un Reel Instagram se publie en deux temps : on crée un « conteneur » pointant
vers l'URL publique de la vidéo (nos clips sont déjà servis publiquement par le
stockage OVH), on attend qu'il soit prêt, puis on le publie.
"""

import logging
import time
from datetime import datetime, timedelta

import requests

from settings.models import get_config, set_config, delete_config

log = logging.getLogger(__name__)

API = 'https://graph.facebook.com'
VERSION = 'v21.0'
DIALOG = 'https://www.facebook.com/{v}/dialog/oauth'

# Publier sur la Page, lister les Pages, lire leur engagement, et publier sur
# l'Instagram professionnel qui leur est rattaché. On ne demande que le
# strict nécessaire : chaque permission supplémentaire est une case de plus à
# activer dans l'application Meta, et une de plus à faire refuser si elle n'y
# est pas. Les vues d'une vidéo de Page passent par pages_read_engagement, donc
# read_insights (statistiques globales de la Page) n'est pas demandé : les vues
# d'une vidéo de Page passent déjà par pages_read_engagement.
# instagram_manage_insights donne le nombre de vues d'un Reel. Il faut d'abord
# l'activer dans l'application Meta (cas d'usage « Instagram API setup with
# Facebook login »), sinon la connexion échoue sur « Invalid Scopes ». Une fois
# activé côté Meta, reconnecter la Page pour que le jeton le porte ; sans lui,
# la publication, les j'aime et les commentaires fonctionnent, mais les vues
# Instagram restent à zéro.
SCOPE = ('pages_show_list,pages_manage_posts,pages_read_engagement,'
         'business_management,instagram_basic,instagram_content_publish,'
         'instagram_manage_insights')

KEY_APP_ID = 'meta_app_id'
KEY_APP_SECRET = 'meta_app_secret'
KEY_PAGE_ID = 'meta_page_id'
KEY_PAGE_NAME = 'meta_page_name'
KEY_PAGE_TOKEN = 'meta_page_token'
KEY_IG_ID = 'meta_ig_user_id'
KEY_IG_NAME = 'meta_ig_username'
KEY_AUTH_ERROR = 'meta_auth_error'
KEY_ARTICLE_MINID = 'meta_fb_article_min_id'

_TIMEOUT = 30


class MetaError(RuntimeError):
    pass


class MetaAuthError(MetaError):
    pass


# ─── Réglages ───────────────────────────────────────────────

def _app_id():
    return (get_config(KEY_APP_ID) or '').strip()


def _app_secret():
    return (get_config(KEY_APP_SECRET) or '').strip()


def has_client_credentials():
    return bool(_app_id() and _app_secret())


def save_settings(app_id, app_secret=None):
    set_config(KEY_APP_ID, (app_id or '').strip())
    if (app_secret or '').strip():
        set_config(KEY_APP_SECRET, app_secret.strip())


def page_id():
    return (get_config(KEY_PAGE_ID) or '').strip()


def page_name():
    return (get_config(KEY_PAGE_NAME) or '').strip()


def _page_token():
    return (get_config(KEY_PAGE_TOKEN) or '').strip()


def ig_user_id():
    return (get_config(KEY_IG_ID) or '').strip()


def ig_username():
    return (get_config(KEY_IG_NAME) or '').strip()


def is_connected():
    return bool(_page_token() and page_id())


def facebook_ready():
    return is_connected()


def instagram_ready():
    return bool(is_connected() and ig_user_id())


def auth_error():
    return (get_config(KEY_AUTH_ERROR) or '').strip()


def _note_auth_error(msg):
    try:
        if (get_config(KEY_AUTH_ERROR) or '') != msg:
            set_config(KEY_AUTH_ERROR, msg)
    except Exception:
        log.exception('Meta : note de panne impossible')


def _clear_auth_error():
    try:
        if get_config(KEY_AUTH_ERROR):
            delete_config(KEY_AUTH_ERROR)
    except Exception:
        log.exception("Meta : effacement de panne impossible")


def disconnect():
    for cle in (KEY_PAGE_ID, KEY_PAGE_NAME, KEY_PAGE_TOKEN, KEY_IG_ID, KEY_IG_NAME):
        delete_config(cle)
    _clear_auth_error()


# ─── OAuth ──────────────────────────────────────────────────

def authorization_url(redirect_uri, state):
    from urllib.parse import urlencode
    if not _app_id():
        raise MetaError("Renseignez d'abord l'App ID et l'App Secret Meta.")
    return DIALOG.format(v=VERSION) + '?' + urlencode({
        'client_id': _app_id(),
        'redirect_uri': redirect_uri,
        'state': state,
        'scope': SCOPE,
        'response_type': 'code',
    })


def _get(path, params=None, token=None):
    params = dict(params or {})
    if token:
        params['access_token'] = token
    r = requests.get(f'{API}/{VERSION}/{path}', params=params, timeout=_TIMEOUT)
    return _corps(r)


def _corps(resp):
    try:
        corps = resp.json()
    except ValueError:
        raise MetaError(f"Réponse Meta illisible ({resp.status_code}).")
    err = corps.get('error')
    if err:
        message = (err.get('error_user_msg') or err.get('message') or '').strip()
        code = err.get('code')
        if code in (190, 102, 10, 200):   # jeton invalide / permission manquante
            raise MetaAuthError(message or "Autorisation Meta refusée.")
        raise MetaError(message or f"Erreur Meta ({code}).")
    return corps


def exchange_code(code, redirect_uri):
    if not code:
        raise MetaError("Meta n'a renvoyé aucun code d'autorisation.")
    # Code → jeton utilisateur courte durée.
    court = _get('oauth/access_token', {
        'client_id': _app_id(), 'client_secret': _app_secret(),
        'redirect_uri': redirect_uri, 'code': code,
    }).get('access_token')
    if not court:
        raise MetaError("Meta n'a pas renvoyé de jeton.")
    # Courte durée → longue durée (soixante jours).
    long = _get('oauth/access_token', {
        'grant_type': 'fb_exchange_token',
        'client_id': _app_id(), 'client_secret': _app_secret(),
        'fb_exchange_token': court,
    }).get('access_token') or court
    # La Page et son jeton (dérivé du jeton longue durée : il n'expire pas).
    pages = (_get('me/accounts', {'fields': 'id,name,access_token,instagram_business_account{id,username}'},
                  token=long).get('data') or [])
    if not pages:
        raise MetaError("Aucune Page Facebook n'est administrée par ce compte.")
    # On prend la première Page ; s'il y en a plusieurs, Bernard pourra la
    # changer plus tard — mais une seule Page de chroniques est le cas courant.
    page = pages[0]
    set_config(KEY_PAGE_ID, page['id'])
    set_config(KEY_PAGE_NAME, (page.get('name') or '')[:200])
    set_config(KEY_PAGE_TOKEN, page['access_token'])
    iba = page.get('instagram_business_account') or {}
    if iba.get('id'):
        set_config(KEY_IG_ID, iba['id'])
        set_config(KEY_IG_NAME, (iba.get('username') or '')[:200])
    else:
        delete_config(KEY_IG_ID)
        delete_config(KEY_IG_NAME)
    _clear_auth_error()
    return len(pages)


def verify_credentials():
    try:
        info = _get(page_id(), {'fields': 'name,fan_count'}, token=_page_token())
        ig = f" · Instagram @{ig_username()}" if instagram_ready() else " · pas d'Instagram rattaché"
        _clear_auth_error()
        return True, f"Connecté à la Page « {info.get('name') or page_name()} »{ig}."
    except MetaError as exc:
        if isinstance(exc, MetaAuthError):
            _note_auth_error(str(exc))
        return False, str(exc)


# ─── Publication ────────────────────────────────────────────

def post_to_page(video_url, caption):
    """Publier une vidéo sur la Page. Renvoie (ok, post_id_ou_erreur)."""
    if not is_connected():
        return False, "Facebook n'est pas connecté"
    try:
        r = requests.post(f'{API}/{VERSION}/{page_id()}/videos',
                          data={'file_url': video_url,
                                'description': (caption or '')[:5000],
                                'access_token': _page_token()},
                          timeout=180)
        corps = _corps(r)
    except MetaError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, f"Connexion à Facebook interrompue : {exc}"
    vid = corps.get('id') or corps.get('post_id')
    if not vid:
        return False, "Facebook n'a pas renvoyé d'identifiant."
    return True, str(vid)


def post_link_to_page(link, message):
    """Publier un lien sur la Page (publication de fil). Renvoie (ok, id).

    L'aperçu — titre, image, extrait — est tiré des balises Open Graph de la
    page liée, donc le `message` ne porte que l'accroche. C'est la forme juste
    pour un article : le lien s'affiche en carte sous le texte.
    """
    if not is_connected():
        return False, "Facebook n'est pas connecté"
    try:
        r = requests.post(f'{API}/{VERSION}/{page_id()}/feed',
                          data={'link': link, 'message': (message or '')[:5000],
                                'access_token': _page_token()},
                          timeout=_TIMEOUT)
        corps = _corps(r)
    except MetaError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, f"Connexion à Facebook interrompue : {exc}"
    pid = corps.get('id') or corps.get('post_id')
    if not pid:
        return False, "Facebook n'a pas renvoyé d'identifiant."
    return True, str(pid)


def post_reel(video_url, caption, attente_max=180):
    """Publier un Reel Instagram : conteneur, attente, publication.

    Renvoie (ok, media_id_ou_erreur). Le conteneur met quelques secondes à
    quelques dizaines de secondes à être prêt — Instagram transcode la vidéo —
    donc on interroge son état avant de publier.
    """
    if not instagram_ready():
        return False, "Instagram n'est pas rattaché"
    ig = ig_user_id()
    tok = _page_token()
    try:
        conteneur = _corps(requests.post(
            f'{API}/{VERSION}/{ig}/media',
            data={'media_type': 'REELS', 'video_url': video_url,
                  'caption': (caption or '')[:2200], 'access_token': tok},
            timeout=_TIMEOUT)).get('id')
        if not conteneur:
            return False, "Instagram n'a pas créé le conteneur."
        # Attendre que le conteneur soit prêt (FINISHED).
        debut = time.time()
        while time.time() - debut < attente_max:
            time.sleep(5)
            etat = _corps(_req_get(f'{ig_container(conteneur)}',
                                   {'fields': 'status_code,status'}, tok))
            code = etat.get('status_code')
            if code == 'FINISHED':
                break
            if code == 'ERROR':
                return False, f"Instagram a refusé la vidéo : {etat.get('status') or 'erreur'}"
        else:
            return False, "Instagram n'a pas fini de préparer la vidéo à temps."
        media = _corps(requests.post(
            f'{API}/{VERSION}/{ig}/media_publish',
            data={'creation_id': conteneur, 'access_token': tok},
            timeout=_TIMEOUT)).get('id')
        if not media:
            return False, "Instagram n'a pas publié le Reel."
        return True, str(media)
    except MetaError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, f"Connexion à Instagram interrompue : {exc}"


def ig_container(container_id):
    return container_id


def _req_get(path, params, token):
    params = dict(params); params['access_token'] = token
    return requests.get(f'{API}/{VERSION}/{path}', params=params, timeout=_TIMEOUT)


# ─── Statistiques ───────────────────────────────────────────

def facebook_stats(video_id):
    """(vues) d'une vidéo de Page, ou None."""
    try:
        corps = _get(f'{video_id}', {'fields': 'views'}, token=_page_token())
        return {'views': int(corps.get('views') or 0)}
    except MetaError:
        return None


def instagram_stats(media_id):
    """(vues, likes, commentaires) d'un Reel, ou None."""
    try:
        base = _get(f'{media_id}', {'fields': 'like_count,comments_count'}, token=_page_token())
        vues = 0
        try:
            ins = _get(f'{media_id}/insights', {'metric': 'plays'}, token=_page_token())
            for m in ins.get('data') or []:
                vues = (m.get('values') or [{}])[0].get('value') or 0
        except MetaError:
            pass
        return {'views': int(vues), 'likes': int(base.get('like_count') or 0),
                'comments': int(base.get('comments_count') or 0)}
    except MetaError:
        return None


def facebook_url(post_id):
    return f'https://www.facebook.com/{post_id}' if post_id else None


def instagram_url(media_id):
    # L'identifiant média n'est pas le shortcode de l'URL publique ; le lien
    # vers le profil est le plus sûr sans appel supplémentaire.
    return f'https://www.instagram.com/{ig_username().lstrip("@")}/' if media_id and ig_username() else None


# ─── Articles → Page Facebook ───────────────────────────────
#
# Même principe que X et LinkedIn : un article par jour, le plus ancien jamais
# publié sur la Page, pour écouler sans tout déverser d'un coup. Mais on ne veut
# PAS vider l'arriéré des dizaines d'anciens articles : la publication auto ne
# concerne que les articles créés après l'activation. Ce seuil est l'identifiant
# maximal des articles au moment où on l'a activée ; tout article d'identifiant
# supérieur est « nouveau ». Les quelques articles rattrapés à la main (les
# derniers) portent déjà leur date de publication Page, donc sont ignorés.

def article_autopost_min_id():
    try:
        return int(get_config(KEY_ARTICLE_MINID) or 0)
    except (TypeError, ValueError):
        return 0


def set_article_autopost_min_id(value):
    set_config(KEY_ARTICLE_MINID, str(int(value)))


def article_autopost_enabled():
    return bool(get_config(KEY_ARTICLE_MINID))


def compose_article_page_post(article, url):
    """Le texte d'un post d'article : l'accroche réseaux si on l'a, le résumé
    sinon ; à défaut le titre. Le lien n'est pas dans le texte — il est passé à
    part et s'affiche en carte."""
    accroche = (getattr(article, 'social_summary', '') or '').strip() \
        or (getattr(article, 'summary', '') or '').strip()
    return accroche or (article.title or '')


def fb_post_article(article):
    """Publier un article sur la Page. Renvoie (ok, id_ou_erreur)."""
    from flask import url_for
    from init_db import db
    if article.fb_page_post_id:
        return False, "déjà publié"
    if not is_connected():
        return False, "Facebook n'est pas connecté"
    url = url_for('articles.public_show', slug=article.slug, _external=True)
    ok, detail = post_link_to_page(url, compose_article_page_post(article, url))
    if not ok:
        return False, str(detail)
    article.fb_page_post_id = str(detail)
    article.fb_page_posted_at = datetime.utcnow()
    db.session.commit()
    return True, article.fb_page_post_id


def _paris_day(dt_utc):
    from zoneinfo import ZoneInfo
    return dt_utc.replace(tzinfo=ZoneInfo('UTC')).astimezone(
        ZoneInfo('Europe/Paris')).date()


def fb_last_article_post_at():
    from sqlalchemy import func
    from articles.models import Article
    from init_db import db
    return db.session.query(func.max(Article.fb_page_posted_at)).scalar()


def fb_already_posted_article_today():
    dernier = fb_last_article_post_at()
    return dernier is not None and _paris_day(dernier) == _paris_day(datetime.utcnow())


def fb_next_article_to_post():
    """Le plus ancien article publié, jamais mis sur la Page, et postérieur au
    seuil d'activation (identifiant supérieur au seuil)."""
    from articles.models import Article
    return (Article.query
            .filter(Article.published == True,  # noqa: E712
                    Article.fb_page_posted_at.is_(None),
                    Article.id > article_autopost_min_id())
            .order_by(Article.created_at.asc())
            .first())


def fb_auto_post_article():
    """Publier le plus ancien article éligible, sauf si un est déjà parti
    aujourd'hui. Renvoie un dict dont `status` vaut posted / too_soon /
    nothing_to_post / disabled / not_connected / failed."""
    if not article_autopost_enabled():
        return {'status': 'disabled'}
    if not is_connected():
        return {'status': 'not_connected'}
    if fb_already_posted_article_today():
        return {'status': 'too_soon', 'last': fb_last_article_post_at()}
    article = fb_next_article_to_post()
    if article is None:
        return {'status': 'nothing_to_post'}
    ok, detail = fb_post_article(article)
    if not ok:
        log.error("auto Page Facebook échoué pour l'article %s : %s", article.id, detail)
        return {'status': 'failed', 'article': article, 'detail': detail}
    return {'status': 'posted', 'article': article}


# ─── Commentaires (Page Facebook et Instagram) ──────────────

def _parse_fb_time(valeur):
    if not valeur:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(str(valeur).replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return None


def fetch_facebook_comments(object_id, limit=100):
    """Les commentaires d'un post de Page. Liste de dicts normalisés.

    Fonctionne pour une vidéo de Page comme pour une publication de lien. On ne
    prend que les commentaires de premier niveau, dans l'ordre le plus récent.
    """
    if not (object_id and is_connected()):
        return []
    try:
        corps = _get(f'{object_id}/comments', {
            'fields': 'id,message,from{name,id},created_time,like_count',
            'order': 'reverse_chronological', 'limit': limit,
        }, token=_page_token())
    except MetaError:
        return []
    out = []
    for c in corps.get('data') or []:
        frm = c.get('from') or {}
        out.append({
            'external_id': c.get('id'),
            'author': frm.get('name'),
            'author_id': frm.get('id'),
            'text': c.get('message') or '',
            'likes': c.get('like_count'),
            'posted_at': _parse_fb_time(c.get('created_time')),
        })
    return out


def reply_facebook_comment(comment_id, text):
    """Répondre à un commentaire de Page. Renvoie (ok, id_ou_erreur)."""
    if not (comment_id and (text or '').strip()):
        return False, "réponse vide"
    try:
        r = requests.post(f'{API}/{VERSION}/{comment_id}/comments',
                          data={'message': text.strip()[:8000], 'access_token': _page_token()},
                          timeout=_TIMEOUT)
        corps = _corps(r)
    except MetaError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, f"Connexion à Facebook interrompue : {exc}"
    return True, str(corps.get('id') or 'ok')


def fetch_instagram_comments(media_id, limit=100):
    """Les commentaires d'un Reel Instagram. Liste de dicts normalisés."""
    if not (media_id and instagram_ready()):
        return []
    try:
        corps = _get(f'{media_id}/comments', {
            'fields': 'id,text,username,timestamp,like_count', 'limit': limit,
        }, token=_page_token())
    except MetaError:
        return []
    out = []
    for c in corps.get('data') or []:
        out.append({
            'external_id': c.get('id'),
            'author': c.get('username'),
            'text': c.get('text') or '',
            'likes': c.get('like_count'),
            'posted_at': _parse_fb_time(c.get('timestamp')),
        })
    return out


def reply_instagram_comment(comment_id, text):
    """Répondre à un commentaire Instagram. Renvoie (ok, id_ou_erreur)."""
    if not (comment_id and (text or '').strip()):
        return False, "réponse vide"
    try:
        r = requests.post(f'{API}/{VERSION}/{comment_id}/replies',
                          data={'message': text.strip()[:2000], 'access_token': _page_token()},
                          timeout=_TIMEOUT)
        corps = _corps(r)
    except MetaError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, f"Connexion à Instagram interrompue : {exc}"
    return True, str(corps.get('id') or 'ok')
