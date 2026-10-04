"""Admin du Livre : lire, classer, écarter — un document à la fois, sur téléphone."""

import logging
import re
from datetime import datetime

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, url_for)

from auth import admin_required
from init_db import db
from livre.models import BookDoc, BookTheme, STATUTS

log = logging.getLogger(__name__)

admin_livre_bp = Blueprint('admin_livre', __name__, url_prefix='/admin/livre',
                           template_folder='templates')

THEMES_DE_DEPART = ('Europe', 'Bretagne', 'Socialisme', 'Monde', 'Élections', 'Chine', 'Hollande')


def _quand(valeur):
    try:
        return datetime.fromisoformat(str(valeur).replace('Z', '+00:00')).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _cle(titre):
    """Un titre réduit à ses lettres, pour rapprocher un Doc d'un article."""
    t = (titre or '').lower()
    t = re.sub(r'[«»"“”’\'.:;,!?()\-–—]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def seed_themes():
    """Les six chapitres de départ, créés une fois. Bernard les renomme ensuite."""
    if BookTheme.query.count():
        return
    for i, nom in enumerate(THEMES_DE_DEPART):
        db.session.add(BookTheme(name=nom, position=i))
    db.session.commit()


def sync_from_drive():
    """Align the shelf on Drive. Returns (nouveaux, revus, disparus).

    Decisions already taken are untouched: a re-sync is about documents that
    appeared or changed, never a reset.
    """
    import gdrive
    from articles.models import Article
    docs = gdrive.list_all_documents()
    existants = {d.drive_id: d for d in BookDoc.query.all()}
    articles = {_cle(a.title): a.id for a in Article.query.all()}
    vus = set()
    nouveaux = revus = 0
    for item in docs:
        vus.add(item['id'])
        ligne = existants.get(item['id'])
        if ligne is None:
            ligne = BookDoc(drive_id=item['id'], name=item['name'])
            db.session.add(ligne)
            nouveaux += 1
        else:
            revus += 1
        modif = _quand(item.get('modified'))
        if ligne.content_fetched_at and modif and ligne.modified_at and modif > ligne.modified_at:
            # Modifié depuis la dernière lecture : on ira le rechercher.
            ligne.content_html = None
            ligne.content_fetched_at = None
        ligne.name = item['name'][:300]
        ligne.modified_at = modif
        ligne.created_at = _quand(item.get('created')) or ligne.created_at
        ligne.missing = False
        if ligne.article_id is None:
            ligne.article_id = articles.get(_cle(item['name']))
    disparus = 0
    for drive_id, ligne in existants.items():
        if drive_id not in vus and not ligne.missing:
            ligne.missing = True
            disparus += 1
    db.session.commit()
    return nouveaux, revus, disparus


def _texte(doc):
    """The document's HTML, fetched from Drive on first ask and kept."""
    if doc.content_html is None:
        _charger(doc)
        db.session.commit()
    return doc.content_html


def _charger(doc):
    """Fetch one document's text from Drive, cleaned the way an article is."""
    import gdrive
    from articles import _clean_html
    from livre.nettoyage import appliquer
    brut = gdrive.get_document(doc.drive_id)
    # L'export, passé au seul nettoyage de sécurité, est gardé tel quel : le
    # reste — titre, date, signature, typographie — se rejoue dessus à volonté.
    doc.source_html = _clean_html(brut['html'])
    doc.content_fetched_at = datetime.utcnow()
    appliquer(doc)


# ─── Import complet, en fond ────────────────────────────────

import threading

IMPORT = {'en_cours': False}
_VERROU = threading.Lock()


def etat_import():
    return dict(IMPORT)


def importer_tout(app):
    """List the shelf, then fetch every text not yet stored — once.

    Seven hundred exports at about a second each is ten minutes nobody should
    watch: the page polls the state. Re-running only fetches what is new or
    changed, since a stored text is kept until Drive says it moved.
    """
    with _VERROU:
        if IMPORT.get('en_cours'):
            return False
        IMPORT.clear()
        IMPORT.update(en_cours=True, etape="Liste des documents sur Drive…", faits=0, total=0,
                      nouveaux=0, textes=0, erreurs=[], demarre=datetime.utcnow().isoformat(timespec='seconds'))

    def _travail():
        with app.app_context():
            try:
                n, r, d = sync_from_drive()
                IMPORT.update(nouveaux=n, disparus=d)
                restants = (BookDoc.query.filter(db.or_(BookDoc.content_fetched_at.is_(None),
                                                        BookDoc.source_html.is_(None)),
                                                 BookDoc.missing.is_(False))
                            .order_by(BookDoc.created_at.desc()).all())
                IMPORT.update(etape="Lecture des textes…", total=len(restants))
                for i, doc in enumerate(restants, 1):
                    try:
                        _charger(doc)
                        db.session.commit()
                        IMPORT['textes'] += 1
                    except Exception as exc:
                        db.session.rollback()
                        IMPORT['erreurs'].append(f"{doc.name[:40]} : {str(exc)[:80]}")
                    IMPORT.update(faits=i)
                IMPORT.update(etape="Terminé")
            except Exception as exc:
                db.session.rollback()
                log.exception('import du livre')
                IMPORT['erreurs'].append(str(exc)[:200])
                IMPORT.update(etape="Interrompu")
            finally:
                IMPORT.update(en_cours=False, fini=datetime.utcnow().isoformat(timespec='seconds'))

    threading.Thread(target=_travail, name='livre-import', daemon=True).start()
    return True


def _compte():
    rows = dict(db.session.query(BookDoc.status, db.func.count(BookDoc.id))
                .filter(BookDoc.missing.is_(False)).group_by(BookDoc.status).all())
    total = sum(rows.values())
    classes = BookDoc.query.filter_by(status='classe', missing=False)
    return {'a_classer': rows.get('a_classer', 0), 'classe': rows.get('classe', 0),
            'ignore': rows.get('ignore', 0), 'total': total,
            'au_livre': classes.filter_by(in_book=True).count(),
            'intro_a_faire': classes.filter(BookDoc.in_book.is_(True), BookDoc.intro.is_(None), BookDoc.no_intro.is_(False)).count(),
            'intro_faite': classes.filter(BookDoc.in_book.is_(True), BookDoc.intro.isnot(None)).count()}


def _suivant(apres_id=None):
    """The next document to decide on, newest first.

    `apres_id` skips past one without deciding — « Passer » — so Bernard can
    leave a hard one for later without it coming straight back.
    """
    q = BookDoc.query.filter_by(status='a_classer', missing=False)
    if apres_id:
        pivot = db.session.get(BookDoc, apres_id)
        if pivot is not None:
            q = q.filter(db.or_(BookDoc.created_at < pivot.created_at,
                                db.and_(BookDoc.created_at == pivot.created_at, BookDoc.id < pivot.id)))
    doc = q.order_by(db.func.coalesce(BookDoc.written_at, db.func.date(BookDoc.created_at)).desc(), BookDoc.id.desc()).first()
    if doc is None and apres_id:
        # Fin de la pile : on repart du début, il reste ce qu'on a passé.
        doc = (BookDoc.query.filter_by(status='a_classer', missing=False)
               .order_by(BookDoc.created_at.desc(), BookDoc.id.desc()).first())
    return doc


def _json_doc(doc):
    if doc is None:
        return None
    quand = doc.date_livre
    return {
        'id': doc.id, 'nom': doc.titre, 'fichier': doc.name,
        'date': quand.strftime('%d/%m/%Y') if quand else '',
        'annee': quand.year if quand else None,
        'intro': doc.intro or '', 'theme': doc.theme.name if doc.theme else None,
        'modifie': doc.modified_at.strftime('%d/%m/%Y') if doc.modified_at else '',
        'article': ({'id': doc.article.id, 'titre': doc.article.title,
                     'url': url_for('articles.public_show', slug=doc.article.slug, _external=False)}
                    if doc.article else None),
        'mots': doc.word_count,
        'drive': f'https://docs.google.com/document/d/{doc.drive_id}/edit',
        'statut': doc.status, 'theme_id': doc.theme_id,
    }


# ─── Pages ──────────────────────────────────────────────────

@admin_livre_bp.route('/')
@admin_required
def classer():
    seed_themes()
    themes = BookTheme.query.order_by(BookTheme.position, BookTheme.name).all()
    doc = _suivant()
    return render_template('livre_classer.html', themes=themes, compte=_compte(),
                           doc=_json_doc(doc), total_docs=BookDoc.query.count())


@admin_livre_bp.route('/importer', methods=['POST'])
@admin_required
def importer():
    from flask import current_app
    if not importer_tout(current_app._get_current_object()):
        flash("Un import est déjà en cours.", 'danger')
    return redirect(request.referrer or url_for('admin_livre.tous'))


@admin_livre_bp.route('/importer/etat')
@admin_required
def importer_etat():
    return jsonify(etat_import())


@admin_livre_bp.route('/tous')
@admin_required
def tous():
    """Every document on the shelf, newest first, twenty-five a page, searchable
    in the title and the text."""
    seed_themes()
    q = BookDoc.query.filter_by(missing=False)
    recherche = (request.args.get('q') or '').strip()
    statut = request.args.get('statut') or ''
    if recherche:
        motif = f'%{recherche}%'
        q = q.filter(db.or_(BookDoc.name.ilike(motif), BookDoc.title.ilike(motif), BookDoc.content_text.ilike(motif)))
    if statut in STATUTS:
        q = q.filter_by(status=statut)
    pagination = (q.order_by(BookDoc.created_at.desc(), BookDoc.id.desc())
                  .paginate(page=request.args.get('page', 1, type=int), per_page=25, error_out=False))
    return render_template('livre_tous.html', pagination=pagination, docs=pagination.items,
                           recherche=recherche, statut=statut, compte=_compte(),
                           themes=BookTheme.query.order_by(BookTheme.position).all(),
                           etat=etat_import(),
                           sans_texte=BookDoc.query.filter(BookDoc.content_fetched_at.is_(None),
                                                           BookDoc.missing.is_(False)).count())


@admin_livre_bp.route('/doc/<int:doc_id>/texte')
@admin_required
def texte(doc_id):
    """The full text for the overlay — fetched from Drive once, then kept."""
    import gdrive
    doc = db.session.get(BookDoc, doc_id) or abort(404)
    try:
        html = _texte(doc)
    except gdrive.GoogleDriveError as exc:
        return jsonify({'ok': False, 'erreur': str(exc)}), 502
    return jsonify({'ok': True, 'html': html, 'mots': doc.word_count, 'nom': doc.name})


@admin_livre_bp.route('/doc/<int:doc_id>/decider', methods=['POST'])
@admin_required
def decider(doc_id):
    """One decision — a theme, « ignorer », or back to the pile — and the next
    document in the same answer, so the page never waits on a reload."""
    doc = db.session.get(BookDoc, doc_id) or abort(404)
    action = request.form.get('action') or ''
    if action == 'theme':
        theme = db.session.get(BookTheme, request.form.get('theme_id', type=int) or 0)
        if theme is None:
            return jsonify({'ok': False, 'erreur': 'Thème inconnu.'}), 400
        # Changer de chapitre fait perdre la position manuelle : la chronique
        # reprend l'ordre chronologique dans son nouveau chapitre.
        if doc.theme_id != theme.id:
            doc.book_position = None
        doc.status, doc.theme_id = 'classe', theme.id
    elif action == 'ignorer':
        doc.status, doc.theme_id = 'ignore', None
    elif action == 'reprendre':
        doc.status, doc.theme_id = 'a_classer', None
    elif action == 'passer':
        db.session.commit()
        return jsonify({'ok': True, 'suivant': _json_doc(_suivant(apres_id=doc.id)), 'compte': _compte()})
    else:
        return jsonify({'ok': False, 'erreur': 'Action inconnue.'}), 400
    doc.decided_at = datetime.utcnow() if action != 'reprendre' else None
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True, 'suivant': _json_doc(_suivant()), 'compte': _compte()})
    return redirect(request.referrer or url_for('admin_livre.classer'))


