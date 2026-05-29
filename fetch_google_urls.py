"""Standalone fetcher: pull the top N Google results for a keyword via
SearchApi and store them in the `google_urls` table.

Usage:
    python fetch_google_urls.py                       # "bernard poignant", 500 results
    python fetch_google_urls.py "autre mot-clé" 300
    python fetch_google_urls.py --db local "bernard poignant" 50

Requires SEARCHAPI__API_KEY in the environment (.env / bpoignant.json).
Runs against the same DB as the web app (OVH by default) — pass `--db local`
to use the local SQLite file instead.
"""

import argparse

from __init__ import create_app
from seo.service import run_and_store, DEFAULT_KEYWORD, DEFAULT_MAX_RESULTS


def main():
    parser = argparse.ArgumentParser(description="Fetch Google results into google_urls.")
    parser.add_argument('keyword', nargs='?', default=DEFAULT_KEYWORD)
    parser.add_argument('max_results', nargs='?', type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument('--db', default='ovh', help='Database section (ovh / local)')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        print(f"Fetching up to {args.max_results} results for '{args.keyword}' …")
        run = run_and_store(keyword=args.keyword, max_results=args.max_results)
        if run.status == 'done':
            print(f"✓ Done — {run.fetched} results stored across {run.pages_fetched} "
                  f"pages (run #{run.id}).")
        else:
            print(f"✗ Failed (run #{run.id}): {run.error}")


if __name__ == '__main__':
    main()
