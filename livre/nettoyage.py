"""Préparer un Google Doc pour le livre : titre, date, texte propre.

Bernard écrit vite et signe en bas. Le document type commence par un titre en
capitales, finit par « Bernard Poignant » et une date, et entre les deux porte
les tics d'une frappe rapide : « mot,mot », « Clémenceau.! », deux points de
suite. Le livre veut le titre à part, la date comme date de l'article, la
signature retirée, et une typographie française tenue.

Tout part de `source_html` — l'export de Drive après le seul nettoyage de
sécurité — et peut donc être rejoué quand une règle change, sans rien aller
rechercher.
"""

import difflib
import re
from datetime import date

from bs4 import BeautifulSoup

from articles.cleanup import clean_article_html, clean_text

MOIS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet',
        'août', 'septembre', 'octobre', 'novembre', 'décembre']
_MOIS_ASCII = {m: i + 1 for i, m in enumerate(MOIS)}
_MOIS_ASCII.update({'fevrier': 2, 'aout': 8, 'decembre': 12})

# Sigles à laisser en capitales quand un titre tout en capitales est ramené en
# bas de casse — « LE RN ET L'EUROPE » doit donner « Le RN et l'Europe ».
SIGLES = {'RN', 'PS', 'LFI', 'FN', 'UE', 'URSS', 'USA', 'US', 'OTAN', 'ONU', 'UMP', 'LR',
          'PCF', 'EELV', 'CGT', 'CFDT', 'FO', 'SNCF', 'EDF', 'PIB', 'TVA', 'CSG', 'RSA',
          'SMIC', 'PMA', 'GPA', 'BCE', 'FMI', 'OMS', 'OMC', 'G7', 'G20', 'AFD', 'CDU', 'SPD',
          'RPR', 'UDF', 'MRP', 'SFIO', 'PSU', 'CNR', 'NUPES', 'NFP', 'JO', 'TGV', 'CHU', 'AP',
          'XI', 'XIV', 'XV', 'XVI', 'XIX', 'XX', 'XXI', 'II', 'III', 'IV', 'VI', 'VII', 'VIII'}
# Mots qui prennent la majuscule en français même au milieu d'un titre.
_MAJ = {'france', 'bretagne', 'europe', 'quimper', 'paris', 'chine', 'allemagne', 'russie',
        'ukraine', 'hollande', 'macron', 'mélenchon', 'melenchon', 'pen', 'bardella', 'mitterrand',
        'jaurès', 'jaures', 'blum', 'pompidou', 'gaulle', 'chirac', 'sarkozy', 'rocard', 'jospin',
        'clemenceau', 'clémenceau', 'républicains', 'république', 'etat', 'état', 'français',
        'française', 'bretons', 'breton', 'américain', 'américains', 'chinois', 'européen',
        'européenne', 'européens', 'finistère', 'cornouaille', 'brest', 'rennes', 'nantes',
        'lorient', 'pologne', 'italie', 'espagne', 'angleterre', 'israël', 'palestine', 'gaza',
        'iran', 'algérie', 'afrique', 'amérique', 'asie', 'trump', 'poutine', 'biden', 'xi'}

_AUTEUR = re.compile(r'^(par\s+)?(bernard\s+poignant|b\.\s*p\.|bp)\s*[.,]?$', re.I)
_LIEU_DATE = re.compile(
    r'^(?:(?P<lieu>[A-ZÉÈ][\w’\'\- ]{1,30}),?\s+)?(?:le\s+)?(?P<jour>\d{1,2})(?:er)?\s+'
    r'(?P<mois>[a-zéûA-ZÉÛ]{3,10})\s+(?P<annee>(?:19|20)\d{2})\s*\.?$', re.I)
_MOIS_ANNEE = re.compile(r'^(?P<mois>[a-zéû]{3,10})\s+(?P<annee>(?:19|20)\d{2})\s*\.?$', re.I)
_NUMERIQUE = re.compile(r'^(?P<jour>\d{1,2})[/.\-](?P<mois>\d{1,2})[/.\-](?P<annee>\d{2,4})\s*\.?$')


def _mois(nom):
    """Le numéro du mois, tolérant aux fautes de frappe (« vovembre »)."""
    n = (nom or '').lower().strip('.')
    if n in _MOIS_ASCII:
        return _MOIS_ASCII[n]
    proche = difflib.get_close_matches(n, MOIS, n=1, cutoff=0.75)
    return _MOIS_ASCII[proche[0]] if proche else None


