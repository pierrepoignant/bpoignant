"""Cloner la voix de Bernard dans ElevenLabs à partir de clips vidéo existants
(clonage instantané). À lancer une fois sur la machine de développement, où se
trouvent ffmpeg et l'accès aux clips. La clé API ElevenLabs doit déjà être
renseignée dans les Réglages → Podcast.

Télécharge l'audio de quelques clips, l'extrait en MP3 (ffmpeg), l'envoie à
ElevenLabs qui crée la voix, et enregistre le voice_id dans la configuration.

Usage :
    python podcast_clone.py                 # 5 clips, nom « Bernard Poignant »
    python podcast_clone.py --clips 8
"""

import argparse
import os
import subprocess
import tempfile

import requests

from __init__ import create_app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default='ovh', choices=['ovh', 'local'])
    ap.add_argument('--clips', type=int, default=5)
    ap.add_argument('--nom', default='Bernard Poignant')
    args = ap.parse_args()

    app = create_app(args.db)
    with app.app_context():
        import podcast
        from tiktok.models import TikTokPost
        from init_db import db
        cle = podcast.api_key()
        if not cle:
            print("Clé ElevenLabs absente — renseignez-la dans Réglages → Podcast.")
            return
        # Les plus longs d'abord : plus de voix propre pour le clonage.
        clips = (TikTokPost.query.filter(TikTokPost.video_url.isnot(None))
                 .order_by(db.func.coalesce(TikTokPost.duration_seconds, 0).desc(),
                           TikTokPost.id.desc())
                 .limit(args.clips).all())
        if not clips:
            print("Aucun clip avec vidéo.")
            return
        with tempfile.TemporaryDirectory() as d:
            fichiers = []
            for i, c in enumerate(clips):
                try:
                    r = requests.get(c.video_url, timeout=120)
                    mp4 = os.path.join(d, f'clip{i}.mp4')
                    open(mp4, 'wb').write(r.content)
                    mp3 = os.path.join(d, f'voix{i}.mp3')
                    subprocess.run(['ffmpeg', '-y', '-i', mp4, '-vn', '-ac', '1',
                                    '-ar', '44100', '-b:a', '128k', mp3],
                                   check=True, capture_output=True)
                    fichiers.append(mp3)
                    print(f"extrait : {c.title[:45]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"clip ignoré ({c.title[:30]}): {exc}")
            if not fichiers:
                print("Aucun audio extrait.")
                return
            print(f"envoi de {len(fichiers)} échantillon(s) à ElevenLabs…")
            resp = requests.post(
                'https://api.elevenlabs.io/v1/voices/add',
                headers={'xi-api-key': cle},
                data={'name': args.nom,
                      'description': "Voix de Bernard Poignant, extraite de ses chroniques vidéo."},
                files=[('files', (os.path.basename(f), open(f, 'rb'), 'audio/mpeg')) for f in fichiers],
                timeout=300)
        if resp.status_code != 200:
            print(f"ElevenLabs a refusé ({resp.status_code}) : {resp.text[:300]}")
            return
        vid = (resp.json() or {}).get('voice_id')
        if not vid:
            print(f"Pas de voice_id renvoyé : {resp.text[:200]}")
            return
        from settings.models import set_config
        set_config('elevenlabs_voice_id', vid)
        print(f"Voix créée et enregistrée : voice_id = {vid}")


if __name__ == '__main__':
    main()
