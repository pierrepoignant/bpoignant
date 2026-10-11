"""Publier les articles sur Threads, par l'API Threads de Meta.

Threads a sa propre API (graph.threads.net) et son propre jeton, distinct de
celui de la Page Facebook et d'Instagram : c'est une application Threads à part,
avec ses identifiants, et une connexion séparée.

Le jeton : on échange le code contre un jeton courte durée (une heure), puis
contre un longue durée (soixante jours), qu'on rafraîchit avant qu'il n'expire.
Un post se publie en deux temps, comme un Reel : on crée un conteneur texte,
puis on le publie.

Un post Threads fait 500 caractères au plus : pour un article, c'est l'accroche
suivie du lien, dont Threads tire un aperçu.
"""

import logging
from datetime import datetime, timedelta

import requests

from settings.models import get_config, set_config, delete_config

log = logging.getLogger(__name__)

API = 'https://graph.threads.net'
AUTH = 'https://threads.net/oauth/authorize'
SCOPE = 'threads_basic,threads_content_publish'
LIMITE = 500
_TIMEOUT = 30

KEY_APP_ID = 'threads_app_id'
KEY_APP_SECRET = 'threads_app_secret'
KEY_TOKEN = 'threads_token'
KEY_EXPIRES = 'threads_token_expires'   # ISO
KEY_USER_ID = 'threads_user_id'
KEY_USERNAME = 'threads_username'
KEY_AUTH_ERROR = 'threads_auth_error'
KEY_ARTICLE_MINID = 'threads_article_min_id'


class ThreadsError(RuntimeError):
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


def user_id():
    return (get_config(KEY_USER_ID) or '').strip()


def username():
    return (get_config(KEY_USERNAME) or '').strip()


def is_connected():
    return bool((get_config(KEY_TOKEN) or '').strip() and user_id())


def auth_error():
    return (get_config(KEY_AUTH_ERROR) or '').strip()


def _note_auth_error(msg):
    try:
        if (get_config(KEY_AUTH_ERROR) or '') != msg:
            set_config(KEY_AUTH_ERROR, msg)
    except Exception:
        log.exception('Threads : note de panne impossible')


def _clear_auth_error():
    try:
        if get_config(KEY_AUTH_ERROR):
            delete_config(KEY_AUTH_ERROR)
    except Exception:
        log.exception('Threads : effacement de panne impossible')


def disconnect():
    for cle in (KEY_TOKEN, KEY_EXPIRES, KEY_USER_ID, KEY_USERNAME):
        delete_config(cle)
    _clear_auth_error()


def article_autopost_min_id():
    try:
        return int(get_config(KEY_ARTICLE_MINID) or 0)
    except (TypeError, ValueError):
        return 0


def set_article_autopost_min_id(value):
    set_config(KEY_ARTICLE_MINID, str(int(value)))


def article_autopost_enabled():
    return bool(get_config(KEY_ARTICLE_MINID))


# ─── OAuth ──────────────────────────────────────────────────

def authorization_url(redirect_uri, state):
    from urllib.parse import urlencode
    if not _app_id():
        raise ThreadsError("Renseignez d'abord l'App ID et l'App Secret Threads.")
    return AUTH + '?' + urlencode({
        'client_id': _app_id(), 'redirect_uri': redirect_uri,
        'scope': SCOPE, 'response_type': 'code', 'state': state})


def exchange_code(code, redirect_uri):
    if not code:
        raise ThreadsError("Threads n'a renvoyé aucun code d'autorisation.")
    r = requests.post(f'{API}/oauth/access_token', data={
        'client_id': _app_id(), 'client_secret': _app_secret(),
        'grant_type': 'authorization_code', 'redirect_uri': redirect_uri, 'code': code,
    }, timeout=_TIMEOUT)
    court = _corps(r)
    uid = str(court.get('user_id') or '')
    token = court.get('access_token')
    if not (uid and token):
        raise ThreadsError("Threads n'a pas renvoyé de jeton.")
    # Courte durée → longue durée (60 jours).
    long_r = requests.get(f'{API}/access_token', params={
        'grant_type': 'th_exchange_token', 'client_secret': _app_secret(),
        'access_token': token}, timeout=_TIMEOUT)
    longc = _corps(long_r)
    token = longc.get('access_token') or token
    duree = int(longc.get('expires_in') or 5180000)
    set_config(KEY_TOKEN, token)
    set_config(KEY_EXPIRES, (datetime.utcnow() + timedelta(seconds=duree)).isoformat())
    set_config(KEY_USER_ID, uid)
    # Nom d'utilisateur, pour l'affichage.
    try:
        me = _corps(requests.get(f'{API}/v1.0/me', params={'fields': 'username', 'access_token': token}, timeout=_TIMEOUT))
        if me.get('username'):
            set_config(KEY_USERNAME, me['username'][:120])
    except ThreadsError:
        pass
    _clear_auth_error()


def _corps(resp):
    try:
        corps = resp.json()
    except ValueError:
        raise ThreadsError(f"Réponse Threads illisible ({resp.status_code}).")
    err = corps.get('error')
    if err:
        msg = (err.get('message') if isinstance(err, dict) else str(err)) or 'erreur'
        raise ThreadsError(str(msg))
    if resp.status_code >= 400:
        raise ThreadsError(f"Threads a répondu {resp.status_code}.")
    return corps


