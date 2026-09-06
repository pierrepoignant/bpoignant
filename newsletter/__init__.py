import csv
import io
import os
import re
import secrets
import threading
from datetime import datetime

from flask import (
    Blueprint, Response, abort, flash, redirect, render_template, request, url_for
)

from init_db import db
from newsletter.models import (
    Subscriber, Campaign, Delivery, EmailEvent, Announcement, AnnouncementDelivery,
    MinuteSend, MinuteDelivery, GmailContact,
)
from newsletter.antispam import score_signup, is_suspicious, CONFIRM_THRESHOLD
from auth import admin_required
from flask import current_app, render_template, url_for
from flask_login import current_user
from mail import send_email, is_configured as mail_is_configured
import logging

log = logging.getLogger(__name__)

newsletter_bp = Blueprint('newsletter', __name__, url_prefix='/newsletter', template_folder='templates')
admin_subscribers_bp = Blueprint(
    'admin_subscribers', __name__, url_prefix='/admin/subscribers', template_folder='templates'
)
# No url_prefix: the landing page lives at /lettre, a URL short enough to say
# out loud and to put in a promoted tweet.
lettre_bp = Blueprint('lettre', __name__, template_folder='templates')
# Même raison pour /minute : une adresse courte, qu'on peut dire à voix haute.
minute_bp = Blueprint('minute', __name__, template_folder='templates')

admin_sends_bp = Blueprint(
    'admin_sends', __name__, url_prefix='/admin/sends', template_folder='templates'
)


# RFC-ish — good enough for sanity-checking input before storage.
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@lettre_bp.route('/lettre')
def landing():
    """Public landing page for the newsletter.

    Everything factual on it is pulled live — themes, recent articles — so the
    page can't drift out of date the way hand-written marketing copy does. The
    reader quotes are the exception and are real comments, see the template.
    """
    from articles.models import Article, Theme

    recent = (
        Article.query.filter_by(published=True)
        .order_by(Article.created_at.desc())
        .limit(3)
        .all()
    )
    # Themes that actually have published articles behind them, most-used
    # first — a promise the archive can keep.
    themes = sorted(
        (t for t in Theme.query.all() if t.published_articles),
        key=lambda t: len(t.published_articles), reverse=True,
    )[:8]
    total_articles = Article.query.filter_by(published=True).count()
    return render_template('lettre.html', recent=recent, themes=themes,
                           total_articles=total_articles)


@minute_bp.route('/minute')
def minute_landing():
    """La Minute: every clip published since 2026, newest first.

    The 2021-2022 clips are another era of the account — promoted, and about
    an election four years past — so the page starts at 2026, which is also
    where the current run of videos begins.
    """
    from tiktok.models import TikTokPost

    videos = (
        TikTokPost.query
        .filter(TikTokPost.video_url.isnot(None),
                TikTokPost.posted_at >= datetime(2026, 1, 1))
        .order_by(TikTokPost.posted_at.desc())
        .all()
    )
    return render_template('minute_landing.html', videos=videos)


@newsletter_bp.route('/subscribe', methods=['POST'])
def subscribe():
    # Honeypot: the form has a hidden "website" field no human ever sees. Bots
    # fill every field, so a non-empty value means a bot — swallow it silently
    # (fake success, no row created) so it can't tell it was blocked.
    if (request.form.get('website') or '').strip():
        return render_template(
            'subscribe_done.html',
            email=(request.form.get('email') or '').strip(),
            prenom=None, reactivated=False, already=True, pending=False,
        )

    email = (request.form.get('email') or '').strip().lower()
    prenom = (request.form.get('prenom') or '').strip() or None
    nom = (request.form.get('nom') or '').strip() or None
    ville = (request.form.get('ville') or '').strip() or None
    redirect_to = request.form.get('next') or url_for('articles.public_list')

    if not _EMAIL_RE.match(email):
        flash("Adresse e-mail invalide.", 'danger')
        return redirect(redirect_to + '#newsletter')

    score, _reasons = score_signup(email, prenom, nom, ville)
    # A risky-looking signup must confirm by e-mail (double opt-in); real
    # people score 0 and stay instant. If we can't send mail we don't lock
    # anyone out — auto-confirm instead.
    needs_confirmation = score >= CONFIRM_THRESHOLD and mail_is_configured()

    existing = Subscriber.query.filter_by(email=email).first()
    if existing:
        # Update name/city in case the visitor filled them in this time.
        existing.prenom = prenom or existing.prenom
        existing.nom = nom or existing.nom
        existing.ville = ville or existing.ville
        reactivated = existing.unsubscribed_at is not None
        if reactivated:
            existing.unsubscribed_at = None
            existing.subscribed_at = datetime.utcnow()
        pending = existing.confirmed_at is None
        db.session.commit()
        if pending:
            _send_confirmation_email(existing)
        return render_template(
            'subscribe_done.html',
            email=existing.email,
            prenom=existing.prenom,
            reactivated=reactivated,
            already=not reactivated and not pending,
            pending=pending,
        )

    sub = Subscriber(
        email=email,
        prenom=prenom,
        nom=nom,
        ville=ville,
        token=secrets.token_urlsafe(24),
        spam_score=score,
        confirmed_at=None if needs_confirmation else datetime.utcnow(),
    )
    db.session.add(sub)
    db.session.commit()
    if needs_confirmation:
        _send_confirmation_email(sub)
    return render_template(
        'subscribe_done.html',
        email=sub.email,
        prenom=sub.prenom,
        reactivated=False,
        already=False,
        pending=needs_confirmation,
    )


def _send_confirmation_email(sub):
    """Send the double opt-in confirmation e-mail. Best-effort — a failure is
    logged, not surfaced (the visitor already saw the 'check your inbox' page,
    and they can re-submit to trigger a resend)."""
    try:
        html = render_template(
            'email/newsletter_confirm.html',
            confirm_url=url_for('newsletter.confirm', token=sub.token, _external=True),
            site_url=url_for('articles.public_list', _external=True),
            site_name=current_app.config['SITE_NAME'],
            site_tagline=current_app.config['SITE_TAGLINE'],
            prenom=sub.prenom,
        )
        send_email(
            to_email=sub.email,
            to_name=sub.display_name,
            subject="Confirmez votre inscription à la newsletter",
            html=html,
        )
    except Exception:
        log.exception("failed to send confirmation e-mail to %s", sub.email)


@newsletter_bp.route('/confirm/<token>')
def confirm(token):
    sub = Subscriber.query.filter_by(token=token).first()
    if sub is None:
        abort(404)
    newly = sub.confirmed_at is None
    if newly:
        sub.confirmed_at = datetime.utcnow()
        # Confirming re-activates a previously unsubscribed address too.
        if sub.unsubscribed_at is not None:
            sub.unsubscribed_at = None
        db.session.commit()
    return render_template('subscribe_confirmed.html', email=sub.email, prenom=sub.prenom, newly=newly)


def _record_engagement(ev, etype):
    """Store one open/click from the Event Webhook. Returns 1 when a row was
    added, 0 otherwise.

    Idempotent on SendGrid's `sg_event_id`: the webhook retries on any non-2xx,
    and without the guard a single retry would inflate every reader's count.
    """
    addr = (ev.get('email') or '').strip().lower()
    if not addr:
        return 0

    sg_id = (ev.get('sg_event_id') or '').strip()[:100] or None
    if sg_id and EmailEvent.query.filter_by(sg_event_id=sg_id).first():
        return 0

    # Which article, if any: newsletter sends carry an `article-<id>` category
    # alongside `newsletter`.
    article_id = None
    cats = ev.get('category') or []
    if isinstance(cats, str):
        cats = [cats]
    for c in cats:
        if isinstance(c, str) and c.startswith('article-'):
            try:
                article_id = int(c.split('-', 1)[1])
            except ValueError:
                pass
            break

    sub = Subscriber.query.filter_by(email=addr).first()
    ts = ev.get('timestamp')
    occurred = datetime.utcfromtimestamp(int(ts)) if ts else datetime.utcnow()

    db.session.add(EmailEvent(
        sg_event_id=sg_id,
        email=addr[:255],
        subscriber_id=(sub.id if sub else None),
        article_id=article_id,
        event=etype,
        url=(ev.get('url') or None) and str(ev.get('url'))[:500],
        occurred_at=occurred,
    ))
    return 1