# ─── Intros ─────────────────────────────────────────────────

def _prochain_sans_intro(apres_id=None):
    """The next classified document still without its few opening lines —
    chapter by chapter, oldest first, so Bernard writes a chapter in the
    order the reader will meet it."""
    q = BookDoc.query.filter(BookDoc.status == 'classe', BookDoc.missing.is_(False),
                             BookDoc.in_book.is_(True), BookDoc.intro.is_(None), BookDoc.no_intro.is_(False))
    if apres_id:
        pivot = db.session.get(BookDoc, apres_id)
        if pivot is not None:
            q = q.filter(BookDoc.id != pivot.id)
    return (q.join(BookTheme, BookTheme.id == BookDoc.theme_id)
            .order_by(BookTheme.position, *_ordre_chapitre())
            .first())


@admin_livre_bp.route('/intros')
@admin_required
def intros():
    """Write the intro of one classified document at a time; `?doc=` reopens
    a given one, from the « Intro faite » list."""
    seed_themes()
    doc_id = request.args.get('doc', type=int)
    doc = db.session.get(BookDoc, doc_id) if doc_id else _prochain_sans_intro()
    return render_template('livre_intro.html', doc=_json_doc(doc), compte=_compte())


@admin_livre_bp.route('/intros/faites')
@admin_required
def intros_faites():
    docs = (BookDoc.query.filter(BookDoc.status == 'classe', BookDoc.intro.isnot(None))
            .order_by(BookDoc.intro_at.desc()).all())
    return render_template('livre_liste.html', mode='intros', docs=docs,
                           themes=BookTheme.query.order_by(BookTheme.position).all(),
                           theme_id=None, par_theme={}, compte=_compte())