def _valid_token():
    token = (get_config(KEY_TOKEN) or '').strip()
    if not token:
        raise ThreadsError("Threads n'est pas connecté.")
    # Rafraîchir si on approche de l'expiration (fenêtre de dix jours). Le jeton
    # doit avoir au moins 24 h, ce qui est le cas en pratique dès le lendemain.
    exp = get_config(KEY_EXPIRES)
    try:
        proche = exp and datetime.fromisoformat(exp) - datetime.utcnow() < timedelta(days=10)
    except ValueError:
        proche = False
    if proche:
        try:
            r = _corps(requests.get(f'{API}/refresh_access_token', params={
                'grant_type': 'th_refresh_token', 'access_token': token}, timeout=_TIMEOUT))
            if r.get('access_token'):
                token = r['access_token']
                set_config(KEY_TOKEN, token)
                set_config(KEY_EXPIRES, (datetime.utcnow() + timedelta(
                    seconds=int(r.get('expires_in') or 5180000))).isoformat())
        except ThreadsError:
            pass
    return token


def verify_credentials():
    try:
        me = _corps(requests.get(f'{API}/v1.0/me', params={
            'fields': 'username', 'access_token': _valid_token()}, timeout=_TIMEOUT))
        _clear_auth_error()
        return True, f"Connecté à Threads @{me.get('username') or username()}."
    except ThreadsError as exc:
        _note_auth_error(str(exc))
        return False, str(exc)


# ─── Publication ────────────────────────────────────────────

def post_text(text):
    """Publier un post texte. Renvoie (ok, id_ou_erreur)."""
    text = (text or '').strip()
    if not text:
        return False, "texte vide"
    if not is_connected():
        return False, "Threads n'est pas connecté"
    try:
        token = _valid_token()
        conteneur = _corps(requests.post(
            f'{API}/v1.0/{user_id()}/threads',
            data={'media_type': 'TEXT', 'text': text[:LIMITE], 'access_token': token},
            timeout=_TIMEOUT)).get('id')
        if not conteneur:
            return False, "Threads n'a pas créé le conteneur."
        pub = _corps(requests.post(
            f'{API}/v1.0/{user_id()}/threads_publish',
            data={'creation_id': conteneur, 'access_token': token},
            timeout=_TIMEOUT)).get('id')
        if not pub:
            return False, "Threads n'a pas publié le post."
        return True, str(pub)
    except ThreadsError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, f"Connexion à Threads interrompue : {exc}"


def compose_article(article, url):
    """Accroche + lien, taillé pour 500 caractères."""
    accroche = (getattr(article, 'social_summary', '') or '').strip() \
        or (getattr(article, 'summary', '') or '').strip() or (article.title or '')
    reste = LIMITE - len(url) - 2
    if len(accroche) > reste:
        accroche = accroche[:max(0, reste - 1)].rstrip() + '…'
    return f"{accroche}\n{url}" if accroche else url


def post_article(article):
    """Publier un article sur Threads. Renvoie (ok, id_ou_erreur)."""
    from flask import url_for
    from init_db import db
    if article.threads_post_id:
        return False, "déjà publié"
    if not is_connected():
        return False, "Threads n'est pas connecté"
    url = url_for('articles.public_show', slug=article.slug, _external=True)
    ok, detail = post_text(compose_article(article, url))
    if not ok:
        return False, str(detail)
    article.threads_post_id = str(detail)
    article.threads_posted_at = datetime.utcnow()
    db.session.commit()
    return True, article.threads_post_id


def post_url(post_id):
    return f'https://www.threads.com/@{username()}/post/{post_id}' if post_id and username() else None


# ─── Articles automatiques ──────────────────────────────────

def _paris_day(dt_utc):
    from zoneinfo import ZoneInfo
    return dt_utc.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo('Europe/Paris')).date()


def last_article_post_at():
    from sqlalchemy import func
    from articles.models import Article
    from init_db import db
    return db.session.query(func.max(Article.threads_posted_at)).scalar()


def already_posted_today():
    d = last_article_post_at()
    return d is not None and _paris_day(d) == _paris_day(datetime.utcnow())


def next_article_to_post():
    from articles.models import Article
    return (Article.query
            .filter(Article.published == True,  # noqa: E712
                    Article.threads_posted_at.is_(None),
                    Article.id > article_autopost_min_id())
            .order_by(Article.created_at.asc()).first())


def backfill(limit=2, log_fn=print):
    """Rattraper l'arriéré : publier sur Threads les `limit` plus anciens
    articles publiés qui n'y sont pas encore, sans tenir compte du seuil
    d'activation. Appelé par le CronJob deux fois par jour jusqu'à épuisement."""
    from articles.models import Article
    if not is_connected():
        log_fn("Threads n'est pas connecté — rien à faire.")
        return 0, 0
    arts = (Article.query
            .filter(Article.published == True,  # noqa: E712
                    Article.threads_posted_at.is_(None))
            .order_by(Article.created_at.asc()).limit(limit).all())
    faits = 0
    for a in arts:
        ok, detail = post_article(a)
        if ok:
            faits += 1
            log_fn(f"publié : {a.title[:50]} → {post_url(detail)}")
        else:
            log_fn(f"échec : {a.title[:50]} — {detail}")
    reste = (Article.query.filter(Article.published == True,  # noqa: E712
                                  Article.threads_posted_at.is_(None)).count())
    log_fn(f"{faits} publié(s) ; {reste} encore à faire.")
    return faits, reste


def auto_post_article():
    if not article_autopost_enabled():
        return {'status': 'disabled'}
    if not is_connected():
        return {'status': 'not_connected'}
    if already_posted_today():
        return {'status': 'too_soon', 'last': last_article_post_at()}
    article = next_article_to_post()
    if article is None:
        return {'status': 'nothing_to_post'}
    ok, detail = post_article(article)
    if not ok:
        log.error("auto Threads échoué pour l'article %s : %s", article.id, detail)
        return {'status': 'failed', 'article': article, 'detail': detail}
    return {'status': 'posted', 'article': article}
