from datetime import datetime, date, time

import bleach
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from slugify import slugify

from init_db import db
from articles.models import Article, Author
from articles.cleanup import clean_article_html, summarize_html
from auth import admin_required

articles_bp = Blueprint('articles', __name__, url_prefix='/articles', template_folder='templates')
admin_articles_bp = Blueprint('admin_articles', __name__, url_prefix='/admin/articles', template_folder='templates')
admin_authors_bp = Blueprint('admin_authors', __name__, url_prefix='/admin/authors', template_folder='templates')


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
    from engagement.models import Reaction
    from engagement import EMOJIS
    articles = (
        Article.query.filter_by(published=True)
        .order_by(Article.created_at.desc())
        .all()
    )
    # Reaction counts per article (emoji → n), computed in one grouped query
    # and kept in EMOJIS display order, dropping any emoji with no reactions.
    reactions_by_article = {}
    ids = [a.id for a in articles]
    if ids:
        rows = (
            db.session.query(
                Reaction.article_id, Reaction.emoji, db.func.count(Reaction.id)
            )
            .filter(Reaction.article_id.in_(ids))
            .group_by(Reaction.article_id, Reaction.emoji)
            .all()
        )
        by_article = {}
        for article_id, emoji, n in rows:
            by_article.setdefault(article_id, {})[emoji] = n
        for article_id, by_emoji in by_article.items():
            reactions_by_article[article_id] = [
                {'emoji': e, 'count': by_emoji[e]} for e in EMOJIS if by_emoji.get(e)
            ]
    return render_template(
        'articles_public_list.html',
        articles=articles,
        reactions_by_article=reactions_by_article,
    )


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
    from engagement.models import Comment
    from engagement import reactions_context
    comments = (
        Comment.query.filter_by(article_id=article.id, approved=True)
        .order_by(Comment.created_at.asc())
        .all()
    )
    return render_template(
        'articles_public_show.html',
        article=article,
        related=related,
        comments=comments,
        reactions=reactions_context(article),
    )


# ─── ADMIN ──────────────────────────────────────────────────

@admin_articles_bp.route('/')
@admin_required
def list_articles():
    from newsletter.models import Campaign
    from engagement.models import Comment, Reaction
    from analytics.models import PageView
    articles = Article.query.order_by(Article.created_at.desc()).all()
    last_campaign_by_article = {}
    for c in Campaign.query.order_by(Campaign.sent_at.desc()).all():
        if c.article_id not in last_campaign_by_article:
            last_campaign_by_article[c.article_id] = c

    # Engagement stats per article, computed with grouped queries to avoid
    # one query per card.
    views_by_path = dict(
        db.session.query(PageView.path, db.func.count(PageView.id))
        .group_by(PageView.path)
        .all()
    )
    views_by_article = {
        a.id: views_by_path.get(f'/articles/{a.slug}', 0) for a in articles
    }
    comments_by_article = dict(
        db.session.query(Comment.article_id, db.func.count(Comment.id))
        .filter(Comment.approved == True)
        .group_by(Comment.article_id)
        .all()
    )
    reactions_by_article = dict(
        db.session.query(Reaction.article_id, db.func.count(Reaction.id))
        .group_by(Reaction.article_id)
        .all()
    )

    return render_template(
        'articles_admin_list.html',
        articles=articles,
        last_campaign_by_article=last_campaign_by_article,
        views_by_article=views_by_article,
        comments_by_article=comments_by_article,
        reactions_by_article=reactions_by_article,
    )