@admin_livre_bp.route('/doc/<int:doc_id>/intro', methods=['POST'])
@admin_required
def intro_save(doc_id):
    """Save the intro — or skip to the next — and hand back the next document."""
    doc = db.session.get(BookDoc, doc_id) or abort(404)
    action = request.form.get('action') or 'enregistrer'
    if action == 'enregistrer':
        texte = (request.form.get('intro') or '').strip()
        doc.intro = texte or None
        doc.intro_at = datetime.utcnow() if texte else None
        doc.no_intro = False
        db.session.commit()
    elif action == 'sans_intro':
        # Pas d'intro pour celle-ci : on ne la repropose plus.
        doc.no_intro = True
        doc.intro = None
        db.session.commit()
    suivant = _prochain_sans_intro(apres_id=doc.id if action == 'passer' else None)
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True, 'suivant': _json_doc(suivant), 'compte': _compte()})
    return redirect(request.referrer or url_for('admin_livre.intros'))


@admin_livre_bp.route('/chapitre/<int:theme_id>')
@admin_required
def chapitre(theme_id):
    """Les chroniques d'un chapitre : lire, retirer du livre, réordonner."""
    t = db.session.get(BookTheme, theme_id) or abort(404)
    chroniques = _chroniques_du_chapitre(t, inclus_seulement=False)
    return render_template('livre_chapitre.html', theme=t, chroniques=chroniques,
                           themes=BookTheme.query.order_by(BookTheme.position, BookTheme.name).all(),
                           retenus=sum(1 for c in chroniques if c.in_book), compte=_compte())


