"""Veille : chaque soir, on lit l'actualité politique française, on la rapproche
de ce que Bernard a déjà écrit (articles + chroniques du livre) et de ses thèmes,
et on propose des sujets de « Minute » dans son ton — avec les liens d'actualité
et l'écho de ses propres textes. Les suggestions sont stockées et envoyées par
e-mail aux administrateurs.

Source d'actualité : un acteur Apify Google News si `apify_actor_news` est
renseigné (Réglages), sinon le flux RSS public de Google News FR (gratuit,
fiable). Rédaction : les prompts et le modèle de bernardpoignant (Claude).
"""

import json
import logging
import re
import threading
from datetime import date, datetime

import requests
from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, url_for)

from init_db import db
from auth import admin_required
from settings.models import get_config
from news.models import NewsSuggestion

log = logging.getLogger(__name__)

admin_news_bp = Blueprint('admin_news', __name__, url_prefix='/admin/veille',
                          template_folder='templates')

KEY_QUERIES = 'news_queries'
KEY_ACTOR_NEWS = 'apify_actor_news'   # optionnel : acteur Apify Google News
KEY_MAX = 'news_max_suggestions'

_QUERIES_DEFAUT = [
    'politique France', 'gauche socialisme', 'extrême droite Rassemblement national',
    'Mélenchon', 'Union européenne France', 'laïcité République',
    'présidentielle 2027', 'retraites budget France',
]

# Le ton de Bernard, résumé pour guider la rédaction.
_STYLE = (
    "Bernard Poignant : socialiste, ancien maire de Quimper, ancien député "
    "(Assemblée nationale et Parlement européen), ancien conseiller de François "
    "Hollande, professeur d'histoire. Ton : mesuré, lucide, républicain, "
    "parfois tendre souvent critique ; il part d'un fait, prend du recul "
    "d'historien (une référence historique quand elle éclaire), respecte les "
    "personnes mais critique les idées, et finit sur une formule courte et "
    "mémorable. Jamais vulgaire, jamais donneur de leçons. Il parle à la "
    "première personne, face caméra, en 30 à 40 secondes."
)


# ─── Actualité ──────────────────────────────────────────────

def queries():
    brut = (get_config(KEY_QUERIES) or '').strip()
    if brut:
        return [q.strip() for q in brut.split('\n') if q.strip()] or _QUERIES_DEFAUT
    return _QUERIES_DEFAUT


def max_suggestions():
    try:
        return max(1, min(12, int(get_config(KEY_MAX) or 6)))
    except (TypeError, ValueError):
        return 6


def _via_rss(query, limit):
    """Google News FR par le flux RSS public."""
    import xml.etree.ElementTree as ET
    url = ('https://news.google.com/rss/search'
           f'?q={requests.utils.quote(query)}&hl=fr&gl=FR&ceid=FR:fr')
    r = requests.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.iter('item'):
        titre = (item.findtext('title') or '').strip()
        lien = (item.findtext('link') or '').strip()
        src_el = item.find('source')
        source = (src_el.text.strip() if src_el is not None and src_el.text else '')
        # Le titre Google News finit souvent par « - Source » : on le nettoie.
        if source and titre.endswith(f"- {source}"):
            titre = titre[: -(len(source) + 2)].strip()
        if titre:
            out.append({'titre': titre[:400], 'url': lien[:800],
                        'source': (source or 'Google News')[:160]})
        if len(out) >= limit:
            break
    return out


def _via_apify(query, limit, actor):
    import apify
    items = apify.run_actor({'query': query, 'language': 'FR', 'region': 'FR',
                             'maxItems': limit}, actor_id=actor)
    out = []
    for it in items or []:
        titre = (it.get('title') or it.get('titre') or '').strip()
        lien = (it.get('link') or it.get('url') or '').strip()
        source = (it.get('source') or it.get('publisher') or 'Google News')
        if isinstance(source, dict):
            source = source.get('name') or source.get('title') or 'Google News'
        if titre:
            out.append({'titre': titre[:400], 'url': str(lien)[:800],
                        'source': str(source)[:160]})
    return out


def fetch_news(per_query=8, log_fn=print):
    """Lit l'actualité du jour pour chaque requête. Dé-doublonne par titre."""
    actor = (get_config(KEY_ACTOR_NEWS) or '').strip()
    vus, out = set(), []
    for q in queries():
        try:
            lot = _via_apify(q, per_query, actor) if actor else _via_rss(q, per_query)
        except Exception as exc:  # noqa: BLE001
            log_fn(f"actualité « {q} » indisponible : {exc}")
            lot = []
        for it in lot:
            cle = re.sub(r'\s+', ' ', it['titre'].lower()).strip()
            if cle and cle not in vus:
                vus.add(cle)
                out.append(it)
    log_fn(f"{len(out)} actualité(s) lue(s) sur {len(queries())} requête(s).")
    return out


