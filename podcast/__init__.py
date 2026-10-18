"""Le podcast : chaque article lu par la voix de Bernard (clonée via ElevenLabs),
hébergé en MP3 et diffusé par un flux RSS que les plateformes (Apple Podcasts,
Spotify) viennent lire.

On synthétise le texte de l'article, on range le MP3 dans le stockage public,
et le flux /podcast.xml liste les épisodes. Il suffit de soumettre ce flux une
fois à chaque plateforme : les nouveaux épisodes y apparaissent ensuite seuls.
"""

import logging
import re
import threading
from datetime import datetime
from email.utils import format_datetime

import requests
from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for, jsonify, abort)

from init_db import db
from auth import admin_required
from settings.models import get_config, set_config, delete_config
from articles.models import Article

log = logging.getLogger(__name__)

API = 'https://api.elevenlabs.io/v1'
MODELE = 'eleven_multilingual_v2'   # bon en français
LIMITE_CHUNK = 2400                 # caractères par requête TTS, sur frontière de phrase

KEY_API = 'elevenlabs_api_key'
KEY_VOICE = 'elevenlabs_voice_id'
KEY_TITRE = 'podcast_titre'
KEY_DESC = 'podcast_description'
KEY_IMAGE = 'podcast_image_url'
KEY_AUTEUR = 'podcast_auteur'
KEY_EMAIL = 'podcast_email'

admin_podcast_bp = Blueprint('admin_podcast', __name__, url_prefix='/admin/podcast',
                             template_folder='templates')
public_podcast_bp = Blueprint('podcast', __name__)


# ─── Réglages ───────────────────────────────────────────────

def api_key():
    import os
    return (get_config(KEY_API) or os.environ.get('ELEVENLABS_API_KEY') or '').strip()


def voice_id():
    import os
    return (get_config(KEY_VOICE) or os.environ.get('ELEVENLABS_VOICE_ID') or '').strip()


def is_configured():
    return bool(api_key() and voice_id())


def meta_podcast():
    return {
        'titre': (get_config(KEY_TITRE) or 'Bernard Poignant — les chroniques').strip(),
        'description': (get_config(KEY_DESC) or "Les chroniques de Bernard Poignant, lues.").strip(),
        'image': (get_config(KEY_IMAGE) or '').strip(),
        'auteur': (get_config(KEY_AUTEUR) or 'Bernard Poignant').strip(),
        'email': (get_config(KEY_EMAIL) or '').strip(),
    }


# ─── Synthèse ───────────────────────────────────────────────

class PodcastError(RuntimeError):
    pass


def _texte_article(article):
    """Le texte à lire : le titre, puis le corps sans balises, en phrases
    propres. On remet des points pour que la voix respire aux bons endroits."""
    corps = re.sub(r'<(p|br|/p|h\d|/h\d)[^>]*>', '. ', article.content_html or '', flags=re.I)
    corps = re.sub(r'<[^>]+>', ' ', corps)
    import html as _html
    corps = _html.unescape(corps)
    corps = re.sub(r'\s+', ' ', corps).strip()
    corps = re.sub(r'(\.\s*){2,}', '. ', corps)
    titre = (article.title or '').strip()
    return f"{titre}.\n\n{corps}" if titre else corps


def _chunks(texte):
    """Découper en morceaux <= LIMITE_CHUNK, sur une fin de phrase."""
    morceaux, courant = [], ''
    for phrase in re.split(r'(?<=[.!?…])\s+', texte):
        if len(courant) + len(phrase) + 1 > LIMITE_CHUNK and courant:
            morceaux.append(courant.strip())
            courant = ''
        courant += phrase + ' '
    if courant.strip():
        morceaux.append(courant.strip())
    return morceaux


def _tts(texte):
    """Synthétiser un morceau de texte en MP3 (bytes)."""
    r = requests.post(
        f'{API}/text-to-speech/{voice_id()}',
        headers={'xi-api-key': api_key(), 'Content-Type': 'application/json',
                 'Accept': 'audio/mpeg'},
        params={'output_format': 'mp3_44100_128'},
        json={'text': texte, 'model_id': MODELE,
              'voice_settings': {'stability': 0.5, 'similarity_boost': 0.8}},
        timeout=180)
    if r.status_code != 200:
        raise PodcastError(f"ElevenLabs a répondu {r.status_code} : {r.text[:200]}")
    return r.content


