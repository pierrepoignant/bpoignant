"""Fabriquer l'ebook Kindle : un EPUB reflowable et une image de première de
couverture (JPEG), à partir du même contenu que le PDF d'impression.

Kindle veut du texte *recomposable* (le lecteur change la police, la taille,
la liseuse adapte la mise en page) — à l'opposé du PDF 6×9 à pages fixes, qui
donne une liseuse illisible. On produit donc :

- un EPUB (zip de XHTML + OPF + NCX + nav), écrit sans dépendance externe ;
- une couverture de face seule, en JPEG 1600×2560 (ratio ~1,6 exigé par KDP),
  dessinée avec Pillow dans la palette « Élysée » (bleu de France, cercle rouge).

Le corps reprend `_docs_par_theme()` et les champs liminaires de `meta`, comme
`livre.pdf.construire`.
"""

import html
import io
import os
import uuid
import zipfile
from datetime import date, datetime

from bs4 import BeautifulSoup

from livre.pdf import MOIS, PALETTES, POLICES, PORTRAIT_ROND, _glyphes


def _date_fr(d):
    if not d:
        return ''
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.day} {MOIS[d.month]} {d.year}"


# ─── Nettoyage du contenu en XHTML valide ───────────────────

def _fragment(raw):
    """Un fragment HTML quelconque → XHTML bien formé (balises fermées),
    sans scripts ni styles. BeautifulSoup ferme les balises vides en `<br/>`."""
    soup = BeautifulSoup(raw or '', 'html.parser')
    for t in soup(['script', 'style', 'meta', 'link', 'head', 'title']):
        t.decompose()
    for wrap in soup(['html', 'body']):
        wrap.unwrap()
    return soup.decode().strip()


def _corps(texte):
    """Un champ liminaire (préface, biographie…) → paragraphes XHTML. Du HTML
    s'il en contient, sinon chaque bloc séparé par une ligne vide devient un
    paragraphe."""
    texte = (texte or '').strip()
    if not texte:
        return ''
    if '<' in texte and '>' in texte:
        return _fragment(texte)
    blocs = [b.strip() for b in texte.replace('\r\n', '\n').split('\n\n') if b.strip()]
    return ''.join(f'<p>{html.escape(b).replace(chr(10), "<br/>")}</p>' for b in blocs)


def _page(titre, corps, lang='fr'):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">\n'
        f'<head><meta charset="utf-8"/><title>{html.escape(titre)}</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f'<body>{corps}</body>\n</html>\n'
    )


_CSS = """\
body { font-family: Garamond, 'EB Garamond', Georgia, serif; line-height: 1.5;
       margin: 0 1em; text-align: justify; hyphens: auto; }
h1, h2, h3 { font-weight: bold; text-align: left; line-height: 1.2; }
h1 { font-size: 1.8em; margin: 1.5em 0 0.3em; }
h2.chapitre { font-size: 1.5em; margin: 3em 0 1em; color: #000091;
              page-break-before: always; }
h3.chronique { font-size: 1.2em; margin: 2em 0 0.1em; }
.date { color: #5a606e; font-style: italic; font-size: 0.85em; margin: 0 0 0.6em;
        text-align: left; }
.intro { color: #333; font-style: italic; margin: 0 0 1em; }
.liminaire { page-break-before: always; }
.titre-livre { text-align: center; margin-top: 25%; }
.titre-livre .auteur { font-size: 1.6em; font-weight: bold; color: #000091; }
.titre-livre .titre { font-size: 2.1em; font-style: italic; margin: 0.6em 0; }
.titre-livre .sous { color: #5a606e; }
.dedicace { text-align: center; font-style: italic; margin-top: 30%; }
.epigraphe { text-align: center; font-style: italic; margin-top: 20%; }
.epigraphe .src { font-style: normal; color: #5a606e; font-size: 0.85em; }
img.cover { max-width: 100%; height: auto; display: block; margin: 0 auto; }
"""


def _chronique_html(d):
    titre = html.escape(d.titre or '')
    date_txt = html.escape(_date_fr(getattr(d, 'date_livre', None)))
    parts = [f'<h3 class="chronique">{titre}</h3>']
    if date_txt:
        parts.append(f'<p class="date">{date_txt}</p>')
    if getattr(d, 'intro', None):
        parts.append(f'<div class="intro">{_corps(d.intro)}</div>')
    parts.append(_fragment(d.content_html))
    return ''.join(parts)