@admin_livre_bp.route('/doc/<int:doc_id>/livre', methods=['POST'])
@admin_required
def doc_in_book(doc_id):
    """Retirer une chronique du livre, ou l'y remettre — sans la déclasser."""
    d = db.session.get(BookDoc, doc_id) or abort(404)
    d.in_book = request.form.get('inclure') == '1'
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True, 'in_book': d.in_book, 'compte': _compte()})
    return redirect(request.referrer or url_for('admin_livre.chapitre', theme_id=d.theme_id))


@admin_livre_bp.route('/chapitre/<int:theme_id>/ordre', methods=['POST'])
@admin_required
def chapitre_ordre(theme_id):
    """Fixer l'ordre des chroniques du chapitre depuis la liste complète d'ids."""
    t = db.session.get(BookTheme, theme_id) or abort(404)
    ids = request.form.getlist('ordre[]') or (request.form.get('ordre') or '').split(',')
    ids = [int(x) for x in ids if str(x).strip().isdigit()]
    par_id = {d.id: d for d in t.docs.filter_by(status='classe').all()}
    for i, did in enumerate(ids):
        if did in par_id:
            par_id[did].book_position = i
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True})
    return redirect(url_for('admin_livre.chapitre', theme_id=theme_id))


@admin_livre_bp.route('/chapitre/<int:theme_id>/doc/<int:doc_id>/deplacer', methods=['POST'])
@admin_required
def chapitre_move(theme_id, doc_id):
    """Monter/descendre une chronique d'un cran dans le chapitre."""
    t = db.session.get(BookTheme, theme_id) or abort(404)
    ordonnes = _chroniques_du_chapitre(t, inclus_seulement=False)
    for i, d in enumerate(ordonnes):
        d.book_position = i
    i = next(k for k, d in enumerate(ordonnes) if d.id == doc_id)
    j = i - 1 if request.form.get('sens') == 'monter' else i + 1
    if 0 <= j < len(ordonnes):
        ordonnes[i].book_position, ordonnes[j].book_position = ordonnes[j].book_position, ordonnes[i].book_position
    db.session.commit()
    return redirect(url_for('admin_livre.chapitre', theme_id=theme_id))


