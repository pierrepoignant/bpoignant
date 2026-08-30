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
    return render_template('video_admin.html', jobs=video.all_jobs(),
                           whisper_model=video.WHISPER_MODEL)


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

    job_id = video.start_job(src, f.filename,
                             vertical=bool(request.form.get('vertical')),
                             title=(request.form.get('title') or '').strip() or None)
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