# ─── Corpus de Bernard ──────────────────────────────────────

def _corpus(limite_articles=70):
    """(thèmes, références) — ce que Bernard a déjà publié, pour l'écho.

    `references` : liste de dicts {titre, resume, url, theme} numérotés par
    l'appelant. On mêle articles publiés et chroniques du livre (titres)."""
    from articles.models import Article, Theme
    from flask import url_for as _uf

    themes = [t.name for t in Theme.query.order_by(Theme.name).all()]
    try:
        from livre.models import BookTheme
        themes += [t.name for t in BookTheme.query.all() if t.name not in themes]
    except Exception:  # noqa: BLE001
        pass

    refs = []
    arts = (Article.query.filter(Article.published.is_(True))
            .order_by(Article.created_at.desc()).limit(limite_articles).all())
    for a in arts:
        try:
            lien = _uf('articles.public_show', slug=a.slug, _external=True)
        except Exception:  # noqa: BLE001
            lien = None
        refs.append({'titre': a.title, 'resume': (a.summary or '')[:220],
                     'url': lien, 'theme': (a.themes[0].name if a.themes else None)})
    # Chroniques du livre : titres seuls (le texte est trop long à embarquer).
    try:
        from livre.models import BookDoc
        for d in (BookDoc.query.filter(BookDoc.in_book.is_(True))
                  .order_by(BookDoc.id.desc()).limit(120).all()):
            if d.titre:
                refs.append({'titre': d.titre, 'resume': '', 'url': None,
                             'theme': (d.theme.name if d.theme else None)})
    except Exception:  # noqa: BLE001
        pass
    return themes, refs


# ─── Rédaction (Claude) ─────────────────────────────────────

_SCHEMA = (
    'Réponds UNIQUEMENT par un tableau JSON. Chaque élément : '
    '{"news_ref":"A","echo_ref":3,"angle":"…","accroche":"…",'
    '"bandeau":"… (MAJUSCULES, ≤52 caractères)","legende":"… + hashtags",'
    '"script":"30-40 s à la première personne, dans son ton",'
    '"pertinence":0-100}. '
    'news_ref = la lettre de l\'actualité ; echo_ref = le numéro du texte passé '
    'qui fait écho (ou null). N\'inclus que les sujets réellement dans ses '
    'thèmes ou déjà traités par lui, du plus pertinent au moins pertinent.'
)


