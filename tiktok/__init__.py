"""Admin section for TikTok clips.

Available everywhere, unlike the video editing tool: the clips and their text
are ordinary content Bernard manages from production, even though the editing
that produces them only runs on the development machine.
"""

from datetime import datetime, timedelta

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

    # Renders available to attach — only meaningful on the dev machine. Each
    # carries its date, duration and title so the picker can show something
    # more identifiable than a hex filename.
    local_videos = video.local_renders()

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
        result = sync_posts(full=False)
    except apify.ApifyError as exc:
        flash(f"Récupération impossible : {exc}", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    message = f"{result['created']} nouveau(x) post(s), {result['updated']} mis à jour."
    if result['skipped']:
        message += (f" {result['skipped']} post(s) de plus de {RECENT_DAYS} jours"
                    " laissés de côté — ils seront actualisés cette nuit.")
    flash(message, 'success')
    return redirect(url_for('admin_tiktok.list_posts'))


# Au-delà de ce délai, les chiffres d'un post ne bougent plus guère : c'est la
# nuit qu'on les rafraîchit, pas à chaque clic sur « Récupérer les posts ».
RECENT_DAYS = 7


def sync_posts(full=False):
    """Pull the profile from Apify and upsert the posts.

    A manual refresh is nearly always about a clip just published, so it
    creates every new post it finds but only re-reads the figures of those
    published in the last RECENT_DAYS. The rest keep the numbers they already
    have until the nightly run, which passes full=True and refreshes
    everything — an old post's counters barely move, and re-writing them on
    every click buys nothing.

    Returns a dict of created / updated / skipped counts.
    """
    import apify

    items = apify.scrape_profile()
    created = updated = skipped = 0
    now = datetime.utcnow()
    cutoff = now - timedelta(days=RECENT_DAYS)

    for raw in items:
        item = apify.normalise(raw)
        if not item.get('id') and not item.get('url'):
            continue
        post = None
        if item.get('id'):
            post = TikTokPost.query.filter_by(tiktok_id=str(item['id'])).first()
        if post is None and item.get('url'):
            post = TikTokPost.query.filter_by(posted_url=item['url']).first()

        is_new = post is None
        if is_new:
            post = TikTokPost(title=(item.get('text') or 'Clip TikTok')[:200])
            db.session.add(post)
            created += 1
        else:
            # Trust the date already on the row; fall back to the scraped one
            # for posts recorded before posted_at was populated.
            published = post.posted_at or _parse_time(item.get('created_at'))
            if not full and published is not None and published < cutoff:
                skipped += 1
                continue
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
    return {'created': created, 'updated': updated, 'skipped': skipped,
            'seen': len(items)}


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


def _tweet_text(post, limit=280):
    """The caption, made to fit X without being hacked off mid-sentence.

    TikTok allows 2200 characters and Bernard uses them — the first clip's
    caption was 465. Slicing at 280 cut it mid-word, so an over-long caption is
    rewritten in his voice instead, falling back to a word-boundary trim with
    an ellipsis when the AI is unavailable.
    """
    import x as xapi
    from articles.ai_summary import condense_for_tweet

    text = (post.caption or post.title or '').strip()
    if not text or len(text) <= limit:
        return text
    try:
        shorter = condense_for_tweet(text, limit=limit - 5)
    except Exception:
        shorter = None
    return shorter or xapi._truncate(text, limit)


@admin_tiktok_bp.route('/<int:post_id>/post-x', methods=['POST'])
@admin_required
def post_to_x(post_id):
    """Re-post the attached clip to X, video and all.

    Works from anywhere, not just the dev machine: the video is fetched from
    the bucket rather than the local disk, so production can do this too. X
    needs the bytes — there is no way to hand it a URL.
    """
    import tempfile
    import requests as http
    import x as xapi

    post = db.session.get(TikTokPost, post_id) or abort(404)
    if not post.has_video:
        flash("Rattachez d'abord une vidéo à ce post.", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))
    if post.x_post_id:
        flash("Ce clip a déjà été publié sur X.", 'info')
        return redirect(url_for('admin_tiktok.list_posts'))
    if not xapi.is_configured():
        flash("X n'est pas configuré (clés API manquantes).", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    text = _tweet_text(post)
    if not text:
        flash("Ce post n'a pas de texte à publier.", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    tmp = None
    try:
        with http.get(post.video_url, stream=True, timeout=120) as resp:
            if resp.status_code != 200:
                flash(f"Vidéo illisible dans le stockage ({resp.status_code}).", 'danger')
                return redirect(url_for('admin_tiktok.list_posts'))
            fd, tmp = tempfile.mkstemp(suffix='.mp4')
            with os.fdopen(fd, 'wb') as fh:
                for chunk in resp.iter_content(1024 * 256):
                    fh.write(chunk)

        media_id, err = xapi.upload_video(tmp)
        if err:
            flash(f"Envoi de la vidéo à X impossible : {err}", 'danger')
            return redirect(url_for('admin_tiktok.list_posts'))

        ok, detail = xapi.post_tweet(text, media_ids=[media_id])
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

    if not ok:
        flash(f"Échec de la publication sur X : {detail}", 'danger')
        return redirect(url_for('admin_tiktok.list_posts'))

    post.x_post_id = str(detail) if detail else None
    post.x_posted_at = datetime.utcnow()
    db.session.commit()
    flash("Clip publié sur X.", 'success')
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
