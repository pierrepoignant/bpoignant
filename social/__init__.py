"""Commentaires YouTube, Page Facebook et Instagram : les lire par l'API, faire
proposer une réponse par le moteur, et répondre d'un clic.

Même moteur que pour TikTok (même voix, mêmes règles : bref, de l'humour, sans
emoji, on ne répond ni au haineux ni à la polémique), à une différence près :
ici la réponse part réellement par l'API, sous le nom de Bernard, au lieu d'être
copiée à la main.

La page ne montre que les commentaires neufs pour lesquels le moteur propose une
réponse, regroupés par publication. Trois gestes : écarter, répondre avec la
proposition, ou modifier avant de répondre.
"""

import logging
import threading
from datetime import datetime

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for, jsonify)

from init_db import db
from auth import admin_required
from social.models import SocialComment

log = logging.getLogger(__name__)

admin_social_bp = Blueprint('admin_social', __name__,
                            url_prefix='/admin/commentaires-reseaux',
                            template_folder='templates')


# ─── Lecture (API) ──────────────────────────────────────────

def _as_dt(valeur):
    """Les dates arrivent en datetime (Meta, déjà converti) ou en chaîne ISO
    (YouTube : publishedAt). MySQL n'avale pas la chaîne, on la convertit."""
    if valeur is None or isinstance(valeur, datetime):
        return valeur
    try:
        return datetime.fromisoformat(str(valeur).replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _ingest(network, object_id, items, post=None, article=None):
    """Écrire les commentaires lus. Les nouveaux arrivent en statut « new » ;
    un déjà connu voit seulement ses j'aime rafraîchis — on ne ressuscite pas
    un commentaire écarté ou déjà répondu."""
    maintenant = datetime.utcnow()
    nouveaux = 0
    for it in items or []:
        eid = (it.get('external_id') or '').strip()
        if not eid or not (it.get('text') or '').strip():
            continue
        row = SocialComment.query.filter_by(network=network, external_id=eid).first()
        if row is not None:
            if it.get('likes') is not None:
                row.likes = it['likes']
            row.scraped_at = maintenant
            continue
        db.session.add(SocialComment(
            network=network, external_id=eid, network_object_id=object_id,
            post_id=(post.id if post else None),
            article_id=(article.id if article else None),
            author=(it.get('author') or None), author_id=(it.get('author_id') or None),
            text=(it.get('text') or '')[:4000], likes=it.get('likes'),
            posted_at=_as_dt(it.get('posted_at')), scraped_at=maintenant, status='new'))
        nouveaux += 1
    db.session.commit()
    return nouveaux


def fetch_all(log_fn=print):
    """Lire les commentaires de toutes les publications en ligne, tous réseaux."""
    import youtube
    import meta
    from tiktok.models import TikTokPost
    from articles.models import Article
    total = 0

    videos = TikTokPost.query.filter(TikTokPost.video_url.isnot(None)).all()
    yt_ok = youtube.is_connected()
    meta_ok = meta.is_connected()
    ig_ok = meta.instagram_ready()
    for v in videos:
        if yt_ok and v.youtube_video_id:
            try:
                total += _ingest('youtube', v.youtube_video_id, youtube.fetch_comments(v.youtube_video_id), post=v)
            except Exception as exc:  # noqa: BLE001
                log_fn(f"YouTube {v.id} : {exc}")
        if meta_ok and v.fb_post_id:
            total += _ingest('facebook', v.fb_post_id, meta.fetch_facebook_comments(v.fb_post_id), post=v)
        if ig_ok and v.instagram_media_id:
            total += _ingest('instagram', v.instagram_media_id, meta.fetch_instagram_comments(v.instagram_media_id), post=v)

    if meta_ok:
        for a in Article.query.filter(Article.fb_page_post_id.isnot(None)).all():
            total += _ingest('facebook', a.fb_page_post_id, meta.fetch_facebook_comments(a.fb_page_post_id), article=a)

    log_fn(f"{total} nouveau(x) commentaire(s) lu(s).")
    return total


# ─── Proposition (IA) ───────────────────────────────────────

def _contexte(comment):
    """Ce que le moteur doit savoir de la publication pour répondre dessous."""
    if comment.post is not None:
        from tiktok.comments import _contexte_video
        return _contexte_video(comment.post)
    if comment.article is not None:
        a = comment.article
        bouts = [f"Titre de l'article : {a.title}"]
        if a.summary:
            bouts.append(f"Résumé : {a.summary[:1200]}")
        return '\n'.join(bouts)
    return "Publication de Bernard Poignant."


def _cle_publication(comment):
    return ('post', comment.post_id) if comment.post_id else ('article', comment.article_id)


def draft_pending(log_fn=print):
    """Faire proposer une réponse aux commentaires neufs qui n'en ont pas encore.

    Regroupés par publication pour donner le contexte au moteur, et traités avec
    la même voix et les mêmes règles que TikTok."""
    from articles.ai_summary import _api_key, MODEL
    from tiktok.comments import VOIX, _tableau_json, LOT_IA
    import re
    key = _api_key()
    if not key:
        log_fn("Clé Anthropic absente : aucune proposition.")
        return 0

    voix = VOIX.replace('sous ses vidéos TikTok',
                        'sous ses vidéos et articles sur YouTube, Facebook et Instagram')

    attente = (SocialComment.query
               .filter(SocialComment.status == 'new', SocialComment.suggested_at.is_(None))
               .all())
    attente = [c for c in attente if (c.text or '').strip()]
    if not attente:
        log_fn("Rien à proposer.")
        return 0

    groupes = {}
    for c in attente:
        groupes.setdefault(_cle_publication(c), []).append(c)

    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=90.0, max_retries=1)
    traites = 0
    for _, comments in groupes.items():
        contexte = _contexte(comments[0])
        for debut in range(0, len(comments), LOT_IA):
            lot = comments[debut:debut + LOT_IA]
            liste = '\n'.join(
                f"- id {c.external_id} · {c.network_label} · @{c.author or 'inconnu'} : {c.text.strip()[:600]}"
                for c in lot)
            try:
                reponse = client.messages.create(
                    model=MODEL, max_tokens=4000,
                    system=[{'type': 'text', 'text': voix, 'cache_control': {'type': 'ephemeral'}}],
                    messages=[{'role': 'user', 'content': f"{contexte}\n\nCommentaires :\n{liste}"}])
                texte = ''.join(b.text for b in reponse.content if b.type == 'text')
                par_id = {str(o.get('id', '')).strip(): o for o in _tableau_json(texte) if isinstance(o, dict)}
            except Exception as exc:  # noqa: BLE001
                log_fn(f"Proposition impossible pour un lot : {exc}")
                continue
            maintenant = datetime.utcnow()
            for c in lot:
                o = par_id.get(c.external_id)
                if o is None:
                    c.suggestion_note = 'non traité par le modèle'
                    c.suggested_reply = None
                elif o.get('repondre'):
                    c.suggested_reply = re.sub(r'\s+', ' ', str(o.get('reponse') or '')).strip()[:600] or None
                    c.suggestion_note = str(o.get('note') or '')[:200] or None
                else:
                    c.suggested_reply = None
                    c.suggestion_note = (str(o.get('note') or '') or 'sans réponse')[:200]
                c.suggested_at = maintenant
                traites += 1
            db.session.commit()
    log_fn(f"{traites} commentaire(s) relu(s) par le moteur.")
    return traites


