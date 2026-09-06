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

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime

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
TITLE_SIDE_PADDING = 48      # marge gauche/droite, en pixels sur 1080

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


def title_filter(text, width=1080, workdir=None):
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

    # Le bandeau s'agrandit pour deux lignes, et remonte d'autant pour rester
    # au même endroit dans l'image.
    hauteur = TITLE_BAND_HEIGHT if len(lignes) == 1 else TITLE_BAND_HEIGHT * 1.75
    haut = TITLE_BAND_TOP - (hauteur - TITLE_BAND_HEIGHT) / 2

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


def polish(src, dest, loudness=None, gamma=None, title=None):
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
                                        workdir=os.path.dirname(dest))
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


def start_job(src_path, original_name, vertical=False, title=None):
    """First phase: cut, measure, transcribe, and propose a band title.

    It stops there rather than rendering straight through. The band is burnt
    into the picture and cannot be undone afterwards, and the proposal is the
    one step worth a human glance — so the job waits for the text to be
    confirmed, and `apply_banner` finishes it.
    """
    job_id = uuid.uuid4().hex[:12]
    _set(job_id, id=job_id, name=original_name, status='queued', step='En attente…',
         created_at=datetime.utcnow().isoformat(timespec='seconds'),
         vertical=vertical, title=title, src=src_path)

    def _work():
        try:
            _set(job_id, status='running', step='Analyse du son…')
            duration = probe_duration(src_path)
            silences = detect_silences(src_path)
            segments = keep_segments(duration, silences)
            kept = sum(e - s for s, e in segments)
            _set(job_id, duration=round(duration, 1), kept=round(kept, 1),
                 removed=round(duration - kept, 1), cuts=len(segments))

            _set(job_id, step='Montage…')
            cut = os.path.join(WORKDIR, f'{job_id}-cut.mp4')
            render(src_path, segments, cut, vertical=vertical)

            _set(job_id, step='Mesure du son et de l’image…')
            loudness = measure_loudness(cut)
            luma = measure_brightness(cut)
            gamma = gamma_for(luma)
            # Conservés dans le job : la deuxième phase en a besoin et tourne
            # dans une autre requête, souvent après un redémarrage.
            _set(job_id, luma=luma, gamma=gamma, cut=cut, loudness=loudness,
                 lufs_before=(round(float(loudness['input_i']), 1)
                              if loudness and loudness.get('input_i') not in (None, '-inf')
                              else None))

            # Transcribe before proposing the band: its text is derived from
            # what is actually said, so the words have to exist first.
            # Transcribing the cut is also cheaper than the original — the
            # silence is already gone.
            _set(job_id, step='Transcription…')
            tr = transcribe(cut)
            _set(job_id, transcript=tr['text'], segments_text=tr['segments'])

            _set(job_id, step='Rédaction du texte…')
            _set(job_id, caption=write_caption(tr['text']))

            # A separate name on purpose: assigning to `title` here would make
            # it local to this closure, and reading it below would raise
            # UnboundLocalError before ever reaching the argument.
            banner = title
            if not banner:
                _set(job_id, step='Titre du bandeau…')
                banner = generate_banner_title(tr['text'])
            _set(job_id, title=banner, status='awaiting_banner',
                 step='Bandeau à confirmer')
        except Exception as exc:
            _set(job_id, status='error', step='Échec', error=str(exc)[:400])

    threading.Thread(target=_work, name=f'video-{job_id}', daemon=True).start()
    return job_id


def apply_banner(job_id, banner=None):
    """Second phase: burn the confirmed band in and finish the render.

    Returns immediately; the page polls as it does for the first phase. Passing
    an empty banner renders the clip without a band, which is a legitimate
    choice rather than a missing value.
    """
    job = get_job(job_id)
    if not job:
        return False
    cut = job.get('cut')
    if not cut or not os.path.exists(cut):
        _set(job_id, status='error', step='Échec',
             error="Le montage intermédiaire a disparu — relancez l'import.")
        return False

    banner = (banner if banner is not None else job.get('title')) or None
    _set(job_id, title=banner, status='running',
         step='Égalisation du son et de l’image…')

    def _work():
        try:
            dest = os.path.join(WORKDIR, f'{job_id}.mp4')
            polish(cut, dest, loudness=job.get('loudness'), gamma=job.get('gamma'),
                   title=banner)
            _set(job_id, output=dest, status='done', step='Terminé')
            # Enchaînement : dix minutes après, le serveur ira chercher le post
            # TikTok correspondant et publiera ailleurs. Sans incidence si la
            # bascule est éteinte — le veilleur ignore alors les jobs armés.
            try:
                from tiktok.auto import armer, is_enabled
                if is_enabled():
                    armer(job_id)
            except Exception:
                log.exception('auto-publication : armement impossible (%s)', job_id)
            # The intermediate is only useful if the polish pass failed.
            try:
                os.remove(cut)
            except OSError:
                pass
        except Exception as exc:
            _set(job_id, status='error', step='Échec', error=str(exc)[:400])

    threading.Thread(target=_work, name=f'video-band-{job_id}', daemon=True).start()
    return True


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