@newsletter_bp.route('/sendgrid/events', methods=['POST'])
def sendgrid_events():
    """SendGrid Event Webhook receiver. Marks hard bounces / blocks / spam
    reports so those addresses are never e-mailed again.

    If a key is configured (config `sendgrid_webhook_key` or env
    `SENDGRID__WEBHOOK_KEY`), it must be supplied as `?key=…` — otherwise the
    endpoint accepts posts (best-effort) and just logs."""
    from settings.models import get_config
    expected = (get_config('sendgrid_webhook_key') or os.environ.get('SENDGRID__WEBHOOK_KEY') or '').strip()
    if expected and request.args.get('key', '') != expected:
        abort(403)

    events = request.get_json(silent=True)
    if not isinstance(events, list):
        return ('', 204)

    HARD = {'bounce', 'dropped', 'blocked', 'spamreport'}
    ENGAGEMENT = {'open', 'click'}
    changed = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = (ev.get('event') or '').lower()
        if etype in ENGAGEMENT:
            changed += _record_engagement(ev, etype)
            continue
        if etype not in HARD:
            continue
        addr = (ev.get('email') or '').strip().lower()
        if not addr:
            continue
        sub = Subscriber.query.filter_by(email=addr).first()
        if sub is None:
            continue
        if sub.bounced_at is None:
            sub.bounced_at = datetime.utcnow()
        reason = ev.get('reason') or ev.get('type') or etype
        sub.bounce_reason = str(reason)[:255]
        # A spam complaint also unsubscribes them — never contact again.
        if etype == 'spamreport' and sub.unsubscribed_at is None:
            sub.unsubscribed_at = datetime.utcnow()
        changed += 1

    if changed:
        db.session.commit()
    return ('', 204)


@newsletter_bp.route('/unsubscribe/<token>', methods=['GET', 'POST'])
def unsubscribe(token):
    sub = Subscriber.query.filter_by(token=token).first()
    if sub is None:
        abort(404)

    # La lettre concernée vient du lien cliqué : le pied de page de chaque
    # envoi porte la sienne, donc « se désinscrire » retire ce qu'on lisait,
    # pas tout d'un coup.
    liste = request.values.get('liste')
    liste = liste if liste in LISTES else 'lettre'

    if request.method == 'POST':
        now = datetime.utcnow()
        quitte = request.form.getlist('listes') or [liste]
        if request.form.get('tout'):
            quitte = list(LISTES)

        if 'lettre' in quitte and sub.wants_lettre:
            sub.wants_lettre, sub.lettre_unsub_at = False, now
        if 'minute' in quitte and sub.wants_minute:
            sub.wants_minute, sub.minute_unsub_at = False, now
        # Plus rien de coché : c'est un retrait complet, et l'adresse ne doit
        # plus jamais être sollicitée.
        if not sub.wants_lettre and not sub.wants_minute and sub.unsubscribed_at is None:
            sub.unsubscribed_at = now
        db.session.commit()
        return render_template('unsubscribe_done.html', email=sub.email,
                               subscriber=sub, listes=LISTES, quitte=quitte)

    return render_template('unsubscribe_confirm.html', subscriber=sub,
                           liste=liste, listes=LISTES)


# ─── ADMIN ──────────────────────────────────────────────────

LISTES = {
    'lettre': "La lettre de Bernard Poignant",
    'minute': "La Minute",
}


def _mailable_query(liste='lettre'):
    """Active, confirmed, non-bounced subscribers who still want `liste`.

    The list matters: someone who left La Minute is still a reader of la
    lettre, and sending them the wrong one is exactly what they asked us not
    to do.
    """
    q = Subscriber.query.filter(
        Subscriber.unsubscribed_at.is_(None),
        Subscriber.confirmed_at.isnot(None),
        Subscriber.bounced_at.is_(None),
    )
    if liste == 'minute':
        return q.filter(Subscriber.wants_minute.is_(True))
    if liste == 'tous':
        # Une annonce n'appartient à aucune des deux lettres : elle va à toute
        # personne encore abonnée à l'une ou à l'autre.
        return q.filter(db.or_(Subscriber.wants_lettre.is_(True),
                               Subscriber.wants_minute.is_(True)))
    return q.filter(Subscriber.wants_lettre.is_(True))


@admin_subscribers_bp.route('/')
@admin_required
def list_subscribers():
    page = request.args.get('page', 1, type=int) or 1
    # Recherche : la liste dépasse la centaine et retrouver quelqu'un en
    # feuilletant cinquante lignes à la fois n'est pas raisonnable.
    q = (request.args.get('q') or '').strip()
    base = _mailable_query('tous')
    if q:
        motif = f'%{q}%'
        base = base.filter(db.or_(
            Subscriber.email.ilike(motif),
            Subscriber.prenom.ilike(motif),
            Subscriber.nom.ilike(motif),
            Subscriber.ville.ilike(motif),
        ))
    active_pg = (
        base
        .order_by(Subscriber.subscribed_at.desc())
        .paginate(page=max(page, 1), per_page=50, error_out=False)
    )
    # Pending confirmation (double opt-in not yet clicked) — where bot signups
    # pile up and never leave.
    pending = (
        Subscriber.query.filter(
            Subscriber.confirmed_at.is_(None),
            Subscriber.unsubscribed_at.is_(None),
        )
        .order_by(Subscriber.subscribed_at.desc())
        .all()
    )
    bounced = (
        Subscriber.query.filter(Subscriber.bounced_at.isnot(None))
        .order_by(Subscriber.bounced_at.desc())
        .all()
    )
    unsubscribed = (
        Subscriber.query.filter(
            Subscriber.unsubscribed_at.isnot(None),
            Subscriber.bounced_at.is_(None),
        )
        .order_by(Subscriber.unsubscribed_at.desc())
        .all()
    )
    if q:
        # Une recherche qui ne trouve pas quelqu'un parce qu'il s'est
        # désinscrit répond à côté de la question posée.
        motif_bas = q.lower()

        def correspond(sub):
            champs = (sub.email, sub.prenom, sub.nom, sub.ville)
            return any(c and motif_bas in c.lower() for c in champs)

        pending = [s for s in pending if correspond(s)]
        bounced = [s for s in bounced if correspond(s)]
        unsubscribed = [s for s in unsubscribed if correspond(s)]

    # Flag confirmed/active rows that still look like spam (e.g. bots that
    # slipped in before double opt-in existed) so they can be pruned.
    suspicious_ids = {
        s.id for s in _mailable_query().all()
        if is_suspicious(s.email, s.prenom, s.nom, s.ville)
    }

    return render_template(
        'subscribers_admin_list.html',
        active=active_pg.items,
        active_total=active_pg.total,
        pagination=active_pg,
        pending=pending,
        bounced=bounced,
        recoverable_count=len([b for b in bounced if is_recoverable_bounce(b.bounce_reason)]),
        unsubscribed=unsubscribed,
        suspicious_ids=suspicious_ids,
        suspicious_count=len(suspicious_ids),
        q=q,
    )


@admin_sends_bp.route('/')
@admin_required
def list_sends():
    """What went out, across the two letters and the announcements.

    A send is only ever counted once per recipient: Delivery, MinuteDelivery
    and AnnouncementDelivery each carry a unique constraint on their pair, and
    each send filters out anyone already served. So this page reports what
    actually reached people, and the counts cannot double.
    """
    from articles.models import Article
    from tiktok.models import TikTokPost

    articles = {a.id: a for a in Article.query.all()}
    clips = {c.id: c for c in TikTokPost.query.all()}
    annonces = {a.id: a for a in Announcement.query.all()}

    # ── Résumé par envoi, les trois sortes ensemble ────────────────────
    summary = []
    for article_id, n, last in (
            db.session.query(Delivery.article_id, db.func.count(Delivery.id),
                             db.func.max(Delivery.sent_at))
            .group_by(Delivery.article_id).all()):
        art = articles.get(article_id)
        summary.append({
            'kind': 'lettre', 'count': n, 'last': last,
            'title': art.title if art else '(article supprimé)',
            'url': url_for('articles.public_show', slug=art.slug) if art else None,
        })
    for post_id, n, last in (
            db.session.query(MinuteDelivery.post_id, db.func.count(MinuteDelivery.id),
                             db.func.max(MinuteDelivery.sent_at))
            .group_by(MinuteDelivery.post_id).all()):
        clip = clips.get(post_id)
        summary.append({
            'kind': 'minute', 'count': n, 'last': last,
            'title': clip.title if clip else '(clip supprimé)',
            'url': url_for('minute.minute_landing', v=post_id) if clip else None,
        })
    for ann_id, n, last in (
            db.session.query(AnnouncementDelivery.announcement_id,
                             db.func.count(AnnouncementDelivery.id),
                             db.func.max(AnnouncementDelivery.sent_at))
            .group_by(AnnouncementDelivery.announcement_id).all()):
        ann = annonces.get(ann_id)
        summary.append({
            'kind': 'annonce', 'count': n, 'last': last,
            'title': ann.subject if ann else '(message supprimé)',
            'url': url_for('admin_sends.edit_announcement', announcement_id=ann_id) if ann else None,
        })
    summary.sort(key=lambda s: s['last'] or datetime.min, reverse=True)

    # ── Les derniers envois individuels, toutes lettres confondues ─────
    # Trois tables, donc pas de pagination SQL commune : on prend les cent
    # dernières de chacune et on garde les cent plus récentes de l'ensemble.
    recent = []
    for d in Delivery.query.order_by(Delivery.sent_at.desc()).limit(100).all():
        art = articles.get(d.article_id)
        recent.append({'kind': 'lettre', 'sent_at': d.sent_at, 'email': d.email,
                       'title': art.title if art else '(article supprimé)',
                       'url': url_for('articles.public_show', slug=art.slug) if art else None})
    for d in MinuteDelivery.query.order_by(MinuteDelivery.sent_at.desc()).limit(100).all():
        clip = clips.get(d.post_id)
        recent.append({'kind': 'minute', 'sent_at': d.sent_at, 'email': d.email,
                       'title': clip.title if clip else '(clip supprimé)',
                       'url': url_for('minute.minute_landing', v=d.post_id) if clip else None})
    for d in AnnouncementDelivery.query.order_by(AnnouncementDelivery.sent_at.desc()).limit(100).all():
        ann = annonces.get(d.announcement_id)
        recent.append({'kind': 'annonce', 'sent_at': d.sent_at, 'email': d.email,
                       'title': ann.subject if ann else '(message supprimé)', 'url': None})
    recent.sort(key=lambda r: r['sent_at'] or datetime.min, reverse=True)
    recent = recent[:100]

    totaux = {
        'lettre': db.session.query(db.func.count(Delivery.id)).scalar() or 0,
        'minute': db.session.query(db.func.count(MinuteDelivery.id)).scalar() or 0,
        'annonce': db.session.query(db.func.count(AnnouncementDelivery.id)).scalar() or 0,
    }

    return render_template(
        'sends_admin_list.html',
        summary=summary, recent=recent, totaux=totaux,
        total=sum(totaux.values()),
    )


