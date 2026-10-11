"""Relevé quotidien du profil Facebook personnel de Bernard (scraping Apify).

Le profil personnel n'a pas d'API : on lit ses posts publics et on reporte les
j'aime et commentaires sur les articles dont le post contient le lien. Fragile
(Facebook bloque, change sa page), donc un échec se solde par zéro mise à jour,
jamais par un plantage.

Usage :
    python facebook_profile_scrape.py             # OVH (prod)
    python facebook_profile_scrape.py --dry-run   # lit, ne touche pas la base
"""

import argparse

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        with app.test_request_context('/', base_url='https://bernardpoignant.fr'):
            import apify
            if not apify.is_configured() or not apify.facebook_profile_url():
                print("Apify ou le profil Facebook ne sont pas configurés — rien à faire.")
                return
            if args.dry_run:
                import articles
                items = apify.scrape_facebook_profile()
                vus = [articles._fb_profile_slug(i) for i in items]
                trouves = [s for s in vus if s]
                print(f"[dry-run] {len(items)} post(s) lus, {len(trouves)} citant un article : {trouves}")
                return
            import articles
            articles.refresh_facebook_profile()


if __name__ == '__main__':
    main()
