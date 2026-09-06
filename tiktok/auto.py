"""Enchaînement automatique après un montage.

Bernard monte une vidéo, la publie depuis l'application TikTok, et plus rien ne
devrait lui être demandé : dix minutes plus tard le serveur récupère le post,
lui rattache le fichier monté, puis le publie sur X et sur LinkedIn.

Le délai existe parce que le scrapeur ne voit un post qu'une fois indexé par
TikTok, ce qui n'est pas immédiat. Et parce que rien ne garantit qu'il aura été
publié dans les dix minutes, l'attente est réessayée à intervalle régulier
plutôt qu'abandonnée à la première tentative : un montage terminé le matin peut
être publié à midi.

Tout est porté par le job de montage, déjà écrit sur disque, donc un
redémarrage du serveur ne perd pas l'attente en cours.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from settings.models import get_config, set_config

log = logging.getLogger(__name__)

KEY_ENABLED = 'video_auto_publish'

DELAI_PREMIER_ESSAI = 10 * 60      # dix minutes avant de regarder
INTERVALLE = 5 * 60                # puis toutes les cinq minutes
DUREE_MAX = 3 * 60 * 60            # on renonce après trois heures
# Un post scrapé n'est candidat que s'il est récent : sans cette borne, un
# vieux post sans vidéo se verrait attribuer le montage du jour.
FENETRE_POST = timedelta(hours=6)

_boucle = None
_verrou = threading.Lock()


def is_enabled():
    valeur = get_config(KEY_ENABLED)
    return str(valeur).strip() in ('1', 'true', 'True', 'on')


def set_enabled(actif):
    set_config(KEY_ENABLED, '1' if actif else '0')


def armer(job_id):
    """Mark a finished montage as awaiting its TikTok post."""
    import video
    video._set(job_id, auto={
        'etat': 'attente',
        'depuis': datetime.utcnow().isoformat(timespec='seconds'),
        'essais': 0,
        'detail': "En attente de la publication sur TikTok…",
    })


def _fin(job_id, etat, detail):
    import video
    job = video.get_job(job_id) or {}
    auto = dict(job.get('auto') or {})
    auto.update(etat=etat, detail=detail,
                fini=datetime.utcnow().isoformat(timespec='seconds'))
    video._set(job_id, auto=auto)
    log.info('auto-publication %s : %s — %s', job_id, etat, detail)


def _candidat(post_scrape_depuis):
    """The most recent scraped post that has no video attached yet."""
    from tiktok.models import TikTokPost
    return (
        TikTokPost.query
        .filter(TikTokPost.video_url.is_(None),
                TikTokPost.posted_at.isnot(None),
                TikTokPost.posted_at >= post_scrape_depuis)
        .order_by(TikTokPost.posted_at.desc())
        .first()
    )


def _traiter(app, job_id):
    """One attempt: sync, find the post, attach, publish. Returns True when
    the job is finished with (successfully or not), False to try again."""
    import video
    from init_db import db
    from tiktok import attach_render, sync_posts, post_to_x_backend, post_to_linkedin_backend

    job = video.get_job(job_id) or {}
    auto = dict(job.get('auto') or {})
    sortie = job.get('output')
    if not sortie:
        _fin(job_id, 'abandon', "Le montage n'a pas de fichier.")
        return True

    depuis = datetime.fromisoformat(auto.get('depuis'))
    if datetime.utcnow() - depuis > timedelta(seconds=DUREE_MAX):
        _fin(job_id, 'abandon',
             "Aucun post TikTok trouvé en trois heures — rattachez la vidéo à la main.")
        return True

    auto['essais'] = auto.get('essais', 0) + 1
    auto['detail'] = "Recherche du post sur TikTok…"
    video._set(job_id, auto=auto)

    try:
        sync_posts(full=False)
    except Exception as exc:
        log.warning('auto-publication %s : récupération impossible (%s)', job_id, exc)
        return False

    post = _candidat(datetime.utcnow() - FENETRE_POST)
    if post is None:
        auto['detail'] = (f"Pas encore de post sans vidéo "
                          f"(essai {auto['essais']}). Nouvelle tentative dans 5 minutes.")
        video._set(job_id, auto=auto)
        return False

    erreur = attach_render(post, sortie)
    if erreur:
        _fin(job_id, 'abandon', f"Rattachement impossible : {erreur}")
        return True

    resultats = []
    ok_x, detail_x = post_to_x_backend(post)
    resultats.append('X' if ok_x else f'X échoué ({detail_x})')
    ok_li, detail_li = post_to_linkedin_backend(post)
    resultats.append('LinkedIn' if ok_li else f'LinkedIn échoué ({detail_li})')
    db.session.commit()

    _fin(job_id, 'fait' if (ok_x and ok_li) else 'partiel',
         f"Rattaché à « {post.title[:60]} » · " + ', '.join(resultats))
    return True


def _tourner(app):
    """Background loop: looks at armed jobs, acts when their time has come."""
    import video
    while True:
        try:
            with app.app_context():
                if is_enabled():
                    for job in video.all_jobs():
                        auto = job.get('auto') or {}
                        if auto.get('etat') != 'attente':
                            continue
                        # Dix minutes après le montage, puis toutes les cinq.
                        depuis = datetime.fromisoformat(auto['depuis'])
                        prochain = depuis + timedelta(
                            seconds=DELAI_PREMIER_ESSAI + INTERVALLE * auto.get('essais', 0))
                        if datetime.utcnow() < prochain:
                            continue
                        _traiter(app, job['id'])
        except Exception:
            log.exception('boucle de publication automatique')
        time.sleep(60)


def demarrer(app):
    """Start the loop once, and only where the montage tool lives."""
    global _boucle
    import video
    if not video.is_enabled():
        return
    with _verrou:
        if _boucle is not None and _boucle.is_alive():
            return
        _boucle = threading.Thread(target=_tourner, args=(app,),
                                   name='tiktok-auto', daemon=True)
        _boucle.start()
        log.info('publication automatique : surveillance démarrée')
