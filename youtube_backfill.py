"""Daily YouTube backfill: upload a few clips not yet on YouTube, oldest first.

YouTube's default quota allows about six uploads a day (1 600 units each of
10 000), so the backlog goes up in small daily batches rather than all at once.
Oldest first, so the channel fills in the order the clips were made. A
« quota épuisé » answer stops the batch cleanly, to resume the next day.

This also serves as a safety net for new clips: if the after-montage chain
missed one, the daily run picks it up.

Usage:
    python youtube_backfill.py                 # OVH (prod), up to 6
    python youtube_backfill.py --limit 3
    python youtube_backfill.py --dry-run       # list what would go, upload nothing
"""

import argparse

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    parser.add_argument('--limit', type=int, default=6)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
            import youtube
            from tiktok.models import TikTokPost
            if not youtube.is_connected():
                print("YouTube n'est pas connecté — rien à faire.")
                return
            if args.dry_run:
                clips = (TikTokPost.query
                         .filter(TikTokPost.video_url.isnot(None),
                                 TikTokPost.youtube_video_id.is_(None))
                         .order_by(TikTokPost.posted_at.asc(), TikTokPost.id.asc())
                         .limit(args.limit).all())
                for p in clips:
                    print(f"[dry-run] {p.posted_at:%Y-%m-%d} · {p.title[:60]}")
                reste = (TikTokPost.query
                         .filter(TikTokPost.video_url.isnot(None),
                                 TikTokPost.youtube_video_id.is_(None)).count())
                print(f"[dry-run] {len(clips)} seraient publiées ; {reste} au total à faire.")
                return
            import tiktok
            tiktok.backfill_youtube(limit=args.limit)


if __name__ == '__main__':
    main()
