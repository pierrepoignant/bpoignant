from datetime import datetime, date, time, timedelta

import bleach
import re
from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
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
    related_videos = _related_videos(article)
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
        related_videos=related_videos,
        comments=comments,
        reactions=reactions_context(article),
    )


def _related_articles(article, limit=3):
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


def _related_videos(article, limit=3):
    """Clips sharing a theme with this article, newest first, topped up with
    recent ones.

    The clips stand alone — none of them is the video of an article — so the
    link is thematic, not editorial, and the block is labelled accordingly.
    """
    from tiktok.models import TikTokPost

    theme_ids = {t.id for t in article.themes}
    picked = []
    if theme_ids:
        candidates = (
            TikTokPost.query
            .filter(TikTokPost.video_url.isnot(None),
                    TikTokPost.themes.any(Theme.id.in_(theme_ids)))
            .all()
        )
        candidates.sort(
            key=lambda v: (-len(theme_ids & {t.id for t in v.themes}),
                           -(v.posted_at or v.created_at).timestamp())
        )
        picked = candidates[:limit]
    if len(picked) < limit:
        seen = {v.id for v in picked}
        picked.extend(
            TikTokPost.query
            .filter(TikTokPost.video_url.isnot(None), TikTokPost.id.notin_(seen or [0]))
            .order_by(TikTokPost.posted_at.desc())
            .limit(limit - len(picked))
            .all()
        )
    return picked


@articles_bp.route('/themes')
def public_themes():
    """Index of the themes actually in use.

    Only themes with something published under them are listed: an empty theme
    is a dead end for a reader and a thin page for a crawler.
    """
    from tiktok.models import TikTokPost

    themes = Theme.query.order_by(Theme.name).all()
    entries = []
    for theme in themes:
        articles = theme.published_articles
        videos = [v for v in theme.tiktok_posts if v.video_url]
        if not articles and not videos:
            continue
        entries.append({
            'theme': theme,
            'articles': len(articles),
            'videos': len(videos),
            'last': max((a.created_at for a in articles), default=None),
        })
    # Le plus fourni d'abord : c'est ce sur quoi il a le plus écrit.
    entries.sort(key=lambda e: (-e['articles'] - e['videos'], e['theme'].name))
    return render_template('articles_public_themes.html', entries=entries)


@articles_bp.route('/theme/<slug>')
def public_theme(slug):
    from tiktok.models import TikTokPost

    theme = Theme.query.filter_by(slug=slug).first() or abort(404)
    articles = (
        Article.query
        .filter(Article.published == True,  # noqa: E712
                Article.themes.any(Theme.id == theme.id))
        .order_by(Article.created_at.desc())
        .all()
    )
    # Les clips du même thème : ils n'ont pas d'article derrière eux, et cette
    # page est le seul endroit du site où ils sont lisibles par un moteur.
    videos = (
        TikTokPost.query
        .filter(TikTokPost.video_url.isnot(None),
                TikTokPost.themes.any(Theme.id == theme.id))
        .order_by(TikTokPost.posted_at.desc())
        .all()
    )
    return render_template('articles_public_theme.html', theme=theme,
                           articles=articles, videos=videos)


# ─── ADMIN ──────────────────────────────────────────────────

admin_themes_bp = Blueprint(
    'admin_themes', __name__, url_prefix='/admin/themes', template_folder='templates'
)


def _theme_entries():
    from tiktok.models import TikTokPost

    entries = []
    for theme in Theme.query.order_by(Theme.name).all():
        articles = theme.published_articles
        videos = [v for v in theme.tiktok_posts if v.video_url]
        entries.append({'theme': theme, 'articles': len(articles),
                        'videos': len(videos),
                        'used': bool(articles or videos)})
    return entries


def _describe(theme):
    """Write a theme's description from the articles filed under it."""
    from articles.ai_summary import generate_theme_description

    pairs = [(a.title, a.summary) for a in theme.published_articles]
    try:
        text = generate_theme_description(theme.name, pairs)
    except Exception:
        current_app.logger.exception('theme description failed for %s', theme.name)
        return None
    if text:
        theme.description = text
        theme.description_at = datetime.utcnow()
    return text


