"""Commentaires TikTok : les lire, proposer une réponse, ne plus les montrer.

TikTok ne donne pas les commentaires d'un créateur ordinaire par son API : ils
arrivent par le même scrapeur Apify que les chiffres des posts. Ensuite un
modèle relit chaque commentaire et dit s'il appelle une réponse — un mot gentil
n'en appelle pas, une insulte ou une polémique non plus, une question ou un
désaccord courtois oui — et la rédige quand c'est le cas, dans la voix de
Bernard : brève, avec de l'humour, sans emoji.

Rien ne part de lui-même. La proposition est un brouillon à copier dans
l'application TikTok ; la réponse publiée reste celle de Bernard, sous son nom,
depuis son téléphone.
"""

import json
import logging
import re
from datetime import datetime

from init_db import db
from tiktok.models import TikTokComment, TikTokPost

log = logging.getLogger(__name__)

# Au-delà, le coût du scrapeur monte sans que personne ne lise : cent
# commentaires par clip, c'est déjà plus que ce que Bernard répondra.
COMMENTS_PAR_POST = 100
# Combien de commentaires le modèle relit d'un coup.
LOT_IA = 40

VOIX = """Tu rédiges, à la place de Bernard Poignant, des réponses aux \
commentaires laissés sous ses vidéos TikTok. Bernard Poignant est un homme \
politique français, socialiste, ancien maire de Quimper, ancien député européen \
et ancien conseiller de François Hollande. Il a plus de soixante-dix ans, une \
culture politique profonde, et parle un français net et soigné.

Sa manière : il répond en son nom, à la première personne, brièvement — deux \
phrases au plus, souvent une — et avec humour. Un humour de vieux Breton qui a \
tout vu : la pointe est fine, jamais méchante, souvent tournée contre lui-même \
ou contre l'absurdité de la situation plutôt que contre la personne. Il ne \
tutoie personne, ne cède ni à l'insulte ni à la flatterie, et ne s'abaisse \
jamais. Devant une question, il répond, avec un trait d'esprit si le sujet le \
permet. Devant un désaccord courtois, il tient sa position en une phrase et \
salue l'autre. Devant une contre-vérité, il rectifie calmement, avec un fait — \
et un sourire.

Ce qui n'appelle PAS de réponse, et tu le dis :
- les commentaires haineux, méprisants ou insultants, quel que soit leur objet ;
- les polémiques : tout ce qui cherche la bagarre, l'attaque personnelle, la \
provocation, le procès d'intention, ou un sujet inflammable où répondre ne \
ferait qu'alimenter le feu ;
- les compliments et encouragements simples (« bravo », « merci », un cœur) ;
- les commentaires hors sujet, incompréhensibles, les emojis seuls, la publicité.
Répondre à tout ferait de lui un robot ; il répond quand la réponse apporte \
quelque chose, ou fait sourire.

Tu reçois une liste de commentaires avec, pour chacun, un identifiant. Tu \
réponds UNIQUEMENT par un tableau JSON, un objet par commentaire, dans l'ordre \
reçu :
[{"id": "…", "repondre": true, "reponse": "…", "note": "…"}, …]
- `repondre` : false si le commentaire n'appelle pas de réponse.
- `reponse` : la réponse de Bernard, vide si `repondre` est false. Aucun emoji, \
aucune majuscule d'insistance, aucun hashtag, pas de « Bonjour ». 280 caractères au plus.
- `note` : trois à huit mots disant pourquoi (« compliment », « question sur \
les retraites », « haineux », « polémique », « désaccord courtois »).
N'invente aucune position que Bernard n'a pas exprimée dans la vidéo : quand \
la réponse demanderait de le faire, réponds sur le terrain de la méthode ou du \
fait, ou marque `repondre` à false avec la note « demanderait une prise de \
position »."""


class CommentsError(RuntimeError):
    pass


# ─── Lecture ────────────────────────────────────────────────

def _quand(valeur):
    from tiktok import _parse_time
    return _parse_time(valeur)


def sync_comments(post, limit=COMMENTS_PAR_POST):
    """Read the clip's comments and store the ones we had not seen.

    Returns (nouveaux, revus). Comments already answered or dismissed keep
    their state: a re-read is about what has been said since, not a reset.
    """
    import apify
    if not post.posted_url:
        raise CommentsError("Ce clip n'a pas d'adresse TikTok : impossible d'en lire les commentaires.")
    if not apify.is_configured():
        raise CommentsError("Apify n'est pas configuré — voir Réglages.")

    try:
        items = apify.scrape_comments([post.posted_url], limit=limit)
    except apify.ApifyError as exc:
        raise CommentsError(str(exc)) from exc

    maintenant = datetime.utcnow()
    existants = {c.comment_id: c for c in post.tiktok_comments.all()}
    nouveaux = revus = 0
    for item in items:
        cid = str(item.get('cid') or item.get('id') or '').strip()
        texte = (item.get('text') or '').strip()
        if not cid or not texte:
            continue
        # Les réponses à d'autres commentaires ne sont pas adressées à Bernard.
        if item.get('repliesToId') or item.get('parentCommentId'):
            continue
        ligne = existants.get(cid)
        if ligne is None:
            ligne = TikTokComment(post_id=post.id, comment_id=cid)
            db.session.add(ligne)
            existants[cid] = ligne
            nouveaux += 1
        else:
            revus += 1
        ligne.author = (item.get('uniqueId') or item.get('username')
                        or (item.get('user') or {}).get('uniqueId') or ligne.author or '')[:120]
        ligne.author_id = str(item.get('uid') or (item.get('user') or {}).get('id') or ligne.author_id or '')[:64] or None
        ligne.text = texte
        ligne.likes = item.get('diggCount') if item.get('diggCount') is not None else ligne.likes
        ligne.replies_count = (item.get('replyCommentTotal')
                               if item.get('replyCommentTotal') is not None else ligne.replies_count)
        ligne.posted_at = _quand(item.get('createTimeISO') or item.get('createTime')) or ligne.posted_at
        ligne.scraped_at = maintenant
    db.session.commit()
    return nouveaux, revus


