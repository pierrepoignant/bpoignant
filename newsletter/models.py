from datetime import datetime

from init_db import db


class Subscriber(db.Model):
    __tablename__ = 'subscribers'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    prenom = db.Column(db.String(120), nullable=True)
    nom = db.Column(db.String(120), nullable=True)
    ville = db.Column(db.String(120), nullable=True)
    # Random per-row token used in the unsubscribe / confirm links so anyone
    # with the email cannot guess someone else's URL.
    token = db.Column(db.String(64), unique=True, nullable=False)
    subscribed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    unsubscribed_at = db.Column(db.DateTime, nullable=True)
    # Double opt-in: null = pending confirmation (never emailed). Low-risk
    # signups are auto-confirmed on subscribe; high-risk ones must click the
    # link in the confirmation e-mail. Existing rows are backfilled as
    # confirmed by the schema migration.
    confirmed_at = db.Column(db.DateTime, nullable=True)
    # Set when SendGrid reports a hard bounce / block / spam-report for this
    # address — such rows are never e-mailed again.
    bounced_at = db.Column(db.DateTime, nullable=True)
    bounce_reason = db.Column(db.String(255), nullable=True)
    # Spam heuristic score computed at signup, kept for admin visibility.
    spam_score = db.Column(db.Integer, nullable=True)

    @property
    def display_name(self):
        full = ' '.join(p for p in (self.prenom, self.nom) if p)
        return full or None

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    @property
    def is_bounced(self):
        return self.bounced_at is not None

    @property
    def is_mailable(self):
        """True when this subscriber may receive newsletters: confirmed, not
        unsubscribed, and not bounced."""
        return (
            self.confirmed_at is not None
            and self.unsubscribed_at is None
            and self.bounced_at is None
        )


class Campaign(db.Model):
    """One row per "send this article to all subscribers" action.

    Lets the admin see what's been sent and avoid resending by accident.
    """
    __tablename__ = 'newsletter_campaigns'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False, index=True)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    sent_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    recipient_count = db.Column(db.Integer, default=0, nullable=False)
    success_count = db.Column(db.Integer, default=0, nullable=False)
    error_count = db.Column(db.Integer, default=0, nullable=False)
    # Recipients skipped because they already received this article.
    skipped_count = db.Column(db.Integer, default=0, nullable=False)
    # The "petit mot" that went out with *this* send. Article.newsletter_intro
    # holds only the latest one, so re-sending an article with a different note
    # would otherwise erase what the first mailing actually said.
    intro = db.Column(db.Text, nullable=True)

    article = db.relationship('Article')
    sent_by = db.relationship('User')

    @property
    def is_active(self):
        return self.unsubscribed_at is None


class Delivery(db.Model):
    """One row per (article, subscriber) that was successfully emailed.

    Lets a re-send skip recipients who already received that article, so
    clicking "send" twice never mails the same article to the same person.
    """
    __tablename__ = 'newsletter_deliveries'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False, index=True)
    subscriber_id = db.Column(db.Integer, db.ForeignKey('subscribers.id'), nullable=False, index=True)
    # Kept for the record even if the subscriber row is later deleted.
    email = db.Column(db.String(255), nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('article_id', 'subscriber_id',
                            name='uq_delivery_article_subscriber'),
    )

    article = db.relationship('Article')
    subscriber = db.relationship('Subscriber')


class EmailEvent(db.Model):
    """One row per open/click reported by SendGrid's Event Webhook.

    The webhook previously discarded everything except hard bounces, so no
    per-person engagement existed — SendGrid's Stats API gives totals only.
    This table is what makes "who actually reads the newsletter" answerable,
    and it necessarily starts from the day it was switched on: SendGrid does
    not replay past events.

    `sg_event_id` is SendGrid's own identifier and is unique here, because the
    webhook retries on any non-2xx and would otherwise double-count.
    """

    __tablename__ = 'newsletter_email_events'

    id = db.Column(db.Integer, primary_key=True)
    sg_event_id = db.Column(db.String(100), unique=True, nullable=True, index=True)
    # Kept even when no subscriber matches (address since deleted, or mail we
    # sent outside the newsletter), so totals stay honest.
    email = db.Column(db.String(255), nullable=False, index=True)
    subscriber_id = db.Column(db.Integer, db.ForeignKey('subscribers.id'),
                              nullable=True, index=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'),
                           nullable=True, index=True)
    event = db.Column(db.String(20), nullable=False, index=True)   # open | click
    url = db.Column(db.String(500), nullable=True)                 # clicks only
    occurred_at = db.Column(db.DateTime, nullable=False, index=True)

    subscriber = db.relationship('Subscriber')
    article = db.relationship('Article')


class Announcement(db.Model):
    """A message sent to the subscribers that is not an article.

    Campaign/Delivery both key on a non-null article_id, so an announcement
    cannot borrow them without making that column nullable across two live
    tables. A separate pair of tables is purely additive — create_all() makes
    them and nothing existing is altered.

    A row starts as a draft, is edited freely, and becomes read-only once
    sent: the subscribers' copy cannot be recalled, so the record of what
    they received must not change afterwards.
    """

    __tablename__ = 'newsletter_announcements'

    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(255), nullable=False)
    # Plain text, rendered with line breaks preserved. Bernard writes these
    # himself and an editor here would be one more thing to explain.
    body = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    sent_at = db.Column(db.DateTime, nullable=True)
    sent_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    recipient_count = db.Column(db.Integer, default=0, nullable=False)
    success_count = db.Column(db.Integer, default=0, nullable=False)
    error_count = db.Column(db.Integer, default=0, nullable=False)

    sent_by = db.relationship('User')

    @property
    def is_sent(self):
        return self.sent_at is not None


class AnnouncementDelivery(db.Model):
    """One row per (announcement, subscriber) successfully emailed — the same
    record Delivery keeps for articles, so "who received the announcement of
    the 12th" stays answerable after the fact."""

    __tablename__ = 'newsletter_announcement_deliveries'

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey('newsletter_announcements.id'),
                                nullable=False, index=True)
    subscriber_id = db.Column(db.Integer, db.ForeignKey('subscribers.id'),
                              nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('announcement_id', 'subscriber_id',
                            name='uq_ann_delivery_announcement_subscriber'),
    )

    announcement = db.relationship('Announcement')
    subscriber = db.relationship('Subscriber')
