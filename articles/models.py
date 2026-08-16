from datetime import datetime

from init_db import db


class Author(db.Model):
    __tablename__ = 'authors'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Article(db.Model):
    __tablename__ = 'articles'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    slug = db.Column(db.String(300), unique=True, nullable=False, index=True)
    summary = db.Column(db.Text, nullable=True)
    # Social-media variant of the summary: first person, livelier. Used to
    # pre-fill the tweet; falls back to `summary` when empty.
    social_summary = db.Column(db.Text, nullable=True)
    # Optional "petit mot" shown in italics above the article in the newsletter
    # e-mail; persisted so it re-appears if the article is sent again.
    newsletter_intro = db.Column(db.Text, nullable=True)
    content_html = db.Column(db.Text, nullable=False, default='')
    published = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    published_at = db.Column(db.DateTime, nullable=True)
    # When this article was posted to X (Twitter). NULL = never posted; set on
    # the first publish auto-post and on any manual share, guarding against
    # double-posting.
    x_posted_at = db.Column(db.DateTime, nullable=True)
    # Tweet id returned by the X API on a successful post, so we can link back
    # to it. NULL on rows posted before we started recording it.
    x_post_id = db.Column(db.String(40), nullable=True)
    # Engagement counters pulled from the X API by the daily poll. NULL until
    # the tweet has been synced once; x_metrics_at dates the snapshot.
    x_like_count = db.Column(db.Integer, nullable=True)
    x_view_count = db.Column(db.Integer, nullable=True)
    x_reply_count = db.Column(db.Integer, nullable=True)
    x_retweet_count = db.Column(db.Integer, nullable=True)
    x_metrics_at = db.Column(db.DateTime, nullable=True)
    # author_id used to point at users.id; it now points at the standalone
    # `authors` table — authors are not necessarily login users.
    author_id = db.Column(db.Integer, db.ForeignKey('authors.id'), nullable=True)

    author = db.relationship('Author', backref='articles', lazy='joined')

    @property
    def x_post_url(self):
        """Permalink to the tweet, or None when we don't have its id. The
        handle-less `/i/web/status/` form redirects to the canonical URL, so we
        don't need to know which account posted it."""
        if not self.x_post_id:
            return None
        return f"https://x.com/i/web/status/{self.x_post_id}"

    @property
    def tweet_summary(self):
        # What goes in the tweet body: the social line when we have one, the
        # editorial summary otherwise.
        return (self.social_summary or '').strip() or (self.summary or '')

    @property
    def display_date(self):
        # `created_at` is admin-editable and is the date shown everywhere.
        return self.created_at