# ─── Propositions ───────────────────────────────────────────

def _contexte_video(post):
    """What the model needs to know about the clip to answer under it."""
    morceaux = [f"Titre : {post.title}"]
    if post.banner_title:
        morceaux.append(f"Bandeau : {post.banner_title}")
    if post.caption:
        morceaux.append(f"Texte publié : {post.caption[:800]}")
    if post.transcript:
        morceaux.append(f"Ce que Bernard dit dans la vidéo :\n{post.transcript[:3500]}")
    return '\n'.join(morceaux)


def _tableau_json(texte):
    """The model is asked for bare JSON; take the first array in what it sent."""
    debut = texte.find('[')
    fin = texte.rfind(']')
    if debut < 0 or fin <= debut:
        raise CommentsError("Réponse du modèle sans tableau JSON.")
    try:
        return json.loads(texte[debut:fin + 1])
    except ValueError as exc:
        raise CommentsError(f"Réponse du modèle illisible : {exc}") from exc


def suggest_replies(post, comments=None):
    """Draft an answer — or the decision not to — for each comment given.

    Defaults to the visible comments that have no proposal yet. Returns the
    number of comments the model looked at.
    """
    from articles.ai_summary import _api_key, MODEL
    key = _api_key()
    if not key:
        raise CommentsError("Clé Anthropic absente : aucune proposition possible.")

    if comments is None:
        comments = (post.tiktok_comments
                    .filter(TikTokComment.hidden.is_(False),
                            TikTokComment.suggested_at.is_(None))
                    .order_by(TikTokComment.posted_at.desc()).all())
    comments = [c for c in comments if (c.text or '').strip()]
    if not comments:
        return 0

    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=90.0, max_retries=1)
    contexte = _contexte_video(post)
    traites = 0
    for debut in range(0, len(comments), LOT_IA):
        lot = comments[debut:debut + LOT_IA]
        liste = '\n'.join(
            f"- id {c.comment_id} · @{c.author or 'inconnu'}"
            f"{f' · {c.likes} j’aime' if c.likes else ''} : {c.text.strip()[:600]}"
            for c in lot)
        reponse = client.messages.create(
            model=MODEL, max_tokens=4000,
            system=[{'type': 'text', 'text': VOIX,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content':
                       f"{contexte}\n\nCommentaires :\n{liste}"}],
        )
        texte = ''.join(b.text for b in reponse.content if b.type == 'text')
        par_id = {str(o.get('id', '')).strip(): o for o in _tableau_json(texte)
                  if isinstance(o, dict)}
        maintenant = datetime.utcnow()
        for c in lot:
            o = par_id.get(c.comment_id)
            if o is None:
                # Le modèle a sauté celui-là : on le laisse sans proposition
                # plutôt que d'inventer, mais on note le passage.
                c.suggestion_note = 'non traité par le modèle'
                c.suggested_reply = None
            elif o.get('repondre'):
                c.suggested_reply = re.sub(r'\s+', ' ', str(o.get('reponse') or '')).strip()[:600] or None
                c.suggestion_note = str(o.get('note') or '')[:200] or None
            else:
                c.suggested_reply = None
                c.suggestion_note = (str(o.get('note') or '') or 'sans réponse')[:200]
            c.suggested_at = maintenant
            traites += 1
        db.session.commit()
    return traites


def refresh(post):
    """Read, then propose. Returns a short human sentence about what happened."""
    nouveaux, revus = sync_comments(post)
    if not post.comments_count or post.comments_count < nouveaux + revus:
        post.comments_count = nouveaux + revus
    traites = suggest_replies(post)
    db.session.commit()
    return (f"{nouveaux} nouveau(x) commentaire(s), {revus} déjà connu(s) · "
            f"{traites} relu(s) par le moteur.")


def visibles(post):
    """Comments still on the table, those with a proposed reply first."""
    lignes = (post.tiktok_comments.filter(TikTokComment.hidden.is_(False))
              .order_by(TikTokComment.posted_at.desc()).all())
    return sorted(lignes, key=lambda c: (c.suggested_reply is None,
                                         -(c.posted_at or datetime.min).timestamp()))


def compter(post_ids):
    """{post_id: (visibles, avec réponse proposée)} for a set of clips."""
    from sqlalchemy import func, case
    if not post_ids:
        return {}
    rows = (db.session.query(
                TikTokComment.post_id,
                func.count(TikTokComment.id),
                func.sum(case((TikTokComment.suggested_reply.isnot(None), 1), else_=0)))
            .filter(TikTokComment.post_id.in_(list(post_ids)),
                    TikTokComment.hidden.is_(False))
            .group_by(TikTokComment.post_id).all())
    return {pid: (int(n or 0), int(r or 0)) for pid, n, r in rows}
