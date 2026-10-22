"""Rattrapage horaire des champs IA d'un article (résumé, accroche X, thèmes).

Le pipeline IA tourne à l'enregistrement d'un article, mais « au mieux » : un
échec passager de l'API laisse les champs vides sans bloquer l'enregistrement.
Ce cron repasse chaque heure et complète ce qui manque, du plus récent au plus
ancien, borné par exécution pour ne pas s'emballer.

Usage :
    python articles_ai_backfill.py
    python articles_ai_backfill.py --limit 20
    python articles_ai_backfill.py --dry-run
"""

import argparse

from __init__ import create_app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    ap.add_argument('--limit', type=int, default=10)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    app = create_app(args.db)
    with app.app_context():
        import articles
        from articles.models import Article
        if args.dry_run:
            def manque(a):
                return ((a.content_html or '').strip()
                        and (not (a.summary or '').strip()
                             or not (a.social_summary or '').strip()
                             or not a.themes))
            en_attente = [a for a in Article.query.order_by(Article.created_at.desc()).all() if manque(a)]
            for a in en_attente[:args.limit]:
                print(f"[dry-run] #{a.id} {a.title[:55]}")
            print(f"[dry-run] {len(en_attente)} article(s) incomplet(s) au total.")
            return
        r = articles.backfill_missing_ai(limit=args.limit, log_fn=print)
        if not r['ok']:
            print(r.get('raison', 'IA indisponible'))
            return
        print(f"complété : {r['summary']} résumé(s), {r['social']} accroche(s), "
              f"{r['themes']} thème(s) · {r['failed']} échec(s) · {r['pending']} incomplet(s) au départ.")


if __name__ == '__main__':
    main()
