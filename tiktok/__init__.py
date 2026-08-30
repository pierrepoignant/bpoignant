"""Admin section for TikTok clips.

Available everywhere, unlike the video editing tool: the clips and their text
are ordinary content Bernard manages from production, even though the editing
that produces them only runs on the development machine.
"""

from datetime import datetime

import os

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)

from init_db import db
from auth import admin_required
from tiktok.models import TikTokPost

admin_tiktok_bp = Blueprint('admin_tiktok', __name__, url_prefix='/admin/tiktok',
                            template_folder='templates')


@admin_tiktok_bp.route('/')
@admin_required
def list_posts():
    # Plain .desc(): MySQL already sorts NULLs last on a descending order, and
    # .nullslast() emits PostgreSQL-only syntax that MySQL rejects outright.
    posts = TikTokPost.query.order_by(
        TikTokPost.posted_at.desc(), TikTokPost.created_at.desc()).all()
    import storage, video, apify

    # Renders available to attach — only meaningful on the dev machine.
    local_videos = []
    if video.is_enabled() and os.path.isdir(video.WORKDIR):
        local_videos = sorted(
            (f for f in os.listdir(video.WORKDIR)
             if f.endswith('.mp4') and not f.startswith('src-')),
            key=lambda f: os.path.getmtime(os.path.join(video.WORKDIR, f)),
            reverse=True)

    return render_template('tiktok_admin_list.html', posts=posts,
                           storage_ok=storage.is_configured(),
                           apify_ok=apify.is_configured(),
                           video_enabled=video.is_enabled(),
                           local_videos=local_videos)


@admin_tiktok_bp.route('/sync', methods=['POST'])
@admin_required
def sync():
    """Pull the account's posts from TikTok via Apify.

    TikTok is the source of truth: Bernard posts from the phone, and this
    reflects what is actually online rather than what we intended to publish.
    Re-running updates the figures on existing rows instead of duplicating.
    """
    import apify

    if not apify.is_configured():
        flash("Apify n'est pas configuré — renseignez le jeton dans Réglages.", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    try:
        items = apify.scrape_profile()
    except apify.ApifyError as exc:
        flash(f"Récupération impossible : {exc}", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    created = updated = 0
    now = datetime.utcnow()
    for raw in items:
        item = apify.normalise(raw)
        if not item.get('id') and not item.get('url'):
            continue
        post = None
        if item.get('id'):
            post = TikTokPost.query.filter_by(tiktok_id=str(item['id'])).first()
        if post is None and item.get('url'):
            post = TikTokPost.query.filter_by(posted_url=item['url']).first()

        if post is None:
            post = TikTokPost(title=(item.get('text') or 'Clip TikTok')[:200])
            db.session.add(post)
            created += 1
        else:
            updated += 1

        post.tiktok_id = str(item['id']) if item.get('id') else post.tiktok_id
        post.posted_url = item.get('url') or post.posted_url
        # The caption on TikTok is what viewers actually saw; keep it in sync.
        if item.get('text'):
            post.caption = item['text']
            if not post.title or post.title == 'Clip TikTok':
                post.title = item['text'][:200]
        post.views = item.get('views')
        post.likes = item.get('likes')
        post.comments_count = item.get('comments')
        post.shares = item.get('shares')
        post.scraped_at = now
        if not post.posted_at:
            post.posted_at = _parse_time(item.get('created_at')) or now

    db.session.commit()
    flash(f"{created} nouveau(x) post(s), {updated} mis à jour.", 'success')
    return redirect(url_for('admin_tiktok.list_posts'))


def _parse_time(value):
    """Actors return either an ISO string or a unix timestamp."""
    if not value:
        return None
    try:
        return datetime.utcfromtimestamp(int(value))
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return None


@admin_tiktok_bp.route('/<int:post_id>/attach', methods=['POST'])
@admin_required
def attach_video(post_id):
    """Attach one of the renders sitting on the dev machine to a scraped post.

    Only possible where the files are — the production server never sees them,
    which is why this route exists solely when the video tool is enabled.
    """
    import video
    if not video.is_enabled():
        abort(404)
    import storage

    post = db.session.get(TikTokPost, post_id) or abort(404)
    name = (request.form.get('filename') or '').strip()
    path = os.path.join(video.WORKDIR, os.path.basename(name))
    if not name or not os.path.isfile(path):
        flash("Fichier introuvable sur le serveur.", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    try:
        with open(path, 'rb') as fh:
            post.video_url = storage.upload_video(fh, os.path.basename(path), 'video/mp4')
    except storage.StorageError as exc:
        flash(f"Envoi impossible : {exc}", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    post.duration_seconds = video.probe_duration(path)
    db.session.commit()
    flash("Vidéo attachée au post.", 'success')
    return redirect(url_for('admin_tiktok.list_posts'))


@admin_tiktok_bp.route('/<int:post_id>/update', methods=['POST'])
@admin_required
def update(post_id):
    """Edit the text, or record the URL once the clip has been posted."""
    post = db.session.get(TikTokPost, post_id) or abort(404)

    if 'title' in request.form:
        title = (request.form.get('title') or '').strip()
        if title:
            post.title = title[:200]
    if 'caption' in request.form:
        post.caption = (request.form.get('caption') or '').strip() or None

    if 'posted_url' in request.form:
        url = (request.form.get('posted_url') or '').strip()
        post.posted_url = url[:500] or None
        # Stamp the first time a URL is recorded; clearing it clears the date
        # too, so the two never contradict each other.
        if url and not post.posted_at:
            post.posted_at = datetime.utcnow()
        elif not url:
            post.posted_at = None

    db.session.commit()
    flash("Post mis à jour.", 'success')
    return redirect(url_for('admin_tiktok.list_posts'))


@admin_tiktok_bp.route('/<int:post_id>/delete', methods=['POST'])
@admin_required
def delete(post_id):
    """Remove the row. The object stays in the bucket on purpose — deleting a
    row by accident should not destroy the only copy of a clip."""
    post = db.session.get(TikTokPost, post_id) or abort(404)
    db.session.delete(post)
    db.session.commit()
    flash("Post supprimé (la vidéo reste dans le stockage).", 'success')
    return redirect(url_for('admin_tiktok.list_posts'))
