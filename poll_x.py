"""Daily X sync: engagement metrics + new replies, then e-mail the digest.

Costs ~2 API calls per run whatever the number of articles: one batched
`GET /2/tweets` for every tweet's metrics, one `GET /2/users/:id/mentions`
(with `since_id`) for everything new across every conversation. See
`tweets/__init__.py` for why the mentions timeline beats per-tweet search.

This is the same routine the admin "Rafraîchir" button runs. Wired to the
`bpoignant-x-poll` k8s CronJob.

Usage:
    python poll_x.py                 # against OVH (prod) by default
    python poll_x.py --db local      # against the local SQLite DB
    python poll_x.py --no-notify     # sync only, don't send the digest
"""

import argparse
import sys

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'],
                        help='which database to use (default: ovh)')
    parser.add_argument('--no-notify', action='store_true',
                        help="sync but don't e-mail the digest")
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        import x
        from tweets import poll

        if not x.is_configured():
            print("X n'est pas configuré (clés BPOIGNANT_X__… manquantes) — rien à faire.")
            return

        try:
            result = poll(notify=not args.no_notify)
        except x.XError as exc:
            # Non-zero exit so the CronJob is marked failed and retried.
            print(f"Échec de la synchronisation X : {exc}", file=sys.stderr)
            sys.exit(1)

        print(
            f"{result['metrics']} tweet(s) mis à jour, "
            f"{result['replies']} nouvelle(s) réponse(s), "
            f"{result['emails']} e-mail(s) envoyé(s)."
        )


if __name__ == '__main__':
    main()
