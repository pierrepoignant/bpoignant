"""Admin "Tweets" section: engagement figures and replies pulled from X.

Two things are synced, and the whole design is about keeping the API bill flat:

  * **Metrics** — `GET /2/tweets` accepts 100 ids per call, so likes / views /
    reply counts for every article cost exactly one request.
  * **Replies** — via the mentions timeline rather than one
    `search/recent?conversation_id:…` per tweet. A reply to Bernard mentions
    him by construction, so a single call returns new replies across every
    tweet whatever its age, and `since_id` keeps each run to what arrived
    since the last one. (`search/recent` only reaches back seven days, so the
    per-conversation approach costs one call per tweet *and* goes blind on
    anything older than a week.)

That is ~2 calls a day, flat, however many articles exist — which is why there
is no tiered "old tweets less often" schedule.

New replies are e-mailed once, to the same admins that get comment alerts.
"""

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import (
    Blueprint, current_app, flash, has_request_context, redirect,
    render_template, request, url_for,
)

from auth import admin_required
from init_db import db
from articles.models import Article
from settings.models import get_config, set_config
from tweets.models import AccountSnapshot, TweetReply

log = logging.getLogger(__name__)

admin_tweets_bp = Blueprint(
    'admin_tweets', __name__, url_prefix='/admin/tweets', template_folder='templates'
)

# Config keys: the mentions cursor and the cached numeric account id.
KEY_SINCE_ID = 'x_mentions_since_id'
KEY_ACCOUNT_ID = 'x_account_id'
KEY_LAST_SYNC = 'x_last_sync_at'


@contextmanager
def _external_urls():
    """Let `url_for(_external=True)` work from the CronJob.

    The digest is built both from a web request (the "Rafraîchir" button) and
    from `poll_x.py`, which has an app context but no request — and Flask
    can't build absolute URLs there without SERVER_NAME. Setting SERVER_NAME
    globally would make the app reject requests whose Host doesn't match
    (it's served on both bernardpoignant.fr and www), so we push a throwaway
    request context only when there isn't a real one.
    """
    if has_request_context():
        yield
    else:
        base = os.environ.get('SITE_BASE_URL', 'https://bernardpoignant.fr')
        with current_app.test_request_context(base_url=base):
            yield


def _parse_x_time(value):
    """X returns ISO 8601 in UTC ('…Z'); we store naive UTC like the rest of
    the app."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return None


def _account_id():
    """Numeric id of the X account, cached in the config table so the daily
    run doesn't spend a call rediscovering it."""
    import x

    cached = (get_config(KEY_ACCOUNT_ID) or '').strip()
    if cached:
        return cached
    resolved = x.account_id()
    set_config(KEY_ACCOUNT_ID, resolved)
    db.session.commit()
    return resolved


def sync_account():
    """Record today's follower figures. Returns the snapshot row.

    Also caches the account id from the same response, so the mentions
    timeline doesn't need its own call to discover it.
    """
    import x

    info = x.fetch_account()
    if not (get_config(KEY_ACCOUNT_ID) or '').strip():
        set_config(KEY_ACCOUNT_ID, info['id'])

    today = datetime.utcnow().date()
    snap = AccountSnapshot.query.filter_by(day=today).first()
    if snap is None:
        snap = AccountSnapshot(day=today)
        db.session.add(snap)
    # Re-running the sync the same day refreshes the row rather than adding one.
    snap.followers = info['followers']
    snap.following = info['following']
    snap.tweets = info['tweets']
    snap.listed = info['listed']

    db.session.commit()
    return snap


def follower_trend():
    """Latest follower count plus how it moved, for the admin header.

    Returns (latest_snapshot, {'1': delta_since_yesterday, '7': …, '30': …}),
    with a delta left out when there's no snapshot old enough to compare
    against — a made-up zero would read as "no growth" rather than "no data".
    """
    latest = AccountSnapshot.query.order_by(AccountSnapshot.day.desc()).first()
    if latest is None or latest.followers is None:
        return None, {}

    deltas = {}
    for days in (1, 7, 30):
        cutoff = latest.day - timedelta(days=days)
        past = (
            AccountSnapshot.query
            .filter(AccountSnapshot.day <= cutoff, AccountSnapshot.followers.isnot(None))
            .order_by(AccountSnapshot.day.desc())
            .first()
        )
        if past is not None:
            deltas[str(days)] = latest.followers - past.followers
    return latest, deltas


