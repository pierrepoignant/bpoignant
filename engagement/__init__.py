import hashlib
import logging
from datetime import datetime

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from init_db import db
from articles.models import Article
from engagement.models import Comment, Reaction
from auth import admin_required
from auth.models import User
from mail import send_email, is_configured as mail_is_configured

log = logging.getLogger(__name__)

APPROVE_TOKEN_SALT = 'comment-approve-v1'
APPROVE_TOKEN_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'])


def _make_approve_token(comment_id):
    return _serializer().dumps({'comment_id': comment_id}, salt=APPROVE_TOKEN_SALT)


def _read_approve_token(token):
    try:
        data = _serializer().loads(token, salt=APPROVE_TOKEN_SALT, max_age=APPROVE_TOKEN_MAX_AGE)
    except SignatureExpired:
        return None, 'expired'
    except BadSignature:
        return None, 'invalid'
    return data.get('comment_id'), None

engagement_bp = Blueprint('engagement', __name__, template_folder='templates')
admin_comments_bp = Blueprint(
    'admin_comments', __name__, url_prefix='/admin/comments', template_folder='templates',
)

EMOJIS = ['👍', '❤️', '💡', '🤔', '👏']

# Auto-approve by default? Default is False = comments need admin moderation.
# Set env var COMMENTS_AUTO_APPROVE=1 to flip.


def _client_ip():
    # ProxyFix rewrote remote_addr from X-Forwarded-For upstream.
    return request.remote_addr or ''


def _stable_visitor_hash():
    """Stable across days (unlike analytics' daily-rotating hash) so a
    visitor can't add another 👍 by waiting until tomorrow. Raw IP is
    never persisted."""
    secret = current_app.config.get('SECRET_KEY', '')
    ua = request.headers.get('User-Agent', '')
    raw = f"{_client_ip()}|{ua}|{secret}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


# ─── PUBLIC: COMMENTS ───────────────────────────────────────

@engagement_bp.route('/articles/<slug>/comment', methods=['POST'])
def post_comment(slug):
    article = Article.query.filter_by(slug=slug, published=True).first() or abort(404)

    prenom = (request.form.get('prenom') or '').strip()
    nom = (request.form.get('nom') or '').strip() or None
    email = (request.form.get('email') or '').strip().lower() or None
    content = (request.form.get('content') or '').strip()

    if not prenom:
        flash("Le prénom est obligatoire pour commenter.", 'danger')
        return redirect(url_for('articles.public_show', slug=slug) + '#comments')
    if not content or len(content) < 3:
        flash("Le commentaire est vide ou trop court.", 'danger')
        return redirect(url_for('articles.public_show', slug=slug) + '#comments')
    if len(content) > 5000:
        flash("Commentaire trop long (5000 caractères maximum).", 'danger')
        return redirect(url_for('articles.public_show', slug=slug) + '#comments')

    auto_approve = current_app.config.get('COMMENTS_AUTO_APPROVE', False)

    c = Comment(
        article_id=article.id,
        prenom=prenom[:120],
        nom=nom[:120] if nom else None,
        email=email[:255] if email else None,
        content=content,
        approved=bool(auto_approve),
        approved_at=datetime.utcnow() if auto_approve else None,
    )
    db.session.add(c)
    db.session.commit()

    # If the visitor ticked the "register me to the newsletter" box AND
    # left an email, sign them up at the same time.
    if email and request.form.get('subscribe_newsletter') == 'on':
        _subscribe_from_comment(email=email, prenom=prenom, nom=nom)

    # Fire-and-forget e-mail to all admins. Failures are logged, not surfaced.
    try:
        _notify_admins_of_comment(article, c)
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("comment-alert send failed: %s", exc)

    if auto_approve:
        flash("Merci pour votre commentaire !", 'success')
    else:
        flash("Merci ! Votre commentaire est en attente de modération.", 'info')
    return redirect(url_for('articles.public_show', slug=slug) + '#comments')


def _subscribe_from_comment(email, prenom, nom):
    """Add a newsletter subscriber if not already present. Idempotent."""
    import secrets
    from newsletter.models import Subscriber

    email = email.strip().lower()
    existing = Subscriber.query.filter_by(email=email).first()
    if existing:
        existing.prenom = prenom or existing.prenom
        existing.nom = nom or existing.nom
        if existing.unsubscribed_at is not None:
            existing.unsubscribed_at = None
            existing.subscribed_at = datetime.utcnow()
        db.session.commit()
        return
    db.session.add(Subscriber(
        email=email, prenom=prenom or None, nom=nom or None,
        token=secrets.token_urlsafe(24),
    ))
    db.session.commit()


