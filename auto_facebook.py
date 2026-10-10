"""Publication automatique quotidienne sur la Page Facebook : le plus ancien
article publié, jamais mis sur la Page, parmi ceux parus depuis l'activation.

Même esprit que auto_tweet.py et auto_linkedin.py : au plus un article par jour,
et seulement si rien n'est déjà parti aujourd'hui, pour que l'arriéré s'écoule
sans arriver en bloc et qu'une publication faite à la main supprime l'auto.

Ne concerne que les articles créés après l'activation (seuil d'identifiant
enregistré dans la configuration) : les dizaines d'anciens articles ne sont pas
déversés rétroactivement. Tant que le seuil n'est pas posé, le script ne fait
rien.

Usage :
    python auto_facebook.py               # OVH (prod) par défaut
    python auto_facebook.py --db local
    python auto_facebook.py --dry-run     # dit ce qui partirait, ne poste rien
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
            import meta
            if args.dry_run:
                if not meta.article_autopost_enabled():
                    print("Publication auto non activée — rien à faire.")
                    return
                if not meta.is_connected():
                    print("Facebook n'est pas connecté — rien à faire.")
                    return
                if meta.fb_already_posted_article_today():
                    print(f"Rien à faire — déjà publié le "
                          f"{meta.fb_last_article_post_at():%d/%m/%Y}.")
                    return
                article = meta.fb_next_article_to_post()
                if article is None:
                    print("Rien à faire — aucun nouvel article à publier sur la Page.")
                    return
                print(f"Publierait : {article.title!r} (créé le {article.created_at:%d/%m/%Y}).")
                return

            r = meta.fb_auto_post_article()
            statut = r['status']
            if statut == 'posted':
                print(f"Publié sur la Page : {r['article'].title!r}")
            elif statut == 'too_soon':
                print(f"Rien à faire — déjà publié le {r['last']:%d/%m/%Y}.")
            elif statut == 'nothing_to_post':
                print("Rien à faire — aucun nouvel article à publier sur la Page.")
            elif statut == 'disabled':
                print("Publication auto non activée — rien à faire.")
            elif statut == 'not_connected':
                print("Facebook n'est pas connecté — rien à faire.")
            else:
                print(f"Échec : {r.get('detail')}", file=sys.stderr)
                sys.exit(1)


if __name__ == '__main__':
    main()