# The schedule is expressed in Paris time, so "already posted today" has to be
# judged in Paris time too.
PARIS = ZoneInfo('Europe/Paris')


def _paris_day(dt_utc):
    """Calendar date in Paris for a naive-UTC timestamp."""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(PARIS).date()


def already_posted_today():
    """True when something already went out today, Paris time.

    This replaced a "≥ 23h since the last post" window, which looked
    equivalent and wasn't. The window is measured from the *last post*, not
    from the schedule, so any post later in the day than the 09:30 slot pushed
    the next day's run under the threshold: a manual share at 11:56 left the
    following 09:30 run measuring 21h33 and skipping the day entirely. No
    threshold fixes that — lower it enough to survive a late-morning share and
    it stops suppressing an evening one.

    A calendar day is what "one article per day" actually means, and it can't
    drift no matter when a manual share happens.
    """
    last = last_post_at()
    return last is not None and _paris_day(last) == _paris_day(datetime.utcnow())


def next_article_to_post():
    """The oldest published article never shared on X, or None when the
    backlog is empty."""
    return (
        Article.query
        .filter(Article.published == True,  # noqa: E712 — SQL, not Python truthiness
                Article.x_posted_at.is_(None))
        .order_by(Article.created_at.asc())
        .first()
    )


def last_post_at():
    """When anything was last shared on X, automatic or by hand."""
    from sqlalchemy import func
    return db.session.query(func.max(Article.x_posted_at)).scalar()


def auto_post():
    """Share the oldest never-posted article, unless something already went
    out today (Paris time). Returns a dict describing what happened — `status` is one of
    posted / too_soon / nothing_to_post / not_configured / failed.

    Deliberately posts at most one article per run: the point is a steady drip
    through the backlog, not a burst that reads as spam.
    """
    import x

    if not x.is_configured():
        return {'status': 'not_configured'}

    if already_posted_today():
        last = last_post_at()
        return {'status': 'too_soon', 'last': last,
                'elapsed': datetime.utcnow() - last}

    article = next_article_to_post()
    if article is None:
        return {'status': 'nothing_to_post'}

    with _external_urls():
        url = x.share_url(url_for('articles.public_show', slug=article.slug, _external=True))
    text = x.compose_article_tweet(article.title, article.tweet_summary, url)

    ok, detail = x.post_tweet(text)
    if not ok:
        log.error("auto_post failed for article %s: %s", article.id, detail)
        return {'status': 'failed', 'article': article, 'detail': detail}

    article.x_posted_at = datetime.utcnow()
    article.x_post_id = str(detail) if detail else None
    db.session.commit()
    log.info("auto_post: article %s partagé (%s)", article.id, article.x_post_url)
    return {'status': 'posted', 'article': article, 'text': text}


def sync_metrics():
    """Refresh like / view / reply counts for everything we've tweeted —
    articles and the TikTok clips re-posted to X alike.

    Both go into one call: fetch_metrics batches by 100 ids, and the two sets
    together stay well inside a single batch, so covering the video tweets
    costs no extra quota.

    Returns the number of rows updated.
    """
    import x
    from tiktok.models import TikTokPost

    articles = Article.query.filter(Article.x_post_id.isnot(None)).all()
    clips = TikTokPost.query.filter(TikTokPost.x_post_id.isnot(None)).all()
    if not articles and not clips:
        return 0

    by_post_id = {a.x_post_id: a for a in articles}
    by_post_id.update({c.x_post_id: c for c in clips})
    metrics = x.fetch_metrics(list(by_post_id))

    now = datetime.utcnow()
    updated = 0
    for post_id, m in metrics.items():
        row = by_post_id.get(post_id)
        if row is None:
            continue
        # Articles and clips carry the same five column names, so one loop
        # serves both.
        row.x_like_count = m['likes']
        row.x_view_count = m['views']
        row.x_reply_count = m['replies']
        row.x_retweet_count = m['retweets']
        row.x_metrics_at = now
        updated += 1

    db.session.commit()
    return updated


