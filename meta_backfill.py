"""Rattrapage Facebook + Instagram : publier un clip pas encore posté, du plus
ancien au plus récent, sur les deux réseaux là où il manque.

Lancé plusieurs fois par jour par le CronJob (créneaux de journée), il traite
un clip par exécution, donc l'arriéré s'écoule sans tout déverser d'un coup. Il
sert aussi de filet : un clip que l'enchaînement après montage aurait manqué
est repris ici.

Usage :
    python meta_backfill.py                 # OVH (prod), 1 clip
    python meta_backfill.py --limit 2
    python meta_backfill.py --dry-run       # liste ce qui partirait, ne poste rien
"""

import argparse

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
            import meta
            from sqlalchemy import or_
            from tiktok.models import TikTokPost
            if not meta.is_connected():
                print("Facebook n'est pas connecté — rien à faire.")
                return
            if args.dry_run:
                clips = (TikTokPost.query
                         .filter(TikTokPost.video_url.isnot(None),
                                 or_(TikTokPost.fb_post_id.is_(None),
                                     TikTokPost.instagram_media_id.is_(None)))
                         .order_by(TikTokPost.posted_at.asc(), TikTokPost.id.asc())
                         .limit(args.limit).all())
                for p in clips:
                    manque = []
                    if not p.fb_post_id:
                        manque.append('Facebook')
                    if not p.instagram_media_id:
                        manque.append('Instagram')
                    print(f"[dry-run] {p.posted_at:%Y-%m-%d} · {p.title[:50]} → {', '.join(manque)}")
                reste = (TikTokPost.query
                         .filter(TikTokPost.video_url.isnot(None),
                                 or_(TikTokPost.fb_post_id.is_(None),
                                     TikTokPost.instagram_media_id.is_(None))).count())
                print(f"[dry-run] {reste} clip(s) au total à faire.")
                return
            import tiktok
            tiktok.backfill_meta(limit=args.limit)
            # Et relever les chiffres de ce qui est déjà en ligne : vues côté
            # Facebook, j'aime et commentaires côté Instagram. Les vues d'un Reel
            # restent à zéro tant que instagram_manage_insights n'est pas accordé.
            tiktok.refresh_meta_stats()


if __name__ == '__main__':
    main()