def generer_suggestions(log_fn=print):
    """Lit l'actu, la croise avec le corpus, et stocke les suggestions du jour.
    Renvoie le nombre de suggestions créées."""
    from articles.ai_summary import _api_key, MODEL
    key = _api_key()
    if not key:
        log_fn("Clé Anthropic absente — pas de suggestions.")
        return 0

    news = fetch_news(log_fn=log_fn)
    if not news:
        return 0
    news = news[:40]
    themes, refs = _corpus()

    lettres = [chr(65 + i) for i in range(len(news))]  # A, B, C…
    bloc_news = '\n'.join(f"[{lettres[i]}] {n['titre']} ({n['source']})"
                          for i, n in enumerate(news))
    bloc_refs = '\n'.join(f"[{i + 1}] {r['titre']}" + (f" — {r['resume']}" if r['resume'] else '')
                          for i, r in enumerate(refs[:120]))

    systeme = (_STYLE + "\n\nTu proposes des sujets de vidéo « La Minute » à partir "
               "de l'actualité, uniquement quand ils recoupent ses thèmes ou ce "
               "qu'il a déjà écrit. " + _SCHEMA)
    user = (f"SES THÈMES : {', '.join(themes)}\n\n"
            f"CE QU'IL A DÉJÀ ÉCRIT :\n{bloc_refs}\n\n"
            f"ACTUALITÉS DU JOUR :\n{bloc_news}\n\n"
            f"Propose au maximum {max_suggestions()} sujets.")

    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=1)
    resp = client.messages.create(
        model=MODEL, max_tokens=6000,
        system=[{'type': 'text', 'text': systeme, 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user', 'content': user}],
    )
    texte = ''.join(b.text for b in resp.content if b.type == 'text')
    m = re.search(r'\[.*\]', texte, re.DOTALL)
    if not m:
        log_fn("Réponse IA sans JSON exploitable.")
        return 0
    try:
        propositions = json.loads(m.group(0))
    except ValueError as exc:
        log_fn(f"JSON illisible : {exc}")
        return 0

    jour = date.today()
    maintenant = datetime.utcnow()
    n = 0
    for p in propositions:
        try:
            i = ord((p.get('news_ref') or 'A').strip().upper()[0]) - 65
            nw = news[i] if 0 <= i < len(news) else None
        except Exception:  # noqa: BLE001
            nw = None
        if not nw:
            continue
        echo = None
        er = p.get('echo_ref')
        if isinstance(er, int) and 1 <= er <= len(refs):
            echo = refs[er - 1]
        s = NewsSuggestion(
            jour=jour, created_at=maintenant,
            news_titre=nw['titre'], news_url=nw['url'], news_source=nw['source'],
            echo_titre=(echo['titre'] if echo else None),
            echo_url=(echo['url'] if echo else None),
            theme=(echo['theme'] if echo else None),
            angle=(p.get('angle') or '')[:400] or None,
            accroche=(p.get('accroche') or None),
            bandeau=((p.get('bandeau') or '').strip().upper()[:60] or None),
            legende=(p.get('legende') or None),
            script=(p.get('script') or None),
            pertinence=(int(p['pertinence']) if str(p.get('pertinence', '')).strip().isdigit() else None),
            statut='nouveau',
        )
        db.session.add(s)
        n += 1
    db.session.commit()
    log_fn(f"{n} suggestion(s) enregistrée(s) pour le {jour.strftime('%d/%m/%Y')}.")
    return n


# ─── E-mail aux administrateurs ─────────────────────────────

def envoyer_digest(suggestions, log_fn=print):
    """Envoie le digest du jour aux comptes admin (comme les alertes)."""
    from mail import is_configured as mail_ok, send_email
    from auth.models import User
    if not suggestions:
        return 0
    if not mail_ok():
        log_fn("SendGrid non configuré — pas d'e-mail.")
        return 0
    admins = User.query.filter(User.is_admin.is_(True), User.email.isnot(None)).all()
    if not admins:
        return 0
    jour = suggestions[0].jour.strftime('%d/%m/%Y')
    sujet = f"Veille du {jour} — {len(suggestions)} idées de Minute"
    with current_app.test_request_context('/', base_url='https://bernardpoignant.fr'):
        lien = url_for('admin_news.index', _external=True)
        html = render_template('news_email.html', suggestions=suggestions, jour=jour, lien=lien)
    envoyes = 0
    for a in admins:
        try:
            if send_email(to_email=a.email, to_name=getattr(a, 'username', None),
                          subject=sujet, html=html, categories=['veille']):
                envoyes += 1
        except Exception:  # noqa: BLE001
            log.exception('digest veille à %s', a.email)
    log_fn(f"Digest envoyé à {envoyes} administrateur(s).")
    return envoyes


def cycle_quotidien(log_fn=print):
    """Génère les suggestions du jour puis envoie le digest. Pour le cron."""
    n = generer_suggestions(log_fn=log_fn)
    if n:
        du_jour = (NewsSuggestion.query.filter_by(jour=date.today())
                   .order_by(NewsSuggestion.pertinence.desc()).all())
        envoyer_digest(du_jour, log_fn=log_fn)
    return n


# ─── Pages admin ────────────────────────────────────────────

@admin_news_bp.route('/')
@admin_required
def index():
    statut = (request.args.get('statut') or '').strip()
    q = NewsSuggestion.query
    if statut in ('nouveau', 'retenu', 'ecarte', 'produit'):
        q = q.filter_by(statut=statut)
    else:
        q = q.filter(NewsSuggestion.statut != 'ecarte')
    suggestions = q.order_by(NewsSuggestion.jour.desc(),
                             NewsSuggestion.pertinence.desc(),
                             NewsSuggestion.id.desc()).limit(200).all()
    # Regroupées par jour pour l'affichage.
    par_jour = {}
    for s in suggestions:
        par_jour.setdefault(s.jour, []).append(s)
    jours = sorted(par_jour.keys(), reverse=True)
    return render_template('news_admin.html', par_jour=par_jour, jours=jours,
                           statut=statut, total=len(suggestions))


_EN_COURS = {'v': False}


@admin_news_bp.route('/generer', methods=['POST'])
@admin_required
def generer():
    if _EN_COURS['v']:
        flash("Une génération est déjà en cours — patientez un instant.", 'info')
        return redirect(url_for('admin_news.index'))
    _EN_COURS['v'] = True
    app = current_app._get_current_object()

    def _travail():
        with app.app_context():
            with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
                try:
                    cycle_quotidien(log_fn=lambda m: app.logger.info("veille: %s", m))
                except Exception:  # noqa: BLE001
                    app.logger.exception('génération veille')
                finally:
                    _EN_COURS['v'] = False

    threading.Thread(target=_travail, name='veille-generer', daemon=True).start()
    flash("Génération lancée en arrière-plan — rechargez la page dans un instant.", 'success')
    return redirect(url_for('admin_news.index'))


@admin_news_bp.route('/<int:sid>/statut', methods=['POST'])
@admin_required
def statut(sid):
    s = db.session.get(NewsSuggestion, sid)
    if not s:
        from flask import abort
        abort(404)
    nouveau = (request.form.get('statut') or '').strip()
    if nouveau in ('nouveau', 'retenu', 'ecarte', 'produit'):
        s.statut = nouveau
        s.decided_at = datetime.utcnow()
        db.session.commit()
    return redirect(request.referrer or url_for('admin_news.index'))