def sync_replies():
    """Pull new replies from the mentions timeline and store the ones that
    belong to an article tweet. Returns the list of newly-created rows."""
    import x

    account = _account_id()
    since_id = (get_config(KEY_SINCE_ID) or '').strip() or None
    mentions, newest_id = x.fetch_mentions(account, since_id=since_id)

    # conversation_id of a reply is the root tweet — i.e. our x_post_id.
    by_post_id = {
        a.x_post_id: a
        for a in Article.query.filter(Article.x_post_id.isnot(None)).all()
    }

    created = []
    for m in mentions:
        article = by_post_id.get(m['conversation_id'])
        if article is None:
            # A mention that isn't a reply to one of our article tweets.
            continue
        if m['author_id'] == account:
            # Bernard answering in his own thread.
            continue
        if TweetReply.query.filter_by(reply_id=m['id']).first():
            continue
        reply = TweetReply(
            reply_id=m['id'],
            article_id=article.id,
            author_username=m['author_username'][:80] or None,
            author_name=m['author_name'][:150] or None,
            content=m['text'],
            posted_at=_parse_x_time(m['created_at']),
        )
        db.session.add(reply)
        created.append(reply)

    # Advance the cursor even when nothing matched an article: those mentions
    # have been seen, and re-reading them tomorrow would just cost credits.
    if newest_id:
        set_config(KEY_SINCE_ID, str(newest_id))

    db.session.commit()
    return created


def notify_admins_of_replies(replies):
    """E-mail the admins one digest for all un-notified replies. Same
    recipients as the comment alerts: every admin user with an address.
    Returns the number of messages sent."""
    from auth.models import User
    from mail import is_configured as mail_is_configured, send_email

    if not replies:
        return 0
    if not mail_is_configured():
        log.info("x-replies digest skipped — mail not configured")
        return 0

    recipients = User.query.filter(User.is_admin == True, User.email.isnot(None)).all()
    if not recipients:
        log.info("x-replies digest skipped — no admin has an e-mail")
        return 0

    # Group by article so the digest reads like a conversation list.
    by_article = {}
    for reply in replies:
        by_article.setdefault(reply.article, []).append(reply)
    groups = [
        (article, sorted(rs, key=lambda r: r.posted_at or r.fetched_at))
        for article, rs in by_article.items()
    ]

    n = len(replies)
    subject = (
        f"{n} nouvelle réponse sur X" if n == 1 else f"{n} nouvelles réponses sur X"
    )
    with _external_urls():
        admin_url = url_for('admin_tweets.list_tweets', _external=True)
        site_url = url_for('articles.public_list', _external=True)

    sent = 0
    for admin in recipients:
        html = render_template(
            'email/x_replies_digest.html',
            groups=groups,
            total=n,
            admin_url=admin_url,
            site_url=site_url,
            site_name=current_app.config['SITE_NAME'],
            site_tagline=current_app.config['SITE_TAGLINE'],
        )
        if send_email(to_email=admin.email, to_name=admin.username,
                      subject=subject, html=html, categories=['x-replies']):
            sent += 1

    if sent:
        now = datetime.utcnow()
        for reply in replies:
            reply.notified_at = now
        db.session.commit()
    return sent


def poll(notify=True):
    """One full sync: metrics, then replies, then the digest. Returns a
    summary dict. Raises x.XError when the API is unreachable — the caller
    decides whether that's a flash message or a non-zero exit code."""
    # Account first: it caches the account id that sync_replies needs, so a
    # cold start doesn't spend a separate call discovering it.
    snap = sync_account()
    updated = sync_metrics()
    created = sync_replies()
    mailed = notify_admins_of_replies(created) if notify else 0

    set_config(KEY_LAST_SYNC, datetime.utcnow().isoformat(timespec='seconds'))
    db.session.commit()
    return {'metrics': updated, 'replies': len(created), 'emails': mailed,
            'followers': snap.followers}


# ─── ADMIN ──────────────────────────────────────────────────

