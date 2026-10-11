"""Rattrapage Threads : publier l'arriéré d'articles, deux par exécution, du
plus ancien au plus récent, jusqu'à épuisement.

Ignore le seuil d'activation (contrairement à auto_threads.py) : le but est
justement de rattraper les anciens articles. Un article déjà sur Threads est
sauté. Lancé deux fois par jour par le CronJob.

Usage :
    python threads_backfill.py
    python threads_backfill.py --limit 1
    python threads_backfill.py --dry-run
"""

import argparse

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    parser.add_argument('--limit', type=int, default=2)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
            import threads
            from articles.models import Article
            if not threads.is_connected():
                print("Threads n'est pas connecté — rien à faire.")
                return
            if args.dry_run:
                arts = (Article.query.filter(Article.published == True,  # noqa: E712
                                             Article.threads_posted_at.is_(None))
                        .order_by(Article.created_at.asc()).limit(args.limit).all())
                for a in arts:
                    print(f"[dry-run] {a.created_at:%Y-%m-%d} · {a.title[:55]}")
                reste = Article.query.filter(Article.published == True,  # noqa: E712
                                             Article.threads_posted_at.is_(None)).count()
                print(f"[dry-run] {reste} au total à rattraper.")
                return
            threads.backfill(limit=args.limit)


if __name__ == '__main__':
    main()
