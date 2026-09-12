"""Video tooling for short-form clips (TikTok / Reels / Shorts).

**Development machine only.** Enabled by the `VIDEO_TOOLS` env var, which the
production deployment does not set — the blueprint is not even registered
there. Its dependencies (ffmpeg binaries, faster-whisper, ctranslate2) weigh
close to a gigabyte and live in `requirements-video.txt`, deliberately kept out
of `requirements.txt` so the deployed image stays small.

The pipeline, in order:

  1. `detect_silences`  — ffmpeg's silencedetect filter finds the dead air.
  2. `keep_segments`    — inverts that into the parts worth keeping, padded so
                          words are not clipped, and drops the leading silence
                          so the clip opens on speech rather than on a breath.
  3. `render`           — one ffmpeg pass trims and concatenates, optionally
                          reframing to 9:16.
  4. `transcribe`       — faster-whisper, French.
  5. `write_caption`    — Claude turns the transcript into a post, in Bernard's
                          voice adapted to the format.
"""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import logging
import tempfile
import threading
import time
import uuid
from datetime import datetime

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────

# Silence quieter than this, lasting at least MIN_SILENCE seconds, is cut.
# -30 dB keeps room tone and breathing; going lower starts cutting soft speech.
# -25 dB rather than -30: on two of Bernard's own recordings, -30 found almost
# nothing — 1s of 44.7 and 1.9s of 56.8 — because the room tone sits above that
# floor. At -25 the same clips lose 7s and the delivery still sounds natural.
NOISE_FLOOR_DB = -25
MIN_SILENCE = 0.30
# Left on either side of kept audio so consonants are not clipped.
PAD = 0.08
# Gaps shorter than this are not worth a cut — stitching them makes speech
# sound unnaturally clipped, and each cut adds a node to the filter graph.
MIN_GAP_TO_CUT = 0.25

WHISPER_MODEL = os.environ.get('VIDEO_WHISPER_MODEL', 'small')

# Loudness target. TikTok, Instagram and YouTube all normalise playback to
# roughly -14 LUFS; delivering at that level means the platform leaves the
# audio alone instead of pulling it down and flattening the dynamics.
TARGET_LUFS = -14.0
TARGET_PEAK_DB = -1.5
TARGET_LRA = 11.0

# Average luma (0–255) a well-exposed talking head sits around. Below
# DARK_THRESHOLD the picture is lifted; above it, left alone — "if needed"
# is the point, and gratuitously regrading good footage makes it worse.
# Bandeau de titre. Le bleu vient du dégradé du site (#1A1A2E → #16213E →
# #0F3460) : c'est la teinte médiane, celle qui lit le mieux sous du blanc.
TITLE_BG = '0x16213E'
TITLE_FG = 'white'
TITLE_FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
# Placement : le bandeau est dans le quart bas, mais son bord inférieur reste
# à 19 % de la hauteur du bas de l'image. TikTok superpose légende, pseudo,
# bandeau musical et colonne de boutons sur les ~300 px du bas d'un cadre de
# 1920 : un bandeau collé au bord y disparaîtrait.
TITLE_BAND_TOP = 0.72        # bord supérieur, en fraction de la hauteur
TITLE_BAND_HEIGHT = 0.09
# Deux lignes tiennent dans un bandeau moins haut que 1,75 fois celui d'une
# ligne : le texte y occupait la moitié de la hauteur, le reste était du bleu.
# Cette marge coûtait cher dès lors que le bandeau doit descendre sous le
# menton sans finir sous la légende de TikTok.
TITLE_BAND_TWO_LINES = 1.45
TITLE_SIDE_PADDING = 48      # marge gauche/droite, en pixels sur 1080
# Écart minimal entre le menton et le haut du bandeau, en fraction de hauteur —
# une trentaine de pixels sur 1920. En dessous, le bandeau ne coupe pas le
# visage mais le touche, ce qui se voit autant.
TITLE_BAND_GAP = 0.025
# Le bandeau ne descend jamais plus bas que cela : TikTok écrit la légende, le
# pseudo et le bandeau musical sur le bas du cadre, et un titre poussé dedans
# devient illisible. Mieux vaut effleurer le menton que finir sous la légende.
TITLE_BAND_MAX_BOTTOM = 0.88

TARGET_LUMA = 120.0
DARK_THRESHOLD = 100.0
MAX_GAMMA = 1.6


def is_enabled():
    """True only when explicitly switched on. Production never sets this."""
    return os.environ.get('VIDEO_TOOLS', '').strip().lower() in ('1', 'true', 'yes', 'on')


class VideoError(RuntimeError):
    """Anything that stops a clip being produced."""


# ── ffmpeg plumbing ──────────────────────────────────────────

def _bin(name):
    """Locate ffmpeg/ffprobe, preferring the pip-installed static build so the
    machine needs no system packages (and no root to install them)."""
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
    except Exception:
        pass
    found = shutil.which(name)
    if not found:
        raise VideoError(f"{name} introuvable. `pip install -r requirements-video.txt`.")
    return found


