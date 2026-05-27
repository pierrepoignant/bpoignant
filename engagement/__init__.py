import hashlib
from datetime import datetime

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)

from init_db import db
from articles.models import Article
from engagement.models import Comment, Reaction
from auth import admin_required

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

    if auto_approve:
        flash("Merci pour votre commentaire !", 'success')
    else:
        flash("Merci ! Votre commentaire est en attente de modération.", 'info')
    return redirect(url_for('articles.public_show', slug=slug) + '#comments')


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