def lire_date(texte):
    """A date written by hand, as a `date` — or None when the line is not one."""
    t = re.sub(r'\s+', ' ', texte or '').strip()
    m = _LIEU_DATE.match(t)
    if m:
        mois = _mois(m.group('mois'))
        if mois:
            try:
                return date(int(m.group('annee')), mois, min(int(m.group('jour')), 28 if mois == 2 else 30 if mois in (4, 6, 9, 11) else 31))
            except ValueError:
                return None
    m = _MOIS_ANNEE.match(t)
    if m:
        mois = _mois(m.group('mois'))
        if mois:
            return date(int(m.group('annee')), mois, 1)
    m = _NUMERIQUE.match(t)
    if m:
        a = int(m.group('annee')); a = a + 2000 if a < 100 else a
        try:
            return date(a, int(m.group('mois')), int(m.group('jour')))
        except ValueError:
            return None
    return None


def est_signature(texte):
    return bool(_AUTEUR.match(re.sub(r'\s+', ' ', texte or '').strip()))


def _tout_en_capitales(texte):
    lettres = [c for c in texte if c.isalpha()]
    return len(lettres) >= 4 and all(c.isupper() for c in lettres)


_OUTILS = {'le', 'la', 'les', 'l', 'de', 'des', 'du', 'un', 'une', 'et', 'ou', 'à', 'au', 'aux',
           'en', 'dans', 'sur', 'sous', 'pour', 'par', 'avec', 'sans', 'ce', 'cet', 'cette', 'ces',
           'son', 'sa', 'ses', 'leur', 'leurs', 'mon', 'ma', 'mes', 'notre', 'nos', 'votre', 'vos',
           'qui', 'que', 'quoi', 'dont', 'où', 'ne', 'pas', 'plus', 'très', 'est', 'sont', 'il', 'elle',
           'ils', 'elles', 'on', 'nous', 'vous', 'je', 'tu', 'y', 'si', 'mais', 'donc', 'or', 'ni',
           'car', 'quand', 'comme', 'entre', 'vers', 'chez', 'contre', 'après', 'avant', 'depuis',
           'tout', 'tous', 'toute', 'toutes', 'autre', 'autres', 'même', 'bien', 'mal', 'peu',
           'quel', 'quelle', 'quels', 'quelles', 'faut', 'être', 'avoir', 'fait', 'faire'}


def _noms_propres(texte_corps):
    """Words the article itself writes with a capital — away from a sentence
    start — so a title in capitals can give them their capital back:
    « XI JINPING » becomes « Xi Jinping » because the body says Jinping."""
    noms = set()
    for phrase in re.split(r'[.!?…]\s+', texte_corps or ''):
        for mot in re.findall(r"[A-ZÀ-Ý][\wà-ÿ’'\-]+", phrase)[1:] if phrase[:1].isupper() else re.findall(r"[A-ZÀ-Ý][\wà-ÿ’'\-]+", phrase):
            if mot.lower() not in _OUTILS:
                noms.add(mot.lower())
    return noms


def casse_de_titre(texte, corps=''):
    """« LE RN ET L'EUROPE » → « Le RN et l'Europe ». Un titre déjà en bas de
    casse est laissé tel quel."""
    t = clean_text(texte)
    if not _tout_en_capitales(t):
        return t
    propres = _MAJ | _noms_propres(corps)
    mots = []
    for i, mot in enumerate(t.split(' ')):
        noyau = re.sub(r'[^\w]', '', mot)
        if noyau.upper() in SIGLES and noyau.lower() not in propres:
            mots.append(mot)
            continue
        bas = mot.lower()
        # Les élisions : « L'EUROPE » → « l'Europe ».
        m = re.match(r"^([ldjmnstc]['’])(.+)$", bas)
        if m:
            reste = m.group(2)
            reste = reste.capitalize() if (i == 0 or re.sub(r'[^\w]', '', reste) in propres) else reste
            mots.append((m.group(1).capitalize() if i == 0 else m.group(1)) + reste)
            continue
        if i == 0 or re.sub(r'[^\w]', '', bas) in propres:
            mots.append(bas.capitalize())
        else:
            mots.append(bas)
    return ' '.join(mots)


# Un titre en capitales collé au premier paragraphe : « FRANCE LIBÉRÉE On est
# entré… ». Au moins deux mots en capitales, puis une phrase qui commence.
_TITRE_COLLE = re.compile(r"^((?:[A-ZÀ-Ý0-9][A-ZÀ-Ý0-9’'\-.,:!?]*\s+){1,}[A-ZÀ-Ý0-9][A-ZÀ-Ý0-9’'\-.,:!?]*)\s+(?=[A-ZÀ-Ý][a-zà-ÿ’'])")


