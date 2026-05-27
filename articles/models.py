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
    content_html = db.Column(db.Text, nullable=False, default='')
    published = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    published_at = db.Column(db.DateTime, nullable=True)
    # author_id used to point at users.id; it now points at the standalone
    # `authors` table — authors are not necessarily login users.
    author_id = db.Column(db.Integer, db.ForeignKey('authors.id'), nullable=True)

    author = db.relationship('Author', backref='articles', lazy='joined')

    @property
    def display_date(self):
        # `created_at` is admin-editable and is the date shown everywhere.
        return self.created_at
