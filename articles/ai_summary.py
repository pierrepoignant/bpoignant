"""AI-written article metadata: summaries in Bernard Poignant's voice, plus
thematic classification.

  * `generate_summary`        — the editorial chapô shown on the site and in
                                the newsletter: neutral, third person.
  * `generate_social_summary` — the line that accompanies the article on X:
                                first person, livelier, made to be clicked.
  * `generate_themes`         — 1–3 labels from a fixed vocabulary, used to
                                browse the archive and to relate articles.

Uses the Claude API (Anthropic SDK). The API key is read from
`ANTHROPIC__API_KEY` (the app's `<SECTION>__<KEY>` secret convention, same as
`SEARCHAPI__API_KEY`) or, failing that, the SDK-standard `ANTHROPIC_API_KEY`.
When no key is set the caller falls back to plain text extraction.
"""

import os
import re

from bs4 import BeautifulSoup


MODEL = 'claude-opus-4-8'

# Shared portrait of the author, reused by both briefs below.
_VOICE = """Bernard Poignant est un homme politique français, socialiste, ancien \
maire de Quimper et ancien conseiller de François Hollande.

Sa voix : un français clair et soigné ; un propos engagé à gauche mais mesuré et \
républicain ; un attachement à la laïcité, à la social-démocratie et à l'ancrage \
local et breton."""

# Tone-of-voice brief. Kept stable across a batch run so it can be prompt-cached.
SYSTEM_PROMPT = f"""Tu rédiges la phrase de résumé d'un article du blog de Bernard \
Poignant. {_VOICE}

Son ton ici est sobre et réfléchi, jamais racoleur ni sensationnaliste.

À partir du titre et du texte fournis, rédige UNE seule phrase de résumé en français \
(environ 120 à 160 caractères) qui restitue l'essentiel de l'article dans cette voix, \
à la manière d'un chapô de presse.

Règles :
- Une seule phrase, pas davantage.
- Pas de guillemets, pas de préfixe (« Résumé : »), pas de hashtag, pas d'emoji.
- Formulation neutre ou à la troisième personne.
- Reste fidèle au contenu : n'invente rien.
Réponds uniquement par la phrase, sans aucun autre texte."""

# Social brief. Same man, different register: on X he speaks in his own name and
# allows himself the turn of phrase a press chapô would not.
SOCIAL_SYSTEM_PROMPT = f"""Tu écris, à la place de Bernard Poignant, la phrase qui \
accompagnera l'un de ses articles lorsqu'il le partage sur X (Twitter). {_VOICE}

Sur les réseaux, il se permet ce qu'un chapô de presse ne permet pas : la première \
personne, une pointe d'ironie, une formule qui accroche — sans jamais verser dans \
la vulgarité, l'outrance ou le racolage.

À partir du titre et du texte fournis, écris UNE seule phrase en français (environ \
100 à 150 caractères) qui donne envie de lire l'article.

Règles :
- Écris à la première personne (« je », « j'ai », « mon »), comme Bernard s'exprimant \
lui-même : ce qu'il a écrit, constaté, ou ce qui le préoccupe.
- Une seule phrase, vive et incarnée : un angle, une prise de position, une pointe \
d'humour ou une question adressée au lecteur — pas un résumé neutre.
- Pas de guillemets autour de la phrase, pas de préfixe, pas de hashtag, pas d'emoji, \
pas de lien : le titre et le lien sont ajoutés automatiquement, ne les répète pas.
- Pas de majuscules d'insistance ni de points d'exclamation en rafale.
- Reste fidèle au contenu et aux positions de l'article : n'invente rien et ne durcis \
pas le propos.
Réponds uniquement par la phrase, sans aucun autre texte."""


# Fixed vocabulary. A closed list is the point: free-form tags drift into
# near-duplicates ("Europe", "européen", "UE") that make the archive less
# navigable, not more. Tuned to what Bernard actually writes about — add to it
# deliberately rather than letting the model invent labels.
THEMES = [
    'Bretagne',
    'Décentralisation',
    'Économie et dette',
    'Éducation',
    'Élections',
    'Europe',
    'Extrême droite',
    'Gauche et socialisme',
    'Histoire',
    'Immigration',
    'Institutions',
    'International',
    'La France insoumise',
    'Laïcité',
    'Médias et réseaux',
    'Social-démocratie',
]

THEMES_SYSTEM_PROMPT = """Tu classes les articles du blog de Bernard Poignant, \
homme politique français, socialiste, ancien maire de Quimper.

Voici la liste FERMÉE des thèmes disponibles :
{themes}

À partir du titre et du texte fournis, choisis les thèmes qui correspondent \
vraiment au propos de l'article.

Règles :
- Entre 1 et 3 thèmes, classés du plus pertinent au moins pertinent.
- Uniquement des thèmes de la liste ci-dessus, recopiés à l'identique.
- Ne retiens un thème que s'il est central dans l'article, pas s'il est \
simplement mentionné en passant. Un seul thème juste vaut mieux que trois \
approximatifs.
- N'invente aucun thème nouveau.
Réponds uniquement par les thèmes retenus, un par ligne, sans numérotation \
ni ponctuation."""


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


