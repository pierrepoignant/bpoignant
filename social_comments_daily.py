"""Lecture quotidienne des commentaires YouTube, Facebook et Instagram, et
rédaction des réponses proposées — pour qu'elles soient prêtes chaque matin
sans avoir à cliquer « Lire les commentaires ».

Lit par l'API (gratuit, pas d'Apify), puis fait proposer une réponse par le
moteur, dans la voix de Bernard. N'écrit jamais de réponse tout seul : la
publication reste un geste de Bernard sur la page des commentaires.

Usage :
    python social_comments_daily.py
    python social_comments_daily.py --dry-run
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
            import social
            if args.dry_run:
                import youtube
                import meta
                print(f"YouTube connecté : {youtube.is_connected()} · "
                      f"Meta connecté : {meta.is_connected()} · "
                      f"Instagram : {meta.instagram_ready()}")
                print("Lirait les commentaires des trois réseaux puis proposerait les réponses.")
                return
            lus = social.fetch_all(print)
            proposes = social.draft_pending(print)
            print(f"{lus} commentaire(s) lu(s), {proposes} relu(s) par le moteur.")


if __name__ == '__main__':
    main()