def _parse_import_date(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _parse_import_rows(file_storage, text_blob):
    """Collect {email, prenom, nom, ville, subscribed_at} rows from an
    uploaded CSV and/or a pasted list of e-mails."""
    rows = []

    if file_storage and file_storage.filename:
        raw = file_storage.read()
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            content = raw.decode('latin-1', errors='replace')
        all_rows = [r for r in csv.reader(io.StringIO(content)) if any(c.strip() for c in r)]
        if all_rows:
            header = [c.strip().lower() for c in all_rows[0]]
            if 'email' in header:
                idx = {n: header.index(n) for n in
                       ('email', 'prenom', 'nom', 'ville', 'subscribed_at') if n in header}
                body = all_rows[1:]
                get = lambda r, n: (r[idx[n]].strip() if n in idx and idx[n] < len(r) else '')
                for r in body:
                    rows.append({
                        'email': get(r, 'email'),
                        'prenom': get(r, 'prenom') or None,
                        'nom': get(r, 'nom') or None,
                        'ville': get(r, 'ville') or None,
                        'subscribed_at': _parse_import_date(get(r, 'subscribed_at')),
                    })
            else:
                for r in all_rows:  # no header → email, prenom, nom, ville
                    rows.append({
                        'email': r[0] if r else '',
                        'prenom': (r[1].strip() or None) if len(r) > 1 else None,
                        'nom': (r[2].strip() or None) if len(r) > 2 else None,
                        'ville': (r[3].strip() or None) if len(r) > 3 else None,
                        'subscribed_at': None,
                    })

    if text_blob:
        for line in text_blob.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in re.split(r'[;,\t]', line)]
            rows.append({
                'email': parts[0] if parts else '',
                'prenom': parts[1] if len(parts) > 1 and parts[1] else None,
                'nom': parts[2] if len(parts) > 2 and parts[2] else None,
                'ville': parts[3] if len(parts) > 3 and parts[3] else None,
                'subscribed_at': None,
            })

    return rows


@admin_subscribers_bp.route('/<int:subscriber_id>')
@admin_required
def show_subscriber(subscriber_id):
    """One subscriber: what they were sent, and what they opened or clicked.

    Opens and clicks only exist from the day the SendGrid webhook was switched
    on — it does not replay the past — so an old delivery with no event means
    "not recorded", not "not read". The page says so rather than showing a
    silent blank.
    """
    from articles.models import Article
    from tiktok.models import TikTokPost

    sub = db.session.get(Subscriber, subscriber_id) or abort(404)

    events = (
        EmailEvent.query
        .filter(db.or_(EmailEvent.subscriber_id == sub.id,
                       EmailEvent.email == sub.email))
        .order_by(EmailEvent.occurred_at.desc())
        .all()
    )
    par_article = {}
    for ev in events:
        entry = par_article.setdefault(ev.article_id, {'opens': 0, 'clicks': 0, 'last': None})
        if ev.event == 'click':
            entry['clicks'] += 1
        else:
            entry['opens'] += 1
        if entry['last'] is None or (ev.occurred_at and ev.occurred_at > entry['last']):
            entry['last'] = ev.occurred_at

    lignes = []
    for d in (Delivery.query.filter_by(subscriber_id=sub.id)
              .order_by(Delivery.sent_at.desc()).all()):
        article = db.session.get(Article, d.article_id)
        stats = par_article.get(d.article_id, {})
        lignes.append({
            'kind': 'article', 'sent_at': d.sent_at,
            'title': article.title if article else '(article supprimé)',
            'url': (url_for('articles.public_show', slug=article.slug)
                    if article else None),
            'opens': stats.get('opens', 0), 'clicks': stats.get('clicks', 0),
            'last': stats.get('last'),
        })
    for d in (MinuteDelivery.query.filter_by(subscriber_id=sub.id)
              .order_by(MinuteDelivery.sent_at.desc()).all()):
        post = db.session.get(TikTokPost, d.post_id)
        lignes.append({
            'kind': 'minute', 'sent_at': d.sent_at,
            'title': post.title if post else '(clip supprimé)',
            'url': url_for('minute.minute_landing'),
            'opens': 0, 'clicks': 0, 'last': None,
        })
    for d in (AnnouncementDelivery.query.filter_by(subscriber_id=sub.id)
              .order_by(AnnouncementDelivery.sent_at.desc()).all()):
        ann = db.session.get(Announcement, d.announcement_id)
        lignes.append({
            'kind': 'annonce', 'sent_at': d.sent_at,
            'title': ann.subject if ann else '(message supprimé)',
            'url': None, 'opens': 0, 'clicks': 0, 'last': None,
        })
    lignes.sort(key=lambda l: l['sent_at'] or datetime.min, reverse=True)

    # Les événements sans article rattaché (ouverture d'une annonce, par
    # exemple) ne se rangent dans aucune ligne : on les compte à part plutôt
    # que de les perdre.
    orphelins = sum(1 for ev in events if ev.article_id is None)

    return render_template(
        'subscriber_admin_show.html', sub=sub, lignes=lignes,
        total_opens=sum(1 for e in events if e.event != 'click'),
        total_clicks=sum(1 for e in events if e.event == 'click'),
        orphelins=orphelins,
        premier_event=(min((e.occurred_at for e in events if e.occurred_at), default=None)),
        listes=LISTES,
    )


@admin_subscribers_bp.route('/import', methods=['POST'])
@admin_required
def import_subscribers():
    rows = _parse_import_rows(request.files.get('file'), request.form.get('emails'))
    if not rows:
        flash("Aucune donnée à importer (fichier CSV ou liste d'e-mails).", 'danger')
        return redirect(url_for('admin_subscribers.list_subscribers'))

    # Lower-cased so the uniqueness check is case-insensitive (matches how
    # e-mails are stored on subscribe, and how the DB compares them).
    seen = {(e or '').strip().lower() for (e,) in db.session.query(Subscriber.email).all()}
    added = skipped = invalid = 0
    for row in rows:
        email = (row.get('email') or '').strip().lower()
        if not _EMAIL_RE.match(email):
            invalid += 1
            continue
        if email in seen:
            skipped += 1
            continue
        db.session.add(Subscriber(
            email=email,
            prenom=row.get('prenom'),
            nom=row.get('nom'),
            ville=row.get('ville'),
            token=secrets.token_urlsafe(24),
            subscribed_at=row.get('subscribed_at') or datetime.utcnow(),
            # Imported lists are admin-curated — treat them as confirmed so
            # they're mailable without a double opt-in step.
            confirmed_at=row.get('subscribed_at') or datetime.utcnow(),
        ))
        seen.add(email)
        added += 1

    db.session.commit()
    parts = [f"{added} ajouté(s)"]
    if skipped:
        parts.append(f"{skipped} déjà inscrit(s)")
    if invalid:
        parts.append(f"{invalid} e-mail(s) invalide(s)")
    flash("Import terminé — " + ", ".join(parts) + ".", 'success' if added else 'info')
    return redirect(url_for('admin_subscribers.list_subscribers'))


