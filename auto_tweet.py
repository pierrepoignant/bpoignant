"""Daily automatic share: post the oldest article that has never been on X.

Runs at 09:30 Europe/Paris and posts at most one article, and only when nothing
has already gone out that day — so a share done by hand suppresses the
automatic one, and the backlog drips out rather than arriving as a burst. When
every published article has been shared, the run is a no-op.

The test is a Paris calendar day rather than a rolling window. A window is
measured from the last post, not from the schedule, so any post later in the
day than the 09:30 slot pushes the next run under the threshold and silently
costs a day.

Wired to the `bpoignant-x-autopost` k8s CronJob.

Usage:
    python auto_tweet.py               # against OVH (prod) by default
    python auto_tweet.py --db local    # against the local SQLite DB
    python auto_tweet.py --dry-run     # say what would be posted, post nothing
"""

import argparse
import sys

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'],
                        help='which database to use (default: ovh)')
    parser.add_argument('--dry-run', action='store_true',
                        help='report the decision without posting')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        from tweets import (
            auto_post, last_post_at, next_article_to_post, already_posted_today,
        )

        if args.dry_run:
            last = last_post_at()
            if already_posted_today():
                print(f"Rien à faire — dernier partage le {last:%d/%m/%Y à %H:%M} UTC.")
                return
            article = next_article_to_post()
            if article is None:
                print("Rien à faire — tous les articles publiés ont déjà été partagés.")
                return
            print(f"Publierait : {article.title!r} (créé le {article.created_at:%d/%m/%Y}).")
            return

        result = auto_post()
        status = result['status']

        if status == 'posted':
            article = result['article']
            print(f"Partagé : {article.title!r} — {article.x_post_url}")
        elif status == 'too_soon':
            hours = result['elapsed'].total_seconds() / 3600
            print(f"Rien à faire — dernier partage il y a {hours:.1f} h.")
        elif status == 'nothing_to_post':
            print("Rien à faire — tous les articles publiés ont déjà été partagés.")
        elif status == 'not_configured':
            print("X n'est pas configuré (clés BPOIGNANT_X__… manquantes) — rien à faire.")
        else:
            # Non-zero exit so the CronJob is marked failed and shows up.
            print(f"Échec du partage : {result.get('detail')}", file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