def _run(args, timeout=1800):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def probe_width(path):
    """Frame width in pixels, or 1080 when it can't be read."""
    code, out, _ = _run([
        _bin('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width', '-of',
        'default=noprint_wrappers=1:nokey=1', path,
    ])
    try:
        return int(out.strip().splitlines()[0]) if code == 0 else 1080
    except (ValueError, IndexError):
        return 1080


def probe_duration(path):
    code, out, err = _run([
        _bin('ffprobe'), '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', path,
    ])
    if code != 0:
        raise VideoError(f"Fichier illisible : {err.strip()[:200]}")
    try:
        return float(out.strip())
    except ValueError:
        raise VideoError("Durée introuvable — le fichier est-il bien une vidéo ?")


_SIL_START = re.compile(r'silence_start:\s*(-?[\d.]+)')
_SIL_END = re.compile(r'silence_end:\s*(-?[\d.]+)')


def detect_silences(path, noise_db=NOISE_FLOOR_DB, min_silence=MIN_SILENCE):
    """Return [(start, end)] of silent stretches, in seconds."""
    code, _, err = _run([
        _bin('ffmpeg'), '-hide_banner', '-nostats', '-i', path,
        '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}',
        '-f', 'null', '-',
    ])
    if code != 0:
        raise VideoError(f"Analyse du son impossible : {err.strip()[-200:]}")

    starts = [float(m) for m in _SIL_START.findall(err)]
    ends = [float(m) for m in _SIL_END.findall(err)]
    # A silence running to the end of the file has no silence_end line.
    if len(starts) == len(ends) + 1:
        ends.append(probe_duration(path))
    return list(zip(starts, ends))


def keep_segments(duration, silences, pad=PAD, min_gap=MIN_GAP_TO_CUT):
    """Invert silences into the spans worth keeping.

    Leading silence is dropped outright: a clip that opens on a breath loses
    the viewer in the first second, which is the whole game on TikTok. Trailing
    silence goes too. Interior gaps shorter than `min_gap` are left alone —
    removing them makes speech sound chopped and costs a filter node each.
    """
    keeps = []
    cursor = 0.0
    for start, end in silences:
        if start - cursor > 0.01:
            keeps.append([cursor, start])
        cursor = max(cursor, end)
    if duration - cursor > 0.01:
        keeps.append([cursor, duration])

    padded = []
    for start, end in keeps:
        s = max(0.0, start - pad)
        e = min(duration, end + pad)
        if e - s <= 0.05:
            continue
        # Merge with the previous span when padding closed the gap, or when the
        # gap was never long enough to be worth cutting.
        if padded and s - padded[-1][1] < min_gap:
            padded[-1][1] = e
        else:
            padded.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in padded]


def render(src, segments, dest, vertical=False):
    """Trim to `segments` and concatenate, in one ffmpeg pass.

    Re-encodes rather than stream-copying: cuts fall wherever speech stops, not
    on keyframes, and a stream copy would either drift or freeze at each join.
    """
    if not segments:
        raise VideoError("Rien à garder — la vidéo est-elle silencieuse ?")

    parts, labels = [], []
    for i, (start, end) in enumerate(segments):
        parts.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    graph = ';'.join(parts)
    graph += f";{''.join(labels)}concat=n={len(segments)}:v=1:a=1[cv][ca]"

    if vertical:
        # Fill a 1080×1920 frame: scale to cover, then centre-crop. Padding
        # instead would letterbox, and TikTok crops those bars off anyway.
        graph += (";[cv]scale=1080:1920:force_original_aspect_ratio=increase,"
                  "crop=1080:1920[outv]")
        vlabel = '[outv]'
    else:
        vlabel = '[cv]'

    code, _, err = _run([
        _bin('ffmpeg'), '-hide_banner', '-nostats', '-y', '-i', src,
        '-filter_complex', graph, '-map', vlabel, '-map', '[ca]',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
        '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
        dest,
    ])
    if code != 0:
        raise VideoError(f"Montage impossible : {err.strip()[-300:]}")
    return dest


# ── Mesure et correction ─────────────────────────────────────

def measure_loudness(path):
    """Run loudnorm's analysis pass and return its measurements.

    Measured on the *cut* file rather than the original: removing silence
    raises integrated loudness appreciably, so figures taken beforehand would
    push the result too loud.
    """
    code, _, err = _run([
        _bin('ffmpeg'), '-hide_banner', '-nostats', '-i', path,
        '-af', (f'loudnorm=I={TARGET_LUFS}:TP={TARGET_PEAK_DB}:LRA={TARGET_LRA}'
                ':print_format=json'),
        '-f', 'null', '-',
    ])
    if code != 0:
        return None
    # The JSON block is the last thing loudnorm writes to stderr.
    start = err.rfind('{')
    end = err.rfind('}')
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(err[start:end + 1])
    except ValueError:
        return None


_YAVG = re.compile(r'lavfi\.signalstats\.YAVG=([\d.]+)')


def measure_brightness(path, sample_fps=1):
    """Mean luma across the clip, 0–255, or None if it can't be read.

    Sampled at one frame per second: brightness is a property of the lighting,
    not of individual frames, and reading every frame of a long clip is slow
    for an answer that doesn't change.
    """
    code, _, err = _run([
        _bin('ffmpeg'), '-hide_banner', '-nostats', '-i', path,
        '-vf', f'fps={sample_fps},signalstats,metadata=print:key=lavfi.signalstats.YAVG',
        '-f', 'null', '-',
    ])
    if code != 0:
        return None
    values = [float(v) for v in _YAVG.findall(err)]
    return round(sum(values) / len(values), 1) if values else None


def gamma_for(luma):
    """Gamma that would lift `luma` towards TARGET_LUMA, or None when the
    picture is already bright enough.

    Gamma rather than a brightness offset: it lifts the midtones and shadows
    while leaving white where it is, so a dim clip gets usable without the
    washed-out look a flat offset gives.
    """
    if luma is None or luma >= DARK_THRESHOLD or luma <= 1:
        return None
    # eq applies out = in^(1/gamma), so lifting the picture needs gamma > 1.
    # Written the other way round this always produced a value below 1, was
    # clamped to 1.0, and silently lightened nothing.
    g = math.log(luma / 255.0) / math.log(TARGET_LUMA / 255.0)
    return round(min(max(g, 1.0), MAX_GAMMA), 3)


# ── Repérage du visage ───────────────────────────────────────

