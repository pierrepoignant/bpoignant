"""Image uploads to OVH Object Storage (S3-compatible).

Article illustrations can't live on the pod's filesystem — it's recreated on
every deploy — so they go to the `bpoignant-storage` bucket whose credentials
already ship in the app secret. Objects are written public-read so the URL can
be used directly in <img> and, more importantly, in og:image, which social
networks fetch anonymously.

Config comes from the usual `<SECTION>__<KEY>` env vars:
    OVH__ENDPOINT_URL   https://s3.eu-west-par.io.cloud.ovh.net/
    OVH__REGION         eu-west-par
    OVH__BUCKET         bpoignant-storage
    OVH__ACCESS_KEY / OVH__SECRET_KEY
"""

import mimetypes
import os
import re
import secrets
from datetime import datetime

ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif'}
MAX_BYTES = 8 * 1024 * 1024  # generous for a photo, small enough to refuse a video

VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.webm'}
VIDEO_MAX_BYTES = 300 * 1024 * 1024

_SAFE = re.compile(r'[^a-z0-9._-]+')


class StorageError(RuntimeError):
    """Raised when the upload can't be completed."""


def _config():
    return {
        'endpoint': os.environ.get('OVH__ENDPOINT_URL', '').strip(),
        'region': os.environ.get('OVH__REGION', '').strip(),
        'bucket': os.environ.get('OVH__BUCKET', '').strip(),
        'access_key': os.environ.get('OVH__ACCESS_KEY', '').strip(),
        'secret_key': os.environ.get('OVH__SECRET_KEY', '').strip(),
    }


def is_configured():
    c = _config()
    return all([c['endpoint'], c['bucket'], c['access_key'], c['secret_key']])


def _client():
    import boto3
    c = _config()
    return boto3.client(
        's3',
        endpoint_url=c['endpoint'],
        aws_access_key_id=c['access_key'],
        aws_secret_access_key=c['secret_key'],
        region_name=c['region'] or None,
    )


def _object_key(filename, prefix='articles', allowed=None, fallback='image'):
    """A collision-proof, URL-safe key that still hints at the original name."""
    allowed = allowed or ALLOWED_EXT
    base = os.path.basename(filename or fallback)
    stem, ext = os.path.splitext(base.lower())
    if ext not in allowed:
        raise StorageError(
            f"Format non accepté ({ext or 'inconnu'}). "
            f"Formats acceptés : {', '.join(sorted(allowed))}."
        )
    stem = _SAFE.sub('-', stem).strip('-')[:60] or fallback
    # The random suffix means re-uploading under the same name never silently
    # replaces (and never gets served stale from a CDN cache).
    return f"{prefix}/{datetime.utcnow():%Y/%m}/{stem}-{secrets.token_hex(4)}{ext}"


def upload_image(file_storage):
    """Store an uploaded image and return its public URL.

    `file_storage` is a Werkzeug FileStorage from request.files.
    """
    return _upload(file_storage, prefix='articles', allowed=ALLOWED_EXT,
                   max_bytes=MAX_BYTES, kind='image', fallback='image')


def upload_video(fileobj, filename, content_type='video/mp4'):
    """Store a video and return its public URL.

    Accepts a plain file object as well as a FileStorage, because the video
    tool hands over a finished render from disk rather than a browser upload.
    """
    return _upload(fileobj, prefix='tiktok', allowed=VIDEO_EXT,
                   max_bytes=VIDEO_MAX_BYTES, kind='video', fallback='video',
                   filename=filename, content_type=content_type)


def _upload(source, prefix, allowed, max_bytes, kind, fallback,
            filename=None, content_type=None):
    if not is_configured():
        raise StorageError("Le stockage n'est pas configuré (OVH__…).")
    name = filename or getattr(source, 'filename', None)
    if not source or not name:
        raise StorageError("Aucun fichier reçu.")

    key = _object_key(name, prefix=prefix, allowed=allowed, fallback=fallback)
    file_storage = source

    data = file_storage.read()
    if not data:
        raise StorageError("Le fichier est vide.")
    if len(data) > max_bytes:
        raise StorageError(
            f"Fichier trop lourd ({len(data) // (1024*1024)} Mo, "
            f"maximum {max_bytes // (1024*1024)} Mo).")

    content_type = (
        content_type
        or getattr(file_storage, 'mimetype', None)
        or mimetypes.guess_type(key)[0]
        or 'application/octet-stream'
    )
    if not content_type.startswith(f'{kind}/'):
        raise StorageError(f"Type de fichier inattendu ({content_type}).")

    c = _config()
    try:
        _client().put_object(
            Bucket=c['bucket'], Key=key, Body=data,
            ContentType=content_type,
            # Public: social networks fetch og:image without credentials.
            ACL='public-read',
            CacheControl='public, max-age=31536000, immutable',
        )
    except Exception as exc:  # boto3 raises a zoo of client errors
        raise StorageError(f"Envoi vers le stockage impossible : {exc}") from exc

    return public_url(key)


def public_url(key):
    """Virtual-hosted-style URL: https://<bucket>.<host>/<key>.

    OVH serves anonymous reads only in this form — the path-style
    https://<host>/<bucket>/<key> is rejected with "Not S3 request", which
    would break og:image for every crawler even though the object is public.
    """
    c = _config()
    host = re.sub(r'^https?://', '', c['endpoint']).strip('/')
    return f"https://{c['bucket']}.{host}/{key}"
