"""Relecture orthographe / typographie des articles — la même routine que pour
le livre (livre/correction.py), appliquée à un article.

Deux temps : l'IA corrige fautes, grammaire, ponctuation et la casse du titre ;
puis la typographie française déterministe (guillemets « », espaces insécables)
par-dessus. Réversible : l'état d'avant est conservé pour annuler.

Déclenchée en tâche de fond à l'enregistrement d'un article (voir articles/
__init__._save_article), et disponible en lot pour le rattrapage.
"""

import logging
import re
import threading
from datetime import datetime

from init_db import db
from settings.models import get_config

log = logging.getLogger(__name__)

KEY_AUTOCORRECT = 'articles_autocorrect'   # '0' pour désactiver la correction à l'enregistrement
_EN_COURS = set()                          # id d'articles en cours de correction (anti-doublon)


def autocorrect_actif():
    return (get_config(KEY_AUTOCORRECT) or '1').strip() != '0'


def corriger_article(article, ia=True):
    """Corriger un article. Conserve l'état d'avant (titre + HTML) pour annuler.
    Renvoie un court compte rendu."""
    from livre.correction import typographie_html, proofread_ia, _degraisser
    from articles import _clean_html
    from articles.cleanup import clean_article_html, clean_text

    if not (article.content_html or '').strip():
        return "rien à corriger (texte vide)"

    # Sauvegarde de l'état tel que l'auteur vient de l'enregistrer : annuler
    # rend donc exactement ce qu'il a écrit, pas une correction antérieure.
    article.content_avant_correction = article.content_html
    article.titre_avant_correction = article.title

    titre = article.title or ''
    corps = _degraisser(article.content_html)
    note = []

    if ia:
        res = proofread_ia(titre, corps)
        if res:
            titre, corps, n = res
            note.append(f"{n} correction(s) IA")
        else:
            note.append("IA indisponible")

    corps = clean_article_html(_clean_html(typographie_html(corps)))
    titre = clean_text(re.sub(r'<[^>]+>', '', titre)).strip()

    article.title = (titre[:300] or article.title)
    article.content_html = corps
    article.corrige_at = datetime.utcnow()
    db.session.commit()
    return ', '.join(note) or "typographie appliquée"


def annuler_article(article):
    """Restituer le titre et le texte d'avant la dernière correction."""
    if article.content_avant_correction is None:
        return False
    article.content_html = article.content_avant_correction
    if article.titre_avant_correction:
        article.title = article.titre_avant_correction
    article.corrige_at = None
    article.content_avant_correction = None
    article.titre_avant_correction = None
    db.session.commit()
    return True


def corriger_en_fond(app, article_id, ia=True):
    """Lancer la correction d'un article en tâche de fond (à l'enregistrement).
    Sans effet si une correction du même article est déjà en cours."""
    if article_id in _EN_COURS:
        return
    _EN_COURS.add(article_id)

    def _travail():
        with app.app_context():
            try:
                from articles.models import Article
                a = db.session.get(Article, article_id)
                if a:
                    note = corriger_article(a, ia=ia)
                    app.logger.info("correction article %s : %s", article_id, note)
            except Exception:  # noqa: BLE001
                app.logger.exception("correction article %s", article_id)
            finally:
                _EN_COURS.discard(article_id)

    threading.Thread(target=_travail, name=f'corr-article-{article_id}', daemon=True).start()


def corriger_tout(limit=None, ia=True, log_fn=print):
    """Rattrapage : corriger les articles publiés pas encore corrigés."""
    from articles.models import Article
    q = (Article.query.filter(Article.published.is_(True), Article.corrige_at.is_(None))
         .order_by(Article.id))
    if limit:
        q = q.limit(limit)
    n = 0
    for a in q.all():
        try:
            note = corriger_article(a, ia=ia)
            n += 1
            log_fn(f"#{a.id} {a.title[:45]} — {note}")
        except Exception as exc:  # noqa: BLE001
            log_fn(f"#{a.id} échec : {exc}")
    return n
