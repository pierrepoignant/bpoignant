"""Veille quotidienne : lit l'actualité politique française, la croise avec ce
que Bernard a déjà écrit, enregistre des suggestions de « Minute » et envoie le
digest aux administrateurs. Branché sur le CronJob k8s `bpoignant-veille` (19 h
Europe/Paris).

Usage :
    python news_daily.py                 # OVH (prod)
    python news_daily.py --no-email      # génère et stocke, sans e-mail
"""

import argparse

from __init__ import create_app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    ap.add_argument('--no-email', action='store_true')
    args = ap.parse_args()

    app = create_app(args.db)
    with app.app_context():
        with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
            import news
            if args.no_email:
                news.generer_suggestions(log_fn=print)
            else:
                news.cycle_quotidien(log_fn=print)


if __name__ == '__main__':
    main()
