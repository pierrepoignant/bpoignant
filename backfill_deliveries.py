"""One-off backfill of newsletter delivery records.

Marks articles as already delivered to subscribers who were registered
before a given date, so the dedupe logic won't re-send them. Idempotent:
re-running it never creates duplicate rows (it skips pairs that already
exist, and the unique constraint backs that up).

Each rule is (article slug, "registered on or before" date). The delivery's
recorded date is set to that cutoff day (an approximation of when the article
was actually sent).

Usage:
    python backfill_deliveries.py            # against OVH (prod) by default
    python backfill_deliveries.py --db local # against the local SQLite DB
    python backfill_deliveries.py --dry-run  # report only, write nothing
"""

import argparse
from datetime import date, datetime, time, timedelta

from __init__ import create_app
from init_db import db
from articles.models import Article
from newsletter.models import Subscriber, Delivery


# (article slug, inclusive "registered on or before" date)
RULES = [
    ('presidence-le-calendrier-des-vainqueurs', date(2026, 6, 3)),
    ('entretien-journal-le-point',              date(2026, 6, 2)),
    ('melenchon-dans-le-texte',                 date(2026, 5, 28)),
]


def backfill(dry_run=False):
    total_created = 0
    for slug, cutoff in RULES:
        article = Article.query.filter_by(slug=slug).first()
        if article is None:
            print(f"  ! Article introuvable pour le slug « {slug} » — ignoré.")
            continue

        # subscribed_at on or before the cutoff day → strictly before the next day.
        exclusive = datetime.combine(cutoff + timedelta(days=1), time.min)
        subscribers = Subscriber.query.filter(Subscriber.subscribed_at < exclusive).all()

        already = {
            d.subscriber_id
            for d in Delivery.query.filter_by(article_id=article.id).all()
        }

        created = 0
        recorded_at = datetime.combine(cutoff, time(12, 0))
        for sub in subscribers:
            if sub.id in already:
                continue
            if not dry_run:
                db.session.add(Delivery(
                    article_id=article.id,
                    subscriber_id=sub.id,
                    email=sub.email,
                    sent_at=recorded_at,
                ))
            created += 1

        if not dry_run:
            db.session.commit()

        total_created += created
        print(f"  • {slug}: {len(subscribers)} inscrit(s) avant le {cutoff.isoformat()} "
              f"(inclus) — {created} livraison(s) {'à créer' if dry_run else 'créée(s)'}, "
              f"{len(subscribers) - created} déjà présente(s).")

    print(f"{'[dry-run] ' if dry_run else ''}Total : {total_created} livraison(s) "
          f"{'à créer' if dry_run else 'créée(s)'}.")


def main():
    parser = argparse.ArgumentParser(description="Backfill newsletter deliveries.")
    parser.add_argument('--db', default='ovh', help='Database section (ovh / local)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report what would be created without writing.')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        backfill(dry_run=args.dry_run)


if __name__ == '__main__':
    main()
