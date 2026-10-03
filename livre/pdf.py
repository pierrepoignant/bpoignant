"""Fabriquer le livre en PDF, au format d'impression d'Amazon KDP.

Le livre reprend les chroniques classées, chapitre par chapitre dans l'ordre
des thèmes, et dans chaque chapitre de la plus ancienne à la plus récente — on
lit une décennie dans le sens où elle s'est écrite. Chaque chronique porte son
titre, sa date, l'intro de Bernard quand il y en a une, puis le texte.

Format : 6 × 9 pouces, la taille brochée la plus courante sur KDP. Les marges
sont dissymétriques — plus larges côté reliure — et alternent donc selon que la
page est à droite ou à gauche. Les polices sont intégrées (EB Garamond),
condition d'acceptation chez KDP.

On écrit avec fpdf2, pur Python : la machine n'a ni Pango ni Cairo, donc pas de
WeasyPrint. Le compromis est un rendu de texte simple — ce qui convient à un
livre de chroniques, qui est du texte courant.
"""

import io
import logging
import os
import re
from datetime import date, datetime

from bs4 import BeautifulSoup
from fpdf import FPDF

log = logging.getLogger(__name__)

POLICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')

# 6 × 9 pouces en millimètres (l'unité de fpdf).
PAGE_L, PAGE_H = 152.4, 228.6
# Marge unique sur toutes les pages : le texte commence toujours au même
# endroit. Des marges dissymétriques (reliure vs extérieur) seraient plus
# justes pour un livre relié, mais fpdf garde la marge de la page précédente
# quand un paragraphe déborde d'une page à l'autre — le bord gauche sautait
# alors d'une page sur deux. 20 mm tiennent au-dessus du minimum de reliure de
# KDP jusqu'à 700 pages.
MARGE_COTE = 20.0
MARGE_HAUT = 18.0
MARGE_BAS = 18.0

BLEU = (22, 33, 62)        # le bleu du site / de La Minute
GRIS = (90, 90, 96)
NOIR = (20, 20, 20)

MOIS = ['', 'janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet',
        'août', 'septembre', 'octobre', 'novembre', 'décembre']


# EB Garamond n'a pas l'espace fine insécable (U+202F) : on la ramène à
# l'espace insécable normale, qu'elle possède.
def _glyphes(texte):
    return (texte or '').replace('\u202f', '\u00a0').replace('\u2009', '\u00a0')


def _date_fr(d):
    if not d:
        return ''
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.day} {MOIS[d.month]} {d.year}"


def _paragraphes(html):
    """Le texte d'une chronique, paragraphe par paragraphe, en clair.

    On ne garde que le texte : le livre est composé par le PDF, pas par le
    HTML de Google Docs. Un titre intermédiaire (en gras dans la source)
    revient comme un paragraphe marqué pour être mis en valeur.
    """
    soup = BeautifulSoup(html or '', 'html.parser')
    out = []
    for bloc in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li']):
        texte = bloc.get_text(' ', strip=True)
        if not texte:
            continue
        gras = bloc.name in ('h1', 'h2', 'h3', 'h4') or bool(bloc.find(['strong', 'b']) and len(bloc.find(['strong', 'b']).get_text(strip=True)) >= len(texte) * 0.8)
        out.append(('titre' if gras else 'p', _glyphes(texte)))
    if not out:  # pas de balises de bloc : on coupe sur les sauts de ligne
        for ligne in re.split(r'\n{2,}', soup.get_text('\n')):
            ligne = ligne.strip()
            if ligne:
                out.append(('p', _glyphes(ligne)))
    return out


