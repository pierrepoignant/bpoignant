"""TikTok posts.

One row per clip: the video itself lives in the OVH bucket, the text that goes
with it lives here, and the published URL is filled in afterwards — the clip is
prepared here but posted by hand in the TikTok app, which has no API for
publishing on a personal account.
"""

from datetime import datetime

from init_db import db


class TikTokPost(db.Model):
    __tablename__ = 'tiktok_posts'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    # Public URL in the OVH bucket. Public because TikTok is downloaded from a
    # phone, often not the machine that produced it.
    video_url = db.Column(db.String(500), nullable=False)
    caption = db.Column(db.Text, nullable=True)
    transcript = db.Column(db.Text, nullable=True)
    duration_seconds = db.Column(db.Float, nullable=True)

    # Filled in once the clip is actually posted. Nullable by design: a clip is
    # prepared well before it goes out, and the two states are worth telling
    # apart in the list.
    posted_url = db.Column(db.String(500), nullable=True)
    posted_at = db.Column(db.DateTime, nullable=True)

    # Optional: the article the clip came from, when there is one.
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    article = db.relationship('Article')

    @property
    def is_posted(self):
        return bool(self.posted_url)