@admin_livre_bp.route('/classes')
@admin_required
def classes():
    seed_themes()
    themes = BookTheme.query.order_by(BookTheme.position, BookTheme.name).all()
    theme_id = request.args.get('theme', type=int)
    q = BookDoc.query.filter_by(status='classe')
    if theme_id:
        q = q.filter_by(theme_id=theme_id)
    docs = q.order_by(BookDoc.theme_id, BookDoc.created_at.desc()).all()
    par_theme = {t.id: t.docs.filter_by(status='classe').count() for t in themes}
    return render_template('livre_liste.html', mode='classes', docs=docs, themes=themes,
                           theme_id=theme_id, par_theme=par_theme, compte=_compte())


@admin_livre_bp.route('/ignores')
@admin_required
def ignores():
    docs = (BookDoc.query.filter_by(status='ignore')
            .order_by(BookDoc.decided_at.desc()).all())
    return render_template('livre_liste.html', mode='ignores', docs=docs,
                           themes=BookTheme.query.order_by(BookTheme.position).all(),
                           theme_id=None, par_theme={}, compte=_compte())


# ─── Thèmes ─────────────────────────────────────────────────

@admin_livre_bp.route('/themes')
@admin_required
def themes():
    seed_themes()
    liste = BookTheme.query.order_by(BookTheme.position, BookTheme.name).all()
    comptes = {t.id: (t.docs.filter_by(status='classe', in_book=True).count(),
                      t.docs.filter_by(status='classe').count()) for t in liste}
    return render_template('livre_themes.html', themes=liste, comptes=comptes, compte=_compte())


@admin_livre_bp.route('/themes/ajouter', methods=['POST'])
@admin_required
def theme_add():
    nom = (request.form.get('name') or '').strip()[:80]
    if nom and not BookTheme.query.filter(db.func.lower(BookTheme.name) == nom.lower()).first():
        dernier = db.session.query(db.func.max(BookTheme.position)).scalar() or 0
        db.session.add(BookTheme(name=nom, position=dernier + 1))
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'fetch':
            t = BookTheme.query.filter_by(name=nom).first()
            return jsonify({'ok': True, 'id': t.id, 'nom': t.name})
    elif request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': False, 'erreur': 'Nom vide ou déjà pris.'}), 400
    return redirect(request.referrer or url_for('admin_livre.themes'))