@admin_subscribers_bp.route('/export.csv')
@admin_required
def export_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['email', 'prenom', 'nom', 'ville', 'subscribed_at', 'unsubscribed_at'])
    for s in Subscriber.query.order_by(Subscriber.subscribed_at.desc()).all():
        writer.writerow([
            s.email,
            s.prenom or '',
            s.nom or '',
            s.ville or '',
            s.subscribed_at.strftime('%Y-%m-%d %H:%M:%S') if s.subscribed_at else '',
            s.unsubscribed_at.strftime('%Y-%m-%d %H:%M:%S') if s.unsubscribed_at else '',
        ])
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=subscribers.csv'},
    )


def _delete_subscribers(subs):
    """Delete subscriber rows, first clearing the delivery rows that
    FK-reference them (there is no ON DELETE cascade, so a stale delivery would
    otherwise make the DELETE fail with a 500)."""
    n = 0
    for sub in subs:
        Delivery.query.filter_by(subscriber_id=sub.id).delete(synchronize_session=False)
        db.session.delete(sub)
        n += 1
    db.session.commit()
    return n


@admin_subscribers_bp.route('/<int:subscriber_id>/delete', methods=['POST'])
@admin_required
def delete_subscriber(subscriber_id):
    sub = db.session.get(Subscriber, subscriber_id) or abort(404)
    _delete_subscribers([sub])
    flash("Abonné supprimé.", 'success')
    return redirect(url_for('admin_subscribers.list_subscribers'))


@admin_subscribers_bp.route('/purge-pending', methods=['POST'])
@admin_required
def purge_pending():
    """Delete every never-confirmed signup — this is where bot registrations
    accumulate under double opt-in."""
    subs = Subscriber.query.filter(Subscriber.confirmed_at.is_(None)).all()
    n = _delete_subscribers(subs)
    flash(f"{n} inscription(s) non confirmée(s) supprimée(s).", 'success' if n else 'info')
    return redirect(url_for('admin_subscribers.list_subscribers'))


# Bounces that say nothing about the address itself: a full mailbox, or the
# recipient's server refusing *us* (IP reputation, tenant policy). Those rows
# are worth un-suppressing after a fix — unlike "user unknown", where the
# mailbox is simply gone and re-mailing it costs sender reputation.
#
# Matching is allow-list only: anything that doesn't match one of these stays
# suppressed. That way an unfamiliar bounce string is never resurrected by
# accident. Order matters less than the default — note that Orange returns
# "552 5.1.1 ... Boite du destinataire pleine" for a full mailbox, so testing
# for the full-mailbox wording is what keeps it out of the 5.1.1 dead pile.
RECOVERABLE_BOUNCE_PATTERNS = (
    r'^4\d\d',                     # 4xx is transient by definition
    r'out of storage',
    r'mailbox (is )?full',
    r'bo[iî]te du destinataire pleine',
    r'over ?quota',
    r'5\.2\.2',                    # mailbox full (RFC 3463)
    r'banned sender',
    r'access denied',
    r'5\.7\.1\b', r'5\.7\.511',   # policy / sender refused
    r'sendgrid\.net',              # our own relay named in the refusal
)


def is_recoverable_bounce(reason):
    """True when the bounce blamed the mailbox's state or our sender, not the
    address. Unknown wording returns False — the safe direction."""
    if not reason:
        return False
    text = reason.lower()
    return any(re.search(pat, text) for pat in RECOVERABLE_BOUNCE_PATTERNS)


def recoverable_bounces():
    """Suppressed subscribers whose bounce looks worth retrying."""
    return [
        s for s in Subscriber.query.filter(Subscriber.bounced_at.isnot(None)).all()
        if is_recoverable_bounce(s.bounce_reason)
    ]


@admin_subscribers_bp.route('/retry-bounced', methods=['POST'])
@admin_required
def retry_bounced():
    """Clear the suppression on bounces that were not the address's fault, so
    the next campaign tries them again. If one fails for real, the SendGrid
    sync re-suppresses it with a fresh reason — so this is self-correcting."""
    subs = recoverable_bounces()
    for sub in subs:
        sub.bounced_at = None
        sub.bounce_reason = None
    db.session.commit()
    if subs:
        flash(
            f"{len(subs)} adresse(s) réactivée(s) — boîte pleine ou envoi refusé, "
            "l'adresse elle-même est valide. Elles repartiront au prochain envoi.",
            'success',
        )
    else:
        flash("Aucune adresse à réactiver : les erreurs restantes sont des boîtes inexistantes.", 'info')
    return redirect(url_for('admin_subscribers.list_subscribers'))


@admin_subscribers_bp.route('/purge-bounced', methods=['POST'])
@admin_required
def purge_bounced():
    """Delete addresses SendGrid reported as bounced / spam."""
    subs = Subscriber.query.filter(Subscriber.bounced_at.isnot(None)).all()
    n = _delete_subscribers(subs)
    flash(f"{n} adresse(s) en erreur supprimée(s).", 'success' if n else 'info')
    return redirect(url_for('admin_subscribers.list_subscribers'))


def sync_bounces_from_sendgrid():
    """Pull SendGrid's account-wide suppression lists and mark any of our
    subscribers found there as bounced (so they're never e-mailed again).
    Returns the number newly marked. Only our own addresses are touched."""
    from mail import fetch_suppressions
    suppressed = fetch_suppressions()
    if not suppressed:
        return 0
    marked = 0
    for sub in Subscriber.query.filter(Subscriber.bounced_at.is_(None)).all():
        info = suppressed.get((sub.email or '').lower())
        if not info:
            continue
        sub.bounced_at = datetime.utcnow()
        sub.bounce_reason = str(info.get('reason') or info.get('kind'))[:255]
        # A spam complaint also unsubscribes them.
        if info.get('kind') == 'spamreport' and sub.unsubscribed_at is None:
            sub.unsubscribed_at = datetime.utcnow()
        marked += 1
    if marked:
        db.session.commit()
    return marked


@admin_subscribers_bp.route('/sync-bounces', methods=['POST'])
@admin_required
def sync_bounces():
    """Admin action: pull SendGrid suppressions on demand."""
    if not mail_is_configured():
        flash("SendGrid n'est pas configuré (SENDGRID__API_KEY manquant).", 'danger')
        return redirect(url_for('admin_subscribers.list_subscribers'))
    try:
        n = sync_bounces_from_sendgrid()
    except Exception as exc:
        log.exception("SendGrid bounce sync failed")
        flash(f"Échec de la synchronisation SendGrid : {exc}", 'danger')
        return redirect(url_for('admin_subscribers.list_subscribers'))
    if n:
        flash(f"{n} adresse(s) marquée(s) en erreur d'après SendGrid.", 'success')
    else:
        flash("Synchronisation terminée — aucune nouvelle adresse en erreur.", 'info')
    return redirect(url_for('admin_subscribers.list_subscribers'))


@admin_subscribers_bp.route('/purge-suspicious', methods=['POST'])
@admin_required
def purge_suspicious():
    """Delete every active subscriber that still scores as spam. These are the
    same rows flagged with the 'suspect' badge — the high score threshold makes
    a real person very unlikely, and deleting cascades their delivery history."""
    candidates = [
        s for s in _mailable_query().all()
        if is_suspicious(s.email, s.prenom, s.nom, s.ville)
    ]
    n = _delete_subscribers(candidates)
    flash(f"{n} abonné(s) suspect(s) supprimé(s).", 'success' if n else 'info')
    return redirect(url_for('admin_subscribers.list_subscribers'))


# ─── SEND NEWSLETTER FOR AN ARTICLE ─────────────────────────

# SendGrid's category stats are slow (several calls once categories exceed the
# per-request limit of 10) and rate-limited, so the dashboard reads through a
# short-lived cache instead of querying on every page load.
_STATS_TTL = 1800   # 30 minutes


def _cached_known_categories():
    """Category names SendGrid knows, cached for a day — the set only grows
    when a new article is mailed."""
    from init_cache import cache
    from mail import fetch_known_categories, is_configured as mail_is_configured

    if not mail_is_configured():
        return None
    hit = cache.get('sg-known-categories')
    if hit is not None:
        return hit
    known = fetch_known_categories()
    if known is not None:
        cache.set('sg-known-categories', known, timeout=86400)
    return known


