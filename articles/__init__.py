from datetime import datetime, date, time

import bleach
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from slugify import slugify

from init_db import db
from articles.models import Article
from auth import admin_required

articles_bp = Blueprint('articles', __name__, url_prefix='/articles', template_folder='templates')
admin_articles_bp = Blueprint('admin_articles', __name__, url_prefix='/admin/articles', template_folder='templates')


# HTML tags / attributes allowed in the WYSIWYG output. Anything outside this
# is stripped before saving — keeps the editor safe from XSS while still
# supporting rich formatting.
ALLOWED_TAGS = [
    'p', 'br', 'hr', 'span', 'div',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'strong', 'b', 'em', 'i', 'u', 's', 'sub', 'sup',
    'blockquote', 'pre', 'code',
    'ul', 'ol', 'li',
    'a', 'img', 'figure', 'figcaption',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
]
ALLOWED_ATTRS = {
    '*': ['class', 'style'],
    'a': ['href', 'title', 'target', 'rel'],
    'img': ['src', 'alt', 'title', 'width', 'height'],
}


def _clean_html(raw):
    return bleach.clean(
        raw or '',
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        strip=True,
    )


def _unique_slug(title, exclude_id=None):
    base = slugify(title) or 'article'
    candidate = base
    n = 2
    while True:
        existing = Article.query.filter_by(slug=candidate).first()
        if existing is None or existing.id == exclude_id:
            return candidate
        candidate = f'{base}-{n}'
        n += 1


# ─── PUBLIC ─────────────────────────────────────────────────

@articles_bp.route('/')
def public_list():
    articles = (
        Article.query.filter_by(published=True)
        .order_by(Article.created_at.desc())
        .all()
    )
    return render_template('articles_public_list.html', articles=articles)


@articles_bp.route('/<slug>')
def public_show(slug):
    article = Article.query.filter_by(slug=slug, published=True).first()
    if article is None:
        abort(404)
    related = (
        Article.query.filter(Article.published == True, Article.id != article.id)
        .order_by(Article.created_at.desc())
        .limit(4)
        .all()
    )
    return render_template('articles_public_show.html', article=article, related=related)


# ─── ADMIN ──────────────────────────────────────────────────

@admin_articles_bp.route('/')
@admin_required
def list_articles():
    articles = Article.query.order_by(Article.created_at.desc()).all()
    return render_template('articles_admin_list.html', articles=articles)


@admin_articles_bp.route('/new', methods=['GET', 'POST'])
@admin_required
def create_article():
    if request.method == 'POST':
        return _save_article(None)
    return render_template('articles_admin_form.html', article=None)


@admin_articles_bp.route('/<int:article_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_article(article_id):
    article = db.session.get(Article, article_id) or abort(404)
    if request.method == 'POST':
        return _save_article(article)
    return render_template('articles_admin_form.html', article=article)


@admin_articles_bp.route('/<int:article_id>/delete', methods=['POST'])
@admin_required
def delete_article(article_id):
    article = db.session.get(Article, article_id) or abort(404)
    db.session.delete(article)
    db.session.commit()
    flash("Article supprimé.", 'success')
    return redirect(url_for('admin_articles.list_articles'))


def _parse_created_date(raw):
    """Parse the YYYY-MM-DD value from the admin date picker into a datetime.

    Time is fixed to 12:00 UTC so the date renders unambiguously in any
    timezone. Returns None on empty / invalid input.
    """
    if not raw:
        return None
    try:
        d = date.fromisoformat(raw.strip())
    except ValueError:
        return None
    return datetime.combine(d, time(12, 0))


def _save_article(article):
    title = (request.form.get('title') or '').strip()
    summary = (request.form.get('summary') or '').strip()
    content_html = _clean_html(request.form.get('content_html'))
    published = request.form.get('published') == 'on'
    created_at = _parse_created_date(request.form.get('created_date'))

    if not title:
        flash("Le titre est obligatoire.", 'danger')
        return redirect(request.url)

    if article is None:
        article = Article(
            title=title,
            slug=_unique_slug(title),
            summary=summary,
            content_html=content_html,
            published=published,
            created_at=created_at or datetime.utcnow(),
            author_id=getattr(current_user, 'id', None),
        )
        db.session.add(article)
    else:
        # Only re-slug when the title changes — keeps URLs stable.
        if title != article.title:
            article.slug = _unique_slug(title, exclude_id=article.id)
        article.title = title
        article.summary = summary
        article.content_html = content_html
        article.published = published
        if created_at is not None:
            article.created_at = created_at

    db.session.commit()
    flash("Article enregistré.", 'success')
    return redirect(url_for('admin_articles.edit_article', article_id=article.id))