@admin_livre_bp.route('/themes/ordre', methods=['POST'])
@admin_required
def themes_ordre():
    """Set every chapter's position from a full ordered list of ids — the
    drag-and-drop sends the whole new order at once."""
    ids = request.form.getlist('ordre[]') or (request.form.get('ordre') or '').split(',')
    ids = [int(x) for x in ids if str(x).strip().isdigit()]
    par_id = {t.id: t for t in BookTheme.query.all()}
    for i, tid in enumerate(ids):
        if tid in par_id:
            par_id[tid].position = i
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True})
    return redirect(url_for('admin_livre.themes'))


@admin_livre_bp.route('/themes/<int:theme_id>/deplacer', methods=['POST'])
@admin_required
def theme_move(theme_id):
    """Monter ou descendre un chapitre : on échange sa position avec le voisin.

    Les positions peuvent être espacées ou égales selon l'historique ; on
    renumérote d'abord proprement, puis on permute, pour que « monter » avance
    toujours d'un cran exactement."""
    t = db.session.get(BookTheme, theme_id) or abort(404)
    sens = request.form.get('sens')
    ordonnes = BookTheme.query.order_by(BookTheme.position, BookTheme.name).all()
    for i, x in enumerate(ordonnes):
        x.position = i
    i = next(k for k, x in enumerate(ordonnes) if x.id == t.id)
    j = i - 1 if sens == 'monter' else i + 1
    if 0 <= j < len(ordonnes):
        ordonnes[i].position, ordonnes[j].position = ordonnes[j].position, ordonnes[i].position
    db.session.commit()
    return redirect(url_for('admin_livre.themes'))


@admin_livre_bp.route('/themes/<int:theme_id>', methods=['POST'])
@admin_required
def theme_update(theme_id):
    t = db.session.get(BookTheme, theme_id) or abort(404)
    if request.form.get('supprimer'):
        # Les documents du chapitre repartent dans la pile, rien n'est perdu.
        for d in t.docs.all():
            d.status, d.theme_id, d.decided_at = 'a_classer', None, None
        db.session.delete(t)
        db.session.commit()
        flash(f"Thème « {t.name} » supprimé ; ses documents sont à reclasser.", 'success')
        return redirect(url_for('admin_livre.themes'))
    t.name = (request.form.get('name') or t.name).strip()[:80] or t.name
    t.description = (request.form.get('description') or '').strip() or None
    t.position = request.form.get('position', t.position, type=int)
    db.session.commit()
    flash("Thème enregistré.", 'success')
    return redirect(url_for('admin_livre.themes'))


# ─── Le livre en PDF ────────────────────────────────────────

KEY_TITRE = 'livre_titre'
KEY_SOUS_TITRE = 'livre_sous_titre'
KEY_AUTEUR = 'livre_auteur'
# Le livre et la couverture sont stockés dans S3, à clé fixe (réécrite à chaque
# génération) : le disque du pod est éphémère et recréé à chaque déploiement,
# donc un fichier écrit là est oublié dès le redémarrage suivant.
S3_LIVRE = 'livre/livre.pdf'
S3_COUVERTURE = 'livre/couverture.pdf'
# Ce qu'on sait de la dernière génération, gardé en base pour que la page le
# retrouve quel que soit le pod qui la sert.
KEY_PDF_META = 'livre_pdf_meta'
# Les textes liminaires et de fin, et les mentions légales. Tous facultatifs :
# une page n'est composée que si son champ est rempli.
CHAMPS_LIVRE = {
    'dedicace':     'livre_dedicace',       # « À … », une ligne, après la page de titre
    'epigraphe':    'livre_epigraphe',      # une citation en exergue
    'epigraphe_src': 'livre_epigraphe_src', # son auteur
    'avant_propos': 'livre_avant_propos',   # l'introduction de Bernard au livre entier
    'biographie':   'livre_biographie',     # « L'auteur », en fin de livre
    'remerciements': 'livre_remerciements', # en fin de livre
    'quatrieme':    'livre_quatrieme',      # la quatrième de couverture (pour le dos)
    'editeur':      'livre_editeur',        # nom de l'éditeur, page de copyright
    'isbn':         'livre_isbn',           # ISBN (KDP en fournit un gratuit)
    'depot_legal':  'livre_depot_legal',    # « octobre 2026 », exigé en France
}