# Le détecteur s'arrête vers la lèvre inférieure : le menton tombe un peu plus
# bas que la boîte qu'il renvoie. Six pour cent de la hauteur de la boîte
# rattrapent l'écart, mesuré sur les clips de Bernard.
CHIN_BELOW_BOX = 0.06
# Assez d'images pour attraper l'instant où le menton descend le plus : sur un
# clip de trente secondes, une image toutes les demi-secondes. Un échantillon
# clairsemé rate ce pic d'une vingtaine de pixels, et c'est exactement celui
# qui coupe le menton.
FACE_SAMPLES = 64
# Une détection nettement plus petite que les autres n'est pas un visage : un
# pli de rideau, un bouton de chemise. Elle ferait descendre le bandeau pour
# rien, ou pire, l'empêcherait de descendre.
FACE_MIN_RATIO = 0.7
# Et un visage ne saute pas d'un huitième de l'image d'une seconde à l'autre :
# au-delà, c'est le détecteur qui a glissé, pas Bernard qui a bougé.
FACE_MAX_JUMP = 0.12


def _cover_crop(frame, ratio=1080 / 1920):
    """The centre crop `render(vertical=True)` will apply, done on one frame.

    Scaling is irrelevant here — a fraction of the height stays the same
    fraction — so only the crop has to be reproduced. Without it, a face
    measured on a wide source would be placed against the wrong frame.
    """
    h, w = frame.shape[:2]
    if not h or not w:
        return frame
    if w / h > ratio:
        nw = max(1, int(round(h * ratio)))
        x = (w - nw) // 2
        return frame[:, x:x + nw]
    nh = max(1, int(round(w / ratio)))
    y = (h - nh) // 2
    return frame[y:y + nh]


