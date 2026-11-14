"""Rattrapage : relecture orthographe/typographie des articles publiés pas
encore corrigés (la même routine que le livre). Réversible par article.

Usage :
    dotenv run -- .venv/bin/python articles_correction.py           # tous
    dotenv run -- .venv/bin/python articles_correction.py --limit 5
    dotenv run -- .venv/bin/python articles_correction.py --no-ia   # typographie seule
"""

import argparse

from __init__ import create_app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--no-ia', action='store_true')
    args = ap.parse_args()

    app = create_app(args.db)
    with app.app_context():
        from articles.correction import corriger_tout
        n = corriger_tout(limit=(args.limit or None), ia=not args.no_ia, log_fn=print)
        print(f"Terminé : {n} article(s) corrigé(s).")


if __name__ == '__main__':
    main()