@admin_themes_bp.route('/')
@admin_required
def list_themes():
    return render_template('themes_admin.html', entries=_theme_entries())


@admin_themes_bp.route('/<int:theme_id>/describe', methods=['POST'])
@admin_required
def describe_theme(theme_id):
    theme = db.session.get(Theme, theme_id) or abort(404)
    text = _describe(theme)
    db.session.commit()
    flash(f"Présentation rédigée pour « {theme.name} »." if text
          else "L'IA n'a rien renvoyé — thème sans article publié, ou clé absente.",
          'success' if text else 'danger')
    return redirect(url_for('admin_themes.list_themes'))


@admin_themes_bp.route('/describe-missing', methods=['POST'])
@admin_required
def describe_missing():
    """Fill in every theme that has articles but no description yet."""
    done = 0
    for theme in Theme.query.order_by(Theme.name).all():
        if theme.description or not theme.published_articles:
            continue
        if _describe(theme):
            done += 1
    db.session.commit()
    flash(f"{done} présentation(s) rédigée(s)." if done
          else "Rien à rédiger — toutes les présentations existent déjà.",
          'success')
    return redirect(url_for('admin_themes.list_themes'))


@admin_themes_bp.route('/<int:theme_id>/save', methods=['POST'])
@admin_required
def save_theme(theme_id):
    theme = db.session.get(Theme, theme_id) or abort(404)
    theme.description = (request.form.get('description') or '').strip() or None
    theme.description_at = datetime.utcnow() if theme.description else None
    db.session.commit()
    flash("Présentation enregistrée.", 'success')
    return redirect(url_for('admin_themes.list_themes'))


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

    # Texte prêt à coller pour le profil Facebook personnel de Bernard. L'API
    # Meta ne publie pas sur un profil personnel (seulement sur une Page), donc
    # ici on ne publie pas : on prépare l'accroche et le lien, Bernard colle.
    fb_prepared_by_article = {}
    for a in articles:
        if a.published:
            lien = __import__('meta').share_url(
                url_for('articles.public_show', slug=a.slug, _external=True), 'facebook-profil')
            accroche = (a.social_summary or '').strip() or (a.summary or '').strip()
            fb_prepared_by_article[a.id] = (
                f"{accroche}\n\n{lien}" if accroche else f"{a.title}\n\n{lien}")

    return render_template(
        'articles_admin_list.html',
        articles=articles,
        last_campaign_by_article=last_campaign_by_article,
        views_by_article=views_by_article,
        comments_by_article=comments_by_article,
        reactions_by_article=reactions_by_article,
        x_configured=x_configured,
        linkedin_configured=__import__('linkedin').is_configured(),
        abonnes_lettre=__import__('newsletter')._mailable_query('lettre').count(),
        mon_email=getattr(current_user, 'email', '') or '',
        x_proposed_by_article=x_proposed_by_article,
        fb_prepared_by_article=fb_prepared_by_article,
        meta_connected=__import__('meta').is_connected(),
        threads_connected=__import__('threads').is_connected(),
    )


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