def synthetiser(texte):
    """Texte entier → MP3 (bytes), morceau par morceau recollé."""
    bouts = [_tts(c) for c in _chunks(texte) if c.strip()]
    return b''.join(bouts)


def generer_article(article):
    """Générer (ou régénérer) l'audio d'un article et le publier. Renvoie l'URL."""
    import storage
    if not is_configured():
        raise PodcastError("ElevenLabs n'est pas configuré (clé + voix).")
    if not storage.is_configured():
        raise PodcastError("Le stockage n'est pas configuré.")
    texte = _texte_article(article)
    if not texte.strip():
        raise PodcastError("Article vide.")
    mp3 = synthetiser(texte)
    if not mp3:
        raise PodcastError("Aucun son produit.")
    url = storage.put_public_file(f'podcast/{article.slug}.mp3', mp3, 'audio/mpeg')
    article.audio_url = url
    article.audio_bytes = len(mp3)
    # Durée estimée à 150 mots/minute — suffisant pour l'affichage et le flux.
    mots = len(texte.split())
    article.audio_duration = max(1, round(mots / 150 * 60))
    article.audio_generated_at = datetime.utcnow()
    db.session.commit()
    return url


# ─── Lot en arrière-plan ────────────────────────────────────

_ETAT = {'en_cours': False, 'fini': None, 'message': '', 'faits': 0, 'total': 0}


def etat():
    return dict(_ETAT)


def _a_generer():
    return (Article.query
            .filter(Article.published.is_(True), Article.audio_url.is_(None))
            .order_by(Article.created_at.desc()).all())


def generer_tout(app):
    if _ETAT['en_cours']:
        return False
    _ETAT.update(en_cours=True, fini=None, message='Préparation…', faits=0, total=0)

    def _travail():
        with app.app_context():
            with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
                try:
                    ids = [a.id for a in _a_generer()]
                    _ETAT['total'] = len(ids)
                    for i, aid in enumerate(ids, 1):
                        a = db.session.get(Article, aid)
                        if not a:
                            continue
                        _ETAT['message'] = f"{i}/{len(ids)} : {(a.title or '')[:50]}"
                        try:
                            generer_article(a)
                        except Exception as exc:  # noqa: BLE001
                            log.exception('audio article %s', aid)
                            db.session.rollback()
                            _ETAT['message'] = f"Erreur sur « {(a.title or '')[:40]} » : {exc}"
                        _ETAT['faits'] = i
                    _ETAT['message'] = f"{_ETAT['faits']} épisode(s) généré(s)."
                except Exception as exc:  # noqa: BLE001
                    _ETAT['message'] = f"Échec : {exc}"
                finally:
                    _ETAT.update(en_cours=False, fini=datetime.utcnow().isoformat())

    threading.Thread(target=_travail, daemon=True).start()
    return True


def backfill_recent(jours=21, limit=3, log_fn=print):
    """Générer l'audio des articles récents publiés qui n'en ont pas — pour que
    les nouveaux articles rejoignent le podcast tout seuls, sans vider d'un coup
    (ni facturer) tout l'arriéré, qui reste au bouton « Tout générer »."""
    from datetime import timedelta
    if not is_configured():
        log_fn("ElevenLabs n'est pas configuré — rien à faire.")
        return 0
    depuis = datetime.utcnow() - timedelta(days=jours)
    arts = (Article.query
            .filter(Article.published.is_(True), Article.audio_url.is_(None),
                    Article.created_at >= depuis)
            .order_by(Article.created_at.desc()).limit(limit).all())
    faits = 0
    for a in arts:
        try:
            generer_article(a)
            faits += 1
            log_fn(f"audio : {a.title[:50]}")
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log_fn(f"échec {a.title[:40]} : {exc}")
    log_fn(f"{faits} épisode(s) généré(s).")
    return faits


# ─── Flux RSS ───────────────────────────────────────────────

def _episodes():
    return (Article.query
            .filter(Article.published.is_(True), Article.audio_url.isnot(None))
            .order_by(Article.created_at.desc()).all())


