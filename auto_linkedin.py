"""Daily automatic share on LinkedIn: the oldest article not yet posted there.

Runs at 09:00 Europe/Paris and posts at most one article, and only when nothing
has already gone out that day — so a share done by hand suppresses the
automatic one, and the backlog drips out rather than arriving as a burst.

Separate from auto_tweet.py on purpose: the two platforms hold their own
"already posted" dates, so an article can be working through the LinkedIn
backlog while a different one goes to X, and a failure on one never blocks the
other.

Usage:
    python auto_linkedin.py               # against OVH (prod) by default
    python auto_linkedin.py --db local    # against the local SQLite DB
    python auto_linkedin.py --dry-run     # say what would be posted, post nothing
"""

import argparse
import sys

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    parser.add_argument('--dry-run', action='store_true',
                        help='report the decision without posting')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        import linkedin
        # url_for(_external=True) a besoin d'un contexte de requête ; le site
        # répond sur l'apex et sur www, donc pas de SERVER_NAME fixé.
        with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
            if args.dry_run:
                if not linkedin.is_configured():
                    print("LinkedIn n'est pas connecté — rien à faire.")
                    return
                if linkedin.already_posted_today():
                    print(f"Rien à faire — déjà partagé le "
                          f"{linkedin.last_post_at():%d/%m/%Y à %H:%M} UTC.")
                    return
                article = linkedin.next_article_to_post()
                if article is None:
                    print("Rien à faire — tous les articles publiés sont déjà sur LinkedIn.")
                    return
                print(f"Publierait : {article.title!r} (créé le {article.created_at:%d/%m/%Y}).")
                return

            resultat = linkedin.auto_post()
            statut = resultat['status']
            if statut == 'posted':
                print(f"Partagé : {resultat['article'].title!r}")
            elif statut == 'too_soon':
                heures = resultat['elapsed'].total_seconds() / 3600
                print(f"Rien à faire — dernier partage il y a {heures:.1f} h.")
            elif statut == 'nothing_to_post':
                print("Rien à faire — tous les articles publiés sont déjà sur LinkedIn.")
            elif statut == 'not_configured':
                print("LinkedIn n'est pas connecté — rien à faire.")
            else:
                print(f"Échec du partage : {resultat.get('detail')}", file=sys.stderr)
                sys.exit(1)


if __name__ == '__main__':
    main()
