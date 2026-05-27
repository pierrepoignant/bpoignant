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