def construire_epub(docs_par_theme, meta, cover_bytes=None):
    """Assemble l'EPUB et renvoie les octets. `cover_bytes` : le JPEG de
    couverture (sinon pas de couverture intégrée)."""
    titre = meta.get('titre') or 'Chroniques'
    auteur = meta.get('auteur') or 'Bernard Poignant'
    sous = meta.get('sous_titre') or ''
    livre_id = (meta.get('isbn') or '').strip() or f"urn:uuid:{uuid.uuid4()}"
    lang = 'fr'

    # (id, nom de fichier, titre de nav, xhtml) ; nav=None → hors sommaire.
    docs = []

    # Couverture.
    if cover_bytes:
        docs.append(('cover', 'cover.xhtml', None,
                     _page(titre, '<div style="text-align:center;margin:0">'
                           '<img class="cover" src="cover.jpg" alt="Couverture"/></div>')))

    # Page de titre.
    titre_html = (
        '<div class="titre-livre">'
        f'<div class="auteur">{html.escape(auteur)}</div>'
        f'<div class="titre">{html.escape(titre)}</div>'
        + (f'<div class="sous">{html.escape(sous)}</div>' if sous else '')
        + '</div>'
    )
    docs.append(('titre', 'titre.xhtml', None, _page(titre, titre_html)))

    if (meta.get('dedicace') or '').strip():
        docs.append(('dedicace', 'dedicace.xhtml', None,
                     _page('Dédicace', f'<div class="dedicace">{_corps(meta["dedicace"])}</div>')))

    if (meta.get('epigraphe') or '').strip():
        src = (meta.get('epigraphe_src') or '').strip()
        ep = (f'<div class="epigraphe">{_corps(meta["epigraphe"])}'
              + (f'<p class="src">{html.escape(src)}</p>' if src else '') + '</div>')
        docs.append(('epigraphe', 'epigraphe.xhtml', None, _page('Exergue', ep)))

    if (meta.get('avant_propos') or '').strip():
        corps = f'<h2 class="chapitre">Préface</h2>{_corps(meta["avant_propos"])}'
        docs.append(('preface', 'preface.xhtml', 'Préface',
                     _page('Préface', f'<div class="liminaire">{corps}</div>')))

    # Chapitres (un fichier XHTML par thème).
    for i, entree in enumerate(docs_par_theme, 1):
        nom, chroniques = entree[0], entree[1]
        intro_chap = entree[2] if len(entree) > 2 else None
        if not chroniques:
            continue
        corps = [f'<h2 class="chapitre">{html.escape(nom)}</h2>']
        if (intro_chap or '').strip():
            corps.append(f'<div class="intro">{_corps(intro_chap)}</div>')
        for d in chroniques:
            corps.append(_chronique_html(d))
        docs.append((f'chap{i}', f'chap{i}.xhtml', nom, _page(nom, ''.join(corps))))

    if (meta.get('biographie') or '').strip():
        corps = f'<h2 class="chapitre">L\'auteur</h2>{_corps(meta["biographie"])}'
        docs.append(('bio', 'bio.xhtml', "L'auteur", _page("L'auteur", corps)))

    if (meta.get('remerciements') or '').strip():
        corps = f'<h2 class="chapitre">Remerciements</h2>{_corps(meta["remerciements"])}'
        docs.append(('merci', 'merci.xhtml', 'Remerciements', _page('Remerciements', corps)))

    # ── OPF (manifest + spine) ──
    manifest = ['<item id="style" href="style.css" media-type="text/css"/>',
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
    if cover_bytes:
        manifest.append('<item id="cover-image" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
    spine = []
    for did, fn, _nav, _xhtml in docs:
        manifest.append(f'<item id="{did}" href="{fn}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{did}"/>')

    meta_cover = '<meta name="cover" content="cover-image"/>' if cover_bytes else ''
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{html.escape(livre_id)}</dc:identifier>\n'
        f'    <dc:title>{html.escape(titre)}</dc:title>\n'
        f'    <dc:creator>{html.escape(auteur)}</dc:creator>\n'
        f'    <dc:language>{lang}</dc:language>\n'
        f'    <dc:date>{date.today().isoformat()}</dc:date>\n'
        f'    <meta property="dcterms:modified">{datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>\n'
        f'    {meta_cover}\n'
        '  </metadata>\n'
        '  <manifest>\n    ' + '\n    '.join(manifest) + '\n  </manifest>\n'
        '  <spine toc="ncx">\n    ' + '\n    '.join(spine) + '\n  </spine>\n'
        '</package>\n'
    )

    # ── nav.xhtml (EPUB3) ──
    liens = ''.join(f'<li><a href="{fn}">{html.escape(nav)}</a></li>'
                    for _did, fn, nav, _x in docs if nav)
    nav_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{lang}" lang="{lang}">\n<head><meta charset="utf-8"/><title>Sommaire</title></head>\n'
        '<body><nav epub:type="toc" id="toc"><h1>Sommaire</h1><ol>'
        f'{liens}</ol></nav></body>\n</html>\n'
    )

    # ── toc.ncx (compat EPUB2 / vieux Kindle) ──
    points = []
    for n, (_did, fn, nav, _x) in enumerate((d for d in docs if d[2]), 1):
        points.append(
            f'<navPoint id="n{n}" playOrder="{n}"><navLabel><text>{html.escape(nav)}</text>'
            f'</navLabel><content src="{fn}"/></navPoint>')
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{html.escape(livre_id)}"/></head>\n'
        f'<docTitle><text>{html.escape(titre)}</text></docTitle>\n'
        '<navMap>' + ''.join(points) + '</navMap>\n</ncx>\n'
    )

    # ── Écriture du zip EPUB ──
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        # mimetype en premier, non compressé (exigence EPUB).
        z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip',
                   compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml',
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>\n')
        z.writestr('OEBPS/content.opf', opf)
        z.writestr('OEBPS/nav.xhtml', nav_xhtml)
        z.writestr('OEBPS/toc.ncx', ncx)
        z.writestr('OEBPS/style.css', _CSS)
        if cover_bytes:
            z.writestr('OEBPS/cover.jpg', cover_bytes)
        for _did, fn, _nav, xhtml in docs:
            z.writestr(f'OEBPS/{fn}', xhtml)
    return buf.getvalue()