GEN = {'en_cours': False}
_GEN_VERROU = threading.Lock()


def _reglages_livre():
    from settings.models import get_config
    return (get_config(KEY_TITRE) or 'Une décennie de Chroniques',
            get_config(KEY_SOUS_TITRE) or 'Bernard Poignant · 2017–2026',
            get_config(KEY_AUTEUR) or 'Bernard Poignant')


def _meta_livre():
    """Tous les champs du livre, pour la composition et le formulaire."""
    from settings.models import get_config
    titre, sous, auteur = _reglages_livre()
    meta = {'titre': titre, 'sous_titre': sous, 'auteur': auteur}
    for cle, config in CHAMPS_LIVRE.items():
        meta[cle] = (get_config(config) or '').strip()
    return meta


def _ordre_chapitre():
    """Clé d'ordre d'une chronique dans son chapitre : la position manuelle
    quand elle existe, sinon la date d'écriture."""
    return (db.func.coalesce(BookDoc.book_position, 1000000),
            db.func.coalesce(BookDoc.written_at, db.func.date(BookDoc.created_at)),
            BookDoc.id)


def _chroniques_du_chapitre(theme, inclus_seulement=True):
    q = theme.docs.filter_by(status='classe')
    if inclus_seulement:
        q = q.filter_by(in_book=True)
    return q.order_by(*_ordre_chapitre()).all()


def _docs_par_theme():
    """(nom, [chroniques], intro) dans l'ordre du livre ; seules les chroniques
    retenues (in_book), dans l'ordre du chapitre."""
    out = []
    for t in BookTheme.query.order_by(BookTheme.position, BookTheme.name).all():
        ch = _chroniques_du_chapitre(t)
        if ch:
            out.append((t.name, ch, t.description))
    return out


def _dossier_pdf():
    import os
    d = os.path.join(WORKDIR_LIVRE, 'pdf')
    os.makedirs(d, exist_ok=True)
    return d


import os as _os
WORKDIR_LIVRE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', 'instance', 'livre')


def etat_generation():
    return dict(GEN)


def generer_livre(app):
    """Compose the whole book — several hundred pages — in the background."""
    with _GEN_VERROU:
        if GEN.get('en_cours'):
            return False
        GEN.clear()
        GEN.update(en_cours=True, etape="Composition du livre…",
                   demarre=datetime.utcnow().isoformat(timespec='seconds'))

    def _travail():
        with app.app_context():
            try:
                import json
                import storage
                from settings.models import set_config
                from livre import pdf as pdfmod
                if not storage.is_configured():
                    raise RuntimeError("Le stockage S3 n'est pas configuré — impossible d'enregistrer le livre.")
                meta = _meta_livre()
                themes = _docs_par_theme()
                n_chr = sum(len(c) for _, c, *_ in themes)
                GEN.update(etape="Composition du livre…", chroniques=n_chr)
                data, n_pages = pdfmod.construire(themes, meta)
                GEN.update(etape="Enregistrement…", octets=len(data), pages=n_pages)
                storage.put_file(S3_LIVRE, data, 'application/pdf')
                couv = pdfmod.couverture(meta['titre'], meta['sous_titre'], meta['auteur'])
                storage.put_file(S3_COUVERTURE, couv, 'application/pdf')
                infos = {'genere_le': datetime.utcnow().isoformat(timespec='seconds'),
                         'pages': n_pages, 'chroniques': n_chr, 'octets': len(data),
                         'couverture_octets': len(couv)}
                set_config(KEY_PDF_META, json.dumps(infos))
                GEN.update(etape="Terminé", **infos)
            except Exception as exc:
                log.exception('génération du livre')
                GEN.update(etape="Échec", erreur=str(exc)[:200])
            finally:
                GEN.update(en_cours=False, fini=datetime.utcnow().isoformat(timespec='seconds'))

    threading.Thread(target=_travail, name='livre-pdf', daemon=True).start()
    return True