def _cached_stats(key, categories, start_date, aggregated_by):
    """Fetch category stats through the app cache. Returns None when SendGrid
    is unavailable, so the page can say so rather than showing false zeros."""
    from datetime import date
    from init_cache import cache
    from mail import fetch_multi_category_stats, is_configured as mail_is_configured

    if not mail_is_configured():
        return None
    cache_key = f'sg-stats:{key}:{start_date}:{date.today()}:{len(categories)}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    stats = fetch_multi_category_stats(
        categories, start_date=start_date, end_date=date.today(),
        aggregated_by=aggregated_by)
    if stats is not None:
        cache.set(cache_key, stats, timeout=_STATS_TTL)
    return stats


@admin_sends_bp.route('/stats')
@admin_required
def newsletter_stats():
    """Comprehensive newsletter dashboard: weekly activity, a per-article
    table, and the readers who engage most.

    Sends come from our own Campaign/Delivery rows, so that half is complete
    back to the first campaign. Opens and clicks come from SendGrid, which only
    knows about mail tagged with our categories — anything sent before those
    tags existed shows sends without engagement, and that gap is labelled in
    the page rather than hidden.
    """
    from datetime import date, timedelta
    from sqlalchemy import func
    from articles.models import Article
    from analytics.models import PageView
    from mail import fetch_multi_category_stats, is_configured as mail_is_configured

    campaigns = Campaign.query.order_by(Campaign.sent_at.asc()).all()
    if not campaigns:
        return render_template('newsletter_stats.html', campaigns=[], weeks=[],
                               per_article=[], top_openers=[], top_clickers=[],
                               totals={}, events_total=0, sendgrid_ok=False,
                               first_send=None)

    first_send = campaigns[0].sent_at.date()
    article_ids = sorted({c.article_id for c in campaigns})

    # ── 1. Weekly activity ────────────────────────────────────
    # Sends are counted from Delivery (one row per person actually mailed),
    # which is the honest denominator for an open rate.
    sends_by_week = {}
    for d, n in (db.session.query(
            func.date(func.subdate(Delivery.sent_at,
                                   func.weekday(Delivery.sent_at))),
            func.count(Delivery.id))
            .group_by(func.date(func.subdate(Delivery.sent_at,
                                             func.weekday(Delivery.sent_at))))
            .all()):
        sends_by_week[str(d)] = n

    # Fetched daily and bucketed here rather than asking SendGrid for
    # aggregated_by=week: its weekly rollup omits the current, incomplete week,
    # which silently dropped the most recent campaign (156 delivered showing as
    # 2). Bucketing locally also guarantees the same Monday boundaries as the
    # SQL above, so the two columns line up.
    sg_weekly = _cached_stats('weekly', ['newsletter'],
                              first_send - timedelta(days=7), 'day')
    opens_by_week, clicks_by_week = {}, {}
    if sg_weekly and 'newsletter' in sg_weekly:
        for row in sg_weekly['newsletter']['series']:
            try:
                day = date.fromisoformat(row['date'])
            except (TypeError, ValueError):
                continue
            monday = str(day - timedelta(days=day.weekday()))
            opens_by_week[monday] = opens_by_week.get(monday, 0) + row['unique_opens']
            clicks_by_week[monday] = clicks_by_week.get(monday, 0) + row['unique_clicks']

    weeks = []
    for wk in sorted(set(sends_by_week) | set(opens_by_week) | set(clicks_by_week)):
        sent = sends_by_week.get(wk, 0)
        opened = opens_by_week.get(wk, 0)
        clicked = clicks_by_week.get(wk, 0)
        # Deliberately no per-week rate. Opens are bucketed by the date the
        # reader opened, not the date we sent — someone opening on Wednesday a
        # mail sent the previous Sunday lands in a different week — and
        # SendGrid stamps its days in the account's timezone, so a send just
        # after midnight UTC can even fall on its previous day. Dividing one
        # column by the other would produce a confident-looking but meaningless
        # number. Rates live in the per-article table, where opens are tied to
        # the campaign by category rather than by date.
        weeks.append({'week': wk, 'sent': sent, 'opens': opened, 'clicks': clicked})

    # ── 2. Per-article table ─────────────────────────────────
    # One multi-category call rather than one per article.
    # Only ask for categories SendGrid has actually seen: one unknown name
    # would 404 the whole request. Articles mailed before tagging existed
    # simply show sends without engagement.
    wanted = [article_category(i) for i in article_ids]
    known = _cached_known_categories()
    if known is not None:
        wanted = [c for c in wanted if c in known]
    sg_articles = _cached_stats('articles', wanted,
                                first_send - timedelta(days=1), 'day') if wanted else {}

    delivered_by_article = dict(
        db.session.query(Delivery.article_id, func.count(Delivery.id))
        .group_by(Delivery.article_id).all())
    views_by_path = dict(
        db.session.query(PageView.path, func.count(PageView.id))
        .group_by(PageView.path).all())

    per_article = []
    for aid in article_ids:
        art = db.session.get(Article, aid)
        if art is None:
            continue
        sent = delivered_by_article.get(aid, 0)
        sg = (sg_articles or {}).get(article_category(aid)) or {}
        opens = sg.get('unique_opens', 0)
        clicks = sg.get('unique_clicks', 0)
        cs = [c for c in campaigns if c.article_id == aid]
        latest = max(cs, key=lambda c: c.sent_at)
        per_article.append({
            'article': art,
            'intro': latest.intro,
            'last_sent': latest.sent_at,
            'campaigns': len(cs),
            'sent': sent,
            'opens': opens,
            'clicks': clicks,
            'open_rate': (opens / sent) if sent else None,
            'click_rate': (clicks / sent) if sent else None,
            'site_views': views_by_path.get(f'/articles/{art.slug}', 0),
        })
    per_article.sort(key=lambda r: r['last_sent'], reverse=True)

    # ── 3. Most engaged readers ──────────────────────────────
    def _top(event_type):
        rows = (db.session.query(EmailEvent.email,
                                 func.count(func.distinct(EmailEvent.article_id)).label('articles'),
                                 func.count(EmailEvent.id).label('n'))
                .filter(EmailEvent.event == event_type)
                .group_by(EmailEvent.email)
                .order_by(func.count(EmailEvent.id).desc())
                .limit(15).all())
        out = []
        for email, articles, n in rows:
            sub = Subscriber.query.filter_by(email=email).first()
            out.append({'email': email, 'name': (sub.display_name if sub else None),
                        'articles': articles, 'n': n,
                        'subscriber': sub})
        return out

    events_total = EmailEvent.query.count()
    totals = {
        'campaigns': len(campaigns),
        'delivered': sum(delivered_by_article.values()),
        'opens': sum(w['opens'] for w in weeks),
        'clicks': sum(w['clicks'] for w in weeks),
    }
    totals['open_rate'] = (totals['opens'] / totals['delivered']) if totals['delivered'] else None
    totals['click_rate'] = (totals['clicks'] / totals['delivered']) if totals['delivered'] else None

    return render_template(
        'newsletter_stats.html',
        campaigns=campaigns, weeks=weeks, per_article=per_article,
        top_openers=_top('open'), top_clickers=_top('click'),
        totals=totals, events_total=events_total,
        sendgrid_ok=bool(sg_weekly), first_send=first_send,
    )


def article_category(article_id):
    """SendGrid category tag for an article's newsletter, used to pull its
    opens/clicks later."""
    return f'article-{article_id}'


def article_email_stats(article):
    """Combined newsletter stats for one article: our own Campaign/Delivery
    figures, enriched with SendGrid opens/clicks for the article's category
    (best-effort — None-safe when SendGrid is off or tracking not enabled).

    Returns a dict: sends (list of campaigns), recipients, delivered, and
    (when available) opens/clicks + rates.
    """
    from datetime import date, timedelta

    campaigns = (
        Campaign.query.filter_by(article_id=article.id)
        .order_by(Campaign.sent_at.desc())
        .all()
    )
    delivered = Delivery.query.filter_by(article_id=article.id).count()
    stats = {
        'campaigns': campaigns,
        'recipients': sum(c.recipient_count for c in campaigns),
        'success': sum(c.success_count for c in campaigns),
        'delivered': delivered,
        'sendgrid': None,
    }

    if campaigns and mail_is_configured():
        from mail import fetch_category_stats
        first_sent = min(c.sent_at for c in campaigns).date() - timedelta(days=1)
        try:
            stats['sendgrid'] = fetch_category_stats(
                article_category(article.id), start_date=first_sent, end_date=date.today()
            )
        except Exception:
            log.exception("article_email_stats: SendGrid pull failed (article %s)", article.id)
    return stats


def _pending_recipients(article):
    """Mailable subscribers (confirmed, active, not bounced) who haven't
    already received this article, plus the count of those skipped because
    they have."""
    subscribers = _mailable_query().all()
    already_sent = {
        d.subscriber_id
        for d in Delivery.query.filter_by(article_id=article.id).all()
    }
    recipients = [s for s in subscribers if s.id not in already_sent]
    return recipients, len(subscribers) - len(recipients)


