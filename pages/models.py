from datetime import datetime

from init_db import db


class Page(db.Model):
    """Editable static page (about, legal, etc.). Same WYSIWYG editor as
    articles, but no author / no newsletter / no listing — pages are
    addressed individually by slug from the navbar or footer.
    """

    __tablename__ = 'pages'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    slug = db.Column(db.String(300), unique=True, nullable=False, index=True)
    meta_description = db.Column(db.String(300), nullable=True)
    content_html = db.Column(db.Text, nullable=False, default='')
    published = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