@admin_tweets_bp.route('/')
@admin_required
def list_tweets():
    import x

    articles = (
        Article.query
        .filter(Article.x_post_id.isnot(None))
        .order_by(Article.x_posted_at.desc())
        .all()
    )
    replies_by_article = {}
    for reply in TweetReply.query.order_by(TweetReply.posted_at.desc()).all():
        replies_by_article.setdefault(reply.article_id, []).append(reply)

    latest_snapshot, follower_deltas = follower_trend()
    # What the daily automation will do next, so it isn't a black box.
    pending = (
        Article.query
        .filter(Article.published == True,  # noqa: E712
                Article.x_posted_at.is_(None))
        .count()
    )

    # Articles et clips dans une seule chronologie : ce qui est parti sur X un
    # mardi est parti un mardi, quelle qu'en soit la nature, et les lire dans
    # deux listes séparées obligeait à recoller les dates de tête.
    from tiktok.models import TikTokPost
    clips = (
        TikTokPost.query
        .filter(TikTokPost.x_post_id.isnot(None))
        .order_by(TikTokPost.x_posted_at.desc())
        .all()
    )

    entries = [{'kind': 'article', 'obj': a, 'posted_at': a.x_posted_at,
                'url': a.x_post_url, 'title': a.title,
                'replies': replies_by_article.get(a.id, [])}
               for a in articles]
    entries += [{'kind': 'clip', 'obj': c, 'posted_at': c.x_posted_at,
                 'url': c.x_url, 'title': c.title, 'replies': []}
                for c in clips]
    # datetime.min pour les rares lignes sans date : elles finissent en bas
    # plutôt que de faire échouer le tri sur une comparaison avec None.
    entries.sort(key=lambda e: e['posted_at'] or datetime.min, reverse=True)

    return render_template(
        'tweets_admin_list.html',
        articles=articles,
        clips=clips,
        entries=entries,
        snapshot=latest_snapshot,
        follower_deltas=follower_deltas,
        next_to_post=next_article_to_post(),
        pending_count=pending,
        replies_by_article=replies_by_article,
        x_configured=x.is_configured(),
        last_sync=get_config(KEY_LAST_SYNC),
        total_replies=TweetReply.query.count(),
    )


@admin_tweets_bp.route('/test', methods=['POST'])
@admin_required
def x_test():
    """Non-destructive check that the X credentials are wired and can post.

    Lives here rather than with the articles: it tests the connection, not any
    particular article."""
    from x import is_configured, verify_credentials

    if not is_configured():
        flash("X n'est pas configuré — les 4 clés BPOIGNANT_X__… sont absentes de l'environnement.", 'danger')
        return redirect(url_for('admin_tweets.list_tweets'))

    ok, info = verify_credentials()
    if not ok:
        flash(f"Échec de la connexion à X : {info.get('error')}", 'danger')
    else:
        level = info.get('access_level') or 'inconnu'
        handle = info.get('screen_name') or '?'
        if 'write' in level:
            flash(f"Connexion X OK — @{handle} (accès : {level}). Prêt à publier.", 'success')
        else:
            flash(
                f"Connexion X OK pour @{handle}, mais l'accès est « {level} » (lecture seule). "
                "Passez l'app en « Lecture et écriture » puis régénérez le jeton d'accès.",
                'warning',
            )
    return redirect(url_for('admin_tweets.list_tweets'))


@admin_tweets_bp.route('/refresh', methods=['POST'])
@admin_required
def refresh():
    """Manual "rafraîchir tout" — the same sync the daily cron runs."""
    import x

    if not x.is_configured():
        flash("X n'est pas configuré (clés API manquantes).", 'danger')
        return redirect(url_for('admin_tweets.list_tweets'))

    try:
        result = poll()
    except x.XError as exc:
        flash(f"Échec de la synchronisation X : {exc}", 'danger')
        return redirect(url_for('admin_tweets.list_tweets'))

    msg = f"{result['followers']} abonné(s), {result['metrics']} tweet(s) mis à jour"
    if result['replies']:
        msg += f", {result['replies']} nouvelle(s) réponse(s)"
        if result['emails']:
            msg += f" — digest envoyé à {result['emails']} admin(s)"
    else:
        msg += ", aucune nouvelle réponse"
    flash(msg + ".", 'success')
    return redirect(url_for('admin_tweets.list_tweets'))


# --------------------------------------------------------------------------
# Statistiques par période
# --------------------------------------------------------------------------

def _period_start(moment, period):
    """The Monday of the week, or the first of the month, containing `moment`."""
    day = moment.date()
    if period == 'month':
        return day.replace(day=1)
    return day - timedelta(days=day.weekday())


MOIS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet',
        'août', 'septembre', 'octobre', 'novembre', 'décembre']


