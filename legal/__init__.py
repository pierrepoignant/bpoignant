"""Static legal pages (RGPD-mandated for any French public website).

Content is rendered from templates so it's editable without a DB change.
A handful of identity values come from env vars / app config so Bernard
can fix the director/contact details without touching the templates.
"""

from flask import Blueprint, current_app, render_template

legal_bp = Blueprint('legal', __name__, template_folder='templates')


def _ctx():
    cfg = current_app.config
    return {
        'director_name':   cfg.get('LEGAL_DIRECTOR_NAME', 'Bernard Poignant'),
        'contact_email':   cfg.get('LEGAL_CONTACT_EMAIL', 'contact@bernardpoignant.fr'),
        'host_provider':   cfg.get('LEGAL_HOST_PROVIDER', 'OVH SAS'),
        'host_address':    cfg.get('LEGAL_HOST_ADDRESS', '2 rue Kellermann, 59100 Roubaix, France'),
        'host_phone':      cfg.get('LEGAL_HOST_PHONE', '+33 9 72 10 10 07'),
    }


@legal_bp.route('/mentions-legales')
def mentions_legales():
    return render_template('mentions_legales.html', **_ctx())


@legal_bp.route('/confidentialite')
def confidentialite():
    return render_template('confidentialite.html', **_ctx())