def _generate(system_prompt, instruction, title, content_html):
    """Run one brief against the article. Returns None when the API key is
    missing and '' when the article has no usable text; raises on API errors so
    the caller can decide how to fall back."""
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
            'text': system_prompt,
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=[{
            'role': 'user',
            'content': (
                f"Titre : {title}\n\n"
                f"Texte de l'article :\n{body}\n\n"
                f"{instruction}"
            ),
        }],
    )
    text = ''.join(b.text for b in response.content if b.type == 'text')
    return _clean(text)


def generate_summary(title, content_html):
    """Return the AI-written editorial one-liner (third person), or None when
    the API key is missing."""
    return _generate(
        SYSTEM_PROMPT, "Rédige la phrase de résumé.", title, content_html,
    )


TWEET_CONDENSE_PROMPT = """Tu réécris, à la place de Bernard Poignant, un texte \
trop long pour X (280 caractères maximum).

Sa voix : un français clair et soigné, un propos engagé à gauche mais mesuré et \
républicain, à la première personne.

Règles :
- 275 caractères maximum, espaces compris. Contrainte stricte.
- Garde l'essentiel du propos et le ton : ce n'est pas un résumé neutre, c'est \
le même homme qui parle plus brièvement.
- N'invente rien, ne durcis pas le propos.
- Pas de hashtag, pas d'emoji, pas de lien.
Réponds uniquement par le texte réécrit."""


def condense_for_tweet(text, limit=275):
    """Rewrite an over-long text to fit a tweet, in the same voice.

    Returns None when the AI is unavailable, so the caller can fall back to
    trimming on a word boundary — a clean cut is worse than a rewrite but far
    better than slicing mid-sentence.
    """
    key = _api_key()
    if not key or not (text or '').strip():
        return None

    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=45.0, max_retries=1)
    resp = client.messages.create(
        model=MODEL, max_tokens=400,
        system=[{'type': 'text', 'text': TWEET_CONDENSE_PROMPT,
                 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user', 'content': f"Texte à raccourcir :\n{text[:4000]}"}],
    )
    out = _clean(''.join(b.text for b in resp.content if b.type == 'text'))
    # The limit is enforced here rather than trusted to the model.
    return out if out and len(out) <= limit else (out[:limit].rsplit(' ', 1)[0] if out else None)


def generate_themes(title, content_html):
    """Return 1–3 themes from the fixed vocabulary, or None when the API key is
    missing. Anything the model returns that isn't in THEMES is dropped — the
    prompt forbids invention, but the vocabulary is enforced here rather than
    trusted."""
    key = _api_key()
    if not key:
        return None
    body = _plain_text(content_html)
    if not body:
        return []

    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=[{
            'type': 'text',
            'text': THEMES_SYSTEM_PROMPT.format(themes='\n'.join(f'- {t}' for t in THEMES)),
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=[{
            'role': 'user',
            'content': (
                f"Titre : {title}\n\n"
                f"Texte de l'article :\n{body}\n\n"
                "Donne les thèmes."
            ),
        }],
    )
    text = ''.join(b.text for b in response.content if b.type == 'text')

    by_lower = {t.lower(): t for t in THEMES}
    picked = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip('-•*0123456789. ').strip()
        canonical = by_lower.get(cleaned.lower())
        if canonical and canonical not in picked:
            picked.append(canonical)
    return picked[:3]


def generate_social_summary(title, content_html):
    """Return the AI-written social one-liner (first person, for X), or None
    when the API key is missing."""
    return _generate(
        SOCIAL_SYSTEM_PROMPT, "Écris la phrase pour X.", title, content_html,
    )


THEME_SYSTEM_PROMPT = """Tu rédiges, pour le site de Bernard Poignant — homme \
politique français, socialiste, ancien maire de Quimper, ancien conseiller de \
François Hollande — la présentation d'un thème du blog.

On te donne le nom du thème et la liste des articles qui y sont classés (titre \
et résumé). Tu écris le court texte qui présente ce thème sur la page qui les \
rassemble.

Règles :
- Deux à trois phrases, 60 mots maximum.
- À la troisième personne, comme une présentation éditoriale — ce n'est pas \
Bernard Poignant qui parle, c'est le site qui présente ce qu'il écrit.
- Dis ce qui est réellement abordé dans ces articles, concrètement. Pas de \
généralités sur le thème en soi : quelqu'un qui lit doit comprendre l'angle.
- Un français clair et soigné, sans emphase ni superlatif.
- N'invente aucun fait, aucune position, aucun nom qui ne soit dans les textes.
- Pas de titre, pas de liste, pas de guillemets autour de la réponse.
Réponds uniquement par le texte de présentation."""


def generate_theme_description(theme_name, articles):
    """Describe what a theme actually covers, from the articles filed under it.

    `articles` is a list of (title, summary) pairs. Returns None when the API
    key is missing and '' when the theme has nothing filed under it — an
    undescribed theme is fine, an invented description is not.
    """
    key = _api_key()
    if not key:
        return None
    lignes = [f"- {t} : {s or '(pas de résumé)'}" for t, s in articles if t]
    if not lignes:
        return ''

    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=[{
            'type': 'text',
            'text': THEME_SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=[{
            'role': 'user',
            'content': (
                f"Thème : {theme_name}\n\n"
                f"Articles classés sous ce thème :\n" + "\n".join(lignes) +
                "\n\nRédige la présentation de ce thème."
            ),
        }],
    )
    return _clean(''.join(b.text for b in response.content if b.type == 'text'))
