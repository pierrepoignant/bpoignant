"""Publier sur le profil LinkedIn de Bernard.

Écrit sur le modèle du module `x` : mêmes fonctions, mêmes retours, pour que
les trois endroits qui publient déjà sur X puissent publier ici sans se
réécrire.

Deux différences tiennent à LinkedIn et non à ce code :

- Le jeton d'accès vit soixante jours. Les jetons de rafraîchissement ne sont
  accordés qu'aux applications partenaires ; une application créée en libre
  service oblige donc à réautoriser tous les deux mois. La date d'expiration
  est conservée pour prévenir avant la panne plutôt qu'après.
- Les publications d'un membre n'exposent aucune statistique. Il n'y a ni
  `fetch_metrics` ni équivalent : LinkedIn ne les publie que pour les pages
  d'organisation, et inventer une colonne vide serait pire que son absence.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import requests

from settings.models import get_config, set_config, delete_config

log = logging.getLogger(__name__)

AUTH_URL = 'https://www.linkedin.com/oauth/v2/authorization'
TOKEN_URL = 'https://www.linkedin.com/oauth/v2/accessToken'
API = 'https://api.linkedin.com'

# `openid` et `profile` donnent l'identifiant du membre, `w_member_social` le
# droit de publier en son nom. Les deux produits correspondants s'obtiennent
# sans validation dans la console LinkedIn.
SCOPE = 'openid profile w_member_social'

# LinkedIn exige un en-tête de version daté (AAAAMM) et retire ses versions au
# bout d'un an environ. Une constante écrite en dur pourrit donc toute seule :
# la valeur par défaut suit le calendrier, et une version refusée est
# rattrapée plus bas en reculant de mois en mois.
def _default_version():
    # Le mois précédent : la version du mois courant n'est pas toujours
    # publiée le premier jour.
    aujourdhui = datetime.utcnow()
    annee, mois = aujourdhui.year, aujourdhui.month - 1
    if mois == 0:
        annee, mois = annee - 1, 12
    return f'{annee}{mois:02d}'


DEFAULT_VERSION = _default_version()

KEY_CLIENT_ID = 'linkedin_client_id'
KEY_CLIENT_SECRET = 'linkedin_client_secret'
KEY_TOKEN = 'linkedin_access_token'
KEY_EXPIRES = 'linkedin_expires_at'
KEY_PERSON = 'linkedin_person_urn'
KEY_NAME = 'linkedin_name'
KEY_VERSION = 'linkedin_version'

_TIMEOUT = 30
POST_LIMIT = 3000          # LinkedIn accepte 3000 caractères de commentaire


class LinkedInError(RuntimeError):
    pass


class LinkedInAuthError(LinkedInError):
    pass


def _client_id():
    return (get_config(KEY_CLIENT_ID) or os.environ.get('LINKEDIN__CLIENT_ID') or '').strip()


def _client_secret():
    return (get_config(KEY_CLIENT_SECRET) or os.environ.get('LINKEDIN__CLIENT_SECRET') or '').strip()


def _version():
    return (get_config(KEY_VERSION) or _default_version()).strip()


def _version_precedente(version):
    """The month before `version`, for walking back to one LinkedIn still
    accepts."""
    try:
        annee, mois = int(version[:4]), int(version[4:6])
    except (ValueError, IndexError):
        return None
    mois -= 1
    if mois == 0:
        annee, mois = annee - 1, 12
    return f'{annee}{mois:02d}'


def _appeler(methode, url, **kw):
    """Send a request, stepping the API version back if LinkedIn has retired it.

    LinkedIn answers 426 NONEXISTENT_VERSION for a version older than about a
    year, and a version that worked in January is refused the following spring.
    The call is retried a month earlier at a time, and the version that answers
    is stored — so this heals itself once instead of failing every time.

    A 426 means the request was refused outright, so retrying it cannot
    duplicate anything.
    """
    version = _version()
    for _ in range(18):
        entetes = dict(kw.pop('headers', {}) or {})
        entetes['LinkedIn-Version'] = version
        r = requests.request(methode, url, headers=entetes, **kw)
        if r.status_code != 426 or 'NONEXISTENT_VERSION' not in (r.text or ''):
            if version != (get_config(KEY_VERSION) or ''):
                set_config(KEY_VERSION, version)
            return r
        precedente = _version_precedente(version)
        if not precedente:
            return r
        log.info('LinkedIn: version %s retirée, essai avec %s', version, precedente)
        version = precedente
        kw['headers'] = entetes
    return r


def has_client_credentials():
    return bool(_client_id() and _client_secret())


def person_urn():
    return (get_config(KEY_PERSON) or '').strip()


def display_name():
    return (get_config(KEY_NAME) or '').strip()


def expires_at():
    brut = (get_config(KEY_EXPIRES) or '').strip()
    if not brut:
        return None
    try:
        return datetime.fromisoformat(brut)
    except ValueError:
        return None


def is_configured():
    """Connected and not expired — what the publish buttons check."""
    fin = expires_at()
    return bool(get_config(KEY_TOKEN) and person_urn()
                and (fin is None or fin > datetime.utcnow()))


def days_left():
    fin = expires_at()
    return None if fin is None else max(0, (fin - datetime.utcnow()).days)


def save_settings(client_id, client_secret, version=None):
    set_config(KEY_CLIENT_ID, (client_id or '').strip())
    set_config(KEY_CLIENT_SECRET, (client_secret or '').strip())
    if version:
        set_config(KEY_VERSION, version.strip())


def disconnect():
    for cle in (KEY_TOKEN, KEY_EXPIRES, KEY_PERSON, KEY_NAME):
        delete_config(cle)


# ─── OAuth ──────────────────────────────────────────────────

def authorization_url(redirect_uri, state):
    from urllib.parse import urlencode
    if not has_client_credentials():
        raise LinkedInError("Renseignez d'abord l'identifiant et le secret LinkedIn.")
    return f'{AUTH_URL}?' + urlencode({
        'response_type': 'code',
        'client_id': _client_id(),
        'redirect_uri': redirect_uri,
        'state': state,
        'scope': SCOPE,
    })


def exchange_code(code, redirect_uri):
    """Swap the callback code for a token, and remember who it belongs to."""
    if not code:
        raise LinkedInError("LinkedIn n'a renvoyé aucun code d'autorisation.")
    r = requests.post(TOKEN_URL, data={
        'grant_type': 'authorization_code', 'code': code,
        'client_id': _client_id(), 'client_secret': _client_secret(),
        'redirect_uri': redirect_uri,
    }, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise LinkedInError(f"LinkedIn a refusé l'autorisation : {r.text[:200]}")
    corps = r.json() or {}
    jeton = corps.get('access_token')
    if not jeton:
        raise LinkedInError("Réponse LinkedIn sans jeton d'accès.")

    set_config(KEY_TOKEN, jeton)
    # Soixante jours en général ; on prend ce que LinkedIn annonce.
    secondes = int(corps.get('expires_in') or 60 * 24 * 3600)
    set_config(KEY_EXPIRES,
               (datetime.utcnow() + timedelta(seconds=secondes)).isoformat(timespec='seconds'))

    infos = _userinfo(jeton)
    set_config(KEY_PERSON, f"urn:li:person:{infos['sub']}")
    set_config(KEY_NAME, infos.get('name') or '')


def _userinfo(token):
    r = requests.get(f'{API}/v2/userinfo',
                     headers={'Authorization': f'Bearer {token}'}, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise LinkedInError(f"Profil LinkedIn illisible : {r.text[:200]}")
    corps = r.json() or {}
    if not corps.get('sub'):
        raise LinkedInError("LinkedIn n'a pas renvoyé d'identifiant de membre.")
    return corps


def _headers(json_body=True):
    jeton = (get_config(KEY_TOKEN) or '').strip()
    if not jeton:
        raise LinkedInAuthError("LinkedIn n'est pas connecté.")
    fin = expires_at()
    if fin and fin <= datetime.utcnow():
        raise LinkedInAuthError(
            "L'autorisation LinkedIn a expiré — reconnectez le compte.")
    h = {
        'Authorization': f'Bearer {jeton}',
        'LinkedIn-Version': _version(),
        'X-Restli-Protocol-Version': '2.0.0',
    }
    if json_body:
        h['Content-Type'] = 'application/json'
    return h


def verify_credentials():
    """Non-destructive check that the connection still works."""
    try:
        jeton = (get_config(KEY_TOKEN) or '').strip()
        if not jeton:
            return False, "LinkedIn n'est pas connecté."
        infos = _userinfo(jeton)
        reste = days_left()
        suffixe = f" — autorisation valable encore {reste} jour(s)." if reste is not None else ''
        return True, f"Connecté en tant que {infos.get('name') or 'ce compte'}{suffixe}"
    except LinkedInError as exc:
        return False, str(exc)


# ─── Publication ────────────────────────────────────────────

def _post_url(urn):
    return f'https://www.linkedin.com/feed/update/{urn}/' if urn else None


def _publier(commentary, media=None):
    """Send one post. Returns (ok, urn_or_error).

    Mirrors `x.post_tweet`'s contract so the callers already written for X can
    treat both the same way.
    """
    corps = {
        'author': person_urn(),
        'commentary': commentary,
        'visibility': 'PUBLIC',
        'distribution': {
            'feedDistribution': 'MAIN_FEED',
            'targetEntities': [],
            'thirdPartyDistributionChannels': [],
        },
        'lifecycleState': 'PUBLISHED',
        'isReshareDisabledByAuthor': False,
    }
    if media:
        corps['content'] = {'media': media}

    r = _appeler('POST', f'{API}/rest/posts', headers=_headers(),
                 data=json.dumps(corps), timeout=_TIMEOUT)
    if r.status_code not in (200, 201):
        return False, f"LinkedIn a refusé la publication ({r.status_code}) : {r.text[:200]}"
    # L'identifiant du post arrive dans un en-tête, pas dans le corps.
    urn = r.headers.get('x-restli-id') or (r.json() or {}).get('id') if r.content else r.headers.get('x-restli-id')
    return True, urn


def post_text(text):
    """Publish a plain text post."""
    texte = (text or '').strip()
    if not texte:
        return False, "Rien à publier."
    return _publier(texte[:POST_LIMIT])


def post_video(path, text, titre=None):
    """Publish a video post: register the upload, send the bytes, then post.

    LinkedIn hands back a list of byte ranges to upload separately and expects
    the ETag of each part back at the end — the same shape as X's chunked
    upload, with different names.
    """
    taille = os.path.getsize(path)
    init = _appeler(
        'POST', f'{API}/rest/videos?action=initializeUpload', headers=_headers(),
        data=json.dumps({'initializeUploadRequest': {
            'owner': person_urn(), 'fileSizeBytes': taille,
            'uploadCaptions': False, 'uploadThumbnail': False,
        }}), timeout=_TIMEOUT)
    if init.status_code not in (200, 201):
        return False, f"Envoi vidéo refusé ({init.status_code}) : {init.text[:200]}"

    valeur = (init.json() or {}).get('value') or {}
    video_urn = valeur.get('video')
    instructions = valeur.get('uploadInstructions') or []
    if not video_urn or not instructions:
        return False, "LinkedIn n'a pas indiqué où envoyer la vidéo."

    etags = []
    with open(path, 'rb') as fh:
        for ins in instructions:
            debut, fin = int(ins.get('firstByte', 0)), int(ins.get('lastByte', taille - 1))
            fh.seek(debut)
            morceau = fh.read(fin - debut + 1)
            up = requests.put(ins['uploadUrl'], data=morceau,
                              headers={'Authorization': _headers(False)['Authorization']},
                              timeout=300)
            if up.status_code not in (200, 201):
                return False, f"Envoi d'un fragment refusé ({up.status_code})."
            etag = up.headers.get('etag') or up.headers.get('ETag')
            if not etag:
                return False, "LinkedIn n'a pas confirmé la réception d'un fragment."
            etags.append(etag.strip('"'))

    fin_up = _appeler(
        'POST', f'{API}/rest/videos?action=finalizeUpload', headers=_headers(),
        data=json.dumps({'finalizeUploadRequest': {
            'video': video_urn, 'uploadToken': '', 'uploadedPartIds': etags,
        }}), timeout=_TIMEOUT)
    if fin_up.status_code not in (200, 201):
        return False, f"Finalisation refusée ({fin_up.status_code}) : {fin_up.text[:200]}"

    return _publier((text or '').strip()[:POST_LIMIT],
                    media={'id': video_urn, 'title': (titre or '')[:200] or None})


def share_url(url, source='linkedin'):
    """Tag a link so the visits it brings can be told apart."""
    from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q['utm_source'] = source
    return urlunparse(parts._replace(query=urlencode(q)))


# ─── Publication automatique ────────────────────────────────
#
# Le même principe que pour X : un article par jour, le plus ancien jamais
# publié, pour écouler l'arriéré sans le déverser d'un coup.

def _paris_day(dt_utc):
    """The Paris calendar day a UTC moment falls in."""
    from zoneinfo import ZoneInfo
    return dt_utc.replace(tzinfo=ZoneInfo('UTC')).astimezone(
        ZoneInfo('Europe/Paris')).date()


def last_post_at():
    """When anything was last shared on LinkedIn, automatic or by hand."""
    from sqlalchemy import func
    from articles.models import Article
    from init_db import db
    return db.session.query(func.max(Article.linkedin_posted_at)).scalar()


def already_posted_today():
    """True when something already went out today, Paris time.

    A calendar day, not a rolling window: measured from the last post rather
    than from the schedule, a window lets a share made later in the day push
    the next run under the threshold and silently cost a day.
    """
    dernier = last_post_at()
    return dernier is not None and _paris_day(dernier) == _paris_day(datetime.utcnow())


def next_article_to_post():
    """The oldest published article never shared on LinkedIn."""
    from articles.models import Article
    return (
        Article.query
        .filter(Article.published == True,  # noqa: E712
                Article.linkedin_posted_at.is_(None))
        .order_by(Article.created_at.asc())
        .first()
    )


def compose_article_post(article, url):
    """The text of an article's post.

    LinkedIn allows three thousand characters, so unlike X there is nothing to
    compress: the title, the editorial summary, and the link.
    """
    resume = (article.summary or '').strip()
    return f"{article.title}\n\n{resume}\n\n{url}" if resume else f"{article.title}\n\n{url}"


def auto_post():
    """Share the oldest never-posted article, unless one already went out
    today. Returns a dict whose `status` is one of posted / too_soon /
    nothing_to_post / not_configured / failed."""
    from flask import url_for
    from init_db import db

    if not is_configured():
        return {'status': 'not_configured'}
    if already_posted_today():
        dernier = last_post_at()
        return {'status': 'too_soon', 'last': dernier,
                'elapsed': datetime.utcnow() - dernier}

    article = next_article_to_post()
    if article is None:
        return {'status': 'nothing_to_post'}

    # url_for(_external=True) hors requête : le contexte est fourni par
    # l'appelant, comme pour X.
    url = share_url(url_for('articles.public_show', slug=article.slug, _external=True))
    texte = compose_article_post(article, url)

    ok, detail = post_text(texte)
    if not ok:
        log.error("auto_post LinkedIn échoué pour l'article %s : %s", article.id, detail)
        return {'status': 'failed', 'article': article, 'detail': detail}

    article.linkedin_post_id = str(detail) if detail else None
    article.linkedin_posted_at = datetime.utcnow()
    db.session.commit()
    log.info("auto_post LinkedIn : article %s partagé", article.id)
    return {'status': 'posted', 'article': article, 'text': texte}


# ─── Écran d'administration ─────────────────────────────────

from flask import Blueprint, render_template  # noqa: E402

from auth import admin_required  # noqa: E402

admin_linkedin_bp = Blueprint('admin_linkedin', __name__,
                              url_prefix='/admin/linkedin',
                              template_folder='templates')


@admin_linkedin_bp.route('/')
@admin_required
def index():
    """Ce qui est parti sur LinkedIn, et l'état de l'autorisation.

    Pas de chiffres : LinkedIn n'en publie aucun pour les publications d'un
    membre. La page dit donc ce qui a été publié et quand, et surveille la date
    d'expiration — la seule chose qui puisse arrêter les publications sans
    prévenir.
    """
    from articles.models import Article
    from tiktok.models import TikTokPost

    articles = (Article.query.filter(Article.linkedin_posted_at.isnot(None))
                .order_by(Article.linkedin_posted_at.desc()).all())
    clips = (TikTokPost.query.filter(TikTokPost.linkedin_post_id.isnot(None))
             .order_by(TikTokPost.linkedin_posted_at.desc()).all())
    lignes = [{'kind': 'article', 'title': a.title, 'at': a.linkedin_posted_at,
               'url': a.linkedin_post_url} for a in articles]
    lignes += [{'kind': 'video', 'title': c.title, 'at': c.linkedin_posted_at,
                'url': c.linkedin_url} for c in clips]
    lignes.sort(key=lambda l: l['at'] or datetime.min, reverse=True)

    return render_template(
        'linkedin_admin.html',
        lignes=lignes,
        connected=is_configured(),
        nom=display_name(),
        jours=days_left(),
        expire=expires_at(),
        attente=Article.query.filter(Article.published == True,  # noqa: E712
                                     Article.linkedin_posted_at.is_(None)).count(),
        prochain=next_article_to_post(),
        aujourdhui=already_posted_today(),
    )
