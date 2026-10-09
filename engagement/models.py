from datetime import datetime

from init_db import db


class Comment(db.Model):
    __tablename__ = 'comments'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=True, index=True)
    # Un commentaire porte sur un article OU sur une vidéo de La Minute.
    video_id = db.Column(db.Integer, db.ForeignKey('tiktok_posts.id'), nullable=True, index=True)
    # Author identity is visitor-provided (no login required).
    # `prenom` is required; everything else is optional.
    prenom = db.Column(db.String(120), nullable=False)
    nom = db.Column(db.String(120), nullable=True)
    email = db.Column(db.String(255), nullable=True)  # never published
    content = db.Column(db.Text, nullable=False)
    approved = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=True)

    article = db.relationship('Article', backref=db.backref('comments', lazy='dynamic'))
    video = db.relationship('TikTokPost', backref=db.backref('comments', lazy='dynamic'))

    @property
    def sur_video(self):
        return self.video_id is not None

    @property
    def cible_titre(self):
        if self.video_id:
            return self.video.title if self.video else 'vidéo supprimée'
        return self.article.title if self.article else 'article supprimé'

    @property
    def display_name(self):
        full = ' '.join(p for p in (self.prenom, self.nom) if p)
        return full or self.prenom


class Reaction(db.Model):
    __tablename__ = 'reactions'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=True, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('tiktok_posts.id'), nullable=True, index=True)
    emoji = db.Column(db.String(8), nullable=False)
    # Stable hash of (IP + UA + SECRET_KEY) — no daily rotation here so a
    # visitor can't bypass de-dup by waiting a day. Raw IP is never stored.
    visitor_hash = db.Column(db.String(32), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('article_id', 'emoji', 'visitor_hash',
                            name='uq_reaction_visitor'),
        # MySQL autorise plusieurs NULL : les réactions aux vidéos (article_id
        # NULL) ne se heurtent pas à celles des articles, et inversement.
        db.UniqueConstraint('video_id', 'emoji', 'visitor_hash',
                            name='uq_reaction_visitor_video'),
    )

    article = db.relationship('Article', backref=db.backref('reactions', lazy='dynamic'))
    video = db.relationship('TikTokPost', backref=db.backref('reactions', lazy='dynamic'))
