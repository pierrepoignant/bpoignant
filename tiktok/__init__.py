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

from sqlalchemy.exc import IntegrityError

from init_db import db
from auth import admin_required
from tiktok.models import TikTokPost

import logging

log = logging.getLogger(__name__)

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

    from articles.models import Theme
    return render_template('tiktok_admin_list.html', posts=posts,
                           all_themes=Theme.query.order_by(Theme.name).all(),
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
    try:
        return _upsert(items, full=full)
    except IntegrityError:
        # Two syncs can overlap — the actor run takes minutes, and a second
        # click (or the nightly job crossing a manual refresh) starts a fresh
        # one. Both then see the same clip as new and both insert it. Retry
        # once against the rows the other run committed; the items are already
        # in hand, so this costs no second Apify run.
        log.warning('tiktok sync: insertion concurrente, nouvelle tentative')
        db.session.rollback()
        return _upsert(items, full=full)


def _upsert(items, full=False):
    """Write one batch of scraped items to the database.

    Existing rows are read up front rather than queried inside the loop. A
    query per item autoflushes the half-built row added by the previous
    iteration, which is how a feed listing the same clip twice used to end in
    a duplicate-key error rather than an update.
    """
    import apify

    by_tiktok_id, by_url = {}, {}
    for post in TikTokPost.query.all():
        if post.tiktok_id:
            by_tiktok_id[post.tiktok_id] = post
        if post.posted_url:
            by_url[post.posted_url] = post

    created = updated = skipped = 0
    now = datetime.utcnow()
    cutoff = now - timedelta(days=RECENT_DAYS)

    for raw in items:
        item = apify.normalise(raw)
        tiktok_id = str(item['id']) if item.get('id') else None
        url = item.get('url')
        if not tiktok_id and not url:
            continue

        post = by_tiktok_id.get(tiktok_id) if tiktok_id else None
        if post is None and url:
            post = by_url.get(url)

        if post is None:
            post = TikTokPost(title=_title_from_caption(item.get('text')) or 'Clip TikTok',
                              tiktok_id=tiktok_id)
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

        # Index the row straight away, under both keys: a feed that lists the
        # same clip twice then updates it the second time instead of adding a
        # second row with the same id.
        if tiktok_id:
            post.tiktok_id = tiktok_id
            by_tiktok_id[tiktok_id] = post
        if url:
            post.posted_url = url
            by_url[url] = post

        # The caption on TikTok is what viewers actually saw; keep it in sync.
        if item.get('text'):
            post.caption = item['text']
            if not post.title or post.title == 'Clip TikTok':
                post.title = _title_from_caption(item['text'])
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


def _title_from_caption(text, limit=200):
    """The caption's first line, which is how Bernard writes them: a headline,
    a blank line, then the argument.

    Slicing the first 200 characters instead ran the headline into the body —
    "Retraites : deux visions, un compromis possible ?Lors de leur débat…" —
    which is what a video rich result would have shown as its title.
    """
    for line in (text or '').splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return (text or '')[:limit]


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


def _auto_themes(post):
    """Classify a clip against the article themes, from its transcript.

    Uses the same closed vocabulary and the same prompt as the articles, so a
    clip and an article on the same subject land on the same theme page. An
    untagged clip is fine; a wrongly tagged one is not, so any failure returns
    nothing rather than a guess.
    """
    from articles import _theme_by_name
    from articles.ai_summary import generate_themes

    text = '\n\n'.join(p for p in (post.caption, post.transcript) if p)
    if not text.strip():
        return []
    try:
        names = generate_themes(post.title or 'Vidéo', text) or []
        return [_theme_by_name(n) for n in names]
    except Exception:
        log.exception('AI themes failed for tiktok post %s', post.id)
        return []


@admin_tiktok_bp.route('/<int:post_id>/themes', methods=['POST'])
@admin_required
def update_themes(post_id):
    """Set a clip's themes by hand, or ask the AI for them."""
    from articles.models import Theme

    post = db.session.get(TikTokPost, post_id) or abort(404)
    if request.form.get('auto'):
        themes = _auto_themes(post)
        if not themes:
            flash("Le classement automatique n'a rien renvoyé.", 'danger')
        post.themes = themes or post.themes
    else:
        ids = request.form.getlist('themes', type=int)
        post.themes = Theme.query.filter(Theme.id.in_(ids)).all() if ids else []
    db.session.commit()
    flash("Thèmes enregistrés.", 'success')
    return redirect(url_for('admin_tiktok.list_posts'))


@admin_tiktok_bp.route('/stats')
@admin_required
def stats():
    """TikTok's own figures. Shares the aggregation and the presentation with
    the X page — same buckets, same caveats, different platform."""
    from tweets import platform_stats, _stats_args, RANGES, GROUPS

    days, group = _stats_args()
    data = platform_stats(days, group)
    return render_template('tiktok_admin_stats.html',
                           points=data['tiktok'], totals=data['tiktok_totals'],
                           days=data['days'], group=data['group'],
                           excluded=data['excluded'],
                           ranges=RANGES, groups=GROUPS)


@admin_tiktok_bp.route('/<int:post_id>/boosted', methods=['POST'])
@admin_required
def toggle_boosted(post_id):
    """Mark a post as promoted, or unmark it. Promoted posts are left out of
    the statistics — their reach was bought, and averaging it with the organic
    posts describes neither."""
    post = db.session.get(TikTokPost, post_id)
    if post is None:
        abort(404)
    post.boosted = not post.boosted
    db.session.commit()
    flash("Post marqué comme sponsorisé — il sort des statistiques."
          if post.boosted else
          "Post repassé en organique — il revient dans les statistiques.", 'success')
    return redirect(url_for('admin_tiktok.list_posts'))


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

    # La transcription vit dans le job de montage, sur cette machine
    # uniquement : si elle n'est pas recopiée ici au moment du rattachement,
    # elle n'existe nulle part côté production.
    job = video.job_for_render(path) or {}
    if job.get('transcript') and not post.transcript:
        post.transcript = job['transcript']

    # Image d'attente : sans elle la vidéo est un rectangle noir sur la page
    # publique, et c'est aussi la vignette attendue pour un résultat vidéo.
    try:
        poster = video.thumbnail(os.path.basename(path))
        if poster:
            with open(poster, 'rb') as fh:
                post.poster_url = storage.upload_poster(
                    fh, f'{os.path.splitext(os.path.basename(path))[0]}.jpg')
    except Exception:
        log.exception('poster upload failed for post %s', post.id)

    if post.transcript and not post.themes:
        post.themes = _auto_themes(post)

    db.session.commit()
    flash("Vidéo attachée au post."
          + (" Transcription enregistrée." if job.get('transcript') else ""), 'success')
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
