"""Alertes aux administrateurs par e-mail, une fois par panne.

Quand un réseau refuse une publication (plus de crédit sur X) ou qu'une
connexion a expiré (YouTube, Gmail…), on prévient les admins par e-mail —
mais une seule fois tant que la panne dure, pour ne pas envoyer vingt messages
identiques. `clear` remet le compteur quand tout refonctionne, de sorte que la
prochaine panne redonnera bien une alerte.
"""

import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)


def _flag(cle):
    return f'alert_active_{cle}'


def notify_once(cle, sujet, message, action_url=None, action_label=None):
    """Envoyer l'alerte une seule fois par panne (identifiée par `cle`)."""
    from settings.models import get_config, set_config
    try:
        if get_config(_flag(cle)):
            return False
        envoye = _envoyer_admins(sujet, message, action_url, action_label)
        set_config(_flag(cle), datetime.utcnow().isoformat(timespec='seconds'))
        return envoye
    except Exception:
        log.exception('alerte %s impossible', cle)
        return False


def clear(cle):
    """Panne résolue : la prochaine redéclenchera une alerte."""
    from settings.models import get_config, delete_config
    try:
        if get_config(_flag(cle)):
            delete_config(_flag(cle))
    except Exception:
        log.exception('effacement alerte %s impossible', cle)


def _envoyer_admins(sujet, message, action_url, action_label):
    from flask import current_app, has_request_context, render_template, url_for
    from auth.models import User
    from mail import is_configured as mail_ok, send_email
    if not mail_ok():
        log.info('alerte ignorée — e-mail non configuré')
        return False
    admins = User.query.filter(User.is_admin == True,  # noqa: E712
                               User.email.isnot(None)).all()
    if not admins:
        return False
    ctx = None
    if not has_request_context():
        base = os.environ.get('SITE_BASE_URL', 'https://bernardpoignant.fr')
        ctx = current_app.test_request_context(base_url=base)
        ctx.push()
    try:
        if not action_url:
            action_url = url_for('admin_settings.index', _external=True)
            action_label = action_label or 'Ouvrir les réglages'
        for a in admins:
            html = render_template('email/alert.html', sujet=sujet, message=message,
                                   action_url=action_url, action_label=action_label,
                                   site_name=current_app.config.get('SITE_NAME', ''))
            send_email(to_email=a.email, to_name=a.username, subject=sujet,
                       html=html, categories=['alerte'])
    finally:
        if ctx is not None:
            ctx.pop()
    return True
