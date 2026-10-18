"""HyperFrame → bernardpoignant.fr: a clip edited in HyperFrame is published here.

Bernard's videos are now uploaded and edited in HyperFrame (agents/hyperframe, project bernardpoignant-videos):
same silence cuts as video/, word-by-word captions, title hook, end card — no banner to validate. The finished
clip is pushed here, becomes a post (TikTokPost, with no tiktok_id: TikTok stays manual, from HyperFrame) and is
published on LinkedIn, Instagram, X, YouTube and Facebook by the very functions of the TikTok flow
(tiktok.post_to_*_backend), so the two cannot drift.

API — key in the `X-API-Key` header, created on /admin/hyperframe (shown once; only its sha256 is kept, in settings):
  GET  /api/v1/ping                  the networks ready to publish
  POST /api/v1/videos                multipart: file (mp4), poster (jpg, optional), title, caption, x_text,
                                     transcript, banner_title, duration, dry_run
       dry_run=1  checks the key, the file and the networks — writes nothing, publishes nothing
       otherwise  creates the post, uploads the video and its poster, publishes in the background → 202 {id}
  GET  /api/v1/videos/<id>           the post and, per network, published / failed (detail) / pending

Independent of the video/ tooling (dev machine only): nothing here needs ffmpeg, so it runs in production.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
from datetime import datetime
from functools import wraps

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from auth import admin_required
from settings.models import get_config, set_config

log = logging.getLogger(__name__)

KEY_HASH = 'hyperframe_api_key_sha256'
KEY_HINT = 'hyperframe_api_key_hint'
STATUS = 'hyperframe_publish_{}'

api_hyperframe_bp = Blueprint('api_hyperframe', __name__, url_prefix='/api/v1')
admin_hyperframe_bp = Blueprint('admin_hyperframe', __name__, url_prefix='/admin/hyperframe', template_folder='templates')


# ── the key ───────────────────────────────────────────────────────────────────

def _check_key():
    stored = get_config(KEY_HASH)
    given = request.headers.get('X-API-Key') or ''
    return bool(stored and given and hmac.compare_digest(stored, hashlib.sha256(given.encode()).hexdigest()))


def require_key(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not _check_key():
            return jsonify({'error': 'Clé API absente ou invalide.', 'code': 'invalid_api_key'}), 401
        return view(*a, **kw)
    return wrapped


def networks():
    """Which networks can publish right now (the same checks as the publishers)."""
    import linkedin
    import meta
    import x as xapi
    import youtube
    return {'linkedin': bool(linkedin.is_configured()), 'x': bool(xapi.is_configured()),
            'youtube': bool(youtube.is_connected()), 'facebook': bool(meta.facebook_ready()),
            'instagram': bool(meta.instagram_ready())}


# ── API ───────────────────────────────────────────────────────────────────────

@api_hyperframe_bp.route('/ping')
@require_key
def ping():
    return jsonify({'ok': True, 'networks': networks()})


@api_hyperframe_bp.route('/videos', methods=['POST'])
@require_key
def push_video():
    f = request.files.get('file')
    title = (request.form.get('title') or '').strip()
    if f is None or not f.filename:
        return jsonify({'error': 'Aucun fichier (champ « file »).', 'code': 'no_file'}), 400
    if not title:
        return jsonify({'error': 'Titre manquant.', 'code': 'no_title'}), 400
    dry = request.form.get('dry_run') in ('1', 'true', 'yes')
    # the upload is read in full either way: a dry run proves the file gets here
    fd, tmp = tempfile.mkstemp(suffix='.mp4')
    with os.fdopen(fd, 'wb') as fh:
        f.save(fh)
    size = os.path.getsize(tmp)
    if dry:
        os.remove(tmp)
        return jsonify({'ok': True, 'dry_run': True, 'received_bytes': size, 'title': title,
                        'networks': networks()})

    import storage
    from init_db import db
    from tiktok.models import TikTokPost
    post = TikTokPost(title=title[:200], caption=(request.form.get('caption') or '').strip() or None,
                      transcript=(request.form.get('transcript') or '').strip() or None,
                      banner_title=(request.form.get('banner_title') or '').strip()[:120] or None)
    x_text = (request.form.get('x_text') or '').strip() or None
    try:
        post.duration_seconds = float(request.form.get('duration') or 0) or None
    except ValueError:
        post.duration_seconds = None
    db.session.add(post)
    db.session.flush()
    try:
        with open(tmp, 'rb') as fh:
            post.video_url = storage.upload_video(fh, f'hyperframe-{post.id}.mp4', 'video/mp4')
        poster = request.files.get('poster')
        if poster and poster.filename:
            post.poster_url = storage.upload_poster(poster.stream, f'hyperframe-{post.id}.jpg')
    except Exception as exc:  # storage errors and the like: nothing half-made stays
        db.session.rollback()
        return jsonify({'error': f'Envoi dans le stockage impossible : {exc}', 'code': 'storage'}), 502
    finally:
        os.remove(tmp)
    db.session.commit()
    _write_status(post.id, {'state': 'en cours', 'since': datetime.utcnow().isoformat(timespec='seconds'), 'networks': {}})
    app = current_app._get_current_object()
    threading.Thread(target=_publish, args=(app, post.id, x_text), daemon=True).start()
    return jsonify({'ok': True, 'id': post.id, 'state': 'en cours'}), 202


@api_hyperframe_bp.route('/videos/<int:post_id>')
@require_key
def video_status(post_id):
    from tiktok.models import TikTokPost
    from init_db import db
    post = db.session.get(TikTokPost, post_id)
    if post is None:
        return jsonify({'error': 'Inconnu.', 'code': 'not_found'}), 404
    st = json.loads(get_config(STATUS.format(post_id)) or '{}')
    return jsonify({'id': post.id, 'title': post.title, 'video_url': post.video_url, **st})


def _write_status(post_id, st):
    from init_db import db
    set_config(STATUS.format(post_id), json.dumps(st, ensure_ascii=False))
    db.session.commit()


def _publish(app, post_id, x_text):
    """One network after the other, with the TikTok flow's own functions; each result recorded as it comes."""
    with app.app_context():
        from init_db import db
        from tiktok import (post_to_facebook_backend, post_to_instagram_backend, post_to_linkedin_backend,
                            post_to_x_backend, post_to_youtube_backend)
        from tiktok.models import TikTokPost
        post = db.session.get(TikTokPost, post_id)
        st = json.loads(get_config(STATUS.format(post_id)) or '{}')
        ready = networks()
        steps = [('linkedin', lambda: post_to_linkedin_backend(post)),
                 ('instagram', lambda: post_to_instagram_backend(post)),
                 ('x', lambda: post_to_x_backend(post, x_text)),
                 ('youtube', lambda: post_to_youtube_backend(post)),
                 ('facebook', lambda: post_to_facebook_backend(post))]
        for name, run in steps:
            if not ready.get(name):
                st['networks'][name] = {'ok': False, 'detail': 'non connecté'}
                _write_status(post_id, st)
                continue
            try:
                ok, detail = run()
            except Exception as exc:  # one network failing must not stop the others
                log.exception('hyperframe: %s failed for post %s', name, post_id)
                ok, detail = False, str(exc)
            st['networks'][name] = {'ok': bool(ok), 'detail': str(detail)[:300]}
            _write_status(post_id, st)
        done = [n for n, r in st['networks'].items() if r['ok']]
        st['state'] = 'fait' if len(done) == len(steps) else ('partiel' if done else 'échec')
        st['done_at'] = datetime.utcnow().isoformat(timespec='seconds')
        _write_status(post_id, st)


# ── the key, for an administrator ─────────────────────────────────────────────

@admin_hyperframe_bp.route('/', methods=['GET', 'POST'])
@admin_required
def admin_key():
    new_key = None
    if request.method == 'POST':
        from init_db import db
        new_key = 'bp_' + secrets.token_urlsafe(32)
        set_config(KEY_HASH, hashlib.sha256(new_key.encode()).hexdigest())
        set_config(KEY_HINT, new_key[:8])
        db.session.commit()
        flash("Nouvelle clé créée : copiez-la maintenant, elle ne sera plus affichée. L'ancienne ne marche plus.", 'success')
    return render_template('hyperframe_admin.html', new_key=new_key, hint=get_config(KEY_HINT),
                           networks=networks())