@public_podcast_bp.route('/podcast.xml')
def feed():
    from xml.sax.saxutils import escape
    m = meta_podcast()
    lien = url_for('articles.public_list', _external=True)
    items = []
    for a in _episodes():
        pub = format_datetime(a.created_at) if a.created_at else format_datetime(datetime.utcnow())
        resume = escape((a.summary or '').strip())
        duree = a.audio_duration or 0
        hhmmss = f"{duree // 3600:02d}:{(duree % 3600) // 60:02d}:{duree % 60:02d}"
        lien_ep = url_for('articles.public_show', slug=a.slug, _external=True)
        items.append(f"""    <item>
      <title>{escape(a.title or 'Chronique')}</title>
      <description>{resume}</description>
      <link>{escape(lien_ep)}</link>
      <guid isPermaLink="false">bpoignant-article-{a.id}</guid>
      <pubDate>{pub}</pubDate>
      <enclosure url="{escape(a.audio_url)}" length="{a.audio_bytes or 0}" type="audio/mpeg"/>
      <itunes:duration>{hhmmss}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    image = (f'<itunes:image href="{escape(m["image"])}"/>'
             f'<image><url>{escape(m["image"])}</url><title>{escape(m["titre"])}</title>'
             f'<link>{escape(lien)}</link></image>') if m['image'] else ''
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{escape(m['titre'])}</title>
    <link>{escape(lien)}</link>
    <language>fr-fr</language>
    <description>{escape(m['description'])}</description>
    <itunes:author>{escape(m['auteur'])}</itunes:author>
    <itunes:summary>{escape(m['description'])}</itunes:summary>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="News"/>
    {f'<itunes:owner><itunes:name>{escape(m["auteur"])}</itunes:name><itunes:email>{escape(m["email"])}</itunes:email></itunes:owner>' if m['email'] else ''}
    {image}
{chr(10).join(items)}
  </channel>
</rss>"""
    return Response(xml, mimetype='application/rss+xml')


# ─── Écran d'administration ─────────────────────────────────

@admin_podcast_bp.route('/')
@admin_required
def index():
    arts = Article.query.filter(Article.published.is_(True)).order_by(Article.created_at.desc()).all()
    return render_template('podcast_admin.html', articles=arts, etat=etat(),
                           configure=is_configured(), meta=meta_podcast(),
                           voice_id=voice_id(),
                           a_generer=sum(1 for a in arts if not a.audio_url),
                           feed_url=url_for('podcast.feed', _external=True))


@admin_podcast_bp.route('/reglages', methods=['POST'])
@admin_required
def reglages():
    f = request.form
    if f.get('api_key', '').strip():
        set_config(KEY_API, f['api_key'].strip())
    for cle, champ in [(KEY_VOICE, 'voice_id'), (KEY_TITRE, 'titre'),
                       (KEY_DESC, 'description'), (KEY_IMAGE, 'image_url'),
                       (KEY_AUTEUR, 'auteur'), (KEY_EMAIL, 'email')]:
        if champ in f:
            set_config(cle, (f.get(champ) or '').strip())
    flash("Réglages podcast enregistrés.", 'success')
    return redirect(url_for('admin_podcast.index'))


@admin_podcast_bp.route('/article/<int:article_id>/generer', methods=['POST'])
@admin_required
def generer(article_id):
    a = db.session.get(Article, article_id) or abort(404)
    try:
        generer_article(a)
        flash(f"Audio généré pour « {(a.title or '')[:50]} ».", 'success')
    except Exception as exc:  # noqa: BLE001
        flash(f"Génération impossible : {exc}", 'danger')
    return redirect(request.referrer or url_for('admin_podcast.index'))


@admin_podcast_bp.route('/tout-generer', methods=['POST'])
@admin_required
def tout_generer():
    from flask import current_app
    if not generer_tout(current_app._get_current_object()):
        flash("Une génération est déjà en cours.", 'info')
    else:
        flash("Génération lancée. La page se met à jour toute seule.", 'info')
    return redirect(url_for('admin_podcast.index'))


@admin_podcast_bp.route('/etat')
@admin_required
def etat_route():
    return jsonify(etat())
