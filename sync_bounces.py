"""Sync SendGrid suppressions into the subscriber list.

Pulls SendGrid's account-wide suppression lists (bounces, blocks, spam
reports, invalid emails) and marks any matching subscribers as bounced, so
they're never e-mailed again. Only our own addresses are touched — safe to run
against a SendGrid account shared by several apps.

This is the "pull" alternative to the /newsletter/sendgrid/events webhook, and
is the same routine the admin "Synchroniser SendGrid" button runs. Wire it to a
cron / k8s CronJob for periodic syncing.

Usage:
    python sync_bounces.py             # against OVH (prod) by default
    python sync_bounces.py --db local  # against the local SQLite DB
"""

import argparse

from __init__ import create_app
from mail import is_configured
from newsletter import sync_bounces_from_sendgrid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'],
                        help='which database to use (default: ovh)')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        if not is_configured():
            print("SendGrid n'est pas configuré (SENDGRID__API_KEY manquant) — rien à faire.")
            return
        n = sync_bounces_from_sendgrid()
        print(f"{n} adresse(s) marquée(s) en erreur d'après SendGrid.")


if __name__ == '__main__':
    main()
