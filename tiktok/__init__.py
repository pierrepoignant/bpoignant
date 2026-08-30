"""Admin section for TikTok clips.

Available everywhere, unlike the video editing tool: the clips and their text
are ordinary content Bernard manages from production, even though the editing
that produces them only runs on the development machine.
"""

from datetime import datetime

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
    posts = TikTokPost.query.order_by(TikTokPost.created_at.desc()).all()
    import storage
    return render_template('tiktok_admin_list.html', posts=posts,
                           storage_ok=storage.is_configured())


@admin_tiktok_bp.route('/upload', methods=['POST'])
@admin_required
def upload():
    """Store a clip in the bucket and open a post for it."""
    import storage

    f = request.files.get('video')
    if not f or not f.filename:
        flash("Choisissez un fichier vidéo.", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    try:
        url = storage.upload_video(f, f.filename, f.mimetype or 'video/mp4')
    except storage.StorageError as exc:
        flash(f"Envoi impossible : {exc}", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    title = (request.form.get('title') or '').strip() or f.filename
    post = TikTokPost(title=title[:200], video_url=url,
                      caption=(request.form.get('caption') or '').strip() or None)
    db.session.add(post)
    db.session.commit()
    flash("Vidéo enregistrée. Publiez-la sur TikTok puis collez l'URL ici.", 'success')
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


def create_from_video_job(job, title=None, article_id=None):
    """Build a post from a finished job of the video tool: upload the render to
    the bucket, and carry over the caption and transcript it produced."""
    import storage

    output = job.get('output')
    if not output:
        raise storage.StorageError("Aucune vidéo montée à enregistrer.")
    with open(output, 'rb') as fh:
        url = storage.upload_video(fh, f"{job.get('name') or 'clip'}.mp4", 'video/mp4')

    post = TikTokPost(
        title=(title or job.get('name') or 'Clip')[:200],
        video_url=url,
        caption=job.get('caption'),
        transcript=job.get('transcript'),
        duration_seconds=job.get('kept'),
        article_id=article_id,
    )
    db.session.add(post)
    db.session.commit()
    return post
