"""Redéfinir la miniature des vidéos déjà publiées sur YouTube : la première
image de chaque vidéo. À lancer depuis la machine de développement (ffmpeg y
est disponible), contre la base de production.

Usage :
    dotenv run -- .venv/bin/python youtube_thumbnails.py --dry-run
    dotenv run -- .venv/bin/python youtube_thumbnails.py            # toutes
    dotenv run -- .venv/bin/python youtube_thumbnails.py --limit 5
"""

import argparse
import os
import tempfile

import requests

from __init__ import create_app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    ap.add_argument('--limit', type=int, default=0, help="0 = toutes")
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    app = create_app(args.db)
    with app.app_context():
        import youtube
        from tiktok.models import TikTokPost
        from init_db import db

        if not youtube.is_connected():
            print("YouTube n'est pas connecté.")
            return

        q = (TikTokPost.query
             .filter(TikTokPost.youtube_video_id.isnot(None),
                     TikTokPost.video_url.isnot(None))
             .order_by(TikTokPost.id.asc()))
        if args.limit:
            q = q.limit(args.limit)
        posts = q.all()
        print(f"{len(posts)} vidéo(s) à traiter.")
        if args.dry_run:
            for p in posts:
                print(f"  - {p.youtube_video_id}  {p.title[:50]}")
            return

        token = None
        ok_n = ko_n = 0
        for p in posts:
            tmp = None
            try:
                with requests.get(p.video_url, stream=True, timeout=180) as r:
                    if r.status_code != 200:
                        print(f"  ✗ {p.youtube_video_id} : vidéo illisible ({r.status_code})")
                        ko_n += 1
                        continue
                    fd, tmp = tempfile.mkstemp(suffix='.mp4')
                    with os.fdopen(fd, 'wb') as fh:
                        for chunk in r.iter_content(1024 * 256):
                            fh.write(chunk)
                img = youtube._premiere_image(tmp)
                if not img:
                    print(f"  ✗ {p.youtube_video_id} : extraction image impossible")
                    ko_n += 1
                    continue
                token = token or youtube._access_token()
                ok, detail = youtube.set_thumbnail(p.youtube_video_id, img, token=token)
                if ok:
                    print(f"  ✓ {p.youtube_video_id}  {p.title[:45]}")
                    ok_n += 1
                else:
                    print(f"  ✗ {p.youtube_video_id} : {detail}")
                    ko_n += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {p.youtube_video_id} : {exc}")
                ko_n += 1
            finally:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        print(f"Terminé : {ok_n} mises à jour, {ko_n} échecs.")


if __name__ == '__main__':
    main()