@admin_articles_bp.route('/<int:article_id>/send-newsletter', methods=['POST'])
@admin_required
def send_newsletter(article_id):
    from flask_login import current_user
    from newsletter import enqueue_article_send
    from mail import is_configured

    article = db.session.get(Article, article_id) or abort(404)
    if not article.published:
        flash("L'article doit être publié avant d'envoyer la newsletter.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    if not is_configured():
        flash("SendGrid n'est pas configuré (SENDGRID__API_KEY manquant).", 'danger')
        return redirect(url_for('admin_articles.list_articles'))

    # Optional "petit mot" — saved so it re-appears next time this article is
    # sent. Stored as None when blank.
    article.newsletter_intro = (request.form.get('intro') or '').strip() or None
    db.session.commit()

    campaign = enqueue_article_send(article, sent_by=current_user)
    if campaign.recipient_count == 0:
        flash(
            "Tous les abonnés ont déjà reçu cet article — aucun nouvel envoi."
            if campaign.skipped_count else "Aucun abonné à contacter.",
            'info',
        )
    else:
        msg = f"Envoi de {campaign.recipient_count} e-mail(s) lancé en arrière-plan"
        if campaign.skipped_count:
            msg += f" — {campaign.skipped_count} déjà destinataire(s), ignoré(s)"
        msg += ". Le détail apparaîtra dans l'onglet « Envois »."
        flash(msg, 'info')
    return redirect(url_for('admin_articles.list_articles'))


@admin_articles_bp.route('/new', methods=['GET', 'POST'])
@admin_required
def create_article():
    if request.method == 'POST':
        return _save_article(None)
    return render_template('articles_admin_form.html', article=None, authors=_all_authors())


@admin_articles_bp.route('/<int:article_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_article(article_id):
    article = db.session.get(Article, article_id) or abort(404)
    if request.method == 'POST':
        return _save_article(article)
    return render_template('articles_admin_form.html', article=article, authors=_all_authors())


def _all_authors():
    return Author.query.order_by(Author.name).all()


def _resolve_author():
    """Pick the author from the form: explicit new name wins, else the
    dropdown value, else None. Creates the Author row on the fly when a
    new name is provided."""
    new_name = (request.form.get('new_author_name') or '').strip()
    if new_name:
        existing = Author.query.filter_by(name=new_name).first()
        if existing:
            return existing
        author = Author(name=new_name)
        db.session.add(author)
        db.session.flush()
        return author

    raw_id = request.form.get('author_id')
    if not raw_id:
        return None
    try:
        return db.session.get(Author, int(raw_id))
    except (ValueError, TypeError):
        return None


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


def _autosummary(title, content_html):
    """Build a summary for an article that was saved without one: ask the AI
    (Bernard's voice) first, falling back to plain text extraction if the AI
    is unavailable or errors. Returns '' when there's no usable content."""
    try:
        from articles.ai_summary import generate_summary
        proposed = generate_summary(title, content_html)
        if proposed:
            return proposed
    except Exception as exc:
        from flask import current_app
        current_app.logger.warning(f"AI summary on save failed: {exc}")
    return summarize_html(content_html or '')


def _save_article(article):
    title = (request.form.get('title') or '').strip()
    summary = (request.form.get('summary') or '').strip()
    content_html = clean_article_html(_clean_html(request.form.get('content_html')))
    published = request.form.get('published') == 'on'
    created_at = _parse_created_date(request.form.get('created_date'))
    author = _resolve_author()

    if not title:
        flash("Le titre est obligatoire.", 'danger')
        return redirect(request.url)

    # No summary typed → generate one on save. A summary the admin wrote is
    # always kept untouched.
    auto_summary = False
    if not summary and content_html:
        summary = _autosummary(title, content_html)
        auto_summary = bool(summary)

    if article is None:
        article = Article(
            title=title,
            slug=_unique_slug(title),
            summary=summary,
            content_html=content_html,
            published=published,
            created_at=created_at or datetime.utcnow(),
            author_id=author.id if author else None,
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
        article.author_id = author.id if author else None
        if created_at is not None:
            article.created_at = created_at

    db.session.commit()
    if auto_summary:
        flash("Article enregistré. Résumé généré automatiquement — pensez à le relire.", 'success')
    else:
        flash("Article enregistré.", 'success')
    return redirect(url_for('admin_articles.edit_article', article_id=article.id))


@admin_articles_bp.route('/cleanup-all', methods=['POST'])
@admin_required
def cleanup_all():
    """Re-run the HTML normaliser on every article. Skips rows whose
    cleaned output is byte-identical to what's stored so we don't bump
    `updated_at` for no reason."""
    changed = 0
    for article in Article.query.all():
        cleaned = clean_article_html(article.content_html or '')
        if cleaned != (article.content_html or ''):
            article.content_html = cleaned
            changed += 1
    db.session.commit()
    flash(f"Nettoyage terminé — {changed} article(s) mis à jour.", 'success')
    return redirect(url_for('admin_articles.list_articles'))


@admin_articles_bp.route('/propose-summaries', methods=['POST'])
@admin_required
def propose_summaries():
    """Fill in a proposed one-line summary for every article that doesn't
    have one yet. Summaries are written by Claude in Bernard's tone of voice;
    if the AI is unavailable (no API key) or errors, we fall back to a plain
    text extraction. Existing summaries are left untouched, and the proposals
    can be edited afterwards on each article.

    AI calls are sequential, so we cap how many we generate per click to stay
    within the request timeout — click again to process the rest."""
    from flask import current_app
    from articles.ai_summary import generate_summary, is_configured

    MAX_AI_PER_RUN = 12

    pending = [a for a in Article.query.order_by(Article.created_at.desc()).all()
               if not (a.summary or '').strip()]

    ai_enabled = is_configured()
    # When the AI is on we process a bounded number per click (sequential calls
    # are slow); the rest wait for the next run so they too get an AI summary.
    # When the AI is off, extraction is cheap, so process everything at once.
    budget = MAX_AI_PER_RUN if ai_enabled else len(pending)

    filled = 0
    ai_used = 0

    for article in pending[:budget]:
        proposed = None
        if ai_enabled:
            try:
                proposed = generate_summary(article.title, article.content_html or '')
                if proposed:
                    ai_used += 1
            except Exception as exc:
                current_app.logger.warning(
                    f"AI summary failed for article {article.id}: {exc}"
                )
                proposed = None
        if not proposed:
            proposed = summarize_html(article.content_html or '')
        if proposed:
            article.summary = proposed
            filled += 1

    db.session.commit()

    if not filled:
        flash("Tous les articles ont déjà un résumé.", 'info')
    else:
        remaining = len(pending) - filled
        msg = f"{filled} résumé(s) proposé(s)"
        if ai_used:
            msg += f" — dont {ai_used} rédigé(s) par l'IA"
        if not ai_enabled:
            msg += " (IA non configurée : extraits du texte — définissez ANTHROPIC__API_KEY)"
        elif remaining > 0:
            msg += f". {remaining} restant(s), relancez pour continuer"
        flash(msg + ".", 'success')
    return redirect(url_for('admin_articles.list_articles'))


# ─── ADMIN: AUTHORS ─────────────────────────────────────────

@admin_authors_bp.route('/')
@admin_required
def list_authors():
    authors = Author.query.order_by(Author.name).all()
    return render_template('authors_admin_list.html', authors=authors)


@admin_authors_bp.route('/new', methods=['POST'])
@admin_required
def create_author():
    name = (request.form.get('name') or '').strip()
    if not name:
        flash("Le nom est requis.", 'danger')
        return redirect(url_for('admin_authors.list_authors'))
    if Author.query.filter_by(name=name).first():
        flash(f'L\'auteur "{name}" existe déjà.', 'danger')
        return redirect(url_for('admin_authors.list_authors'))
    db.session.add(Author(name=name))
    db.session.commit()
    flash(f'Auteur "{name}" créé.', 'success')
    return redirect(url_for('admin_authors.list_authors'))


@admin_authors_bp.route('/<int:author_id>/rename', methods=['POST'])
@admin_required
def rename_author(author_id):
    author = db.session.get(Author, author_id) or abort(404)
    new_name = (request.form.get('name') or '').strip()
    if not new_name:
        flash("Le nom est requis.", 'danger')
        return redirect(url_for('admin_authors.list_authors'))
    clash = Author.query.filter_by(name=new_name).first()
    if clash and clash.id != author.id:
        flash(f'L\'auteur "{new_name}" existe déjà.', 'danger')
        return redirect(url_for('admin_authors.list_authors'))
    author.name = new_name
    db.session.commit()
    flash("Auteur renommé.", 'success')
    return redirect(url_for('admin_authors.list_authors'))


@admin_authors_bp.route('/<int:author_id>/delete', methods=['POST'])
@admin_required
def delete_author(author_id):
    author = db.session.get(Author, author_id) or abort(404)
    # Unlink articles before deleting so we don't leave orphan FKs.
    Article.query.filter_by(author_id=author.id).update({'author_id': None})
    db.session.delete(author)
    db.session.commit()
    flash("Auteur supprimé. Les articles ne sont plus rattachés.", 'success')
    return redirect(url_for('admin_authors.list_authors'))
