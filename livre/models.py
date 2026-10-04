"""Le Livre : trier sept cents Google Docs en chapitres.

Bernard écrit dans Google Docs depuis 2017 ; une petite partie seulement est
devenue des articles du site. Le livre se fait à partir du tout : chaque
document reçoit un thème — un chapitre — ou est écarté. La décision est
gardée ici, document par document, pour qu'on ne relise jamais deux fois le
même texte.
"""

from datetime import datetime

from init_db import db


class BookTheme(db.Model):
    """Un chapitre du livre. Distinct des thèmes du site : le site classe pour
    le lecteur qui cherche, le livre pour celui qui lit d'un bout à l'autre."""
    __tablename__ = 'book_themes'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    position = db.Column(db.Integer, default=0, nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


STATUTS = ('a_classer', 'classe', 'ignore')


class BookDoc(db.Model):
    """Un Google Doc tel que Drive le liste, et ce qui en a été décidé."""
    __tablename__ = 'book_docs'

    id = db.Column(db.Integer, primary_key=True)
    drive_id = db.Column(db.String(80), unique=True, nullable=False, index=True)
    name = db.Column(db.String(300), nullable=False)
    # Le titre lu dans le document — sa première ligne, souvent en capitales —
    # qui n'est pas toujours le nom du fichier Drive.
    title = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, nullable=True)
    modified_at = db.Column(db.DateTime, nullable=True)
    # La date que Bernard a écrite en signant, quand il y en a une : c'est elle
    # qui date l'article dans le livre, pas la date du fichier.
    written_at = db.Column(db.Date, nullable=True)
    # L'article du site qui en est sorti, quand il y en a un — repéré au titre.
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=True)

    # Le texte, exporté une fois de Drive puis gardé : l'overlay s'ouvre sans
    # attendre, et le livre pourra être assemblé sans retourner chercher.
    # L'export brut de Drive après le seul passage de sécurité : on le garde
    # pour rejouer le nettoyage quand une règle change, sans retourner le chercher.
    source_html = db.Column(db.Text(16777215), nullable=True)
    content_html = db.Column(db.Text(16777215), nullable=True)
    # Le même texte sans balises : c'est là-dedans qu'on cherche un mot.
    content_text = db.Column(db.Text(16777215), nullable=True)
    content_fetched_at = db.Column(db.DateTime, nullable=True)
    word_count = db.Column(db.Integer, nullable=True)

    status = db.Column(db.String(12), default='a_classer', nullable=False, index=True)
    theme_id = db.Column(db.Integer, db.ForeignKey('book_themes.id'), nullable=True, index=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.String(300), nullable=True)

    # Le petit mot de Bernard en tête de chaque texte du livre.
    intro = db.Column(db.Text, nullable=True)
    intro_at = db.Column(db.DateTime, nullable=True)

    # Dans le livre ou mis de côté sans être déclassé : la curation de l'édition
    # imprimée (trop longue pour un seul volume) se fait ici, pas en retirant
    # le document du chapitre.
    in_book = db.Column(db.Boolean, default=True, nullable=False)
    # Ordre manuel dans le chapitre ; à vide, c'est l'ordre chronologique.
    book_position = db.Column(db.Integer, nullable=True)

    @property
    def titre(self):
        return self.title or self.name

    @property
    def date_livre(self):
        return self.written_at or (self.created_at.date() if self.created_at else None)

    seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # Disparu de Drive lors de la dernière lecture : gardé, mais signalé.
    missing = db.Column(db.Boolean, default=False, nullable=False)

    theme = db.relationship('BookTheme', backref=db.backref('docs', lazy='dynamic'))
    article = db.relationship('Article')