# ─── Réponse (API) ──────────────────────────────────────────

def post_reply(comment, texte=None):
    """Publier la réponse sur le bon réseau. Renvoie (ok, detail)."""
    texte = (texte or comment.suggested_reply or '').strip()
    if not texte:
        return False, "réponse vide"
    if comment.status == 'replied':
        return False, "déjà répondu"
    if comment.network == 'youtube':
        import youtube
        ok, detail = youtube.reply_to_comment(comment.external_id, texte)
    elif comment.network == 'facebook':
        import meta
        ok, detail = meta.reply_facebook_comment(comment.external_id, texte)
    elif comment.network == 'instagram':
        import meta
        ok, detail = meta.reply_instagram_comment(comment.external_id, texte)
    else:
        return False, f"réseau inconnu : {comment.network}"
    if ok:
        comment.status = 'replied'
        comment.reply_external_id = str(detail)
        comment.replied_at = datetime.utcnow()
        if texte != (comment.suggested_reply or ''):
            comment.suggested_reply = texte
        db.session.commit()
        return ok, detail
    # Le commentaire n'existe plus (supprimé par son auteur ou par la modération) :
    # on ne peut pas y répondre. On le sort de la file plutôt que de buter dessus.
    bas = str(detail).lower()
    if ('does not exist' in bas or 'cannot be loaded' in bas or 'supprimé' in bas
            or "n'existe plus" in bas or 'deleted' in bas):
        comment.status = 'discarded'
        db.session.commit()
        return False, "Ce commentaire n'existe plus (supprimé) — il a été retiré de la liste."
    return ok, detail


# ─── Rafraîchissement en arrière-plan ───────────────────────

_ETAT = {'en_cours': False, 'fini': None, 'message': ''}


def etat_global():
    return dict(_ETAT)


