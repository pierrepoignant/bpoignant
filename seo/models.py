from datetime import datetime

from init_db import db


class SearchRun(db.Model):
    """One execution of the SearchApi → Google fetch for a keyword.

    Every run keeps its own batch of `GoogleUrl` rows so we can show a single
    snapshot ("the latest run") and a trend across runs without mutating old
    data. Runs are never deleted automatically.
    """

    __tablename__ = 'search_runs'

    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(255), nullable=False, index=True)
    # running → done | error
    status = db.Column(db.String(20), nullable=False, default='running', index=True)
    requested = db.Column(db.Integer, nullable=False, default=0)   # target result count
    fetched = db.Column(db.Integer, nullable=False, default=0)     # rows actually stored
    pages_fetched = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    results = db.relationship(
        'GoogleUrl',
        backref='run',
        cascade='all, delete-orphan',
        lazy='dynamic',
    )

    @property
    def duration_seconds(self):
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class GoogleUrl(db.Model):
    """A single organic Google result captured for a keyword during a run."""

    __tablename__ = 'google_urls'

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('search_runs.id'), nullable=False, index=True)
    keyword = db.Column(db.String(255), nullable=False, index=True)
    # 1-based rank across the whole result set (page 2, position 3 → rank 13).
    rank = db.Column(db.Integer, nullable=False, index=True)
    page = db.Column(db.Integer, nullable=True)
    title = db.Column(db.Text, nullable=True)
    link = db.Column(db.Text, nullable=False)
    displayed_link = db.Column(db.String(512), nullable=True)
    domain = db.Column(db.String(255), nullable=True, index=True)
    snippet = db.Column(db.Text, nullable=True)
    # Google's "date" field on a result, when present (kept as raw string).
    result_date = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
