from datetime import datetime, date, time, timedelta

import bleach
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from slugify import slugify

from init_db import db
from articles.models import Article, Author, Theme
from articles.cleanup import clean_article_html, summarize_html, clean_text
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
    from engagement.models import Reaction, Comment
    from engagement import EMOJIS
    # Free-text search over title, summary and body. At 25 articles a LIKE
    # scan is far cheaper than maintaining an index, and MySQL's default
    # collation makes it accent- and case-insensitive for free.
    query = (request.args.get('q') or '').strip()
    base = Article.query.filter_by(published=True)
    if query:
        like = f"%{query}%"
        base = base.filter(db.or_(
            Article.title.like(like),
            Article.summary.like(like),
            Article.content_html.like(like),
        ))
    articles = base.order_by(Article.created_at.desc()).all()
    if query:
        # Logged after the query runs so the result count is recorded with the
        # term — a search returning nothing is the actionable kind.
        from analytics.tracking import log_search
        log_search(query, len(articles))
    # Reaction counts per article (emoji → n), computed in one grouped query
    # and kept in EMOJIS display order, dropping any emoji with no reactions.
    reactions_by_article = {}
    comments_by_article = {}
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
        # Approved-comment counts per article, one grouped query.
        comments_by_article = dict(
            db.session.query(Comment.article_id, db.func.count(Comment.id))
            .filter(Comment.article_id.in_(ids), Comment.approved == True)
            .group_by(Comment.article_id)
            .all()
        )
    return render_template(
        'articles_public_list.html',
        articles=articles,
        reactions_by_article=reactions_by_article,
        comments_by_article=comments_by_article,
    )


@articles_bp.route('/<slug>')
def public_show(slug):
    article = Article.query.filter_by(slug=slug, published=True).first()
    if article is None:
        abort(404)
    related = _related_articles(article)
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


def _related_articles(article, limit=4):
    """Other articles sharing a theme, newest first, topped up with recent
    ones when the article has few or no themes — the page always shows
    something rather than an empty block."""
    theme_ids = {t.id for t in article.themes}
    picked = []
    if theme_ids:
        candidates = (
            Article.query
            .filter(Article.published == True,  # noqa: E712
                    Article.id != article.id,
                    Article.themes.any(Theme.id.in_(theme_ids)))
            .all()
        )
        # Rank by how many themes are shared, then by recency. Ordering by date
        # alone lets an article sharing one theme outrank one sharing three,
        # which is what "sur le même sujet" is supposed to surface.
        candidates.sort(
            key=lambda a: (-len(theme_ids & {t.id for t in a.themes}), -a.created_at.timestamp())
        )
        picked = candidates[:limit]
    if len(picked) < limit:
        seen = {a.id for a in picked} | {article.id}
        filler = (
            Article.query
            .filter(Article.published == True,  # noqa: E712
                    Article.id.notin_(seen))
            .order_by(Article.created_at.desc())
            .limit(limit - len(picked))
            .all()
        )
        picked.extend(filler)
    return picked


@articles_bp.route('/theme/<slug>')
def public_theme(slug):
    theme = Theme.query.filter_by(slug=slug).first() or abort(404)
    articles = (
        Article.query
        .filter(Article.published == True,  # noqa: E712
                Article.themes.any(Theme.id == theme.id))
        .order_by(Article.created_at.desc())
        .all()
    )
    return render_template('articles_public_theme.html', theme=theme, articles=articles)


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

    from x import (is_configured as x_is_configured, compose_article_tweet,
                   share_url as x_share_url)
    x_configured = x_is_configured()
    # Proposed (editable) tweet text per published article, for the share modal.
    x_proposed_by_article = {}
    if x_configured:
        for a in articles:
            if a.published:
                url = x_share_url(url_for('articles.public_show', slug=a.slug, _external=True))
                x_proposed_by_article[a.id] = compose_article_tweet(a.title, a.tweet_summary, url)

    return render_template(
        'articles_admin_list.html',
        articles=articles,
        last_campaign_by_article=last_campaign_by_article,
        views_by_article=views_by_article,
        comments_by_article=comments_by_article,
        reactions_by_article=reactions_by_article,
        x_configured=x_configured,
        x_proposed_by_article=x_proposed_by_article,
    )