def _create_campaign(article, recipients, skipped, sent_by):
    campaign = Campaign(
        article_id=article.id,
        sent_by_id=getattr(sent_by, 'id', None),
        recipient_count=len(recipients),
        skipped_count=skipped,
        # Snapshot rather than a reference: the article's note can be edited or
        # replaced later, and this records what subscribers actually read.
        intro=(article.newsletter_intro or None),
    )
    db.session.add(campaign)
    db.session.commit()
    return campaign


def _build_payload(article, recipients):
    """Render one e-mail per recipient now (inside the request context), so
    the background worker only does network I/O — no url_for / render_template
    outside a request."""
    site_url = url_for('articles.public_list', _external=True)
    article_url = url_for('articles.public_show', slug=article.slug, _external=True)
    subject = article.title.upper()
    payload = []
    for sub in recipients:
        html = render_template(
            'email/newsletter_article.html',
            article=article,
            article_url=article_url,
            site_url=site_url,
            site_name=current_app.config['SITE_NAME'],
            site_tagline=current_app.config['SITE_TAGLINE'],
            unsubscribe_url=url_for('newsletter.unsubscribe', token=sub.token,
                                    liste='lettre', _external=True),
        )
        payload.append({
            'subscriber_id': sub.id,
            'email': sub.email,
            'name': ' '.join(p for p in (sub.prenom, sub.nom) if p) or None,
            'subject': subject,
            'html': html,
        })
    return payload


def _send_payload(article_id, campaign_id, payload):
    """Send the pre-rendered e-mails and record deliveries. Requires an active
    app context; safe to run in a background thread."""
    # Tag every message so opens/clicks can be pulled per-article later.
    categories = ['newsletter', article_category(article_id)]
    successes, errors = 0, 0
    for item in payload:
        ok = send_email(
            to_email=item['email'],
            to_name=item['name'],
            subject=item['subject'],
            html=item['html'],
            categories=categories,
        )
        if ok:
            successes += 1
            # Record each delivery immediately so an interrupted run still
            # remembers who was emailed; the unique constraint guards against
            # duplicates.
            db.session.add(Delivery(
                article_id=article_id,
                subscriber_id=item['subscriber_id'],
                email=item['email'],
            ))
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            errors += 1

    campaign = db.session.get(Campaign, campaign_id)
    if campaign is not None:
        campaign.success_count = successes
        campaign.error_count = errors
        db.session.commit()
    return successes, errors


def enqueue_article_send(article, sent_by=None):
    """Prepare the send and hand the e-mailing to a background thread so the
    request returns immediately (sends can take a while with many subscribers).
    Returns the Campaign; success/error counts are filled in by the worker."""
    recipients, skipped = _pending_recipients(article)
    campaign = _create_campaign(article, recipients, skipped, sent_by)
    if not recipients:
        return campaign

    payload = _build_payload(article, recipients)
    app = current_app._get_current_object()
    article_id, campaign_id = article.id, campaign.id

    def _worker():
        with app.app_context():
            try:
                # Refresh SendGrid suppressions first, then drop anyone freshly
                # bounced/unsubscribed so this send never hits a bad address.
                to_send = _suppress_before_send(payload, 'lettre')
                _send_payload(article_id, campaign_id, to_send)
            except Exception:
                log.exception("background newsletter send failed (campaign %s)", campaign_id)
            finally:
                db.session.remove()

    threading.Thread(target=_worker, name=f"newsletter-send-{campaign_id}", daemon=True).start()
    return campaign


def _suppress_before_send(payload, liste='lettre'):
    """Sync SendGrid suppressions (best-effort) and return the payload items
    whose subscriber is still mailable *for this letter*.

    The list has to be passed in: someone who left la lettre but kept La Minute
    is still mailable in general, and checking that alone would send them the
    very thing they unsubscribed from. Runs inside the send task so pulling the
    account-wide bounce list never slows the admin request.
    """
    try:
        n = sync_bounces_from_sendgrid()
        if n:
            log.info("pre-send SendGrid sync suppressed %s address(es)", n)
    except Exception:
        log.exception("pre-send SendGrid bounce sync failed")
    mailable_ids = {s.id for s in _mailable_query(liste).all()}
    kept = [p for p in payload if p['subscriber_id'] in mailable_ids]
    dropped = len(payload) - len(kept)
    if dropped:
        log.info("pre-send suppression dropped %s recipient(s) from the send", dropped)
    return kept


def send_article_to_subscribers(article, sent_by=None):
    """Synchronous send (used by tests / the CLI). Returns the Campaign with
    success/error counts filled in."""
    recipients, skipped = _pending_recipients(article)
    campaign = _create_campaign(article, recipients, skipped, sent_by)
    if recipients:
        payload = _build_payload(article, recipients)
        payload = _suppress_before_send(payload, 'lettre')
        _send_payload(article.id, campaign.id, payload)
    return campaign


# --------------------------------------------------------------------------
# Annonces : un message aux abonnés qui n'est pas un article.
# --------------------------------------------------------------------------

def announcement_category(announcement_id):
    """SendGrid category tag for an announcement, so its opens and clicks can
    be pulled the same way an article's are."""
    return f'annonce-{announcement_id}'


def _announcement_recipients(announcement):
    """Mailable subscribers who have not already received this announcement,
    plus the number skipped because they have."""
    subscribers = _mailable_query('tous').all()
    already = {
        d.subscriber_id
        for d in AnnouncementDelivery.query.filter_by(announcement_id=announcement.id).all()
    }
    recipients = [s for s in subscribers if s.id not in already]
    return recipients, len(subscribers) - len(recipients)


def _build_announcement_payload(announcement, recipients):
    """Render one e-mail per recipient inside the request context, so the
    background worker only does network I/O."""
    site_url = url_for('articles.public_list', _external=True)
    payload = []
    for sub in recipients:
        html = render_template(
            'email/newsletter_announcement.html',
            announcement=announcement,
            site_url=site_url,
            site_name=current_app.config['SITE_NAME'],
            site_tagline=current_app.config['SITE_TAGLINE'],
            unsubscribe_url=url_for('newsletter.unsubscribe', token=sub.token, _external=True),
        )
        payload.append({
            'subscriber_id': sub.id,
            'email': sub.email,
            'name': ' '.join(p for p in (sub.prenom, sub.nom) if p) or None,
            'subject': announcement.subject,
            'html': html,
        })
    return payload


def _send_announcement_payload(announcement_id, payload):
    categories = ['newsletter', 'annonce', announcement_category(announcement_id)]
    successes, errors = 0, 0
    for item in payload:
        ok = send_email(
            to_email=item['email'],
            to_name=item['name'],
            subject=item['subject'],
            html=item['html'],
            categories=categories,
        )
        if ok:
            successes += 1
            db.session.add(AnnouncementDelivery(
                announcement_id=announcement_id,
                subscriber_id=item['subscriber_id'],
                email=item['email'],
            ))
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            errors += 1

    ann = db.session.get(Announcement, announcement_id)
    if ann is not None:
        ann.success_count = successes
        ann.error_count = errors
        db.session.commit()
    return successes, errors


def enqueue_announcement_send(announcement, sent_by=None):
    """Mark the announcement sent and hand the e-mailing to a background
    thread, exactly as an article send does."""
    recipients, _skipped = _announcement_recipients(announcement)
    # Rendre avant de marquer l'envoi : marquer d'abord rendrait le message
    # définitivement « envoyé » — donc non modifiable et non supprimable — si
    # le rendu échouait, alors que personne ne l'aurait reçu.
    payload = _build_announcement_payload(announcement, recipients) if recipients else []

    announcement.sent_at = datetime.utcnow()
    announcement.sent_by_id = getattr(sent_by, 'id', None)
    announcement.recipient_count = len(recipients)
    db.session.commit()

    if not recipients:
        return announcement

    app = current_app._get_current_object()
    announcement_id = announcement.id

    def _worker():
        with app.app_context():
            try:
                to_send = _suppress_before_send(payload, 'tous')
                _send_announcement_payload(announcement_id, to_send)
            except Exception:
                log.exception("background announcement send failed (id %s)", announcement_id)
            finally:
                db.session.remove()

    threading.Thread(target=_worker, name=f"announcement-send-{announcement_id}",
                     daemon=True).start()
    return announcement


@admin_sends_bp.route('/messages')
@admin_required
def list_announcements():
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).all()
    return render_template(
        'announcements_admin.html',
        announcements=announcements,
        mailable=_mailable_query('tous').count(),
        mail_ok=mail_is_configured(),
        editing=None,
    )


