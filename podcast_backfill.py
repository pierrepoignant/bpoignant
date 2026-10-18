"""Génération quotidienne de l'audio des nouveaux articles pour le podcast.

Ne traite que les articles publiés récents sans audio (fenêtre de 21 jours,
quelques-uns par jour) : les nouveaux rejoignent le podcast tout seuls, sans
vider ni facturer d'un coup l'arriéré, qui reste au bouton « Tout générer ».

Usage :
    python podcast_backfill.py
    python podcast_backfill.py --limit 5 --jours 30
    python podcast_backfill.py --dry-run
"""

import argparse

from __init__ import create_app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    ap.add_argument('--limit', type=int, default=3)
    ap.add_argument('--jours', type=int, default=21)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    app = create_app(args.db)
    with app.app_context():
        with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
            import podcast
            if not podcast.is_configured():
                print("ElevenLabs n'est pas configuré — rien à faire.")
                return
            if args.dry_run:
                from datetime import datetime, timedelta
                from articles.models import Article
                depuis = datetime.utcnow() - timedelta(days=args.jours)
                arts = (Article.query.filter(Article.published.is_(True),
                        Article.audio_url.is_(None), Article.created_at >= depuis)
                        .order_by(Article.created_at.desc()).limit(args.limit).all())
                for a in arts:
                    print(f"[dry-run] {a.created_at:%Y-%m-%d} · {a.title[:55]}")
                return
            podcast.backfill_recent(jours=args.jours, limit=args.limit)


if __name__ == '__main__':
    main()
