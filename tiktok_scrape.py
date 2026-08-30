"""Nightly TikTok refresh: pull the profile and update every post's figures.

The admin's "Récupérer les posts" button deliberately refreshes only the posts
published in the last week, so a click after publishing a clip does not rewrite
the counters of the whole back catalogue. This run is the other half of that
arrangement — it passes full=True and refreshes everything, once a day at
midnight Paris.

Wired to the `bpoignant-tiktok-scrape` k8s CronJob.

Usage:
    python tiktok_scrape.py               # against OVH (prod) by default
    python tiktok_scrape.py --db local    # against the local SQLite DB
    python tiktok_scrape.py --recent      # only the last week, as the button does
"""

import argparse
import sys

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'],
                        help='which database to use (default: ovh)')
    parser.add_argument('--recent', action='store_true',
                        help='refresh only posts from the last week')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        import apify
        from tiktok import sync_posts

        if not apify.is_configured():
            print("Apify n'est pas configuré — rien à faire.")
            return

        try:
            result = sync_posts(full=not args.recent)
        except apify.ApifyError as exc:
            # Non-zero exit so the CronJob is marked failed and shows up.
            print(f"Récupération impossible : {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"{result['seen']} post(s) vus — {result['created']} créé(s), "
              f"{result['updated']} mis à jour, {result['skipped']} ignoré(s).")


if __name__ == '__main__':
    main()
