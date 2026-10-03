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
    brut = gdrive.get_document(doc.drive_id)
    # Même chaîne que l'import d'un article : nettoyer, puis retirer le titre et
    # les lignes d'auteur et de date que Google met en tête du document.
    html = _clean_html(brut['html'])
    _titre, html = gdrive.strip_boilerplate(html, brut['name'])
    doc.content_html = html
    doc.content_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()
    doc.content_fetched_at = datetime.utcnow()
    doc.word_count = len(doc.content_text.split())


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
                restants = (BookDoc.query.filter(BookDoc.content_fetched_at.is_(None),
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
    return {'a_classer': rows.get('a_classer', 0), 'classe': rows.get('classe', 0),
            'ignore': rows.get('ignore', 0), 'total': total}


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
    doc = q.order_by(BookDoc.created_at.desc(), BookDoc.id.desc()).first()
    if doc is None and apres_id:
        # Fin de la pile : on repart du début, il reste ce qu'on a passé.
        doc = (BookDoc.query.filter_by(status='a_classer', missing=False)
               .order_by(BookDoc.created_at.desc(), BookDoc.id.desc()).first())
    return doc


def _json_doc(doc):
    if doc is None:
        return None
    return {
        'id': doc.id, 'nom': doc.name,
        'date': doc.created_at.strftime('%d/%m/%Y') if doc.created_at else '',
        'annee': doc.created_at.year if doc.created_at else None,
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
        q = q.filter(db.or_(BookDoc.name.ilike(motif), BookDoc.content_text.ilike(motif)))
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
    comptes = {t.id: t.docs.filter_by(status='classe').count() for t in liste}
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