def _typographie(texte):
    """Les tics de frappe : « Clémenceau.! » → « Clémenceau ! », « .. » → « . »,
    « mot,mot » → « mot, mot », espace avant la ponctuation haute."""
    t = texte
    t = re.sub(r'\.{2}(?!\.)', '.', t)                 # deux points → un (les « … » restent)
    t = re.sub(r'\.\s*([!?])', r'\1', t)               # « .! » « .? » → « ! » « ? »
    t = re.sub(r'\s*([!?:;])', ' \\1', t)         # espace fine insécable avant ! ? : ;
    t = re.sub(r'(?<=\d) :', ':', t)              # sauf dans une heure « 18:30 »
    t = re.sub(r'\s+([,.)])', r'\1', t)                # jamais d'espace avant , . )
    t = re.sub(r',(?=[^\s\d])', ', ', t)               # toujours un après la virgule
    t = re.sub(r'\(\s+', '(', t)
    # Les suites d'espaces insécables qui servaient à centrer dans Google Docs.
    t = re.sub(r'[ \t\xa0]{2,}', ' ', t)
    return t


def preparer(source_html, nom_fichier):
    """From the raw export: (title, written_at, body_html, body_text).

    Title: the first block when it is a heading, is all in capitals, or is a
    short line without sentence punctuation; failing that the Drive name.
    Signature: author and date lines at the very end (up to four), and a
    date line at the very top, removed — the date kept.
    """
    html = clean_article_html(source_html or '')
    soup = BeautifulSoup(html, 'html.parser')
    blocs = [b for b in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
             if b.get_text(strip=True)]

    titre, quand = None, None

    # En tête : une date seule, puis le titre.
    while blocs:
        texte = blocs[0].get_text(' ', strip=True)
        d = lire_date(texte)
        if d and not quand:
            quand = d; blocs[0].decompose(); blocs = blocs[1:]
            continue
        if est_signature(texte):
            blocs[0].decompose(); blocs = blocs[1:]
            continue
        break
    corps_texte = ' '.join(b.get_text(' ', strip=True) for b in blocs[1:]) if blocs else ''
    if blocs:
        premier = blocs[0]
        texte = premier.get_text(' ', strip=True)
        court_sans_point = len(texte) <= 110 and not re.search(r'[.!?]\s*$', texte.rstrip('.!? '))
        if premier.name in ('h1', 'h2', 'h3') or _tout_en_capitales(texte) or (court_sans_point and len(texte) <= 80):
            titre = casse_de_titre(texte, corps_texte)
            premier.decompose(); blocs = blocs[1:]
        else:
            m = _TITRE_COLLE.match(texte)
            if m and len(m.group(1).split()) >= 2 and len(m.group(1)) <= 90:
                titre = casse_de_titre(m.group(1), texte)
                # Le reste du paragraphe devient le premier paragraphe.
                nouveau = soup.new_tag('p'); nouveau.string = texte[m.end():].strip()
                premier.replace_with(nouveau); blocs[0] = nouveau

    # Un sous-titre en capitales collé au premier paragraphe du corps —
    # « FRANCE LIBÉRÉE..FRANCE RASSEMBLÉE On est entré… » — devient sa propre ligne.
    if blocs:
        texte = blocs[0].get_text(' ', strip=True)
        m = _TITRE_COLLE.match(texte)
        if m and len(m.group(1).split()) >= 2 and len(m.group(1)) <= 90:
            sous = soup.new_tag('p'); gras = soup.new_tag('strong')
            gras.string = clean_text(m.group(1)); sous.append(gras)
            reste = soup.new_tag('p'); reste.string = texte[m.end():].strip()
            blocs[0].insert_before(sous); blocs[0].replace_with(reste); blocs[0] = reste

    # En pied : signature et date, dans l'ordre qu'on voudra, quatre blocs au plus.
    retires = 0
    while blocs and retires < 4:
        texte = blocs[-1].get_text(' ', strip=True)
        d = lire_date(texte)
        if d:
            quand = quand or d
        elif not est_signature(texte):
            break
        blocs[-1].decompose(); blocs = blocs[:-1]; retires += 1

    for node in list(soup.find_all(string=True)):
        propre = _typographie(str(node))
        if propre != str(node):
            node.replace_with(propre)
    for p in list(soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])):
        if not p.get_text(strip=True) and not p.find('img'):
            p.decompose()
            continue
        # Pas d'espace en tête de paragraphe : c'était du centrage à la main.
        premier = p.find(string=True)
        if premier and str(premier) != str(premier).lstrip(' \xa0\t'):
            premier.replace_with(str(premier).lstrip(' \xa0\t'))

    corps = str(soup).strip()
    texte = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', corps)).strip()
    return (titre or casse_de_titre(nom_fichier, texte) or nom_fichier, quand, corps, texte)


def appliquer(doc):
    """Run `preparer` on a stored document and write the result on it."""
    titre, quand, corps, texte = preparer(doc.source_html or doc.content_html or '', doc.name)
    doc.title = titre[:300]
    doc.written_at = quand
    doc.content_html = corps
    doc.content_text = texte
    doc.word_count = len(texte.split())