@admin_sends_bp.route('/messages/<int:announcement_id>')
@admin_required
def edit_announcement(announcement_id):
    editing = db.session.get(Announcement, announcement_id)
    if editing is None:
        abort(404)
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).all()
    return render_template(
        'announcements_admin.html',
        announcements=announcements,
        mailable=_mailable_query('tous').count(),
        mail_ok=mail_is_configured(),
        editing=editing,
    )


@admin_sends_bp.route('/messages/save', methods=['POST'])
@admin_required
def save_announcement():
    """Create or update a draft. A sent announcement is never modified — its
    copy is already in people's mailboxes."""
    announcement_id = request.form.get('announcement_id', type=int)
    subject = (request.form.get('subject') or '').strip()
    body = (request.form.get('body') or '').strip()

    if not subject or not body:
        flash("Il faut un objet et un message.", 'error')
        return redirect(url_for('admin_sends.list_announcements'))

    if announcement_id:
        ann = db.session.get(Announcement, announcement_id)
        if ann is None:
            abort(404)
        if ann.is_sent:
            flash("Ce message a déjà été envoyé : il ne peut plus être modifié.", 'error')
            return redirect(url_for('admin_sends.list_announcements'))
        ann.subject, ann.body = subject, body
    else:
        ann = Announcement(subject=subject, body=body)
        db.session.add(ann)
    db.session.commit()

    flash("Brouillon enregistré.", 'success')
    return redirect(url_for('admin_sends.edit_announcement', announcement_id=ann.id))


@admin_sends_bp.route('/messages/<int:announcement_id>/test', methods=['POST'])
@admin_required
def test_announcement(announcement_id):
    """Send the message to one address only — the way to see the real e-mail
    before it goes to everyone."""
    ann = db.session.get(Announcement, announcement_id)
    if ann is None:
        abort(404)

    to = (request.form.get('email') or getattr(current_user, 'email', '') or '').strip()
    if not _EMAIL_RE.match(to):
        flash("Adresse de test invalide.", 'error')
        return redirect(url_for('admin_sends.edit_announcement', announcement_id=ann.id))

    html = render_template(
        'email/newsletter_announcement.html',
        announcement=ann,
        site_url=url_for('articles.public_list', _external=True),
        site_name=current_app.config['SITE_NAME'],
        site_tagline=current_app.config['SITE_TAGLINE'],
        # Un lien de désinscription est obligatoire dans l'e-mail réel ; pour
        # le test il pointe vers le site, faute d'abonné à désinscrire.
        unsubscribe_url=url_for('articles.public_list', _external=True),
    )
    ok = send_email(to_email=to, subject=f'[Test] {ann.subject}', html=html,
                    categories=['newsletter', 'annonce-test'])
    flash(f"E-mail de test envoyé à {to}." if ok
          else "L'envoi du test a échoué — voir les journaux.", 'success' if ok else 'error')
    return redirect(url_for('admin_sends.edit_announcement', announcement_id=ann.id))


@admin_sends_bp.route('/messages/<int:announcement_id>/send', methods=['POST'])
@admin_required
def send_announcement(announcement_id):
    ann = db.session.get(Announcement, announcement_id)
    if ann is None:
        abort(404)
    if ann.is_sent:
        flash("Ce message a déjà été envoyé.", 'error')
        return redirect(url_for('admin_sends.list_announcements'))
    if not mail_is_configured():
        flash("SendGrid n'est pas configuré — envoi impossible.", 'error')
        return redirect(url_for('admin_sends.edit_announcement', announcement_id=ann.id))

    enqueue_announcement_send(ann, sent_by=current_user)
    flash(f"Envoi en cours à {ann.recipient_count} abonné(s). "
          "Les compteurs se remplissent au fur et à mesure.", 'success')
    return redirect(url_for('admin_sends.list_announcements'))


@admin_sends_bp.route('/messages/<int:announcement_id>/delete', methods=['POST'])
@admin_required
def delete_announcement(announcement_id):
    ann = db.session.get(Announcement, announcement_id)
    if ann is None:
        abort(404)
    if ann.is_sent:
        flash("Un message envoyé ne peut pas être supprimé.", 'error')
        return redirect(url_for('admin_sends.list_announcements'))
    db.session.delete(ann)
    db.session.commit()
    flash("Brouillon supprimé.", 'success')
    return redirect(url_for('admin_sends.list_announcements'))


# --------------------------------------------------------------------------
# La Minute : l'envoi d'un clip aux abonnés de la seconde lettre.
# --------------------------------------------------------------------------

def minute_body(post):
    """The TikTok caption as it belongs in an e-mail.

    The caption is written for TikTok: a headline, the argument, then a line of
    hashtags. The headline is already the subject and the title of the mail, and
    hashtags mean nothing in an inbox, so both are dropped and what remains is
    the text itself.
    """
    texte = (post.caption or '').strip()
    if not texte:
        return None

    lignes = texte.splitlines()
    # Retirer la première ligne quand c'est le titre déjà affiché au-dessus.
    if lignes and post.title and lignes[0].strip() == (post.title or '').strip():
        lignes = lignes[1:]
    # Retirer le bloc de hashtags final, mais pas un mot-dièse au fil du texte.
    while lignes:
        derniere = lignes[-1].strip()
        if not derniere:
            lignes.pop()
            continue
        mots = derniere.split()
        if mots and all(m.startswith('#') for m in mots):
            lignes.pop()
            continue
        break
    return '\n'.join(lignes).strip() or None


def minute_category(post_id):
    """SendGrid category for a Minute mailing, so its opens and clicks can be
    read the same way an article's are."""
    return f'minute-{post_id}'


def minute_recipients(post):
    """Minute subscribers who have not already received this clip."""
    subscribers = _mailable_query('minute').all()
    already = {
        d.subscriber_id
        for d in MinuteDelivery.query.filter_by(post_id=post.id).all()
    }
    recipients = [s for s in subscribers if s.id not in already]
    return recipients, len(subscribers) - len(recipients)


def _build_minute_payload(post, recipients, intro=None):
    site_url = url_for('articles.public_list', _external=True)
    # ?v= ouvre le lecteur sur ce clip : cliquer la vignette d'un e-mail doit
    # lancer la vidéo, pas déposer le lecteur devant une grille où il faut la
    # retrouver.
    minute_url = url_for('minute.minute_landing', v=post.id, _external=True)
    subject = f'La Minute : {post.title}'
    payload = []
    for sub in recipients:
        html = render_template(
            'email/newsletter_minute.html',
            post=post, intro=intro, body=minute_body(post),
            minute_url=minute_url, site_url=site_url,
            site_name=current_app.config['SITE_NAME'],
            site_tagline=current_app.config['SITE_TAGLINE'],
            # Le lien porte « minute » : se désinscrire ici ne doit retirer que
            # cette lettre, pas celle des articles.
            unsubscribe_url=url_for('newsletter.unsubscribe', token=sub.token,
                                    liste='minute', _external=True),
        )
        payload.append({
            'subscriber_id': sub.id,
            'email': sub.email,
            'name': ' '.join(p for p in (sub.prenom, sub.nom) if p) or None,
            'subject': subject,
            'html': html,
        })
    return payload


def _send_minute_payload(post_id, send_id, payload):
    categories = ['newsletter', 'minute', minute_category(post_id)]
    successes, errors = 0, 0
    for item in payload:
        ok = send_email(
            to_email=item['email'], to_name=item['name'],
            subject=item['subject'], html=item['html'], categories=categories,
        )
        if ok:
            successes += 1
            db.session.add(MinuteDelivery(
                post_id=post_id, subscriber_id=item['subscriber_id'],
                email=item['email'],
            ))
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            errors += 1

    send = db.session.get(MinuteSend, send_id)
    if send is not None:
        send.success_count = successes
        send.error_count = errors
        db.session.commit()
    return successes, errors


def send_minute_test(post, to_email, intro=None):
    """Send one clip to a single address, to see the real e-mail before it
    goes to everyone.

    The unsubscribe link points at the site rather than at a subscriber's
    token: a test has no subscriber behind it, and borrowing someone's token
    would let a mistyped address unsubscribe a real reader.
    """
    html = render_template(
        'email/newsletter_minute.html',
        post=post, intro=intro, body=minute_body(post),
        minute_url=url_for('minute.minute_landing', v=post.id, _external=True),
        site_url=url_for('articles.public_list', _external=True),
        site_name=current_app.config['SITE_NAME'],
        site_tagline=current_app.config['SITE_TAGLINE'],
        unsubscribe_url=url_for('minute.minute_landing', _external=True),
    )
    return send_email(to_email=to_email, subject=f'[Test] La Minute : {post.title}',
                      html=html, categories=['newsletter', 'minute-test'])


