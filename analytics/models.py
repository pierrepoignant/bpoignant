from datetime import datetime

from init_db import db


class PageView(db.Model):
    __tablename__ = 'page_views'

    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(512), nullable=False, index=True)
    referrer = db.Column(db.String(512), nullable=True)
    # SHA-256 of (IP + UA + daily-rotating salt), truncated. Lets us count
    # unique visitors without storing raw IPs.
    visitor_hash = db.Column(db.String(32), nullable=False, index=True)
    user_agent = db.Column(db.String(512), nullable=True)
    country = db.Column(db.String(2), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class SearchQuery(db.Model):
    """One row per search performed on the public site.

    Deliberately carries no `visitor_hash`, unlike PageView: what people search
    for is more revealing than what they read, and the useful signal here is
    aggregate anyway. Without an identifier a term can't be tied back to a
    person, so the table stays low-risk to keep indefinitely.

    `results_count` is the point of the table as much as the term itself — a
    search that returns nothing is a reader asking for something Bernard
    hasn't written, or has written under words he didn't use.
    """

    __tablename__ = 'search_queries'

    id = db.Column(db.Integer, primary_key=True)
    # Normalised (lowercased, whitespace-collapsed) so the same search typed
    # three ways groups into one row in the dashboard.
    #
    # Named `term`, not `query`: a column called `query` shadows
    # Flask-SQLAlchemy's `Model.query`, so `SearchQuery.query.filter(...)`
    # would resolve to the column and raise instead of building a query.
    term = db.Column(db.String(200), nullable=False, index=True)
    results_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