@admin_livre_bp.route('/pdf')
@admin_required
def livre_pdf():
    import os
    import json
    from settings.models import get_config
    meta = _meta_livre()
    infos = {}
    try:
        infos = json.loads(get_config(KEY_PDF_META) or '{}')
    except ValueError:
        infos = {}
    genere_le = None
    if infos.get('genere_le'):
        try:
            genere_le = datetime.fromisoformat(infos['genere_le'])
        except ValueError:
            genere_le = None
    return render_template('livre_pdf.html', compte=_compte(), meta=meta,
                           titre=meta['titre'], sous_titre=meta['sous_titre'], auteur=meta['auteur'],
                           themes=_docs_par_theme(), etat=etat_generation(),
                           genere_le=genere_le, infos=infos,
                           taille=infos.get('octets'))


@admin_livre_bp.route('/pdf/reglages', methods=['POST'])
@admin_required
def livre_pdf_reglages():
    from settings.models import set_config
    set_config(KEY_TITRE, (request.form.get('titre') or '').strip())
    set_config(KEY_SOUS_TITRE, (request.form.get('sous_titre') or '').strip())
    set_config(KEY_AUTEUR, (request.form.get('auteur') or '').strip())
    for cle, config in CHAMPS_LIVRE.items():
        set_config(config, (request.form.get(cle) or '').strip())
    flash("Réglages du livre enregistrés.", 'success')
    return redirect(url_for('admin_livre.livre_pdf'))


@admin_livre_bp.route('/pdf/apercu')
@admin_required
def livre_pdf_apercu():
    """A short PDF — the first chroniques of each chapter — served inline, now."""
    from flask import Response
    try:
        from livre import pdf as pdfmod
    except Exception as exc:
        flash(f"Génération PDF indisponible : {exc}", 'danger')
        return redirect(url_for('admin_livre.livre_pdf'))
    data, _ = pdfmod.apercu(_docs_par_theme(), _meta_livre(), max_chroniques=3)
    return Response(data, mimetype='application/pdf',
                    headers={'Content-Disposition': 'inline; filename="apercu-livre.pdf"'})


@admin_livre_bp.route('/pdf/couverture')
@admin_required
def livre_pdf_couverture():
    from flask import Response
    from livre import pdf as pdfmod
    titre, sous, auteur = _reglages_livre()
    return Response(pdfmod.couverture(titre, sous, auteur), mimetype='application/pdf',
                    headers={'Content-Disposition': 'inline; filename="couverture.pdf"'})


@admin_livre_bp.route('/pdf/generer', methods=['POST'])
@admin_required
def livre_pdf_generer():
    from flask import current_app
    if not generer_livre(current_app._get_current_object()):
        flash("Une génération est déjà en cours.", 'danger')
    return redirect(url_for('admin_livre.livre_pdf'))


@admin_livre_bp.route('/pdf/etat')
@admin_required
def livre_pdf_etat():
    return jsonify(etat_generation())


@admin_livre_bp.route('/pdf/telecharger/<quoi>')
@admin_required
def livre_pdf_telecharger(quoi):
    import storage
    from flask import Response
    cle = {'livre': S3_LIVRE, 'couverture': S3_COUVERTURE}.get(quoi)
    if not cle:
        abort(404)
    data = storage.get_file(cle)
    if not data:
        abort(404)
    nom = 'chroniques-bernard-poignant.pdf' if quoi == 'livre' else 'couverture-chroniques.pdf'
    return Response(data, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="{nom}"'})