def detect_face_bottom(path, samples=FACE_SAMPLES, vertical=False):
    """How low the face reaches over the whole clip, as a fraction of height.

    Returns ``{'bottom': …, 'at': seconds}`` or None — None being an honest
    answer, and the one that leaves the band where it has always been.

    Sampled across the clip rather than read off one frame: the band is burnt
    in for the whole duration, so what counts is the lowest the chin ever goes,
    not where it sits at second three. The worst frame's timestamp comes back
    too, because that is the frame worth showing before burning anything in.
    """
    try:
        import cv2
    except ImportError:
        log.info('OpenCV absent : bandeau placé par défaut')
        return None

    cap = None
    try:
        cap = cv2.VideoCapture(path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
        if total <= 0:
            return None
        casc = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        if casc.empty():
            log.info('cascade de visages introuvable : bandeau placé par défaut')
            return None

        pas = max(1, total // max(1, samples))
        trouves = []
        for index in range(0, total, pas):
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = cap.read()
            if not ok:
                continue
            if vertical:
                frame = _cover_crop(frame)
            h, w = frame.shape[:2]
            # Détecter sur une image réduite : la précision utile est de
            # quelques pixels sur 1920, et un cadre entier coûte dix fois plus.
            petit = cv2.resize(frame, (360, max(1, int(360 * h / w))))
            gris = cv2.equalizeHist(cv2.cvtColor(petit, cv2.COLOR_BGR2GRAY))
            boites = casc.detectMultiScale(gris, 1.1, 5, minSize=(60, 60))
            if len(boites) == 0:
                continue
            x, y, bw, bh = max(boites, key=lambda b: b[2] * b[3])
            trouves.append((bh / petit.shape[0],
                            (y + bh + bh * CHIN_BELOW_BOX) / petit.shape[0],
                            index / fps))
    except Exception:
        log.exception('repérage du visage impossible')
        return None
    finally:
        if cap is not None:
            cap.release()

    if len(trouves) < 3:
        return None
    tailles = sorted(t[0] for t in trouves)
    mediane = tailles[len(tailles) // 2]
    milieu = sorted(t[1] for t in trouves)[len(trouves) // 2]
    retenus = [t for t in trouves
               if t[0] >= mediane * FACE_MIN_RATIO
               and abs(t[1] - milieu) <= FACE_MAX_JUMP]
    if not retenus:
        return None
    # Le plus bas, pas la moyenne : le bandeau est gravé pour toute la durée du
    # clip, donc ce qui compte est l'instant où le menton descend le plus. Les
    # deux filtres ci-dessus ont déjà écarté les fausses détections, qui sont
    # la seule raison de ne pas prendre le maximum.
    choisi = max(retenus, key=lambda t: t[1])
    # Des flottants Python, pas des scalaires numpy : le job part en JSON, et
    # `json` ne sait pas écrire un np.float64.
    return {'bottom': round(float(choisi[1]), 4), 'at': round(float(choisi[2]), 2)}


def band_geometry(lines, face_bottom=None, offset=0.0):
    """Where the band sits: (top, height) as fractions of the frame height.

    The default is unchanged — low in the frame but clear of TikTok's own
    furniture. A detected chin only ever pushes the band *down*, never up, and
    never past the point where TikTok's caption would sit on top of it: a band
    grazing the chin is a nuisance, a band under the caption is unreadable.
    """
    hauteur = (TITLE_BAND_HEIGHT if lines <= 1
               else TITLE_BAND_HEIGHT * TITLE_BAND_TWO_LINES)
    # Deux lignes : le bandeau grandit autour du même centre, donc son bord
    # supérieur remonte — c'est là qu'il attrape le menton.
    haut = TITLE_BAND_TOP - (hauteur - TITLE_BAND_HEIGHT) / 2
    plancher = TITLE_BAND_MAX_BOTTOM - hauteur
    if face_bottom:
        haut = max(haut, min(face_bottom + TITLE_BAND_GAP, plancher))
    if offset:
        haut = max(0.45, min(haut + offset, plancher))
    return haut, hauteur


def _fit_font_size(text, usable_px, ceiling, floor):
    """Largest point size whose rendered width fits `usable_px`.

    Measured with the real font rather than estimated from a per-character
    average: capital-heavy French titles run about 0.66–0.72 em per character
    against the 0.58 an estimate suggested, which pushed text off both edges.
    Falls back to a conservative constant if Pillow isn't installed, since the
    band is worth having even when it can't be measured exactly.
    """
    try:
        from PIL import ImageFont
        ref = ImageFont.truetype(TITLE_FONT, 100)
        per_px = ref.getlength(text) / 100.0        # width scales linearly
    except Exception:
        per_px = 0.72 * max(len(text), 1)
    if per_px <= 0:
        return ceiling
    return int(min(ceiling, max(floor, usable_px / per_px)))


BANNER_MAX_LINE = 26          # caractères par ligne avant de passer à deux


def wrap_banner(text, max_line=BANNER_MAX_LINE):
    """Split a banner into at most two balanced lines.

    A single line has to shrink to fit the frame, and past a certain length it
    becomes too small to read from a phone. Two lines keep the type large.
    The break is chosen to even out the two halves rather than filling the
    first line greedily, which otherwise leaves an orphan word underneath.
    """
    text = ' '.join((text or '').split())
    if len(text) <= max_line:
        return [text] if text else []

    mots = text.split(' ')
    if len(mots) == 1:
        return [text]

    meilleur, ecart = None, None
    for i in range(1, len(mots)):
        haut, bas = ' '.join(mots[:i]), ' '.join(mots[i:])
        # Deux lignes déséquilibrées se lisent mal ; on prend la coupure qui
        # rapproche le plus les deux longueurs.
        d = abs(len(haut) - len(bas)) + 40 * (max(len(haut), len(bas)) > 2 * max_line)
        # Ne pas finir la première ligne sur un mot outil ou un nombre :
        # « LA RETRAITE À 60 / ANS » sépare le nombre de son unité.
        dernier = mots[i - 1].strip(',;:').lower()
        if dernier.isdigit() or (len(dernier) <= 2 and dernier.isalpha()):
            d += 12
        if ecart is None or d < ecart:
            meilleur, ecart = (haut, bas), d
    return list(meilleur)


def title_filter(text, width=1080, workdir=None, face_bottom=None, offset=0.0):
    """Filter chain drawing a title band across the lower part of the frame.

    The text goes through a file rather than inline: drawtext treats colons,
    apostrophes, backslashes and percent signs as syntax, and French titles are
    full of apostrophes. A textfile sidesteps the entire escaping problem.
    """
    lignes = wrap_banner((text or '').strip().upper())
    if not lignes:
        return None, None

    # Un fichier et un drawtext par ligne. Un seul textfile contenant un saut
    # de ligne paraît plus simple, mais drawtext dessine alors le saut lui-même
    # sous forme de carré blanc, et `x=(w-text_w)/2` centre le bloc entier — ce
    # qui aligne les lignes à gauche les unes sous les autres au lieu de les
    # centrer chacune.
    paths = []
    for ligne in lignes:
        fd, chemin = tempfile.mkstemp(suffix='.txt', dir=workdir or None)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(ligne)
        paths.append(chemin)

    # Fit to width rather than using a fixed size: DejaVu Bold averages about
    # 0.58 em per character, so a long title shrinks instead of running off the
    # frame. Floored so it never becomes unreadable.
    # Sized from the actual frame width, not a fixed 1080: the same title on a
    # 576-wide source would otherwise run off both edges.
    padding = max(16, int(TITLE_SIDE_PADDING * width / 1080))
    usable = width - 2 * padding
    # La taille est calée sur la ligne la plus longue, et le plafond baisse sur
    # deux lignes : à taille égale le bloc déborderait du bandeau.
    plafond = 0.082 if len(lignes) == 1 else 0.062
    size = _fit_font_size(max(lignes, key=len), usable,
                          ceiling=int(width * plafond),
                          floor=int(width * 0.030))

    # Placement : par défaut le bas du cadre, abaissé si le menton descend
    # jusque-là.
    haut, hauteur = band_geometry(len(lignes), face_bottom, offset)

    interligne = 1.24            # hauteur d'une ligne, en multiples du corps
    bloc = size * (1 + interligne * (len(lignes) - 1))

    parties = [f"drawbox=x=0:y=ih*{haut:.4f}:w=iw:h=ih*{hauteur:.4f}"
               f":color={TITLE_BG}@1.0:t=fill"]
    for i, chemin in enumerate(paths):
        # Chaque ligne est centrée pour elle-même, et décalée d'un interligne.
        # drawtext uses `h` for the frame height; `ih` is drawbox vocabulary and
        # makes drawtext fail to initialise with a bare "Invalid argument".
        decalage = i * size * interligne - (bloc - size) / 2
        parties.append(
            f"drawtext=fontfile={TITLE_FONT}:textfile={chemin}"
            f":fontcolor={TITLE_FG}:fontsize={size}"
            f":x=(w-text_w)/2"
            f":y=h*{haut:.4f}+(h*{hauteur:.4f}-text_h)/2+({decalage:.1f})"
        )
    return ','.join(parties), paths


def polish(src, dest, loudness=None, gamma=None, title=None,
           face_bottom=None, band_offset=0.0):
    """Second pass: normalise loudness, and lift the picture when it is dark.

    Video is stream-copied when no regrade is needed, so the common case costs
    an audio re-encode and little else.
    """
    audio = (f'highpass=f=80,'   # room rumble and handling noise, below speech
             f'loudnorm=I={TARGET_LUFS}:TP={TARGET_PEAK_DB}:LRA={TARGET_LRA}')
    if loudness:
        # Feeding the measurements back turns loudnorm's adaptive one-pass mode
        # into the accurate two-pass one.
        try:
            audio += (f":measured_I={loudness['input_i']}"
                      f":measured_TP={loudness['input_tp']}"
                      f":measured_LRA={loudness['input_lra']}"
                      f":measured_thresh={loudness['input_thresh']}"
                      f":offset={loudness['target_offset']}:linear=true")
        except KeyError:
            pass

    args = [_bin('ffmpeg'), '-hide_banner', '-nostats', '-y', '-i', src,
            '-af', audio]

    video_chain, textfiles = [], []
    if gamma:
        video_chain.append(f'eq=gamma={gamma}')
    if title:
        chain, textfiles = title_filter(title, width=probe_width(src),
                                        workdir=os.path.dirname(dest),
                                        face_bottom=face_bottom,
                                        offset=band_offset)
        if chain:
            video_chain.append(chain)

    if video_chain:
        args += ['-vf', ','.join(video_chain), '-c:v', 'libx264',
                 '-preset', 'veryfast', '-crf', '20']
    else:
        # Nothing to draw and nothing to regrade: copying the video stream
        # keeps this pass to an audio re-encode.
        args += ['-c:v', 'copy']
    args += ['-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', dest]

    try:
        code, _, err = _run(args)
    finally:
        for chemin in textfiles:
            try:
                os.remove(chemin)
            except OSError:
                pass
    if code != 0:
        raise VideoError(f"Égalisation impossible : {err.strip()[-300:]}")
    return dest


# ── Transcription ────────────────────────────────────────────

_model = None
_model_lock = threading.Lock()


def _whisper():
    """Load the model once and keep it: loading costs seconds, transcribing a
    short clip costs less."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(WHISPER_MODEL, device='cpu', compute_type='int8')
        return _model


def transcribe(path, language='fr'):
    """Return {'text', 'segments': [{'start','end','text'}]}."""
    segments, _info = _whisper().transcribe(path, language=language, vad_filter=True)
    out = []
    for s in segments:
        out.append({'start': round(s.start, 2), 'end': round(s.end, 2),
                    'text': s.text.strip()})
    return {'text': ' '.join(s['text'] for s in out).strip(), 'segments': out}


# ── Caption ──────────────────────────────────────────────────

CAPTION_PROMPT = """Tu écris, à la place de Bernard Poignant, le texte qui \
accompagnera une de ses vidéos courtes sur TikTok. Bernard Poignant est un \
homme politique français, socialiste, ancien maire de Quimper et ancien \
conseiller de François Hollande.

Sa voix : un français clair et soigné, un propos engagé à gauche mais mesuré \
et républicain. Sur une vidéo courte il parle à la première personne, va droit \
au but, et ne prend pas les gens de haut.

À partir de la transcription fournie, écris le texte de publication, prêt à \
être collé tel quel :

- Première ligne : une accroche très courte (moins de 60 caractères) qui donne \
envie de rester — une question ou une affirmation nette, jamais du racolage.
- Puis une ligne vide, puis deux à trois phrases à la première personne qui \
résument ce qu'il dit.
- Puis une ligne vide, puis trois à cinq mots-clés précédés de #, en \
minuscules, sans accents, séparés par des espaces.

Règles :
- Reste fidèle à ce qui est dit : n'invente aucune position.
- Pas d'emoji, pas de majuscules d'insistance, pas de « lien en bio ».
- N'écris aucune étiquette de section : le texte doit pouvoir être copié tel \
quel dans TikTok, sans rien retirer.
Réponds uniquement par ce texte."""


# Filet de sécurité : si le modèle remet malgré tout des étiquettes, on les
# retire plutôt que de les laisser arriver dans un copier-coller.
_CAPTION_LABEL = re.compile(
    r'^\s*(ACCROCHE|TEXTE|HASHTAGS|LÉGENDE|LEGENDE)\s*:\s*', re.IGNORECASE)


def _strip_labels(text):
    lines = [_CAPTION_LABEL.sub('', ln) for ln in (text or '').splitlines()]
    # Deux lignes vides consécutives au plus, et rien qui traîne aux extrémités.
    out, blank = [], 0
    for ln in lines:
        if ln.strip():
            blank = 0
            out.append(ln.rstrip())
        else:
            blank += 1
            if blank == 1 and out:
                out.append('')
    return '\n'.join(out).strip()


BANNER_PROMPT = """Tu titres une vidéo courte de Bernard Poignant, homme \
politique français, pour un bandeau affiché à l'écran.

À partir de la transcription, écris UN titre court qui dit ce que la vidéo \
soutient.

Règles :
- 44 caractères maximum, espaces compris. Contrainte stricte : le bandeau tient \
sur deux lignes au plus.
- Le propos, pas l'étiquette du sujet. Pour une vidéo qui montre que Le Pen et \
Mélenchon veulent toujours quitter l'Union sans l'avouer, on écrit \
« SORTIR DE L'EUROPE SANS LE DIRE », et non « EUROPE ET PRÉSIDENTIELLE ».
- Une formule brève : un groupe nominal ou une phrase sans verbe conjugué \
convient, l'infinitif aussi.
- Fidèle à ce qui est dit. N'invente rien, ne durcis pas le propos.
- Pas de ponctuation finale, pas de guillemets, pas d'emoji.
Réponds uniquement par le titre."""

BANNER_MAX_CHARS = 44


def generate_banner_title(transcript_text):
    """A short on-screen title derived from what is actually said.

    Separate from the caption: the caption is a paragraph to paste, this has to
    fit one line inside a band. Returns None when no API key is set, so the
    video is simply rendered without a band rather than failing.
    """
    from articles.ai_summary import _api_key, MODEL

    key = _api_key()
    if not key or not (transcript_text or '').strip():
        return None

    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=45.0, max_retries=1)
    resp = client.messages.create(
        model=MODEL, max_tokens=100,
        system=[{'type': 'text', 'text': BANNER_PROMPT,
                 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user',
                   'content': f"Transcription :\n{transcript_text[:4000]}"}],
    )
    text = ''.join(b.text for b in resp.content if b.type == 'text')
    # Keep the first non-empty line, drop any quoting, and enforce the limit
    # here rather than trusting the model to have counted.
    for line in (text or '').splitlines():
        line = line.strip().strip('«»"“”\'').strip()
        if line:
            return line[:BANNER_MAX_CHARS].strip()
    return None


def write_caption(transcript_text):
    """Ask Claude for a TikTok caption. Returns the raw text, or None when no
    API key is configured."""
    from articles.ai_summary import _api_key, MODEL

    key = _api_key()
    if not key or not (transcript_text or '').strip():
        return None

    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=1)
    resp = client.messages.create(
        model=MODEL, max_tokens=500,
        system=[{'type': 'text', 'text': CAPTION_PROMPT,
                 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user',
                   'content': f"Transcription de la vidéo :\n{transcript_text[:6000]}"}],
    )
    return _strip_labels(''.join(b.text for b in resp.content if b.type == 'text'))


# ── Jobs ─────────────────────────────────────────────────────

WORKDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'instance', 'video')
JOBS = {}
_jobs_lock = threading.Lock()
_loaded = False


def _job_file(job_id):
    return os.path.join(WORKDIR, f'{job_id}.json')


def _persist(job):
    """Write the job beside its video.

    Jobs used to live only in memory, so every restart of the dev server threw
    away the transcript, the caption and the link to the file — leaving a
    finished render on disk that the download route answered 404 for. The
    server restarts on every deploy, so this was a matter of when, not if.
    """
    try:
        os.makedirs(WORKDIR, exist_ok=True)
        with open(_job_file(job['id']), 'w', encoding='utf-8') as fh:
            json.dump(job, fh, ensure_ascii=False)
    except (OSError, TypeError) as exc:
        log.warning('could not persist job %s: %s', job.get('id'), exc)


def _load_jobs():
    """Restore jobs from disk once per process.

    Also adopts any render that has no job file — videos produced before jobs
    were persisted would otherwise stay invisible and undownloadable.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not os.path.isdir(WORKDIR):
        return
    for name in os.listdir(WORKDIR):
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(WORKDIR, name), encoding='utf-8') as fh:
                job = json.load(fh)
            if job.get('id'):
                JOBS.setdefault(job['id'], job)
        except (OSError, ValueError):
            continue
    for name in sorted(os.listdir(WORKDIR)):
        if not name.endswith('.mp4') or name.startswith('src-'):
            continue
        job_id = os.path.splitext(name)[0]
        if job_id in JOBS:
            continue
        path = os.path.join(WORKDIR, name)
        JOBS[job_id] = {
            'id': job_id, 'name': name, 'status': 'done',
            'step': 'Terminé (repris depuis le disque)', 'output': path,
            'created_at': datetime.utcfromtimestamp(
                os.path.getmtime(path)).isoformat(timespec='seconds'),
        }


def _set(job_id, **fields):
    with _jobs_lock:
        _load_jobs()
        job = JOBS.setdefault(job_id, {})
        job.update(fields)
        snapshot = dict(job)
    _persist(snapshot)


def get_job(job_id):
    with _jobs_lock:
        _load_jobs()
        return dict(JOBS.get(job_id) or {})


def all_jobs():
    with _jobs_lock:
        _load_jobs()
        return sorted(JOBS.values(), key=lambda j: j.get('created_at', ''), reverse=True)


def notify(job):
    """Tell whoever uploaded the clip that it is ready — or that it failed.

    Sent from the worker thread, so it goes straight through SendGrid rather
    than a template: `mail.send_email` reads its configuration from the
    environment and needs no application context. The publication text travels
    with the message, because the next thing anyone does is copy it.
    """
    if not job:
        return
    adresse = (job.get('notify_email') or '').strip()
    if not adresse:
        return
    try:
        from mail import send_email
    except Exception:
        log.exception('notification : mail indisponible')
        return

    base = (job.get('base_url')
            or os.environ.get('VIDEO_BASE_URL')
            or os.environ.get('SITE_BASE_URL') or '').rstrip('/')
    lien_page = f"{base}/admin/video/job/{job.get('id')}"
    lien_fichier = f"{lien_page}/download"
    nom = job.get('name') or 'la vidéo'
    rate = job.get('status') == 'error'

    if rate:
        sujet = f"Montage en échec : {nom}"
        corps = (f"<p>Le montage de <strong>{_echapper(nom)}</strong> s'est "
                 f"arrêté :</p><p style=\"color:#8a1c1c\">{_echapper(job.get('error') or 'raison inconnue')}</p>"
                 f"<p><a href=\"{lien_page}\">Voir le détail</a></p>")
    else:
        texte = job.get('caption') or ''
        minutes = job.get('kept')
        resume = (f"{minutes:.0f} secondes retenues sur {job.get('duration', 0):.0f}"
                  if isinstance(minutes, (int, float)) else '')
        sujet = f"Montage terminé : {nom}"
        corps = (
            f"<p>Le montage de <strong>{_echapper(nom)}</strong> est prêt"
            + (f" — {resume}." if resume else ".") + "</p>"
            f"<p><a href=\"{lien_fichier}\" style=\"display:inline-block;background:#16213E;"
            f"color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none\">"
            f"Télécharger le MP4</a></p>"
            f"<p style=\"font-size:14px;color:#666\">Ou depuis la page du montage : "
            f"<a href=\"{lien_page}\">{lien_page}</a></p>"
            + (f"<h3 style=\"font-size:16px;margin-top:28px\">Texte à publier</h3>"
               f"<pre style=\"white-space:pre-wrap;font-family:inherit;font-size:15px;"
               f"line-height:1.6;background:#f5f5f7;padding:16px;border-radius:8px\">"
               f"{_echapper(texte)}</pre>" if texte else '')
        )
    try:
        send_email(adresse, sujet, corps, categories=['video', f"video-{job.get('id')}"])
    except Exception:
        log.exception('notification : envoi impossible (%s)', adresse)


def _echapper(texte):
    return (str(texte or '').replace('&', '&amp;')
            .replace('<', '&lt;').replace('>', '&gt;'))


def start_job(src_path, original_name, vertical=False, title=None,
              notify_email=None, base_url=None):
    """First phase: listen to the clip and propose a band. Nothing is rendered.

    The order is deliberate. Everything that has to happen before a human can
    answer « is this the right band? » happens here — the transcript, the
    proposed wording, and where the face sits — and everything else waits for
    the answer. Cutting and encoding before asking made someone watch a
    progress bar for a question that had not been asked yet.
    """
    job_id = uuid.uuid4().hex[:12]
    _set(job_id, id=job_id, name=original_name, status='queued', step='En attente…',
         created_at=datetime.utcnow().isoformat(timespec='seconds'),
         vertical=vertical, title=title, src=src_path,
         notify_email=notify_email, base_url=base_url)

    def _work():
        try:
            # La transcription lit la source telle quelle : elle porte sur la
            # parole, que le montage ne change pas, et l'attendre coûterait à
            # celui qui doit répondre.
            _set(job_id, status='running', step='Transcription…', error=None)
            tr = transcribe(src_path)
            _set(job_id, transcript=tr['text'], segments_text=tr['segments'])

            # A separate name on purpose: assigning to `title` here would make
            # it local to this closure, and reading it below would raise
            # UnboundLocalError before ever reaching the argument.
            banner = title
            if not banner:
                _set(job_id, step='Titre du bandeau…')
                banner = generate_banner_title(tr['text'])

            # Repérage du visage : il décide de la hauteur du bandeau, sur le
            # cadrage final et non sur la source, qui peut être plus large.
            _set(job_id, step='Repérage du visage…')
            visage = detect_face_bottom(src_path, vertical=vertical)
            _set(job_id,
                 face_bottom=(visage or {}).get('bottom'),
                 face_at=(visage or {}).get('at'))

            _set(job_id, title=banner, status='awaiting_banner',
                 step='Bandeau à confirmer')
        except Exception as exc:
            _set(job_id, status='error', step='Échec', error=str(exc)[:400])
            # Même en première phase : la transcription prend assez de temps
            # pour qu'on soit parti faire autre chose.
            notify(get_job(job_id))

    threading.Thread(target=_work, name=f'video-{job_id}', daemon=True).start()
    return job_id


def apply_banner(job_id, banner=None, offset=0.0):
    """Second phase: cut, level, burn the band in, write the caption, and say
    so by e-mail.

    Returns immediately. Everything here runs unattended, which is the point:
    the only question worth a human was asked in phase one.
    """
    job = get_job(job_id)
    if not job:
        return False
    src = job.get('src')
    if not src or not os.path.exists(src):
        _set(job_id, status='error', step='Échec',
             error="Le fichier d'origine a disparu — relancez l'import.")
        return False

    banner = (banner if banner is not None else job.get('title')) or None
    _set(job_id, title=banner, band_offset=offset, status='running', error=None,
         step='Analyse du son…')

    def _work():
        cut = None
        try:
            duration = probe_duration(src)
            silences = detect_silences(src)
            segments = keep_segments(duration, silences)
            kept = sum(e - s for s, e in segments)
            _set(job_id, duration=round(duration, 1), kept=round(kept, 1),
                 removed=round(duration - kept, 1), cuts=len(segments))

            _set(job_id, step='Montage…')
            cut = os.path.join(WORKDIR, f'{job_id}-cut.mp4')
            render(src, segments, cut, vertical=job.get('vertical'))

            _set(job_id, step='Mesure du son et de l’image…')
            loudness = measure_loudness(cut)
            gamma = gamma_for(measure_brightness(cut))
            _set(job_id, gamma=gamma,
                 lufs_before=(round(float(loudness['input_i']), 1)
                              if loudness and loudness.get('input_i') not in (None, '-inf')
                              else None))

            _set(job_id, step='Égalisation et bandeau…')
            dest = os.path.join(WORKDIR, f'{job_id}.mp4')
            polish(cut, dest, loudness=loudness, gamma=gamma, title=banner,
                   face_bottom=job.get('face_bottom'), band_offset=offset)

            # Le texte de publication vient après l'image : personne ne
            # l'attend pour répondre, et il part dans l'e-mail de fin.
            _set(job_id, step='Rédaction du texte…')
            try:
                texte = write_caption(job.get('transcript') or '')
            except Exception:
                # L'image est faite : un modèle indisponible ne doit pas
                # transformer un montage réussi en montage en échec.
                log.exception('texte de publication indisponible (%s)', job_id)
                texte = ''
            _set(job_id, caption=texte, output=dest, status='done', step='Terminé')

            # Enchaînement : dix minutes après, le serveur ira chercher le post
            # TikTok correspondant et publiera ailleurs.
            #
            # On arme sans consulter le réglage : le lire demande la base, et ce
            # fil n'a pas de contexte applicatif. Le veilleur, lui, en a un et
            # ignore les jobs armés quand l'enchaînement est éteint. Armer n'a
            # aucun effet en soi — c'est une mention dans le fichier du job.
            try:
                from tiktok.auto import armer
                armer(job_id)
            except Exception:
                log.exception('auto-publication : armement impossible (%s)', job_id)
            notify(get_job(job_id))
        except Exception as exc:
            _set(job_id, status='error', step='Échec', error=str(exc)[:400])
            notify(get_job(job_id))
        finally:
            # The intermediate is only useful if the polish pass failed.
            if cut:
                try:
                    os.remove(cut)
                except OSError:
                    pass

    threading.Thread(target=_work, name=f'video-band-{job_id}', daemon=True).start()
    return True


PREVIEW_DIR = os.path.join(WORKDIR, 'apercus')


def _prune_previews(max_age=86400):
    """Aperçus d'hier : une frappe en produit une poignée, et personne ne les
    regarde deux jours de suite."""
    limite = time.time() - max_age
    try:
        for nom in os.listdir(PREVIEW_DIR):
            chemin = os.path.join(PREVIEW_DIR, nom)
            if os.path.isfile(chemin) and os.path.getmtime(chemin) < limite:
                os.remove(chemin)
    except OSError:
        pass


def banner_preview(job, title, offset=0.0, width=405):
    """A real frame of the clip with the band drawn on it, as a JPEG path.

    A CSS mock-up of the band says what the words will read; it cannot say
    whether they land on the chin. This takes the frame where the face sits
    lowest — the worst case, the one that decides — and burns the band in
    exactly as the render will, at one twentieth of the cost.
    """
    # Avant le montage il n'y a que la source : l'aperçu lui applique le même
    # recadrage vertical que le rendu, sinon il montrerait un cadre qui
    # n'existera pas.
    src = job.get('cut') or job.get('output') or job.get('src')
    if not src or not os.path.exists(src):
        return None
    recadre = bool(job.get('vertical')) and not (job.get('cut') or job.get('output'))
    title = (title or '').strip()
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    cle = hashlib.sha1(
        f"{job.get('id')}|{title}|{offset}|{os.path.getmtime(src)}".encode()
    ).hexdigest()[:14]
    dest = os.path.join(PREVIEW_DIR, f'{cle}.jpg')
    if os.path.exists(dest):
        return dest
    _prune_previews()

    largeur = 1080 if recadre else probe_width(src)
    chain, textfiles = (None, [])
    if title:
        chain, textfiles = title_filter(title, width=largeur,
                                        workdir=PREVIEW_DIR,
                                        face_bottom=job.get('face_bottom'),
                                        offset=offset)
    filtres = ['scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920'] if recadre else []
    filtres += ([chain] if chain else []) + [f'scale={int(width)}:-2']
    # L'instant du pire cadrage quand il est connu ; sinon le milieu du clip,
    # qui vaut mieux que la première image, souvent prise avant que Bernard
    # ait fini de s'installer.
    quand = job.get('face_at')
    if quand is None:
        quand = (job.get('kept') or job.get('duration') or 4) / 2

    try:
        code, _, err = _run([
            _bin('ffmpeg'), '-hide_banner', '-nostats', '-y',
            '-ss', f'{max(0, float(quand)):.2f}', '-i', src,
            '-frames:v', '1', '-vf', ','.join(filtres), '-q:v', '4', dest,
        ], timeout=120)
    finally:
        for chemin in textfiles:
            try:
                os.remove(chemin)
            except OSError:
                pass
    if code != 0:
        raise VideoError(f"Aperçu impossible : {err.strip()[-200:]}")
    return dest


THUMB_DIR = os.path.join(WORKDIR, 'thumbs')


def thumbnail(filename, at=1.0, width=320):
    """Return the path to a JPEG still for a render, generating it on first
    ask and caching it beside the video.

    The picker shows a dozen of these at once, so extracting a frame per page
    load would be a dozen ffmpeg runs per refresh. The cache is keyed on the
    render's mtime: a file replaced under the same name gets a new still
    rather than the stale one.
    """
    src = os.path.join(WORKDIR, filename)
    if not os.path.exists(src):
        return None

    os.makedirs(THUMB_DIR, exist_ok=True)
    stamp = int(os.path.getmtime(src))
    dest = os.path.join(THUMB_DIR, f'{os.path.splitext(filename)[0]}-{stamp}.jpg')
    if os.path.exists(dest):
        return dest

    # A clip shorter than the seek point would yield no frame at all; fall
    # back to the very first one.
    seek = at if (probe_duration(src) or 0) > at + 0.2 else 0
    try:
        _run([_bin('ffmpeg'), '-y', '-ss', str(seek), '-i', src,
              '-frames:v', '1', '-vf', f'scale={width}:-2', '-q:v', '4', dest],
             timeout=60)
    except Exception:
        log.exception('thumbnail failed for %s', filename)
        return None
    return dest if os.path.exists(dest) else None


def local_renders():
    """The finished renders on this machine, newest first, each with what the
    picker needs to tell them apart: when it was made, how long it runs, and
    the banner title if the job that produced it is still on record."""
    if not is_enabled() or not os.path.isdir(WORKDIR):
        return []

    by_output = {}
    for job in all_jobs():
        out = job.get('output')
        if out:
            by_output[os.path.basename(out)] = job

    renders = []
    for name in os.listdir(WORKDIR):
        if not name.endswith('.mp4') or name.startswith('src-') or name.endswith('-cut.mp4'):
            continue
        path = os.path.join(WORKDIR, name)
        job = by_output.get(name, {})
        try:
            stat = os.stat(path)
        except OSError:
            continue
        renders.append({
            'filename': name,
            'created_at': datetime.fromtimestamp(stat.st_mtime),
            'size_mb': round(stat.st_size / (1024 * 1024), 1),
            # Prefer the duration the job measured; probing every file on
            # every page load would be one ffprobe per render.
            'duration': job.get('kept') or job.get('duration'),
            'title': job.get('title'),
            'source_name': job.get('name'),
            'job_id': job.get('id'),
        })
    renders.sort(key=lambda r: r['created_at'], reverse=True)
    return renders


def job_for_render(filename):
    """The job that produced a given render, or None.

    Renders are named after their job id, but a file adopted from disk before
    jobs were persisted may not be, so the output path is checked too.
    """
    name = os.path.basename(filename)
    stem = os.path.splitext(name)[0]

    # Le fichier envoyé dans le bucket reçoit un suffixe aléatoire
    # (« 55624574db68-37c91a91.mp4 ») : on essaie donc aussi la racine avant le
    # dernier tiret, sinon un post ne retrouve jamais son job.
    for candidate in (stem, stem.rsplit('-', 1)[0]):
        job = get_job(candidate)
        if job:
            return job

    for candidate in all_jobs():
        out = candidate.get('output')
        if out and os.path.basename(out) in (name, f'{stem}.mp4'):
            return candidate
    return None