def refresh_all(app):
    """Lire puis proposer, en arrière-plan. Un seul à la fois."""
    if _ETAT['en_cours']:
        return False
    _ETAT.update(en_cours=True, fini=None, message='Lecture des commentaires…')

    def _travail():
        with app.app_context():
            with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
                try:
                    lus = fetch_all(lambda m: _ETAT.update(message=m))
                    _ETAT['message'] = 'Rédaction des propositions…'
                    proposes = draft_pending(lambda m: _ETAT.update(message=m))
                    _ETAT['message'] = f"{lus} commentaire(s) lu(s), {proposes} relu(s)."
                except Exception as exc:  # noqa: BLE001
                    log.exception('social refresh')
                    _ETAT['message'] = f"Échec : {exc}"
                finally:
                    _ETAT.update(en_cours=False, fini=datetime.utcnow().isoformat())

    threading.Thread(target=_travail, daemon=True).start()
    return True


# ─── À traiter : neufs avec une réponse proposée ────────────

def a_traiter():
    """Les commentaires neufs pour lesquels le moteur propose une réponse,
    regroupés par publication, chaque groupe trié du plus récent au plus ancien."""
    lignes = (SocialComment.query
              .filter(SocialComment.status == 'new',
                      SocialComment.suggested_reply.isnot(None))
              .order_by(SocialComment.posted_at.desc(), SocialComment.id.desc())
              .all())
    groupes = {}
    for c in lignes:
        cle = _cle_publication(c)
        groupes.setdefault(cle, {'comments': [], 'titre': _titre(c), 'networks': set()})
        groupes[cle]['comments'].append(c)
        groupes[cle]['networks'].add(c.network)
    # Trier les publications par le commentaire le plus récent.
    ordonne = sorted(groupes.values(),
                     key=lambda g: g['comments'][0].posted_at or datetime.min, reverse=True)
    return ordonne


def _titre(comment):
    if comment.post is not None:
        return comment.post.title or 'Vidéo'
    if comment.article is not None:
        return comment.article.title or 'Article'
    return 'Publication'


# ─── Écran ──────────────────────────────────────────────────

def _resume_reseaux():
    """Par réseau : combien lus, combien à traiter (réponse proposée), combien
    écartés par le moteur, combien déjà répondus. Pour montrer qu'un réseau est
    bien lu même quand rien n'appelle de réponse."""
    from sqlalchemy import func, case
    lignes = (db.session.query(
        SocialComment.network,
        func.count(SocialComment.id),
        func.sum(case((db.and_(SocialComment.status == 'new',
                               SocialComment.suggested_reply.isnot(None)), 1), else_=0)),
        func.sum(case((db.and_(SocialComment.status == 'new',
                               SocialComment.suggested_reply.is_(None)), 1), else_=0)),
        func.sum(case((SocialComment.status == 'replied', 1), else_=0)),
    ).group_by(SocialComment.network).all())
    etiquettes = {'youtube': 'YouTube', 'facebook': 'Facebook', 'instagram': 'Instagram'}
    return [{'reseau': etiquettes.get(r[0], r[0]), 'lus': r[1],
             'a_traiter': int(r[2] or 0), 'ecartes': int(r[3] or 0),
             'repondus': int(r[4] or 0)} for r in lignes]


@admin_social_bp.route('/')
@admin_required
def index():
    import youtube
    import meta
    en_attente = SocialComment.query.filter_by(status='new').count()
    return render_template(
        'social_comments.html',
        publications=a_traiter(),
        etat=etat_global(),
        total_neufs=en_attente,
        resume=_resume_reseaux(),
        repondus=SocialComment.query.filter_by(status='replied').count(),
        youtube_ok=youtube.is_connected(),
        meta_ok=meta.is_connected(),
        ig_ok=meta.instagram_ready(),
    )


@admin_social_bp.route('/refresh', methods=['POST'])
@admin_required
def refresh():
    from flask import current_app
    if not refresh_all(current_app._get_current_object()):
        flash("Une lecture est déjà en cours.", 'info')
    else:
        flash("Lecture des commentaires lancée. La page se met à jour toute seule.", 'info')
    return redirect(url_for('admin_social.index'))


@admin_social_bp.route('/etat')
@admin_required
def etat():
    return jsonify(etat_global())


@admin_social_bp.route('/<int:comment_id>/discard', methods=['POST'])
@admin_required
def discard(comment_id):
    c = db.session.get(SocialComment, comment_id) or _404()
    c.status = 'discarded'
    db.session.commit()
    flash("Commentaire écarté.", 'info')
    return redirect(url_for('admin_social.index'))


@admin_social_bp.route('/<int:comment_id>/reply', methods=['POST'])
@admin_required
def reply(comment_id):
    c = db.session.get(SocialComment, comment_id) or _404()
    texte = (request.form.get('text') or '').strip() or None
    ok, detail = post_reply(c, texte)
    flash(f"Réponse publiée sur {c.network_label}." if ok else f"Échec : {detail}",
          'success' if ok else 'danger')
    return redirect(url_for('admin_social.index'))


def _404():
    from flask import abort
    abort(404)
