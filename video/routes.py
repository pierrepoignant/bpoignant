"""Admin page for the video tool. Registered only when VIDEO_TOOLS is set."""

import os
import re
from datetime import datetime

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request,
    send_file, url_for,
)
from werkzeug.utils import secure_filename

import video
from auth import admin_required

admin_video_bp = Blueprint('admin_video', __name__, url_prefix='/admin/video',
                           template_folder='templates')

ALLOWED = {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi'}
MAX_BYTES = 500 * 1024 * 1024


@admin_video_bp.before_request
def _guard():
    # Belt and braces: the blueprint is only registered when enabled, but a
    # stale process or a mis-set variable should still refuse rather than run
    # a gigabyte of dependencies in production.
    if not video.is_enabled():
        abort(404)


@admin_video_bp.route('/')
@admin_required
def index():
    from tiktok.auto import is_enabled
    return render_template('video_admin.html', jobs=video.all_jobs(),
                           whisper_model=video.WHISPER_MODEL,
                           auto_publish=is_enabled())


@admin_video_bp.route('/auto', methods=['POST'])
@admin_required
def toggle_auto():
    """Turn the after-montage chain on or off.

    Off by default: it publishes to X and LinkedIn without asking, which is the
    point, and which is exactly why it should be switched on deliberately.
    """
    from tiktok.auto import set_enabled, is_enabled
    set_enabled(not is_enabled())
    flash("Enchaînement automatique activé — après un montage, le serveur "
          "récupérera le post TikTok et publiera sur X et LinkedIn."
          if is_enabled() else
          "Enchaînement automatique désactivé.", 'success')
    return redirect(url_for('admin_video.index'))


def _notify_email():
    """Where to write when a montage finishes: the person who started it.

    Falls back to the other admins — an unattended import is exactly the case
    where nobody is watching the page, and a montage nobody is told about is a
    montage nobody publishes.
    """
    from flask_login import current_user
    adresse = (getattr(current_user, 'email', '') or '').strip()
    if adresse:
        return adresse
    from auth.models import User
    autre = User.query.filter(User.is_admin.is_(True),
                              User.email.isnot(None)).first()
    return (autre.email if autre else None)


@admin_video_bp.route('/upload', methods=['POST'])
@admin_required
def upload():
    f = request.files.get('video')
    if not f or not f.filename:
        flash("Choisissez un fichier vidéo.", 'danger')
        return redirect(url_for('admin_video.index'))

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED:
        flash(f"Format non accepté ({ext or 'inconnu'}). Acceptés : {', '.join(sorted(ALLOWED))}.", 'danger')
        return redirect(url_for('admin_video.index'))

    os.makedirs(video.WORKDIR, exist_ok=True)
    safe = secure_filename(f.filename) or f'clip{ext}'
    stamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    src = os.path.join(video.WORKDIR, f'src-{stamp}-{safe}')
    f.save(src)

    size = os.path.getsize(src)
    if size > MAX_BYTES:
        os.remove(src)
        flash(f"Fichier trop lourd ({size // (1024*1024)} Mo, maximum {MAX_BYTES // (1024*1024)} Mo).", 'danger')
        return redirect(url_for('admin_video.index'))

    # Adresse et racine résolues ici : le fil qui finira le montage n'aura ni
    # requête ni contexte applicatif pour les retrouver.
    job_id = video.start_job(src, f.filename,
                             vertical=bool(request.form.get('vertical')),
                             title=(request.form.get('title') or '').strip() or None,
                             notify_email=_notify_email(),
                             base_url=request.url_root)
    return redirect(url_for('admin_video.job_page', job_id=job_id))


@admin_video_bp.route('/job/<job_id>')
@admin_required
def job_page(job_id):
    job = video.get_job(job_id)
    if not job:
        abort(404)
    return render_template('video_job.html', job=job)


@admin_video_bp.route('/job/<job_id>/status')
@admin_required
def job_status(job_id):
    """Polled by the job page while processing runs."""
    job = video.get_job(job_id)
    if not job:
        abort(404)
    return jsonify({k: v for k, v in job.items() if k not in ('src', 'output')})


@admin_video_bp.route('/job/<job_id>/download')
@admin_required
def download(job_id):
    job = video.get_job(job_id)
    out = job.get('output')
    if not out or not os.path.exists(out):
        abort(404)
    name = re.sub(r'[^\w.-]+', '-', job.get('name') or 'clip')
    return send_file(out, as_attachment=True,
                     download_name=f"tiktok-{os.path.splitext(name)[0]}.mp4")


def _render_path(filename):
    """Resolve a render by name, refusing anything that escapes WORKDIR."""
    safe = os.path.basename(filename)
    path = os.path.join(video.WORKDIR, safe)
    if not safe.endswith('.mp4') or safe.startswith('src-') or not os.path.exists(path):
        abort(404)
    return path


@admin_video_bp.route('/render/<path:filename>/thumb')
@admin_required
def render_thumb(filename):
    """Still frame for the attach picker."""
    path = video.thumbnail(os.path.basename(filename))
    if not path:
        abort(404)
    return send_file(path, mimetype='image/jpeg', max_age=86400)


@admin_video_bp.route('/render/<path:filename>/preview')
@admin_required
def render_preview(filename):
    """The render itself, played inline in the picker."""
    return send_file(_render_path(filename), mimetype='video/mp4',
                     conditional=True)


@admin_video_bp.route('/job/<job_id>/apercu')
@admin_required
def banner_preview(job_id):
    """A still of the clip with the band as it will actually be burnt in.

    Asked for on every keystroke (debounced): the result is cached on the
    text and the nudge, so typing a title costs one render per pause, not one
    per letter.
    """
    job = video.get_job(job_id)
    if not job:
        abort(404)
    try:
        path = video.banner_preview(job, request.args.get('title') or '',
                                    offset=request.args.get('d', 0.0, type=float) or 0.0)
    except video.VideoError:
        abort(500)
    if not path:
        abort(404)
    return send_file(path, mimetype='image/jpeg', max_age=0)


@admin_video_bp.route('/job/<job_id>/banner', methods=['POST'])
@admin_required
def confirm_banner(job_id):
    """Confirm (or clear) the proposed band, then finish the render."""
    job = video.get_job(job_id)
    if not job:
        abort(404)
    if job.get('status') != 'awaiting_banner':
        flash("Ce montage n'attend pas de bandeau.", 'danger')
        return redirect(url_for('admin_video.job_page', job_id=job_id))

    banner = (request.form.get('title') or '').strip()
    if request.form.get('sans_bandeau'):
        banner = ''
    # Le décalage manuel : la détection place le bandeau, l'œil tranche.
    decalage = request.form.get('decalage', 0.0, type=float) or 0.0
    if not video.apply_banner(job_id, banner, offset=max(-0.15, min(0.15, decalage))):
        flash("Impossible de terminer le montage — voir le détail ci-dessous.", 'danger')
    return redirect(url_for('admin_video.job_page', job_id=job_id))