def _period_label(start, period):
    if period == 'month':
        return f'{MOIS[start.month - 1]} {start.year}'
    return f'semaine du {start.strftime("%d/%m/%Y")}'


def _period_short(start, period):
    """Axis label — the long form does not fit under a bar."""
    if period == 'month':
        return f'{MOIS[start.month - 1][:4]}. {start.strftime("%y")}'
    return start.strftime('%d/%m')


# Par défaut on n'affiche qu'une fenêtre récente : le compte TikTok a une
# première vie en 2022 dont les chiffres écrasent tout le reste, et une barre
# calculée sur ce pic rend les semaines actuelles invisibles.
DEFAULT_PERIODS = 12


def platform_stats(period='week', limit=DEFAULT_PERIODS):
    """Totals per period for X and for TikTok, newest period first.

    Grouped by the date a post went out, not by when the views arrived: the
    figures we hold are lifetime counters read at the last sync, and nothing
    records how they grew. So a row answers "what the posts published that week
    have drawn since" — which means the most recent periods are necessarily
    lower, their posts having had less time. The page says so; without that the
    last bar reads as a collapse in reach.
    """
    from tiktok.models import TikTokPost

    if period not in ('week', 'month'):
        period = 'week'

    def blank():
        return {'posts': 0, 'views': 0, 'likes': 0, 'replies': 0, 'shares': 0}

    x_buckets, tt_buckets = {}, {}

    # `boosted` décrit une promotion payée sur TikTok : elle ne change rien à
    # l'audience du même clip sur X, dont les chiffres restent organiques. Le
    # filtre ne s'applique donc qu'aux totaux TikTok, plus bas.
    articles = Article.query.filter(Article.x_posted_at.isnot(None)).all()
    clips = TikTokPost.query.filter(TikTokPost.x_post_id.isnot(None)).all()

    for row in articles + clips:
        start = _period_start(row.x_posted_at, period)
        b = x_buckets.setdefault(start, blank())
        b['posts'] += 1
        b['views'] += row.x_view_count or 0
        b['likes'] += row.x_like_count or 0
        b['replies'] += row.x_reply_count or 0
        b['shares'] += row.x_retweet_count or 0

    for clip in (TikTokPost.query
                 .filter(TikTokPost.posted_at.isnot(None),
                         TikTokPost.boosted.is_(False))
                 .all()):
        start = _period_start(clip.posted_at, period)
        b = tt_buckets.setdefault(start, blank())
        b['posts'] += 1
        b['views'] += clip.views or 0
        b['likes'] += clip.likes or 0
        b['replies'] += clip.comments_count or 0
        b['shares'] += clip.shares or 0

    def rows(buckets):
        out = []
        keys = sorted(buckets, reverse=True)
        if limit:
            keys = keys[:limit]
        # Le pic est celui des lignes montrées, pas de tout l'historique :
        # sinon la barre la plus longue est hors écran et les autres sont plates.
        peak = max((buckets[k]['views'] for k in keys), default=0)
        for start in keys:
            b = dict(buckets[start])
            b['start'] = start
            b['label'] = _period_label(start, period)
            b['short'] = _period_short(start, period)
            # Largeur de barre relative au pic, pour lire la série d'un coup
            # d'œil sans dépendre d'une bibliothèque de graphiques.
            b['bar'] = round(100 * b['views'] / peak) if peak else 0
            b['per_post'] = round(b['views'] / b['posts']) if b['posts'] else 0
            out.append(b)
        return out

    excluded = TikTokPost.query.filter(TikTokPost.boosted.is_(True)).count()
    return {'x': rows(x_buckets), 'tiktok': rows(tt_buckets), 'period': period,
            'total_periods': max(len(x_buckets), len(tt_buckets)),
            'excluded': excluded}


@admin_tweets_bp.route('/stats')
@admin_required
def stats():
    period = request.args.get('period', 'week')
    everything = request.args.get('tout') == '1'
    data = platform_stats(period, limit=None if everything else DEFAULT_PERIODS)
    return render_template('tweets_admin_stats.html',
                           x_rows=data['x'], tt_rows=data['tiktok'],
                           period=data['period'], everything=everything,
                           total_periods=data['total_periods'],
                           excluded=data['excluded'],
                           shown=DEFAULT_PERIODS)
