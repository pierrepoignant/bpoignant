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
from newsletter.models import Subscriber, Campaign, Delivery, EmailEvent
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

    if request.method == 'POST':
        if sub.unsubscribed_at is None:
            sub.unsubscribed_at = datetime.utcnow()
            db.session.commit()
        return render_template('unsubscribe_done.html', email=sub.email)

    return render_template('unsubscribe_confirm.html', subscriber=sub)


# ─── ADMIN ──────────────────────────────────────────────────

def _mailable_query():
    """Active, confirmed, non-bounced subscribers — the ones a send goes to."""
    return Subscriber.query.filter(
        Subscriber.unsubscribed_at.is_(None),
        Subscriber.confirmed_at.isnot(None),
        Subscriber.bounced_at.is_(None),
    )


@admin_subscribers_bp.route('/')
@admin_required
def list_subscribers():
    page = request.args.get('page', 1, type=int) or 1
    active_pg = (
        _mailable_query()
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
    )


@admin_sends_bp.route('/')
@admin_required
def list_sends():
    """Admin view of the e-mails that went out: a per-article summary plus
    the most recent individual deliveries."""
    from articles.models import Article

    articles = {a.id: a for a in Article.query.all()}

    rows = (
        db.session.query(
            Delivery.article_id,
            db.func.count(Delivery.id),
            db.func.max(Delivery.sent_at),
        )
        .group_by(Delivery.article_id)
        .all()
    )
    summary = [
        {'article': articles.get(article_id), 'count': n, 'last': last}
        for article_id, n, last in rows
    ]
    summary.sort(key=lambda s: s['last'] or datetime.min, reverse=True)

    total = db.session.query(db.func.count(Delivery.id)).scalar() or 0
    page = request.args.get('page', 1, type=int) or 1
    recent_pg = (
        Delivery.query.order_by(Delivery.sent_at.desc())
        .paginate(page=max(page, 1), per_page=50, error_out=False)
    )
    recent = recent_pg.items

    return render_template(
        'sends_admin_list.html',
        summary=summary,
        pagination=recent_pg,
        recent=recent,
        total=total,
        articles=articles,
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
        per_article.append({
            'article': art,
            'last_sent': max(c.sent_at for c in cs),
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
            unsubscribe_url=url_for('newsletter.unsubscribe', token=sub.token, _external=True),
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
                to_send = _suppress_before_send(payload)
                _send_payload(article_id, campaign_id, to_send)
            except Exception:
                log.exception("background newsletter send failed (campaign %s)", campaign_id)
            finally:
                db.session.remove()

    threading.Thread(target=_worker, name=f"newsletter-send-{campaign_id}", daemon=True).start()
    return campaign


def _suppress_before_send(payload):
    """Sync SendGrid suppressions (best-effort) and return the payload items
    whose subscriber is still mailable. Runs inside the send task so pulling
    the account-wide bounce list never slows the admin request."""
    try:
        n = sync_bounces_from_sendgrid()
        if n:
            log.info("pre-send SendGrid sync suppressed %s address(es)", n)
    except Exception:
        log.exception("pre-send SendGrid bounce sync failed")
    mailable_ids = {s.id for s in _mailable_query().all()}
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
        payload = _suppress_before_send(payload)
        _send_payload(article.id, campaign.id, payload)
    return campaign