class Livre(FPDF):
    def __init__(self, titre, sous_titre, auteur):
        super().__init__(unit='mm', format=(PAGE_L, PAGE_H))
        self.titre_livre = titre
        self.sous_titre = sous_titre
        self.auteur = auteur
        self.chapitre_courant = ''
        self.set_auto_page_break(True, margin=MARGE_BAS)
        self.set_margins(MARGE_COTE, MARGE_HAUT, MARGE_COTE)
        self.set_title(titre)
        self.set_author(auteur)
        self.set_lang('fr')
        for style, fichier in (('', 'ebg-400-normal'), ('B', 'ebg-700-normal'),
                               ('I', 'ebg-400-italic'), ('BI', 'ebg-500-italic')):
            self.add_font('garamond', style, os.path.join(POLICES, f'{fichier}.ttf'))
        self.set_font('garamond', '', 11)

    def header(self):
        self.set_margins(MARGE_COTE, MARGE_HAUT, MARGE_COTE)
        # Pas d'en-tête sur les ouvertures de chapitre ni les pages liminaires.
        if getattr(self, 'sans_tete', False) or self.page_no() <= self.pages_liminaires:
            return
        self.set_font('garamond', 'I', 8.5)
        self.set_text_color(*GRIS)
        droite = self.page_no() % 2 == 1
        titre = self.chapitre_courant if droite else self.titre_livre
        self.set_y(9)
        self.cell(0, 6, titre, align='R' if droite else 'L')
        self.set_y(MARGE_HAUT)
        self.set_text_color(*NOIR)

    def footer(self):
        if self.page_no() <= self.pages_liminaires:
            return
        self.set_y(-13)
        self.set_font('garamond', '', 9)
        self.set_text_color(*GRIS)
        self.cell(0, 6, str(self.page_no()), align='C')
        self.set_text_color(*NOIR)

    pages_liminaires = 4   # titre, copyright, (sommaire posé après coup)

    # ─── Pages ──────────────────────────────────────────────

    def page_de_titre(self):
        self.sans_tete = True
        self.add_page()
        self.set_y(60)
        self.set_font('garamond', 'B', 30)
        self.set_text_color(*BLEU)
        self.multi_cell(0, 14, _glyphes(self.titre_livre), align='C')
        self.ln(4)
        if self.sous_titre:
            self.set_font('garamond', 'I', 15)
            self.set_text_color(*GRIS)
            self.multi_cell(0, 9, _glyphes(self.sous_titre), align='C')
        self.set_y(-55)
        self.set_font('garamond', '', 15)
        self.set_text_color(*NOIR)
        self.cell(0, 8, self.auteur, align='C')

    def page_copyright(self, annee):
        self.add_page()
        # Bloc calé en bas, sans saut de page automatique au dernier mot.
        self.set_auto_page_break(False)
        self.set_y(-58)
        self.set_font('garamond', '', 9.5)
        self.set_text_color(*GRIS)
        lignes = [
            f"© {annee} {self.auteur}",
            "Tous droits réservés.",
            "",
            _glyphes(self.titre_livre) + (f" — {_glyphes(self.sous_titre)}" if self.sous_titre else ""),
            "Chroniques parues sur bernardpoignant.fr.",
            "",
            f"Première édition, {annee}.",
        ]
        for l in lignes:
            self.cell(0, 5, l, align='C', new_x='LMARGIN', new_y='NEXT')
        self.set_text_color(*NOIR)
        self.set_auto_page_break(True, margin=MARGE_BAS)
        self.sans_tete = False

    def ouvrir_chapitre(self, nom, n_chroniques, intro=None):
        self.chapitre_courant = nom
        # Un chapitre commence toujours sur une page de droite (impaire).
        self.sans_tete = True
        self.add_page()
        if self.page_no() % 2 == 0:
            self.add_page()
        self.set_y(70)
        self.set_font('garamond', '', 13)
        self.set_text_color(*GRIS)
        self.cell(0, 8, '·', align='C', new_x='LMARGIN', new_y='NEXT')
        self.ln(6)
        self.set_font('garamond', 'B', 26)
        self.set_text_color(*BLEU)
        self.multi_cell(0, 13, _glyphes(nom), align='C')
        self.ln(3)
        self.set_font('garamond', 'I', 11)
        self.set_text_color(*GRIS)
        self.cell(0, 7, f"{n_chroniques} chronique{'s' if n_chroniques > 1 else ''}", align='C', new_x='LMARGIN', new_y='NEXT')
        if intro:
            self.ln(8)
            self.set_font('garamond', 'I', 12)
            self.set_text_color(*NOIR)
            marge = (PAGE_L - self.l_margin - self.r_margin) * 0.12
            self.set_x(self.l_margin + marge)
            self.multi_cell(PAGE_L - self.l_margin - self.r_margin - 2 * marge, 7, _glyphes(intro.strip()), align='C')
        self.set_text_color(*NOIR)
        # Enregistre l'entrée du sommaire, à la page du chapitre.
        self.sommaire.append((nom, self.page_no()))
        self.sans_tete = False

    def chronique(self, titre, quand, intro, html):
        # Une chronique ne doit pas commencer en bas de page : si moins de six
        # centimètres restent, on passe à la suivante.
        if self.get_y() > PAGE_H - MARGE_BAS - 60:
            self.add_page()
        else:
            self.ln(10)
        self.set_font('garamond', 'B', 15)
        self.set_text_color(*NOIR)
        self.multi_cell(0, 8, _glyphes(titre))
        if quand:
            self.ln(1)
            self.set_font('garamond', 'I', 10)
            self.set_text_color(*GRIS)
            self.cell(0, 6, _date_fr(quand), new_x='LMARGIN', new_y='NEXT')
        self.ln(3)
        if intro:
            self.set_font('garamond', 'I', 11)
            self.set_text_color(*BLEU)
            self.set_x(self.l_margin + 6)
            self.multi_cell(PAGE_L - self.l_margin - self.r_margin - 12, 6.2, _glyphes(intro.strip()))
            self.ln(3)
        self.set_text_color(*NOIR)
        for genre, texte in _paragraphes(html):
            if genre == 'titre':
                self.ln(2)
                self.set_font('garamond', 'B', 11.5)
                self.multi_cell(0, 6.4, texte)
                self.ln(1)
            else:
                self.set_font('garamond', '', 11)
                self.multi_cell(0, 6.2, texte, align='J')
                self.ln(1.6)