@admin_articles_bp.route('/x-test', methods=['POST'])
@admin_required
def x_test():
    """Non-destructive check that the X credentials are wired and can post."""
    from x import is_configured, verify_credentials

    if not is_configured():
        flash("X n'est pas configuré — les 4 clés BPOIGNANT_X__… sont absentes de l'environnement.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))

    ok, info = verify_credentials()
    if not ok:
        flash(f"Échec de la connexion à X : {info.get('error')}", 'danger')
    else:
        level = info.get('access_level') or 'inconnu'
        handle = info.get('screen_name') or '?'
        if 'write' in level:
            flash(f"Connexion X OK — @{handle} (accès : {level}). Prêt à publier.", 'success')
        else:
            flash(
                f"Connexion X OK pour @{handle}, mais l'accès est « {level} » (lecture seule). "
                "Passez l'app en « Lecture et écriture » puis régénérez le jeton d'accès.",
                'warning',
            )
    return redirect(url_for('admin_articles.list_articles'))


@admin_articles_bp.route('/<int:article_id>/post-x', methods=['POST'])
@admin_required
def post_to_x(article_id):
    """Post the article to X using the text edited in the share modal. Falls
    back to the auto-composed text if the field came through empty. Stamps
    ``x_posted_at`` on success."""
    from x import is_configured, compose_article_tweet, post_tweet, share_url

    article = db.session.get(Article, article_id) or abort(404)
    if not article.published:
        flash("L'article doit être publié avant de le partager sur X.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    if not is_configured():
        flash("X n'est pas configuré (clés API manquantes).", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    # The share button is hidden once an article is posted; this guards against
    # a stale page or a re-submitted form double-posting.
    if article.x_posted_at:
        flash(
            f"Article déjà partagé sur X le {article.x_posted_at.strftime('%d/%m/%Y')}.",
            'info',
        )
        return redirect(url_for('admin_articles.list_articles'))

    text = (request.form.get('text') or '').strip()
    if not text:
        url = share_url(url_for('articles.public_show', slug=article.slug, _external=True))
        text = compose_article_tweet(article.title, article.tweet_summary, url)

    ok, detail = post_tweet(text)
    if ok:
        article.x_posted_at = datetime.utcnow()
        # `detail` is the tweet id on success — keep it so we can link to the post.
        article.x_post_id = str(detail) if detail else None
        db.session.commit()
        if article.x_post_url:
            flash(f"Article partagé sur X : {article.x_post_url}", 'success')
        else:
            flash("Article partagé sur X.", 'success')
    else:
        flash(f"Échec du partage sur X : {detail}", 'danger')
    return redirect(url_for('admin_articles.list_articles'))


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
    # Default a new article's author to Bernard Poignant (fallback: first author).
    authors = _all_authors()
    default = next((a for a in authors if a.name == 'Bernard Poignant'), None) or (authors[0] if authors else None)
    from gdrive import is_configured as gdrive_is_configured
    return render_template(
        'articles_admin_form.html',
        article=None,
        authors=authors,
        default_author_id=(default.id if default else None),
        gdrive_configured=gdrive_is_configured(),
        all_themes=ai_summary_themes(),
    )


@admin_articles_bp.route('/<int:article_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_article(article_id):
    article = db.session.get(Article, article_id) or abort(404)
    if request.method == 'POST':
        return _save_article(article)
    from gdrive import is_configured as gdrive_is_configured
    return render_template(
        'articles_admin_form.html',
        article=article,
        authors=_all_authors(),
        default_author_id=None,
        gdrive_configured=gdrive_is_configured(),
        all_themes=ai_summary_themes(),
    )


@admin_articles_bp.route('/<int:article_id>/stats')
@admin_required
def article_stats(article_id):
    """Detailed per-article stats: a daily view timeline (views + unique
    visitors), top referrers, reactions/comments, and newsletter send figures
    enriched with SendGrid open/click rates when available."""
    from sqlalchemy import func
    from analytics.models import PageView
    from analytics.tracking import default_grouping, bucketed_series
    from engagement.models import Reaction, Comment
    from engagement import EMOJIS
    from newsletter import article_email_stats

    article = db.session.get(Article, article_id) or abort(404)
    path = f'/articles/{article.slug}'

    try:
        days = int(request.args.get('days', 90))
    except (TypeError, ValueError):
        days = 90
    days = max(7, min(days, 365))

    group = request.args.get('group')
    if group not in ('day', 'week', 'month'):
        group = default_grouping(days)

    today = datetime.utcnow().date()
    since = datetime.utcnow() - timedelta(days=days)
    window = [PageView.path == path, PageView.created_at >= since]

    total_views = db.session.query(func.count(PageView.id)).filter(
        PageView.path == path).scalar() or 0
    window_views = db.session.query(func.count(PageView.id)).filter(*window).scalar() or 0
    unique_visitors = db.session.query(
        func.count(func.distinct(PageView.visitor_hash))).filter(*window).scalar() or 0

    # Per-bucket views + unique visitors, restricted to this article's path.
    chart_data = bucketed_series([PageView.path == path], days, group, today)

    top_referrers = db.session.query(
        PageView.referrer, func.count(PageView.id).label('n')
    ).filter(*window, PageView.referrer.isnot(None)).group_by(
        PageView.referrer).order_by(func.count(PageView.id).desc()).limit(10).all()

    # Reactions (in display order) + comment counts.
    reaction_rows = dict(
        db.session.query(Reaction.emoji, func.count(Reaction.id))
        .filter(Reaction.article_id == article.id).group_by(Reaction.emoji).all()
    )
    reactions = [{'emoji': e, 'count': reaction_rows[e]} for e in EMOJIS if reaction_rows.get(e)]
    approved_comments = db.session.query(func.count(Comment.id)).filter(
        Comment.article_id == article.id, Comment.approved == True).scalar() or 0
    pending_comments = db.session.query(func.count(Comment.id)).filter(
        Comment.article_id == article.id, Comment.approved == False).scalar() or 0

    email_stats = article_email_stats(article)

    return render_template(
        'articles_admin_stats.html',
        article=article,
        days=days,
        group=group,
        total_views=total_views,
        window_views=window_views,
        unique_visitors=unique_visitors,
        chart_data=chart_data,
        top_referrers=top_referrers,
        reactions=reactions,
        approved_comments=approved_comments,
        pending_comments=pending_comments,
        email_stats=email_stats,
    )


# ─── ADMIN: GOOGLE DRIVE IMPORT ─────────────────────────────
#
# The article editor has an "Importer depuis Google Drive" button that lists
# Bernard's Google Docs and loads a chosen one into the editor. These two
# JSON endpoints back that UI; both are admin-only and read-only.

@admin_articles_bp.route('/gdrive/documents')
@admin_required
def gdrive_documents():
    """List Bernard's Google Docs as JSON for the import picker."""
    from gdrive import (
        is_configured, list_documents, GoogleDriveError, GoogleDriveAuthError,
    )
    if not is_configured():
        return jsonify(configured=False, documents=[]), 200
    try:
        docs = list_documents(query=request.args.get('q'))
    except GoogleDriveAuthError as exc:
        # `reconnect` tells the picker to offer the OAuth link under the error.
        return jsonify(configured=True, error=str(exc), reconnect=True), 502
    except GoogleDriveError as exc:
        return jsonify(configured=True, error=str(exc)), 502
    return jsonify(configured=True, documents=docs), 200


@admin_articles_bp.route('/gdrive/documents/<file_id>')
@admin_required
def gdrive_document(file_id):
    """Return one Google Doc's title and cleaned HTML body, ready to drop into
    the editor. The HTML goes through the same bleach + normaliser pipeline as
    a saved article, so imported content matches hand-typed content."""
    from gdrive import (
        is_configured, get_document, strip_boilerplate, GoogleDriveError,
        GoogleDriveAuthError,
    )
    if not is_configured():
        return jsonify(error="Google Drive n'est pas configuré."), 400
    try:
        doc = get_document(file_id)
    except GoogleDriveAuthError as exc:
        return jsonify(error=str(exc), reconnect=True), 502
    except GoogleDriveError as exc:
        return jsonify(error=str(exc)), 502
    content_html = clean_article_html(_clean_html(doc['html']))
    # Lift the title line out of the body and drop trailing author/date lines,
    # then tidy the title's typography (space after commas, etc.).
    title, content_html = strip_boilerplate(content_html, doc['name'])
    return jsonify(title=clean_text(title), content_html=content_html), 200


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

    # Cascade: remove everything that references this article before deleting
    # it (there are no ON DELETE rules, so the FK constraints would otherwise
    # block the delete).
    from engagement.models import Comment, Reaction
    from newsletter.models import Campaign, Delivery
    from analytics.models import PageView

    Comment.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    Reaction.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    Delivery.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    Campaign.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    # Page views are logged by path, not by FK.
    PageView.query.filter_by(path=f'/articles/{article.slug}').delete(synchronize_session=False)

    db.session.delete(article)
    db.session.commit()
    flash("Article supprimé (commentaires, réactions, envois et vues associés inclus).", 'success')
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


def _auto_social_summary(title, content_html):
    """Build the social (X) one-liner for an article saved without one. Unlike
    the editorial summary there is no text-extraction fallback: a first-person,
    witty line only makes sense if the AI wrote it, so we return '' when the AI
    is unavailable and the tweet falls back to the editorial summary."""
    try:
        from articles.ai_summary import generate_social_summary
        return generate_social_summary(title, content_html) or ''
    except Exception as exc:
        from flask import current_app
        current_app.logger.warning(f"AI social summary on save failed: {exc}")
        return ''


def ai_summary_themes():
    from articles.ai_summary import THEMES
    return THEMES


def _theme_by_name(name):
    """Get-or-create a Theme row. The vocabulary is fixed in ai_summary.THEMES,
    so rows are created lazily the first time a theme is actually used rather
    than seeded up front."""
    theme = Theme.query.filter_by(name=name).first()
    if theme is None:
        theme = Theme(name=name, slug=slugify(name))
        db.session.add(theme)
        db.session.flush()
    return theme


def _auto_themes(title, content_html):
    """Ask the AI to classify the article. Returns [] when the AI is
    unavailable — an untagged article is fine, a wrongly tagged one is not."""
    try:
        from articles.ai_summary import generate_themes
        return [_theme_by_name(n) for n in (generate_themes(title, content_html) or [])]
    except Exception as exc:
        from flask import current_app
        current_app.logger.warning(f"AI themes on save failed: {exc}")
        return []


def _save_article(article):
    # Normalise the title's typography (space after commas, etc.) whatever its
    # source — typed, edited, or imported.
    title = clean_text((request.form.get('title') or '').strip())
    summary = (request.form.get('summary') or '').strip()
    social_summary = (request.form.get('social_summary') or '').strip()
    # Themes the admin ticked; empty means "let the AI decide" on first save.
    chosen_themes = request.form.getlist('themes')
    image_url = (request.form.get('image_url') or '').strip()
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

    # An uploaded file wins over whatever the hidden URL field carried, so
    # re-saving without choosing a new image keeps the current one.
    if request.form.get('remove_image'):
        image_url = ''
    upload = request.files.get('image_file')
    if upload and upload.filename:
        import storage
        try:
            image_url = storage.upload_image(upload)
        except storage.StorageError as exc:
            flash(f"Image non enregistrée : {exc}", 'danger')

    # Same rule for the social line used on X.
    auto_social = False
    if not social_summary and content_html:
        social_summary = _auto_social_summary(title, content_html)
        auto_social = bool(social_summary)

    if article is None:
        article = Article(
            title=title,
            slug=_unique_slug(title),
            summary=summary,
            social_summary=social_summary or None,
            image_url=image_url or None,
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
        article.social_summary = social_summary or None
        article.image_url = image_url or None
        article.content_html = content_html
        article.published = published
        article.author_id = author.id if author else None
        if created_at is not None:
            article.created_at = created_at

    # Explicit ticks always win; the AI only fills a blank slate.
    auto_theme = False
    if chosen_themes:
        article.themes = [_theme_by_name(n) for n in chosen_themes
                          if n in ai_summary_themes()]
    elif not article.themes and content_html:
        found = _auto_themes(title, content_html)
        article.themes = found
        auto_theme = bool(found)

    db.session.commit()
    if auto_summary and auto_social:
        flash("Article enregistré. Résumé et accroche X générés automatiquement — pensez à les relire.", 'success')
    elif auto_summary:
        flash("Article enregistré. Résumé généré automatiquement — pensez à le relire.", 'success')
    elif auto_social:
        flash("Article enregistré. Accroche X générée automatiquement — pensez à la relire.", 'success')
    else:
        flash("Article enregistré.", 'success')
    return redirect(url_for('admin_articles.edit_article', article_id=article.id))


@admin_articles_bp.route('/cleanup-all', methods=['POST'])
@admin_required
def cleanup_all():
    """Re-run the HTML normaliser (and title tidy) on every article. Skips
    rows whose cleaned output is byte-identical to what's stored so we don't
    bump `updated_at` for no reason. Titles are tidied in place without
    re-slugging, so URLs stay stable."""
    changed = 0
    for article in Article.query.all():
        touched = False
        cleaned = clean_article_html(article.content_html or '')
        if cleaned != (article.content_html or ''):
            article.content_html = cleaned
            touched = True
        cleaned_title = clean_text(article.title or '')
        if cleaned_title and cleaned_title != (article.title or ''):
            article.title = cleaned_title
            touched = True
        if touched:
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


@admin_articles_bp.route('/propose-social-summaries', methods=['POST'])
@admin_required
def propose_social_summaries():
    """Same idea as `propose_summaries`, for the X accroche: a first-person,
    livelier line written by Claude in Bernard's voice. There is no text
    extraction fallback here — an article without an accroche simply keeps
    using its editorial summary in the tweet — so this is a no-op when the AI
    isn't configured.

    Bounded per click like the summary backfill; relaunch to process the rest."""
    from flask import current_app
    from articles.ai_summary import generate_social_summary, is_configured

    MAX_AI_PER_RUN = 12

    if not is_configured():
        flash("IA non configurée — définissez ANTHROPIC__API_KEY pour générer les accroches X.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))

    pending = [a for a in Article.query.order_by(Article.created_at.desc()).all()
               if not (a.social_summary or '').strip() and (a.content_html or '').strip()]

    filled = 0
    failed = 0
    for article in pending[:MAX_AI_PER_RUN]:
        try:
            proposed = generate_social_summary(article.title, article.content_html or '')
        except Exception as exc:
            current_app.logger.warning(
                f"AI social summary failed for article {article.id}: {exc}"
            )
            failed += 1
            continue
        if proposed:
            article.social_summary = proposed
            filled += 1

    db.session.commit()

    if not filled:
        if failed:
            flash(f"Aucune accroche générée — {failed} échec(s) côté IA, réessayez.", 'danger')
        else:
            flash("Tous les articles ont déjà une accroche X.", 'info')
    else:
        remaining = len(pending) - filled
        msg = f"{filled} accroche(s) X rédigée(s) par l'IA"
        if failed:
            msg += f" — {failed} échec(s)"
        if remaining > 0:
            msg += f". {remaining} restant(s), relancez pour continuer"
        flash(msg + ".", 'success')
    return redirect(url_for('admin_articles.list_articles'))


@admin_articles_bp.route('/propose-themes', methods=['POST'])
@admin_required
def propose_themes():
    """Classify every article that has no theme yet. Same bounded-per-click
    shape as the summary backfills — sequential AI calls are slow."""
    from flask import current_app
    from articles.ai_summary import generate_themes, is_configured

    MAX_AI_PER_RUN = 12

    if not is_configured():
        flash("IA non configurée — définissez ANTHROPIC__API_KEY pour classer les articles.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))

    pending = [a for a in Article.query.order_by(Article.created_at.desc()).all()
               if not a.themes and (a.content_html or '').strip()]

    tagged = failed = 0
    for article in pending[:MAX_AI_PER_RUN]:
        try:
            names = generate_themes(article.title, article.content_html or '')
        except Exception as exc:
            current_app.logger.warning(f"AI themes failed for article {article.id}: {exc}")
            failed += 1
            continue
        if names:
            article.themes = [_theme_by_name(n) for n in names]
            tagged += 1
    db.session.commit()

    if not tagged:
        flash("Aucun thème ajouté — tous les articles sont déjà classés." if not failed
              else f"Aucun thème ajouté ({failed} échec(s) côté IA).",
              'info' if not failed else 'danger')
    else:
        remaining = len(pending) - tagged
        msg = f"{tagged} article(s) classé(s) par thème"
        if remaining > 0:
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
