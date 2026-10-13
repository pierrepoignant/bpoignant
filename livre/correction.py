"""Relire et corriger les chroniques du livre : la typographie française (les
tics récurrents de Bernard — guillemets, espaces, ponctuation), la casse des
titres, et l'orthographe/grammaire par un modèle, prudemment.

Rien n'est perdu : avant de corriger, on garde le titre et le texte d'origine
(titre_avant_correction / content_avant_correction), et « annuler » les
restitue.
"""

import json
import logging
import re

from bs4 import BeautifulSoup

from init_db import db
from livre.models import BookDoc
from livre.nettoyage import _typographie, casse_de_titre, _tout_en_capitales

log = logging.getLogger(__name__)

AQ = ' '   # espace fine insécable, avant la ponctuation haute et dans les guillemets


def _guillemets(t):
    """Guillemets français propres : les droits « "mot" » deviennent « mot »
    avec une espace fine insécable à l'intérieur, et les « … » déjà là voient
    leur espacement normalisé. Les apostrophes droites deviennent courbes."""
    # Paires de guillemets droits -> guillemets français.
    t = re.sub(r'"\s*([^"]+?)\s*"', '«' + AQ + r'\1' + AQ + '»', t)
    # Normaliser l'espacement des guillemets français déjà présents.
    t = re.sub(r'«[\s\xa0' + AQ + r']*', '«' + AQ, t)
    t = re.sub(r'[\s\xa0' + AQ + r']*»', AQ + '»', t)
    # Apostrophe droite -> courbe (hors contractions anglaises rares).
    t = t.replace("'", '’')
    return t


def _espaces_apres_ponctuation(t):
    """Une espace manquante après un point, un « ! » ou un « ? » suivi d'une
    majuscule — la faute que Bernard fait le plus (« Clémenceau.Il »)."""
    t = re.sub(r'([.!?…])([A-ZÀ-ÝŒÆ])', r'\1 \2', t)
    return t


def typographie_html(html):
    """Appliquer la typographie française au texte d'un HTML, sans toucher aux
    balises."""
    soup = BeautifulSoup(html or '', 'html.parser')
    for node in list(soup.find_all(string=True)):
        brut = str(node)
        propre = _espaces_apres_ponctuation(_guillemets(_typographie(brut)))
        if propre != brut:
            node.replace_with(propre)
    return str(soup)


# ─── Relecture par le modèle ────────────────────────────────

_SYSTEME = """Tu es correcteur professionnel de français. On te donne le titre \
et le corps HTML d'une chronique de Bernard Poignant. Tu corriges UNIQUEMENT \
les fautes : orthographe, grammaire, accords, conjugaison, ponctuation et \
typographie française (guillemets « », espaces insécables, majuscules).

Règles absolues :
- Ne change RIEN au sens, aux idées, au ton, ni au choix des mots. Tu corriges, \
tu ne réécris pas. Si une tournure est correcte mais que tu l'aurais dite \
autrement, tu la laisses.
- Conserve exactement la structure HTML : les mêmes balises, le même nombre de \
paragraphes, dans le même ordre. Tu ne touches qu'au texte à l'intérieur.
- Garde les noms propres, les sigles (RN, UE, PS, ONU…) et les citations tels quels.
- Le titre : corrige sa casse en capitalisation française normale (pas de TOUT \
EN CAPITALES, pas de Capitale À Chaque Mot) ; première lettre en majuscule, \
noms propres et sigles préservés.

Tu réponds UNIQUEMENT par un objet JSON, sans texte autour :
{"titre": "...", "html": "...", "corrections": <nombre de corrections faites>}"""


def proofread_ia(titre, html):
    """Renvoie (titre_corrigé, html_corrigé, nb_corrections) ou None si l'IA
    n'est pas disponible / a échoué."""
    from articles.ai_summary import _api_key, MODEL
    key = _api_key()
    if not key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=1)
    demande = f"Titre : {titre}\n\nCorps HTML :\n{html}"
    try:
        rep = client.messages.create(
            model=MODEL, max_tokens=8000,
            system=[{'type': 'text', 'text': _SYSTEME, 'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': demande}])
        texte = ''.join(b.text for b in rep.content if b.type == 'text')
        debut, fin = texte.find('{'), texte.rfind('}')
        if debut < 0 or fin <= debut:
            return None
        obj = json.loads(texte[debut:fin + 1])
    except Exception as exc:  # noqa: BLE001
        log.warning('correction IA échouée : %s', exc)
        return None
    return (obj.get('titre') or titre, obj.get('html') or html, int(obj.get('corrections') or 0))


# ─── Application ────────────────────────────────────────────

def corriger_doc(doc, ia=True):
    """Corriger une chronique. Garde l'original pour pouvoir annuler. Renvoie un
    court compte rendu."""
    from datetime import datetime
    from articles import _clean_html
    from articles.cleanup import clean_article_html, clean_text
    if not (doc.content_html or '').strip():
        return "rien à corriger (texte vide)"

    # Sauvegarde d'origine (seulement la première fois, pour ne pas écraser
    # l'original par une correction précédente si on recorrige).
    if doc.content_avant_correction is None:
        doc.content_avant_correction = doc.content_html
        doc.titre_avant_correction = doc.title

    titre = doc.title or doc.name or ''
    corps = doc.content_html
    note = []

    # 1) IA : orthographe, grammaire, ponctuation, casse du titre.
    if ia:
        res = proofread_ia(titre, corps)
        if res:
            titre, corps, n = res
            note.append(f"{n} correction(s) IA")
        else:
            note.append("IA indisponible")

    # 2) Typographie déterministe par-dessus (guillemets, espaces).
    corps = clean_article_html(_clean_html(typographie_html(corps)))

    # 3) Casse du titre : l'IA l'a faite ; sinon, repli déterministe sur les
    #    titres tout en capitales.
    titre = clean_text(titre).strip()
    if not ia and _tout_en_capitales(titre):
        texte_corps = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', corps)).strip()
        titre = casse_de_titre(titre, texte_corps)

    doc.title = (titre[:300] or doc.title)
    doc.content_html = corps
    doc.content_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', corps)).strip()
    doc.word_count = len(doc.content_text.split())
    doc.corrige_at = datetime.utcnow()
    db.session.commit()
    return ', '.join(note) or "typographie appliquée"


def annuler(doc):
    """Restituer le titre et le texte d'avant la correction."""
    if doc.content_avant_correction is None:
        return False
    doc.content_html = doc.content_avant_correction
    doc.content_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', doc.content_html)).strip()
    doc.word_count = len(doc.content_text.split())
    if doc.titre_avant_correction:
        doc.title = doc.titre_avant_correction
    doc.corrige_at = None
    doc.content_avant_correction = None
    doc.titre_avant_correction = None
    db.session.commit()
    return True
