"""AI-generated article summaries in Bernard Poignant's tone of voice.

Uses the Claude API (Anthropic SDK). The API key is read from
`ANTHROPIC__API_KEY` (the app's `<SECTION>__<KEY>` secret convention, same as
`SEARCHAPI__API_KEY`) or, failing that, the SDK-standard `ANTHROPIC_API_KEY`.
When no key is set the caller falls back to plain text extraction.
"""

import os
import re

from bs4 import BeautifulSoup


MODEL = 'claude-opus-4-8'

# Tone-of-voice brief. Kept stable across a batch run so it can be prompt-cached.
SYSTEM_PROMPT = """Tu rédiges la phrase de résumé d'un article du blog de Bernard Poignant — \
homme politique français, socialiste, ancien maire de Quimper et ancien conseiller \
de François Hollande.

Sa voix : un français clair et soigné ; un propos engagé à gauche mais mesuré et \
républicain ; un ton sobre et réfléchi, jamais racoleur ni sensationnaliste ; un \
attachement à la laïcité, à la social-démocratie et à l'ancrage local et breton.

À partir du titre et du texte fournis, rédige UNE seule phrase de résumé en français \
(environ 120 à 160 caractères) qui restitue l'essentiel de l'article dans cette voix, \
à la manière d'un chapô de presse.

Règles :
- Une seule phrase, pas davantage.
- Pas de guillemets, pas de préfixe (« Résumé : »), pas de hashtag, pas d'emoji.
- Formulation neutre ou à la troisième personne.
- Reste fidèle au contenu : n'invente rien.
Réponds uniquement par la phrase, sans aucun autre texte."""


def _api_key():
    return (
        os.environ.get('ANTHROPIC__API_KEY')
        or os.environ.get('ANTHROPIC_API_KEY')
        or ''
    ).strip()


def is_configured():
    """True when an Anthropic API key is available."""
    return bool(_api_key())


def _plain_text(html, limit=6000):
    text = BeautifulSoup(html or '', 'html.parser').get_text(separator=' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


def _clean(text):
    # The model is told to answer with the sentence alone; defensively keep the
    # last non-empty line and strip any surrounding quotes.
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
    sentence = lines[-1] if lines else ''
    return sentence.strip().strip('«»"“”\'').strip()


def generate_summary(title, content_html):
    """Return an AI-written one-line summary, or None when the API key is
    missing. Raises on API errors so the caller can decide how to fall back."""
    key = _api_key()
    if not key:
        return None
    body = _plain_text(content_html)
    if not body:
        return ''

    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=[{
            'type': 'text',
            'text': SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=[{
            'role': 'user',
            'content': (
                f"Titre : {title}\n\n"
                f"Texte de l'article :\n{body}\n\n"
                "Rédige la phrase de résumé."
            ),
        }],
    )
    text = ''.join(b.text for b in response.content if b.type == 'text')
    return _clean(text)
