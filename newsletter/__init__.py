import csv
import io
import re
import secrets
import threading
from datetime import datetime

from flask import (
    Blueprint, Response, abort, flash, redirect, render_template, request, url_for
)

from init_db import db
from newsletter.models import Subscriber, Campaign, Delivery
from auth import admin_required
from flask import current_app, render_template, url_for
from flask_login import current_user
from mail import send_email
import logging

log = logging.getLogger(__name__)

newsletter_bp = Blueprint('newsletter', __name__, url_prefix='/newsletter', template_folder='templates')
admin_subscribers_bp = Blueprint(
    'admin_subscribers', __name__, url_prefix='/admin/subscribers', template_folder='templates'
)
admin_sends_bp = Blueprint(
    'admin_sends', __name__, url_prefix='/admin/sends', template_folder='templates'
)


# RFC-ish — good enough for sanity-checking input before storage.
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@newsletter_bp.route('/subscribe', methods=['POST'])
def subscribe():
    email = (request.form.get('email') or '').strip().lower()
    prenom = (request.form.get('prenom') or '').strip() or None
    nom = (request.form.get('nom') or '').strip() or None
    ville = (request.form.get('ville') or '').strip() or None
    redirect_to = request.form.get('next') or url_for('articles.public_list')

    if not _EMAIL_RE.match(email):
        flash("Adresse e-mail invalide.", 'danger')
        return redirect(redirect_to + '#newsletter')

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
        db.session.commit()
        return render_template(
            'subscribe_done.html',
            email=existing.email,
            prenom=existing.prenom,
            reactivated=reactivated,
            already=not reactivated,
        )

    sub = Subscriber(
        email=email,
        prenom=prenom,
        nom=nom,
        ville=ville,
        token=secrets.token_urlsafe(24),
    )
    db.session.add(sub)
    db.session.commit()
    return render_template(
        'subscribe_done.html',
        email=sub.email,
        prenom=sub.prenom,
        reactivated=False,
        already=False,
    )


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

@admin_subscribers_bp.route('/')
@admin_required
def list_subscribers():
    active = Subscriber.query.filter(Subscriber.unsubscribed_at.is_(None)).order_by(
        Subscriber.subscribed_at.desc()
    ).all()
    unsubscribed = Subscriber.query.filter(Subscriber.unsubscribed_at.isnot(None)).order_by(
        Subscriber.unsubscribed_at.desc()
    ).all()
    return render_template(
        'subscribers_admin_list.html',
        active=active,
        unsubscribed=unsubscribed,
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
    recent = Delivery.query.order_by(Delivery.sent_at.desc()).limit(300).all()

    return render_template(
        'sends_admin_list.html',
        summary=summary,
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


@admin_subscribers_bp.route('/<int:subscriber_id>/delete', methods=['POST'])
@admin_required
def delete_subscriber(subscriber_id):
    sub = db.session.get(Subscriber, subscriber_id) or abort(404)
    db.session.delete(sub)
    db.session.commit()
    flash("Abonné supprimé.", 'success')
    return redirect(url_for('admin_subscribers.list_subscribers'))


# ─── SEND NEWSLETTER FOR AN ARTICLE ─────────────────────────

def _pending_recipients(article):
    """Active subscribers who haven't already received this article, plus the
    count of those skipped because they have."""
    subscribers = Subscriber.query.filter(Subscriber.unsubscribed_at.is_(None)).all()
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
    successes, errors = 0, 0
    for item in payload:
        ok = send_email(
            to_email=item['email'],
            to_name=item['name'],
            subject=item['subject'],
            html=item['html'],
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
                _send_payload(article_id, campaign_id, payload)
            except Exception:
                log.exception("background newsletter send failed (campaign %s)", campaign_id)
            finally:
                db.session.remove()

    threading.Thread(target=_worker, name=f"newsletter-send-{campaign_id}", daemon=True).start()
    return campaign


def send_article_to_subscribers(article, sent_by=None):
    """Synchronous send (used by tests / the CLI). Returns the Campaign with
    success/error counts filled in."""
    recipients, skipped = _pending_recipients(article)
    campaign = _create_campaign(article, recipients, skipped, sent_by)
    if recipients:
        payload = _build_payload(article, recipients)
        _send_payload(article.id, campaign.id, payload)
    return campaign
