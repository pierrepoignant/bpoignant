import bleach
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from slugify import slugify

from init_db import db
from pages.models import Page
from auth import admin_required

pages_bp = Blueprint('pages', __name__, template_folder='templates')
admin_pages_bp = Blueprint('admin_pages', __name__, url_prefix='/admin/pages', template_folder='templates')


# Same allow-list as articles — pages share the same editor and so the
# same sanitization rules. Kept duplicated rather than imported to avoid
# a circular import between the two blueprints.
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
    return bleach.clean(raw or '', tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)


def _unique_slug(value, exclude_id=None):
    base = slugify(value) or 'page'
    candidate = base
    n = 2
    while True:
        existing = Page.query.filter_by(slug=candidate).first()
        if existing is None or existing.id == exclude_id:
            return candidate
        candidate = f'{base}-{n}'
        n += 1


# ─── PUBLIC ─────────────────────────────────────────────────

@pages_bp.route('/<slug>')
def show(slug):
    page = Page.query.filter_by(slug=slug, published=True).first()
    if page is None:
        abort(404)
    return render_template('pages_public_show.html', page=page)


# ─── ADMIN ──────────────────────────────────────────────────

@admin_pages_bp.route('/')
@admin_required
def list_pages():
    items = Page.query.order_by(Page.title).all()
    return render_template('pages_admin_list.html', pages=items)


@admin_pages_bp.route('/new', methods=['GET', 'POST'])
@admin_required
def create_page():
    if request.method == 'POST':
        return _save_page(None)
    return render_template('pages_admin_form.html', page=None)


@admin_pages_bp.route('/<int:page_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_page(page_id):
    page = db.session.get(Page, page_id) or abort(404)
    if request.method == 'POST':
        return _save_page(page)
    return render_template('pages_admin_form.html', page=page)


@admin_pages_bp.route('/<int:page_id>/delete', methods=['POST'])
@admin_required
def delete_page(page_id):
    page = db.session.get(Page, page_id) or abort(404)
    db.session.delete(page)
    db.session.commit()
    flash("Page supprimée.", 'success')
    return redirect(url_for('admin_pages.list_pages'))


def _save_page(page):
    title = (request.form.get('title') or '').strip()
    raw_slug = (request.form.get('slug') or '').strip()
    meta_description = (request.form.get('meta_description') or '').strip() or None
    content_html = _clean_html(request.form.get('content_html'))
    published = request.form.get('published') == 'on'

    if not title:
        flash("Le titre est obligatoire.", 'danger')
        return redirect(request.url)

    # The admin can override the slug to keep stable URLs (e.g. /mentions-legales);
    # blank means "regenerate from the title".
    slug_source = raw_slug or title

    if page is None:
        page = Page(
            title=title,
            slug=_unique_slug(slug_source),
            meta_description=meta_description,
            content_html=content_html,
            published=published,
        )
        db.session.add(page)
    else:
        if raw_slug and raw_slug != page.slug:
            page.slug = _unique_slug(raw_slug, exclude_id=page.id)
        elif not raw_slug and slugify(title) != page.slug:
            # Title changed and no explicit slug: keep the existing slug to
            # avoid breaking footer / external links.
            pass
        page.title = title
        page.meta_description = meta_description
        page.content_html = content_html
        page.published = published

    db.session.commit()
    flash("Page enregistrée.", 'success')
    return redirect(url_for('admin_pages.edit_page', page_id=page.id))
