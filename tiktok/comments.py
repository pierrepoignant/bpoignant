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


_VIDEO_ID = re.compile(r'/video/(\d+)')


def _video_id(url):
    m = _VIDEO_ID.search(url or '')
    return m.group(1) if m else None


def _upsert(post, items, maintenant):
    """Store what the scraper returned for one clip. Returns (nouveaux, revus)."""
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
    return nouveaux, revus


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
    nouveaux, revus = _upsert(post, items, datetime.utcnow())
    db.session.commit()
    return nouveaux, revus


# Combien d'adresses par exécution Apify : assez pour ne pas payer un démarrage
# par clip, pas trop pour rester sous le délai de l'appel synchrone.
LOT_APIFY = 8


def sync_many(posts, limit=COMMENTS_PAR_POST, progression=None):
    """Read the comments of many clips in a few actor runs instead of one each.

    The actor takes a list of URLs and tags every comment with the video it
    came from; we route them back by the video id in that URL. Returns
    {post_id: (nouveaux, revus)}.
    """
    import apify
    if not apify.is_configured():
        raise CommentsError("Apify n'est pas configuré — voir Réglages.")
    par_id = {_video_id(p.posted_url): p for p in posts if _video_id(p.posted_url)}
    resultats = {}
    cles = list(par_id)
    for debut in range(0, len(cles), LOT_APIFY):
        lot = cles[debut:debut + LOT_APIFY]
        try:
            items = apify.scrape_comments([par_id[k].posted_url for k in lot], limit=limit)
        except apify.ApifyError as exc:
            raise CommentsError(str(exc)) from exc
        groupes = {}
        for item in items:
            vid = _video_id(item.get('videoWebUrl') or item.get('postUrl')
                            or item.get('videoUrl') or item.get('webVideoUrl') or '')
            if vid in par_id:
                groupes.setdefault(vid, []).append(item)
        maintenant = datetime.utcnow()
        for k in lot:
            resultats[par_id[k].id] = _upsert(par_id[k], groupes.get(k, []), maintenant)
        db.session.commit()
        if progression:
            progression(min(len(cles), debut + LOT_APIFY), len(cles))
    return resultats


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


def suggest_replies(post, comments=None, consigne=None):
    """Draft an answer — or the decision not to — for each comment given.

    Defaults to the visible comments that have no proposal yet. Returns the
    number of comments the model looked at.

    `consigne` is Bernard's steer for a redo — « cite l'exemple du vote sur… »,
    « plus court », « sans ironie ». It is passed on as his instruction, with
    one guard: a fact he asks for that the model cannot vouch for is named in
    the note, not invented into the reply.
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
        demande = f"{contexte}\n\nCommentaires :\n{liste}"
        if consigne:
            demande += (
                f"\n\nConsigne de Bernard pour cette réponse : {consigne.strip()[:500]}\n"
                "Suis-la, et réponds (`repondre` à true) même si le commentaire "
                "aurait pu être laissé sans réponse. Si la consigne demande un fait "
                "précis — un vote, une date, une citation — que tu ne peux pas "
                "garantir exact, ne l'invente pas : rédige la réponse sans ce fait et "
                "dis dans la note ce qu'il faudrait vérifier.")
        reponse = client.messages.create(
            model=MODEL, max_tokens=4000,
            system=[{'type': 'text', 'text': VOIX,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': demande}],
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
    """Comments still on the table, newest first."""
    return (post.tiktok_comments.filter(TikTokComment.hidden.is_(False))
            .order_by(TikTokComment.posted_at.desc(), TikTokComment.id.desc()).all())


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


# ─── Tous les clips d'un coup ───────────────────────────────

import threading

ETAT = {'en_cours': False}
_VERROU = threading.Lock()


def etat_global():
    return dict(ETAT)


def refresh_all(app, posts_ids):
    """Read and draft for every clip, in the background, with a visible state.

    One click on the list, then minutes of work: the page polls `ETAT` and
    says where it has got to. A second click while it runs is refused.
    """
    with _VERROU:
        if ETAT.get('en_cours'):
            return False
        ETAT.clear()
        ETAT.update(en_cours=True, etape="Lecture des commentaires sur TikTok…",
                    faits=0, total=len(posts_ids), nouveaux=0, revus=0, relus=0,
                    erreurs=[], demarre=datetime.utcnow().isoformat(timespec='seconds'))

    def _travail():
        with app.app_context():
            try:
                posts = [p for p in TikTokPost.query.filter(TikTokPost.id.in_(posts_ids)).all()
                         if p.posted_url]
                def avance(faits, total):
                    ETAT.update(faits=faits, total=total)
                resultats = sync_many(posts, progression=avance)
                ETAT.update(nouveaux=sum(n for n, _ in resultats.values()),
                            revus=sum(r for _, r in resultats.values()),
                            etape="Le moteur relit les commentaires…", faits=0, total=len(posts))
                for i, p in enumerate(posts, 1):
                    try:
                        ETAT['relus'] += suggest_replies(p)
                        if (p.comments_count or 0) < sum(resultats.get(p.id, (0, 0))):
                            p.comments_count = sum(resultats[p.id])
                        db.session.commit()
                    except CommentsError as exc:
                        db.session.rollback()
                        ETAT['erreurs'].append(f"{p.title[:40]} : {exc}")
                    ETAT.update(faits=i)
                ETAT.update(etape="Terminé")
            except Exception as exc:
                db.session.rollback()
                log.exception('lecture globale des commentaires')
                ETAT['erreurs'].append(str(exc)[:200])
                ETAT.update(etape="Interrompu")
            finally:
                ETAT.update(en_cours=False, fini=datetime.utcnow().isoformat(timespec='seconds'))

    threading.Thread(target=_travail, name='tiktok-commentaires', daemon=True).start()
    return True
