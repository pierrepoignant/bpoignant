"""Un commentaire laissé sous une publication de Bernard sur YouTube, la Page
Facebook ou Instagram — lu par l'API, et le brouillon de réponse proposé.

Un seul modèle pour les trois réseaux : ils se traitent de la même manière, et
la page des commentaires les présente ensemble. La publication visée est soit
une vidéo (TikTokPost, pour un Short YouTube, une vidéo de Page ou un Reel),
soit un article (pour une publication de lien sur la Page) — l'un des deux, pas
les deux.
"""

from datetime import datetime

from init_db import db


class SocialComment(db.Model):
    __tablename__ = 'social_comments'
    __table_args__ = (
        db.UniqueConstraint('network', 'external_id', name='uq_social_comment'),
    )

    id = db.Column(db.Integer, primary_key=True)
    network = db.Column(db.String(16), nullable=False, index=True)  # youtube / facebook / instagram
    external_id = db.Column(db.String(191), nullable=False)
    # L'objet réseau sous lequel est le commentaire (youtube_video_id,
    # fb_post_id, instagram_media_id, ou fb_page_post_id d'un article).
    network_object_id = db.Column(db.String(120), nullable=True, index=True)

    # La publication locale visée : une vidéo OU un article.
    post_id = db.Column(db.Integer, db.ForeignKey('tiktok_posts.id'), nullable=True, index=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=True, index=True)

    author = db.Column(db.String(160), nullable=True)
    author_id = db.Column(db.String(120), nullable=True)
    text = db.Column(db.Text, nullable=False, default='')
    likes = db.Column(db.Integer, nullable=True)
    posted_at = db.Column(db.DateTime, nullable=True)
    scraped_at = db.Column(db.DateTime, nullable=True)

    # Ce que le moteur propose de répondre, et la raison à côté. Vide quand il a
    # jugé qu'il n'y avait rien à répondre.
    suggested_reply = db.Column(db.Text, nullable=True)
    suggestion_note = db.Column(db.String(200), nullable=True)
    suggested_at = db.Column(db.DateTime, nullable=True)

    # new = à traiter · replied = répondu · discarded = écarté sans réponse.
    status = db.Column(db.String(12), nullable=False, default='new', index=True)
    reply_external_id = db.Column(db.String(191), nullable=True)
    replied_at = db.Column(db.DateTime, nullable=True)

    post = db.relationship('TikTokPost', backref=db.backref('social_comments', lazy='dynamic'))
    article = db.relationship('Article', backref=db.backref('social_comments', lazy='dynamic'))

    @property
    def network_label(self):
        return {'youtube': 'YouTube', 'facebook': 'Facebook', 'instagram': 'Instagram'}.get(self.network, self.network)