@admin_articles_bp.route('/<int:article_id>/share-linkedin', methods=['POST'])
@admin_required
def share_linkedin(article_id):
    """Share an article on Bernard's LinkedIn profile.

    LinkedIn allows three thousand characters, so the text is the summary and
    the link rather than the compressed line X needs.
    """
    import linkedin

    article = db.session.get(Article, article_id) or abort(404)
    if article.linkedin_posted_at:
        flash(f"Déjà partagé sur LinkedIn le {article.linkedin_posted_at:%d/%m/%Y}.", 'info')
        return redirect(url_for('admin_articles.list_articles'))
    if not linkedin.is_configured():
        flash("LinkedIn n'est pas connecté — voir Réglages.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))

    texte = (request.form.get('text') or '').strip()
    if not texte:
        url = linkedin.share_url(
            url_for('articles.public_show', slug=article.slug, _external=True))
        resume = (article.summary or '').strip()
        texte = f"{article.title}\n\n{resume}\n\n{url}" if resume else f"{article.title}\n\n{url}"

    ok, detail = linkedin.post_text(texte)
    if ok:
        article.linkedin_post_id = str(detail) if detail else None
        article.linkedin_posted_at = datetime.utcnow()
        db.session.commit()
        flash("Article partagé sur LinkedIn.", 'success')
    else:
        flash(f"Échec du partage sur LinkedIn : {detail}", 'danger')
    return redirect(url_for('admin_articles.list_articles'))


@admin_articles_bp.route('/<int:article_id>/share-facebook-page', methods=['POST'])
@admin_required
def share_facebook_page(article_id):
    """Publier un article sur la Page Facebook (publication de lien).

    Le profil personnel n'est pas publiable par l'API : c'est la Page. L'aperçu
    (titre, image, extrait) vient des balises Open Graph de l'article.
    """
    import meta

    article = db.session.get(Article, article_id) or abort(404)
    if not article.published:
        flash("L'article doit être publié avant de le partager sur la Page.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    if article.fb_page_posted_at:
        flash(f"Déjà sur la Page Facebook le {article.fb_page_posted_at:%d/%m/%Y}.", 'info')
        return redirect(url_for('admin_articles.list_articles'))
    if not meta.is_connected():
        flash("Facebook n'est pas connecté — voir Réglages.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))

    ok, detail = meta.fb_post_article(article)
    flash("Article publié sur la Page Facebook." if ok else f"Échec Facebook : {detail}",
          'success' if ok else 'danger')
    return redirect(url_for('admin_articles.list_articles'))


@admin_articles_bp.route('/<int:article_id>/share-threads', methods=['POST'])
@admin_required
def share_threads(article_id):
    """Publier un article sur Threads (accroche + lien)."""
    import threads
    article = db.session.get(Article, article_id) or abort(404)
    if not article.published:
        flash("L'article doit être publié avant Threads.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    if article.threads_posted_at:
        flash(f"Déjà sur Threads le {article.threads_posted_at:%d/%m/%Y}.", 'info')
        return redirect(url_for('admin_articles.list_articles'))
    if not threads.is_connected():
        flash("Threads n'est pas connecté — voir Réglages.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    ok, detail = threads.post_article(article)
    flash("Article publié sur Threads." if ok else f"Échec Threads : {detail}",
          'success' if ok else 'danger')
    return redirect(url_for('admin_articles.list_articles'))


# ─── Facebook profil personnel (scraping Apify) ─────────────
#
# Le profil personnel de Bernard n'a pas d'API : Meta l'a retirée en 2018. On
# lit donc ses posts publics par un scrapeur Apify, et on relie chaque post à
# l'article dont il contient le lien bernardpoignant.fr. Fragile par nature
# (Facebook bloque, change sa page), donc un relevé raté ne touche à rien.

_LIEN_ARTICLE = re.compile(r'bernardpoignant\.fr/articles/([A-Za-z0-9\-]+)', re.I)


def _fb_profile_pick(item, *names):
    for n in names:
        v = item.get(n)
        if v not in (None, ''):
            return v
    return None


def _fb_profile_date(item):
    brut = _fb_profile_pick(item, 'time', 'timestamp', 'date', 'publishTime', 'pubDate')
    if brut is None:
        return None
    try:
        if isinstance(brut, (int, float)) or (isinstance(brut, str) and brut.isdigit()):
            return datetime.utcfromtimestamp(int(brut))
        from datetime import datetime as _dt
        return _dt.fromisoformat(str(brut).replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, OSError, OverflowError):
        return None


def _fb_profile_slug(item):
    """Le slug d'article contenu dans un post, ou None.

    On regarde le texte, les liens attachés et l'URL du post : selon l'acteur
    et la mise en page Facebook, le lien peut se trouver dans l'un ou l'autre.
    """
    champs = [str(_fb_profile_pick(item, 'text', 'message', 'postText') or '')]
    for cle in ('links', 'link', 'url', 'postUrl', 'facebookUrl',
                'previewTarget', 'previewSource', 'topLevelUrl'):
        v = item.get(cle)
        if isinstance(v, str):
            champs.append(v)
        elif isinstance(v, (list, tuple)):
            champs += [str(x.get('url') if isinstance(x, dict) else x) for x in v]
    for texte in champs:
        m = _LIEN_ARTICLE.search(texte or '')
        if m:
            return m.group(1).rstrip('.,);')
    return None


def _fb_profile_apply(obj, item, maintenant):
    """Reporter les chiffres d'un post de profil sur un article ou une vidéo —
    ils portent les mêmes champs fb_profile_*."""
    def entier(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    obj.fb_profile_post_url = (_fb_profile_pick(item, 'url', 'topLevelUrl', 'postUrl', 'facebookUrl') or '')[:500] or None
    obj.fb_profile_posted_at = _fb_profile_date(item) or obj.fb_profile_posted_at
    likes = entier(_fb_profile_pick(item, 'likes', 'likesCount', 'reactionsCount', 'reactions', 'topReactionsCount'))
    comm = entier(_fb_profile_pick(item, 'comments', 'commentsCount'))
    shares = entier(_fb_profile_pick(item, 'shares', 'sharesCount'))
    if likes is not None:
        obj.fb_profile_likes = likes
    if comm is not None:
        obj.fb_profile_comments = comm
    if shares is not None:
        obj.fb_profile_shares = shares
    obj.fb_profile_metrics_at = maintenant


def _fb_norm(texte):
    return re.sub(r'\s+', ' ', (texte or '').lower()).strip()


def _fb_video_index():
    """{texte normalisé → vidéo} pour retrouver une vidéo à partir du texte d'un
    post de profil : Bernard colle notre légende, donc elle s'y retrouve. On
    indexe légende et titre, et on ne garde que les textes assez longs pour que
    la correspondance par sous-chaîne ne tombe pas sur un hasard."""
    from tiktok.models import TikTokPost
    paires = []
    for v in TikTokPost.query.filter(TikTokPost.video_url.isnot(None)).all():
        for champ in (v.caption, v.title):
            norm = _fb_norm(champ)
            if len(norm) >= 20:
                paires.append((norm, v))
    # Les plus longs d'abord : une correspondance spécifique prime sur une
    # légende courte incluse par hasard dans une autre.
    paires.sort(key=lambda t: len(t[0]), reverse=True)
    return paires


def _fb_match_video(poste_txt, paires):
    if not poste_txt:
        return None
    for norm, v in paires:
        if norm == poste_txt or norm in poste_txt:
            return v
    return None


def refresh_facebook_profile(log_fn=print):
    """Relever les posts publics du profil Facebook de Bernard et reporter les
    chiffres sur l'article cité (par son lien) ou sur la vidéo (par le texte de
    la légende, que Bernard colle). Renvoie le nombre de publications mises à
    jour."""
    import apify
    from flask import url_for  # noqa: F401  (contexte fourni par l'appelant)
    if not apify.is_configured():
        log_fn("Apify n'est pas configuré — rien à faire.")
        return 0
    try:
        items = apify.scrape_facebook_profile()
    except apify.ApifyError as exc:
        log_fn(f"Scraping Facebook impossible : {exc}")
        return 0
    maintenant = datetime.utcnow()
    videos = _fb_video_index()
    n = 0
    for item in items or []:
        slug = _fb_profile_slug(item)
        if slug:
            article = Article.query.filter_by(slug=slug).first()
            if article is not None:
                _fb_profile_apply(article, item, maintenant)
                n += 1
                continue
        # Pas de lien d'article : c'est peut-être une vidéo, reconnue au texte.
        video = _fb_match_video(_fb_norm(_fb_profile_pick(item, 'text', 'message', 'postText')), videos)
        if video is not None:
            _fb_profile_apply(video, item, maintenant)
            n += 1
    db.session.commit()
    log_fn(f"Facebook profil : {n} publication(s) mise(s) à jour sur {len(items or [])} post(s) lus.")
    return n


def refresh_article_social_stats(log_fn=print):
    """Relever les chiffres des articles sur la Page Facebook (j'aime,
    commentaires, partages) et sur Threads (vues, j'aime, réponses, reposts)."""
    import meta
    import threads
    maintenant = datetime.utcnow()
    n = 0
    if meta.is_connected():
        for a in Article.query.filter(Article.fb_page_post_id.isnot(None)).all():
            st = meta.facebook_post_stats(a.fb_page_post_id)
            if st:
                a.fb_page_likes, a.fb_page_comments, a.fb_page_shares = st['likes'], st['comments'], st['shares']
                a.fb_page_metrics_at = maintenant
                n += 1
    if threads.is_connected():
        for a in Article.query.filter(Article.threads_post_id.isnot(None)).all():
            st = threads.post_stats(a.threads_post_id)
            if st:
                a.threads_views, a.threads_likes = st['views'], st['likes']
                a.threads_replies, a.threads_reposts = st['replies'], st['reposts']
                a.threads_metrics_at = maintenant
                n += 1
    db.session.commit()
    log_fn(f"chiffres réseaux des articles : {n} mise(s) à jour.")
    return n


@admin_articles_bp.route('/facebook-profile/refresh', methods=['POST'])
@admin_required
def refresh_facebook_profile_route():
    messages = []
    try:
        messages.append(f"profil Facebook : {refresh_facebook_profile()} article(s)")
    except Exception as exc:  # noqa: BLE001
        messages.append(f"profil Facebook impossible ({exc})")
    try:
        refresh_article_social_stats(lambda m: messages.append(m))
    except Exception as exc:  # noqa: BLE001
        messages.append(f"chiffres Page/Threads impossibles ({exc})")
    flash("Relevé : " + " · ".join(messages), 'success')
    return redirect(url_for('admin_articles.list_articles'))


_MIN_CORPS = 100  # caractères : en deçà, l'article est vide ou presque, on bloque l'envoi


def _corps_trop_court(article):
    """True si le texte de l'article (sans balises) fait moins de _MIN_CORPS
    caractères — garde-fou contre l'envoi d'une lettre vide."""
    txt = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', article.content_html or '')).strip()
    return len(txt) < _MIN_CORPS


@admin_articles_bp.route('/<int:article_id>/send-newsletter/test', methods=['POST'])
@admin_required
def send_newsletter_test(article_id):
    """Send this article to one address only."""
    from newsletter import send_article_test, _EMAIL_RE
    from mail import is_configured as mail_is_configured

    article = db.session.get(Article, article_id) or abort(404)
    to = (request.form.get('email') or getattr(current_user, 'email', '') or '').strip()
    if not _EMAIL_RE.match(to):
        flash("Adresse de test invalide.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    if not mail_is_configured():
        flash("SendGrid n'est pas configuré — envoi impossible.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    if _corps_trop_court(article):
        flash("Article trop court (moins de 100 caractères) — envoi bloqué pour éviter une lettre vide.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))

    ok = send_article_test(article, to, intro=(request.form.get('intro') or '').strip() or None)
    flash(f"E-mail de test envoyé à {to}." if ok
          else "L'envoi du test a échoué — voir les journaux.",
          'success' if ok else 'danger')
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
    if _corps_trop_court(article):
        flash("Article trop court (moins de 100 caractères) — envoi bloqué pour éviter d'envoyer une lettre vide comme la dernière fois.", 'danger')
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
        if _jeton_deja_vu(request.form.get('form_token')):
            # Deuxième envoi du même formulaire : ne pas créer un jumeau, et
            # emmener là où le premier a mené.
            dernier = (Article.query.order_by(Article.id.desc()).first())
            flash("Cet article vient d'être créé — le second envoi a été ignoré.", 'info')
            return redirect(url_for('admin_articles.edit_article', article_id=dernier.id)
                            if dernier else url_for('admin_articles.list_articles'))
        return _save_article(None)
    # Default a new article's author to Bernard Poignant (fallback: first author).
    authors = _all_authors()
    default = next((a for a in authors if a.name == 'Bernard Poignant'), None) or (authors[0] if authors else None)
    from gdrive import is_configured as gdrive_is_configured
    return render_template(
        'articles_admin_form.html',
        article=None,
        form_token=_jeton_neuf(),
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


@admin_articles_bp.route('/<int:article_id>/edit/autosave', methods=['POST'])
@admin_required
def edit_article_autosave(article_id):
    """Enregistrer le texte en cours de frappe, sans effet de bord : ni
    nouveau slug, ni résumé auto, ni changement de publication, ni thèmes. On
    ne touche qu'au titre, au résumé, à l'accroche et au corps — pour ne rien
    perdre si on ferme sans cliquer « Enregistrer »."""
    from articles.cleanup import clean_article_html, clean_text
    article = db.session.get(Article, article_id)
    if article is None:
        return jsonify(ok=False), 404
    if 'title' in request.form:
        titre = clean_text((request.form.get('title') or '').strip())
        if titre:
            article.title = titre
    if 'summary' in request.form:
        article.summary = (request.form.get('summary') or '').strip()
    if 'social_summary' in request.form:
        article.social_summary = (request.form.get('social_summary') or '').strip() or None
    if 'content_html' in request.form:
        article.content_html = clean_article_html(_clean_html(request.form.get('content_html') or ''))
    db.session.commit()
    return jsonify(ok=True, at=datetime.utcnow().strftime('%H:%M:%S'))


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

    # Il n'y a aucune règle ON DELETE : chaque table qui pointe vers l'article
    # doit être traitée à la main, sinon sa contrainte bloque la suppression.
    # Ce qui appartient à l'article est supprimé ; les liens optionnels vers une
    # vidéo ou une chronique du livre sont seulement détachés, pour ne pas
    # détruire la vidéo ou le texte au passage. (`article_themes` est géré par
    # la relation SQLAlchemy et se vide tout seul.)
    from engagement.models import Comment, Reaction
    from newsletter.models import Campaign, Delivery, EmailEvent
    from analytics.models import PageView
    from tweets.models import TweetReply
    from tiktok.models import TikTokPost
    from livre.models import BookDoc

    Comment.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    Reaction.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    EmailEvent.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    Delivery.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    Campaign.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    TweetReply.query.filter_by(article_id=article.id).delete(synchronize_session=False)
    # Détacher, sans supprimer : une vidéo ou une chronique du livre survit à
    # l'article qui lui était associé.
    TikTokPost.query.filter_by(article_id=article.id).update(
        {'article_id': None}, synchronize_session=False)
    BookDoc.query.filter_by(article_id=article.id).update(
        {'article_id': None}, synchronize_session=False)
    # Les vues sont enregistrées par chemin, pas par clé étrangère.
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


_JETONS_UTILISES = 'articles_jetons_soumis'
_JETONS_MAX = 20


def _jeton_neuf():
    """A one-shot token placed in the creation form."""
    import secrets as _s
    return _s.token_urlsafe(16)


def _jeton_deja_vu(jeton):
    """True when this form has already been submitted.

    Bernard clicks Créer twice — the first click has no visible effect while
    the article is being saved, so a second is the natural thing to do. The
    button is disabled on submit as well, but that only helps where the script
    runs and not at all on a reload of the posted form, so the guard belongs
    here too. Consumed tokens are kept in the session, capped, since only the
    last few can plausibly be resubmitted.
    """
    if not jeton:
        return False
    vus = session.get(_JETONS_UTILISES) or []
    if jeton in vus:
        return True
    vus.append(jeton)
    session[_JETONS_UTILISES] = vus[-_JETONS_MAX:]
    return False


def _save_article(article):
    # Normalise the title's typography (space after commas, etc.) whatever its
    # source — typed, edited, or imported.
    title = clean_text((request.form.get('title') or '').strip())
    summary = (request.form.get('summary') or '').strip()
    social_summary = (request.form.get('social_summary') or '').strip()
    # Themes the admin ticked; empty means "let the AI decide" on first save.
    chosen_themes = request.form.getlist('themes')
    image_url = (request.form.get('image_url') or '').strip()
    # Un gabarit qui rend None sans garde-fou envoie la chaîne « None », qui est
    # ensuite vraie partout : <img src="None"> sur la page publique et dans les
    # données structurées. Le gabarit est corrigé, mais la valeur est refusée
    # ici aussi — c'est le seul point par lequel tout passe.
    if image_url.lower() in ('none', 'null', 'undefined'):
        image_url = ''
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


def backfill_missing_ai(limit=8, log_fn=None):
    """Compléter par IA les champs manquants (résumé, accroche X, thèmes) des
    articles qui en manquent, du plus récent au plus ancien, jusqu'à `limit`.
    Partagé par le bouton d'admin et par le cron horaire qui rattrape les
    échecs d'IA au moment de l'enregistrement. Renvoie un compte rendu."""
    from flask import current_app
    from articles.ai_summary import (
        generate_summary, generate_social_summary, generate_themes, is_configured,
    )
    if not is_configured():
        return {'ok': False, 'raison': 'IA non configurée',
                'summary': 0, 'social': 0, 'themes': 0, 'failed': 0, 'pending': 0}

    def manquant(a):
        return {'summary': not (a.summary or '').strip(),
                'social': not (a.social_summary or '').strip(),
                'themes': not a.themes}

    pending = [a for a in Article.query.order_by(Article.created_at.desc()).all()
               if (a.content_html or '').strip() and any(manquant(a).values())]
    rempli = {'summary': 0, 'social': 0, 'themes': 0}
    failed = 0
    for article in pending[:limit]:
        trous = manquant(article)
        body = article.content_html or ''
        try:
            if trous['summary']:
                article.summary = generate_summary(article.title, body) or summarize_html(body)
                rempli['summary'] += 1
            if trous['social']:
                v = generate_social_summary(article.title, body)
                if v:
                    article.social_summary = v
                    rempli['social'] += 1
            if trous['themes']:
                names = generate_themes(article.title, body)
                if names:
                    article.themes = [_theme_by_name(n) for n in names]
                    rempli['themes'] += 1
            if log_fn:
                log_fn(f"#{article.id} {(article.title or '')[:45]} — "
                       + ', '.join(k for k, v in trous.items() if v))
        except Exception as exc:  # noqa: BLE001
            try:
                current_app.logger.warning(f"AI backfill failed for article {article.id}: {exc}")
            except Exception:
                pass
            failed += 1
            continue
    db.session.commit()
    return {'ok': True, 'summary': rempli['summary'], 'social': rempli['social'],
            'themes': rempli['themes'], 'failed': failed, 'pending': len(pending)}


@admin_articles_bp.route('/propose-missing', methods=['POST'])
@admin_required
def propose_missing():
    """Bouton d'admin : compléter les champs IA manquants, huit articles au
    plus par passage (chaque article coûte jusqu'à trois appels)."""
    MAX = 8
    r = backfill_missing_ai(limit=MAX)
    if not r['ok']:
        flash("IA non configurée — définissez ANTHROPIC__API_KEY.", 'danger')
        return redirect(url_for('admin_articles.list_articles'))
    done = [f"{n} {label}" for label, n in (
        ("résumé(s)", r['summary']),
        ("accroche(s) X", r['social']),
        ("classement(s) par thème", r['themes'])) if n]
    if not done:
        flash("Rien à compléter — tous les articles sont à jour." if not r['failed']
              else f"Rien complété ({r['failed']} échec(s) côté IA).",
              'info' if not r['failed'] else 'danger')
    else:
        remaining = r['pending'] - min(r['pending'], MAX)
        msg = "Complété : " + ", ".join(done)
        if r['failed']:
            msg += f" — {r['failed']} échec(s)"
        if remaining > 0:
            msg += f". {remaining} article(s) restant(s), relancez pour continuer"
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
