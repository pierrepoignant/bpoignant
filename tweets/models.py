"""Replies collected from X.

One row per reply to one of Bernard's article tweets. The engagement counters
themselves live on `Article` (there is exactly one tweet per article), so this
table only holds the conversation.

`notified_at` is what makes the daily digest idempotent: a reply is e-mailed
once and then never again, whatever happens on later runs.
"""

from datetime import datetime

from init_db import db


class TweetReply(db.Model):
    __tablename__ = 'tweet_replies'

    id = db.Column(db.Integer, primary_key=True)
    # X's own id for the reply — unique so a re-run can never duplicate a row.
    reply_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False, index=True)
    author_username = db.Column(db.String(80), nullable=True)
    author_name = db.Column(db.String(150), nullable=True)
    content = db.Column(db.Text, nullable=True)
    # When the reply was posted on X (not when we noticed it).
    posted_at = db.Column(db.DateTime, nullable=True)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    notified_at = db.Column(db.DateTime, nullable=True)

    article = db.relationship('Article', backref=db.backref('tweet_replies', lazy='dynamic'))

    @property
    def url(self):
        """Permalink to the reply, so Bernard lands straight on it and can
        answer in one click."""
        return f"https://x.com/i/web/status/{self.reply_id}"

    @property
    def author_url(self):
        return f"https://x.com/{self.author_username}" if self.author_username else None

    @property
    def display_author(self):
        if self.author_username:
            return f"@{self.author_username}"
        return self.author_name or "?"
