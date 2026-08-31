"""TikTok posts.

One row per clip: the video itself lives in the OVH bucket, the text that goes
with it lives here, and the published URL is filled in afterwards — the clip is
prepared here but posted by hand in the TikTok app, which has no API for
publishing on a personal account.
"""

from datetime import datetime

from init_db import db

# Many-to-many with the article themes: the clips are not tied to articles, but
# they are about the same subjects, and the theme pages are where they earn
# their keep in search.
tiktok_post_themes = db.Table(
    'tiktok_post_themes',
    db.Column('post_id', db.Integer, db.ForeignKey('tiktok_posts.id'), primary_key=True),
    db.Column('theme_id', db.Integer, db.ForeignKey('themes.id'), primary_key=True),
)


class TikTokPost(db.Model):
    __tablename__ = 'tiktok_posts'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    # TikTok's own id for the post, from the scrape. Unique so re-scraping
    # updates a row instead of duplicating it.
    tiktok_id = db.Column(db.String(64), unique=True, nullable=True, index=True)
    # The edited render, attached afterwards from the dev machine — nullable
    # because a post exists as soon as it is scraped, long before anyone links
    # the file that produced it.
    video_url = db.Column(db.String(500), nullable=True)
    caption = db.Column(db.Text, nullable=True)
    transcript = db.Column(db.Text, nullable=True)
    duration_seconds = db.Column(db.Float, nullable=True)

    # Filled in once the clip is actually posted. Nullable by design: a clip is
    # prepared well before it goes out, and the two states are worth telling
    # apart in the list.
    posted_url = db.Column(db.String(500), nullable=True)
    posted_at = db.Column(db.DateTime, nullable=True)

    # Set once the clip has been re-posted to X, so the button can't fire twice.
    x_post_id = db.Column(db.String(40), nullable=True)
    x_posted_at = db.Column(db.DateTime, nullable=True)

    # Figures for the X post, refreshed by the same daily sync that updates the
    # articles' — a video tweet is a tweet, and its numbers are only
    # comparable to TikTok's if someone actually reads them.
    x_like_count = db.Column(db.Integer, nullable=True)
    x_view_count = db.Column(db.Integer, nullable=True)
    x_reply_count = db.Column(db.Integer, nullable=True)
    x_retweet_count = db.Column(db.Integer, nullable=True)
    x_metrics_at = db.Column(db.DateTime, nullable=True)

    # Posts that were promoted with paid advertising. Their figures are not
    # comparable with the organic ones — the 2021-2022 clips were all boosted
    # and run into six figures, against a few hundred for the current ones —
    # so the statistics leave them out rather than average the two together.
    boosted = db.Column(db.Boolean, default=False, nullable=False)

    # Figures from the last Apify scrape.
    views = db.Column(db.Integer, nullable=True)
    likes = db.Column(db.Integer, nullable=True)
    comments_count = db.Column(db.Integer, nullable=True)
    shares = db.Column(db.Integer, nullable=True)
    scraped_at = db.Column(db.DateTime, nullable=True)

    # Optional: the article the clip came from, when there is one.
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    # Poster frame, uploaded beside the video: a bare <video> shows a black
    # rectangle until it is played, and the thumbnail is also what Google wants
    # for a video rich result.
    poster_url = db.Column(db.String(500), nullable=True)

    themes = db.relationship('Theme', secondary=tiktok_post_themes,
                             backref='tiktok_posts', lazy='selectin')

    article = db.relationship('Article')

    @property
    def is_posted(self):
        return bool(self.posted_url)

    @property
    def has_video(self):
        return bool(self.video_url)

    @property
    def x_url(self):
        return f"https://x.com/i/web/status/{self.x_post_id}" if self.x_post_id else None