def construire(docs_par_theme, titre, sous_titre, auteur, portrait_png=None):
    """Assemble le PDF et renvoie les octets.

    `docs_par_theme` : liste de (nom_theme, [chroniques]) dans l'ordre du livre.
    Chaque chronique a .titre, .date_livre, .intro, .content_html.
    """
    pdf = Livre(titre, sous_titre, auteur)
    pdf.sommaire = []

    pdf.page_de_titre()
    pdf.page_copyright(date.today().year)

    # On réserve la place du sommaire : une page (deux si long), remplie après.
    pages_sommaire = max(1, (sum(1 for _ in docs_par_theme) + 24) // 26)
    for _ in range(pages_sommaire):
        pdf.sans_tete = True
        pdf.add_page()
    pdf.pages_liminaires = pdf.page_no()
    debut_sommaire = pdf.pages_liminaires - pages_sommaire + 1

    for entree in docs_par_theme:
        nom, chroniques = entree[0], entree[1]
        intro_chap = entree[2] if len(entree) > 2 else None
        if not chroniques:
            continue
        pdf.ouvrir_chapitre(nom, len(chroniques), intro_chap)
        for d in chroniques:
            pdf.chronique(d.titre, d.date_livre, d.intro, d.content_html)

    # Composer le sommaire sur les pages réservées.
    pdf.page = debut_sommaire
    pdf.sans_tete = True
    pdf.set_xy(pdf.l_margin, MARGE_HAUT)
    pdf.set_font('garamond', 'B', 20)
    pdf.set_text_color(*BLEU)
    pdf.cell(0, 12, 'Sommaire', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(6)
    for nom, page in pdf.sommaire:
        pdf.set_font('garamond', '', 12)
        pdf.set_text_color(*NOIR)
        largeur = PAGE_L - pdf.l_margin - pdf.r_margin
        pdf.cell(largeur - 16, 8, _glyphes(nom))
        pdf.set_font('garamond', '', 11)
        pdf.set_text_color(*GRIS)
        pdf.cell(16, 8, str(page), align='R', new_x='LMARGIN', new_y='NEXT')

    sortie = pdf.output()
    return bytes(sortie)


def apercu(docs_par_theme, titre, sous_titre, auteur, max_chroniques=6):
    """Un PDF court — les premières chroniques de chaque chapitre — pour voir
    la mise en page sans composer quatre cents pages."""
    court = [(nom, chroniques[:max_chroniques]) for nom, chroniques in docs_par_theme]
    return construire(court, titre, sous_titre, auteur)


# ─── Couverture ─────────────────────────────────────────────

PORTRAIT_ROND = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'portrait_rond.png')


def _plume(pdf, cx, bas, hauteur, couleur):
    """Une plume stylisée, dessinée verticalement, centrée en cx, pointe en bas."""
    pdf.set_fill_color(*couleur)
    larg = hauteur * 0.12
    haut = bas - hauteur
    # corps
    pdf.rect(cx - larg / 2, haut, larg, hauteur * 0.62, style='F')
    # bague
    pdf.rect(cx - larg * 0.6, haut + hauteur * 0.60, larg * 1.2, hauteur * 0.05, style='F')
    # bec triangulaire
    y0 = haut + hauteur * 0.66
    pdf.polygon([(cx - larg * 0.6, y0), (cx + larg * 0.6, y0), (cx, bas)], style='F')
    # fente
    pdf.set_fill_color(22, 33, 62)
    pdf.rect(cx - larg * 0.04, y0 + hauteur * 0.05, larg * 0.08, hauteur * 0.22, style='F')


def couverture(titre, sous_titre, auteur, portrait=None):
    """La première de couverture seule, 6×9 + fond perdu, en PDF.

    Bleu européen plein, le portrait dans un rond en haut à droite, la plume en
    bas, le titre au centre. Un aperçu : la couverture complète d'impression
    (dos + quatrième) se calcule une fois le nombre de pages connu.
    """
    BLEED = 3.175  # 0,125 pouce
    L, H = PAGE_L + 2 * BLEED, PAGE_H + 2 * BLEED
    pdf = FPDF(unit='mm', format=(L, H))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    for style, f in (('', 'ebg-400-normal'), ('B', 'ebg-700-normal'), ('I', 'ebg-400-italic')):
        pdf.add_font('garamond', style, os.path.join(POLICES, f'{f}.ttf'))
    pdf.add_page()
    pdf.set_fill_color(22, 33, 62)          # bleu européen
    pdf.rect(0, 0, L, H, style='F')
    # Filet doré pour poser le titre, dans la largeur utile.
    pdf.set_draw_color(201, 162, 77)
    pdf.set_line_width(0.4)

    port = portrait or (PORTRAIT_ROND if os.path.exists(PORTRAIT_ROND) else None)
    if port and os.path.exists(port):
        d = 52
        pdf.image(port, x=L - BLEED - d - 14, y=BLEED + 16, w=d, h=d)

    pdf.set_xy(BLEED + 16, H * 0.46)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('garamond', 'B', 40)
    pdf.multi_cell(L - 2 * (BLEED + 16), 17, _glyphes(titre), align='L')
    pdf.ln(3)
    pdf.set_x(BLEED + 16)
    pdf.line(BLEED + 16, pdf.get_y(), BLEED + 16 + 40, pdf.get_y())
    pdf.ln(5)
    pdf.set_x(BLEED + 16)
    pdf.set_text_color(201, 162, 77)
    pdf.set_font('garamond', 'I', 16)
    pdf.multi_cell(L - 2 * (BLEED + 16), 9, _glyphes(sous_titre), align='L')

    _plume(pdf, L / 2, H - BLEED - 24, 46, (201, 162, 77))

    pdf.set_xy(BLEED + 16, H - BLEED - 30)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('garamond', '', 17)
    pdf.cell(L - 2 * (BLEED + 16), 10, _glyphes(auteur), align='C')
    return bytes(pdf.output())