# ─── Couverture de face en JPEG (pour Kindle) ───────────────

def couverture_image(meta, palette='elysee', taille=(1600, 2560)):
    """Première de couverture seule, fond blanc, portrait dans un rond, titre.
    Renvoie les octets JPEG. Ratio ~1,6 (1600×2560), conforme à KDP."""
    from PIL import Image, ImageDraw, ImageFont

    pal = PALETTES.get(palette, PALETTES['elysee'])
    bleu = tuple(pal['texte'])
    rouge = tuple(pal['cercle'])
    gris = (90, 96, 110)
    W, H = taille

    img = Image.new('RGB', (W, H), 'white')
    draw = ImageDraw.Draw(img)

    def font(fichier, taille_px):
        return ImageFont.truetype(os.path.join(POLICES, fichier), taille_px)

    f_auteur = font('ebg-700-normal.ttf', 118)
    f_titre = font('ebg-400-italic.ttf', 150)
    f_sous = font('ebg-400-normal.ttf', 58)

    def centre(texte, fnt, y, couleur):
        l = draw.textlength(texte, font=fnt)
        draw.text(((W - l) / 2, y), texte, font=fnt, fill=couleur)
        bbox = fnt.getbbox(texte)
        return y + (bbox[3] - bbox[1]) + 20

    def enroule(texte, fnt, largeur_max):
        mots, lignes, cur = texte.split(), [], ''
        for m in mots:
            essai = (cur + ' ' + m).strip()
            if draw.textlength(essai, font=fnt) <= largeur_max:
                cur = essai
            else:
                if cur:
                    lignes.append(cur)
                cur = m
        if cur:
            lignes.append(cur)
        return lignes

    # Portrait dans un rond, en haut.
    diam = int(W * 0.52)
    cx = W // 2
    cy = int(H * 0.30)
    try:
        if PORTRAIT_ROND and os.path.exists(PORTRAIT_ROND):
            p = Image.open(PORTRAIT_ROND).convert('RGBA')
            p = p.resize((diam, diam), Image.LANCZOS)
            img.paste(p, (cx - diam // 2, cy - diam // 2), p)
    except Exception:
        pass
    # Anneau rouge.
    r = diam // 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=rouge, width=10)

    # Auteur, filet, titre, sous-titre.
    y = cy + r + 110
    y = centre(_glyphes(meta.get('auteur') or 'Bernard Poignant'), f_auteur, y, bleu)
    draw.line([(cx - 120, y + 10), (cx + 120, y + 10)], fill=rouge, width=5)
    y += 60
    for ligne in enroule(_glyphes(meta.get('titre') or 'Chroniques'), f_titre, W * 0.82):
        y = centre(ligne, f_titre, y, rouge)
    sous = (meta.get('sous_titre') or '').strip()
    if sous:
        y += 24
        for ligne in enroule(_glyphes(sous), f_sous, W * 0.80):
            y = centre(ligne, f_sous, y, gris)

    out = io.BytesIO()
    img.save(out, format='JPEG', quality=90)
    return out.getvalue()