def enqueue_minute_send(post, sent_by=None, intro=None):
    """Send a clip to the Minute subscribers, in the background."""
    recipients, _skipped = minute_recipients(post)
    # Rendu avant l'enregistrement de l'envoi, comme pour les annonces : sinon
    # un échec de rendu laisserait une trace d'envoi que personne n'a reçu.
    payload = _build_minute_payload(post, recipients, intro=intro) if recipients else []

    send = MinuteSend(post_id=post.id, sent_by_id=getattr(sent_by, 'id', None),
                      recipient_count=len(recipients), intro=intro or None)
    db.session.add(send)
    db.session.commit()
    if not recipients:
        return send

    app = current_app._get_current_object()
    post_id, send_id = post.id, send.id

    def _worker():
        with app.app_context():
            try:
                to_send = _suppress_before_send(payload, 'minute')
                _send_minute_payload(post_id, send_id, to_send)
            except Exception:
                log.exception("background Minute send failed (send %s)", send_id)
            finally:
                db.session.remove()

    threading.Thread(target=_worker, name=f'minute-send-{send_id}', daemon=True).start()
    return send


# --------------------------------------------------------------------------
# Contacts Gmail : ce que Bernard décide de chaque correspondant.
# --------------------------------------------------------------------------

_GMAIL_STATE = 'gmail_oauth_state'
GMAIL_STATUTS = ('nouveau', 'ignoré', 'invité', 'ajouté')


def sync_gmail_contacts(limit=100):
    """Read the mailbox and record the correspondents found.

    Upserts on the address: a contact already decided keeps its decision and
    only has its name and last exchange refreshed. Returns (créés, revus).
    """
    import gmail_contacts

    trouves = gmail_contacts.recent_contacts(limit=limit)
    maintenant = datetime.utcnow()
    crees = revus = 0
    for c in trouves:
        row = GmailContact.query.filter_by(email=c['email']).first()
        if row is None:
            row = GmailContact(email=c['email'], status='nouveau',
                               first_seen_at=maintenant)
            db.session.add(row)
            crees += 1
        else:
            revus += 1
        # Le nom peut arriver vide d'un message et rempli d'un autre.
        if c.get('name'):
            row.name = c['name']
        if c.get('last') and (row.last_exchange_at is None
                              or c['last'] > row.last_exchange_at):
            row.last_exchange_at = c['last']
            row.direction = c.get('direction')
        row.last_seen_at = maintenant
    db.session.commit()
    return crees, revus


def _decide(contact, statut, par=None):
    contact.status = statut
    contact.decided_at = datetime.utcnow()
    contact.decided_by_id = getattr(par, 'id', None)


def _invite_contact(contact, par=None):
    """Create an unconfirmed subscriber and send the double opt-in e-mail."""
    existant = Subscriber.query.filter(
        db.func.lower(Subscriber.email) == contact.email).first()
    if existant is None:
        nom = (contact.name or '').strip()
        sub = Subscriber(
            email=contact.email,
            prenom=nom.split(' ')[0] if nom else None,
            nom=' '.join(nom.split(' ')[1:]) or None,
            token=secrets.token_urlsafe(32), confirmed_at=None)
        db.session.add(sub)
        db.session.commit()
        _send_confirmation_email(sub)
    _decide(contact, 'invité', par)


def _add_contact(contact, par=None):
    """Subscribe directly, without the confirmation step.

    Reserved for people whose agreement Bernard already has — he knows them,
    and the consent was given off-line. Who decided it and when is recorded on
    the contact, because the obligation is to be able to show consent, and an
    address that simply appears in the list shows nothing.
    """
    existant = Subscriber.query.filter(
        db.func.lower(Subscriber.email) == contact.email).first()
    if existant is None:
        nom = (contact.name or '').strip()
        db.session.add(Subscriber(
            email=contact.email,
            prenom=nom.split(' ')[0] if nom else None,
            nom=' '.join(nom.split(' ')[1:]) or None,
            token=secrets.token_urlsafe(32),
            confirmed_at=datetime.utcnow()))
    elif existant.confirmed_at is None:
        existant.confirmed_at = datetime.utcnow()
    _decide(contact, 'ajouté', par)


@admin_subscribers_bp.route('/gmail')
@admin_required
def gmail_contacts_page():
    """The stored correspondents and what was decided about each."""
    import gmail_contacts

    filtre = request.args.get('statut') or 'nouveau'
    q = GmailContact.query
    if filtre in GMAIL_STATUTS:
        q = q.filter_by(status=filtre)
    contacts = q.order_by(GmailContact.last_exchange_at.desc()).limit(300).all()

    # L'état côté abonnés, qui peut avoir changé depuis la décision.
    connus = {s.email.lower(): s for s in Subscriber.query.all()}
    for c in contacts:
        c.abonne = connus.get(c.email)

    compte = dict(db.session.query(GmailContact.status, db.func.count(GmailContact.id))
                  .group_by(GmailContact.status).all())

    return render_template(
        'gmail_contacts_admin.html',
        contacts=contacts, compte=compte, filtre=filtre,
        total=sum(compte.values()),
        connected=gmail_contacts.is_connected(),
        adresse=gmail_contacts.address(),
        has_client=gmail_contacts.has_client_credentials(),
        erreur=request.args.get('erreur'),
        redirect_uri=url_for('admin_subscribers.gmail_callback', _external=True),
    )


@admin_subscribers_bp.route('/gmail/sync', methods=['POST'])
@admin_required
def gmail_sync():
    import gmail_contacts
    try:
        crees, revus = sync_gmail_contacts(
            limit=request.form.get('n', 100, type=int) or 100)
    except gmail_contacts.GmailError as exc:
        return redirect(url_for('admin_subscribers.gmail_contacts_page', erreur=str(exc)))
    flash(f"{crees} nouveau(x) correspondant(s), {revus} déjà connu(s).", 'success')
    return redirect(url_for('admin_subscribers.gmail_contacts_page'))


@admin_subscribers_bp.route('/gmail/decider', methods=['POST'])
@admin_required
def gmail_decide():
    """Apply one decision to one contact, or the same to a selection."""
    action = request.form.get('action')
    ids = request.form.getlist('ids', type=int)
    if request.form.get('id', type=int):
        ids = [request.form.get('id', type=int)]
    if action not in ('ignorer', 'inviter', 'ajouter') or not ids:
        flash("Rien à faire.", 'danger')
        return redirect(url_for('admin_subscribers.gmail_contacts_page'))

    fait = 0
    for contact in GmailContact.query.filter(GmailContact.id.in_(ids)).all():
        if action == 'ignorer':
            _decide(contact, 'ignoré', current_user)
        elif action == 'inviter':
            _invite_contact(contact, current_user)
        else:
            _add_contact(contact, current_user)
        fait += 1
    db.session.commit()

    libelle = {'ignorer': 'ignoré(s)', 'inviter': 'invité(s)',
               'ajouter': 'inscrit(s) directement'}[action]
    flash(f"{fait} contact(s) {libelle}.", 'success')
    return redirect(url_for('admin_subscribers.gmail_contacts_page',
                            statut=request.form.get('retour') or 'nouveau'))


@admin_subscribers_bp.route('/gmail/connect')
@admin_required
def gmail_connect():
    import gmail_contacts
    from flask import session

    etat = secrets.token_urlsafe(24)
    session[_GMAIL_STATE] = etat
    try:
        url = gmail_contacts.authorization_url(
            url_for('admin_subscribers.gmail_callback', _external=True), etat)
    except gmail_contacts.GmailError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_subscribers.gmail_contacts_page'))
    return redirect(url)


@admin_subscribers_bp.route('/gmail/callback')
@admin_required
def gmail_callback():
    import gmail_contacts
    from flask import session

    attendu = session.pop(_GMAIL_STATE, None)
    if not attendu or request.args.get('state') != attendu:
        flash("Réponse Google inattendue — recommencez la connexion.", 'danger')
        return redirect(url_for('admin_subscribers.gmail_contacts_page'))
    if request.args.get('error'):
        flash(f"Connexion refusée : {request.args['error']}", 'danger')
        return redirect(url_for('admin_subscribers.gmail_contacts_page'))
    try:
        gmail_contacts.exchange_code(
            request.args.get('code'),
            url_for('admin_subscribers.gmail_callback', _external=True))
    except gmail_contacts.GmailError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_subscribers.gmail_contacts_page'))
    flash(f"Boîte {gmail_contacts.address() or 'Gmail'} connectée.", 'success')
    return redirect(url_for('admin_subscribers.gmail_contacts_page'))


@admin_subscribers_bp.route('/gmail/disconnect', methods=['POST'])
@admin_required
def gmail_disconnect():
    import gmail_contacts
    gmail_contacts.disconnect()
    flash("Boîte Gmail déconnectée. Les contacts déjà lus restent en place.", 'success')
    return redirect(url_for('admin_subscribers.gmail_contacts_page'))
