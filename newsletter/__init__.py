import csv
import io
import re
import secrets
from datetime import datetime

from flask import (
    Blueprint, Response, abort, flash, redirect, render_template, request, url_for
)

from init_db import db
from newsletter.models import Subscriber
from auth import admin_required

newsletter_bp = Blueprint('newsletter', __name__, url_prefix='/newsletter', template_folder='templates')
admin_subscribers_bp = Blueprint(
    'admin_subscribers', __name__, url_prefix='/admin/subscribers', template_folder='templates'
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
