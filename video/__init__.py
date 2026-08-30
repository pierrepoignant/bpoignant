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
NOISE_FLOOR_DB = -30
MIN_SILENCE = 0.35
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


def polish(src, dest, loudness=None, gamma=None):
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
    if gamma:
        args += ['-vf', f'eq=gamma={gamma}', '-c:v', 'libx264',
                 '-preset', 'veryfast', '-crf', '20']
    else:
        args += ['-c:v', 'copy']
    args += ['-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', dest]

    code, _, err = _run(args)
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

À partir de la transcription fournie, écris :

ACCROCHE : une première phrase très courte (moins de 60 caractères) qui donne \
envie de rester — une question, une affirmation nette, jamais du racolage.
TEXTE : deux à trois phrases qui résument ce qu'il dit, à la première personne.
HASHTAGS : trois à cinq mots-clés pertinents, précédés de #, en minuscules, \
sans accents, séparés par des espaces.

Règles :
- Reste fidèle à ce qui est dit : n'invente aucune position.
- Pas d'emoji, pas de majuscules d'insistance, pas de « lien en bio ».
- Réponds exactement dans ce format, une section par ligne, rien d'autre."""


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
    return ''.join(b.text for b in resp.content if b.type == 'text').strip()


# ── Jobs ─────────────────────────────────────────────────────

WORKDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'instance', 'video')
JOBS = {}
_jobs_lock = threading.Lock()


def _set(job_id, **fields):
    with _jobs_lock:
        JOBS.setdefault(job_id, {}).update(fields)


def get_job(job_id):
    with _jobs_lock:
        return dict(JOBS.get(job_id) or {})


def all_jobs():
    with _jobs_lock:
        return sorted(JOBS.values(), key=lambda j: j.get('created_at', ''), reverse=True)


def start_job(src_path, original_name, vertical=False):
    """Kick off processing in a thread and return the job id. Everything slow
    happens here; the page polls for progress."""
    job_id = uuid.uuid4().hex[:12]
    _set(job_id, id=job_id, name=original_name, status='queued', step='En attente…',
         created_at=datetime.utcnow().isoformat(timespec='seconds'),
         vertical=vertical, src=src_path)

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
            _set(job_id, luma=luma, gamma=gamma,
                 lufs_before=(round(float(loudness['input_i']), 1)
                              if loudness and loudness.get('input_i') not in (None, '-inf')
                              else None))

            _set(job_id, step='Égalisation du son et de l’image…')
            dest = os.path.join(WORKDIR, f'{job_id}.mp4')
            polish(cut, dest, loudness=loudness, gamma=gamma)
            _set(job_id, output=dest)
            # The intermediate is only useful if the polish pass failed.
            try:
                os.remove(cut)
            except OSError:
                pass

            _set(job_id, step='Transcription…')
            tr = transcribe(dest)
            _set(job_id, transcript=tr['text'], segments_text=tr['segments'])

            _set(job_id, step='Rédaction du texte…')
            _set(job_id, caption=write_caption(tr['text']))

            _set(job_id, status='done', step='Terminé')
        except Exception as exc:
            _set(job_id, status='error', step='Échec', error=str(exc)[:400])

    threading.Thread(target=_work, name=f'video-{job_id}', daemon=True).start()
    return job_id
