"""Publication automatique quotidienne sur Threads : le plus ancien article
publié, jamais mis sur Threads, parmi ceux parus depuis l'activation.

Même esprit que auto_facebook.py : au plus un par jour, seuil d'identifiant pour
ne pas déverser l'arriéré, et rien tant que le seuil n'est pas posé.

Usage :
    python auto_threads.py
    python auto_threads.py --dry-run
"""

import argparse
import sys

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
            import threads
            if args.dry_run:
                if not threads.article_autopost_enabled():
                    print("Publication auto non activée — rien à faire.")
                    return
                if not threads.is_connected():
                    print("Threads n'est pas connecté — rien à faire.")
                    return
                if threads.already_posted_today():
                    print(f"Rien à faire — déjà publié le {threads.last_article_post_at():%d/%m/%Y}.")
                    return
                a = threads.next_article_to_post()
                print("Rien à faire." if a is None else f"Publierait : {a.title!r}.")
                return
            r = threads.auto_post_article()
            st = r['status']
            if st == 'posted':
                print(f"Publié sur Threads : {r['article'].title!r}")
            elif st in ('too_soon', 'nothing_to_post', 'disabled', 'not_connected'):
                print(f"Rien à faire ({st}).")
            else:
                print(f"Échec : {r.get('detail')}", file=sys.stderr)
                sys.exit(1)


if __name__ == '__main__':
    main()