def _notify_admins_of_comment(article, comment):
    """Email every admin user that has an address. Includes a one-click
    tokenized approval link that doesn't require logging in."""
    if not mail_is_configured():
        log.info("comment-alert skipped — mail not configured")
        return

    recipients = User.query.filter(User.is_admin == True, User.email.isnot(None)).all()
    if not recipients:
        log.info("comment-alert skipped — no admin has an e-mail")
        return

    token = _make_approve_token(comment.id)
    approve_url = url_for('engagement.approve_via_link', token=token, _external=True)
    moderation_url = url_for('admin_comments.list_comments', _external=True)
    site_url = url_for('articles.public_list', _external=True)

    subject = f"Nouveau commentaire : {article.title[:80]}"
    for admin in recipients:
        html = render_template(
            'email/comment_alert.html',
            article=article,
            comment=comment,
            approve_url=approve_url,
            moderation_url=moderation_url,
            site_url=site_url,
            site_name=current_app.config['SITE_NAME'],
            site_tagline=current_app.config['SITE_TAGLINE'],
        )
        send_email(to_email=admin.email, to_name=admin.username, subject=subject, html=html)


# ─── PUBLIC: REACTIONS ──────────────────────────────────────

@engagement_bp.route('/api/articles/<int:article_id>/react', methods=['POST'])
def toggle_reaction(article_id):
    article = db.session.get(Article, article_id) or abort(404)
    if not article.published:
        abort(404)

    emoji = (request.json or {}).get('emoji') if request.is_json else request.form.get('emoji')
    if emoji not in EMOJIS:
        return jsonify({'error': 'unsupported emoji'}), 400

    visitor = _stable_visitor_hash()
    existing = Reaction.query.filter_by(
        article_id=article.id, emoji=emoji, visitor_hash=visitor,
    ).first()

    if existing:
        db.session.delete(existing)
        db.session.commit()
        action = 'removed'
    else:
        db.session.add(Reaction(
            article_id=article.id,
            emoji=emoji,
            visitor_hash=visitor,
        ))
        try:
            db.session.commit()
            action = 'added'
        except Exception:
            # Race on the unique constraint — treat as already added.
            db.session.rollback()
            action = 'added'

    counts = _reaction_counts(article.id)
    return jsonify({'action': action, 'counts': counts, 'user_reacted': _user_reactions(article.id, visitor)})


def _reaction_counts(article_id):
    rows = (
        db.session.query(Reaction.emoji, db.func.count(Reaction.id))
        .filter(Reaction.article_id == article_id)
        .group_by(Reaction.emoji)
        .all()
    )
    by_emoji = {emoji: 0 for emoji in EMOJIS}
    for emoji, count in rows:
        if emoji in by_emoji:
            by_emoji[emoji] = count
    return by_emoji


def _user_reactions(article_id, visitor_hash):
    rows = Reaction.query.filter_by(article_id=article_id, visitor_hash=visitor_hash).all()
    return [r.emoji for r in rows]


def reactions_context(article):
    """Helper for the article-show template: returns counts + which
    emojis the current visitor has already clicked."""
    visitor = _stable_visitor_hash()
    return {
        'emojis': EMOJIS,
        'counts': _reaction_counts(article.id),
        'user_reacted': _user_reactions(article.id, visitor),
    }


# ─── ONE-CLICK APPROVAL FROM E-MAIL ─────────────────────────

@engagement_bp.route('/comments/approve/<token>')
def approve_via_link(token):
    """Approve a pending comment using a signed token from the admin alert
    email. No login required — the token is HMAC-signed with SECRET_KEY
    and expires after 30 days."""
    comment_id, err = _read_approve_token(token)
    if err == 'expired':
        return render_template('approve_via_link.html', state='expired'), 410
    if err == 'invalid' or comment_id is None:
        return render_template('approve_via_link.html', state='invalid'), 400

    c = db.session.get(Comment, comment_id)
    if c is None:
        return render_template('approve_via_link.html', state='not_found'), 404

    if c.approved:
        return render_template('approve_via_link.html', state='already', comment=c)

    c.approved = True
    c.approved_at = datetime.utcnow()
    db.session.commit()
    return render_template('approve_via_link.html', state='approved', comment=c)


# ─── ADMIN: COMMENT MODERATION ──────────────────────────────

@admin_comments_bp.route('/')
@admin_required
def list_comments():
    pending = Comment.query.filter_by(approved=False).order_by(Comment.created_at.desc()).all()
    approved = Comment.query.filter_by(approved=True).order_by(Comment.created_at.desc()).limit(100).all()
    return render_template('comments_admin_list.html', pending=pending, approved=approved)


@admin_comments_bp.route('/<int:comment_id>/approve', methods=['POST'])
@admin_required
def approve_comment(comment_id):
    c = db.session.get(Comment, comment_id) or abort(404)
    c.approved = True
    c.approved_at = datetime.utcnow()
    db.session.commit()
    flash("Commentaire approuvé.", 'success')
    return redirect(url_for('admin_comments.list_comments'))


@admin_comments_bp.route('/<int:comment_id>/delete', methods=['POST'])
@admin_required
def delete_comment(comment_id):
    c = db.session.get(Comment, comment_id) or abort(404)
    db.session.delete(c)
    db.session.commit()
    flash("Commentaire supprimé.", 'success')
    return redirect(url_for('admin_comments.list_comments'))
