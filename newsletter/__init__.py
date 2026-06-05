import csv
import io
import re
import secrets
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

def send_article_to_subscribers(article, sent_by=None):
    """Mail the given article to every active subscriber. Returns a
    Campaign with success/error counts (also committed to the DB)."""
    from articles.models import Article  # local import to avoid cycle

    subscribers = Subscriber.query.filter(Subscriber.unsubscribed_at.is_(None)).all()

    # Skip anyone who already received this exact article, so a second click
    # on "send" never mails the same article to the same recipient.
    already_sent = {
        d.subscriber_id
        for d in Delivery.query.filter_by(article_id=article.id).all()
    }
    recipients = [s for s in subscribers if s.id not in already_sent]
    skipped = len(subscribers) - len(recipients)

    campaign = Campaign(
        article_id=article.id,
        sent_by_id=getattr(sent_by, 'id', None),
        recipient_count=len(recipients),
        skipped_count=skipped,
    )
    db.session.add(campaign)
    db.session.commit()

    site_url = url_for('articles.public_list', _external=True)
    article_url = url_for('articles.public_show', slug=article.slug, _external=True)

    successes, errors = 0, 0
    for sub in recipients:
        unsub_url = url_for('newsletter.unsubscribe', token=sub.token, _external=True)
        html = render_template(
            'email/newsletter_article.html',
            article=article,
            article_url=article_url,
            site_url=site_url,
            site_name=current_app.config['SITE_NAME'],
            site_tagline=current_app.config['SITE_TAGLINE'],
            unsubscribe_url=unsub_url,
        )
        name = ' '.join(p for p in (sub.prenom, sub.nom) if p) or None
        ok = send_email(
            to_email=sub.email,
            to_name=name,
            subject=article.title,
            html=html,
        )
        if ok:
            successes += 1
            # Record the delivery immediately so an interrupted run still
            # remembers who was already emailed. The unique constraint guards
            # against duplicates (e.g. two near-simultaneous sends).
            db.session.add(Delivery(
                article_id=article.id,
                subscriber_id=sub.id,
                email=sub.email,
            ))
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            errors += 1

    campaign.success_count = successes
    campaign.error_count = errors
    db.session.commit()
    return campaign
