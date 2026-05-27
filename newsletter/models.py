from datetime import datetime

from init_db import db


class Subscriber(db.Model):
    __tablename__ = 'subscribers'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    prenom = db.Column(db.String(120), nullable=True)
    nom = db.Column(db.String(120), nullable=True)
    ville = db.Column(db.String(120), nullable=True)
    # Random per-row token used in the unsubscribe link so anyone with the
    # email cannot guess someone else's unsub URL.
    token = db.Column(db.String(64), unique=True, nullable=False)
    subscribed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    unsubscribed_at = db.Column(db.DateTime, nullable=True)

    @property
    def display_name(self):
        full = ' '.join(p for p in (self.prenom, self.nom) if p)
        return full or None

    @property
    def is_active(self):
        return self.unsubscribed_at is None
