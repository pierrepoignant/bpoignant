from datetime import datetime, date

from init_db import db


class NewsSuggestion(db.Model):
    """Une suggestion de « Minute » née de l'actualité du jour, rapprochée de ce
    que Bernard a déjà écrit (articles + chroniques du livre) et de ses thèmes.

    Produite chaque soir par le cron de veille, relue sur /admin/veille, et
    envoyée par e-mail aux administrateurs."""

    __tablename__ = 'news_suggestions'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    jour = db.Column(db.Date, default=date.today, nullable=False, index=True)

    # L'actualité qui déclenche la suggestion.
    news_titre = db.Column(db.String(400), nullable=False)
    news_url = db.Column(db.String(800), nullable=True)
    news_source = db.Column(db.String(160), nullable=True)

    # L'écho : ce que Bernard a déjà dit sur le sujet (chronique / article / thème).
    echo_titre = db.Column(db.String(400), nullable=True)
    echo_url = db.Column(db.String(800), nullable=True)
    theme = db.Column(db.String(120), nullable=True)

    # Le brouillon, dans son ton.
    angle = db.Column(db.String(400), nullable=True)      # l'idée en une ligne
    accroche = db.Column(db.Text, nullable=True)           # la première phrase
    bandeau = db.Column(db.String(60), nullable=True)      # titre incrusté, capitales
    legende = db.Column(db.Text, nullable=True)            # texte du post
    script = db.Column(db.Text, nullable=True)             # ~35 s à dire face caméra

    pertinence = db.Column(db.Integer, nullable=True)      # 0–100, jugée par l'IA
    statut = db.Column(db.String(20), default='nouveau', nullable=False, index=True)
    # nouveau · retenu · ecarte · produit
    decided_at = db.Column(db.DateTime, nullable=True)
