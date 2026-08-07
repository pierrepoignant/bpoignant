"""Admin "Réglages" page.

Currently hosts the Google Drive connection: enter the OAuth client id/secret
(stored in the `config` table) and run the consent flow that stores a refresh
token, so the article editor can import Google Docs.
"""

import secrets

from flask import (
    Blueprint, flash, redirect, render_template, request, session, url_for,
)

from auth import admin_required

admin_settings_bp = Blueprint(
    'admin_settings', __name__, url_prefix='/admin/settings', template_folder='templates'
)

# Session key holding the anti-CSRF `state` between the redirect to Google and
# the callback.
_STATE_SESSION_KEY = 'gdrive_oauth_state'


def _callback_url():
    # ProxyFix makes _external URLs come out as https behind the ingress.
    return url_for('admin_settings.gdrive_callback', _external=True)


@admin_settings_bp.route('/')
@admin_required
def index():
    import gdrive
    return render_template(
        'settings_index.html',
        gdrive_client_id=gdrive._client_id(),
        gdrive_has_secret=bool(gdrive._client_secret()),
        gdrive_connected=gdrive.is_connected(),
        gdrive_redirect_uri=_callback_url(),
    )


@admin_settings_bp.route('/gdrive/credentials', methods=['POST'])
@admin_required
def gdrive_credentials():
    import gdrive
    client_id = (request.form.get('client_id') or '').strip()
    client_secret = (request.form.get('client_secret') or '').strip()
    # A blank secret field means "keep the existing secret" — so it isn't wiped
    # every time the id is edited (the secret is never sent back to the page).
    if not client_secret:
        client_secret = gdrive._client_secret()
    if not client_id:
        flash("L'identifiant OAuth est requis.", 'danger')
        return redirect(url_for('admin_settings.index'))
    gdrive.save_client_credentials(client_id, client_secret)
    flash("Identifiants Google enregistrés.", 'success')
    return redirect(url_for('admin_settings.index'))


@admin_settings_bp.route('/gdrive/connect')
@admin_required
def gdrive_connect():
    import gdrive
    from gdrive import GoogleDriveError
    state = secrets.token_urlsafe(24)
    session[_STATE_SESSION_KEY] = state
    try:
        url = gdrive.authorization_url(_callback_url(), state)
    except GoogleDriveError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_settings.index'))
    return redirect(url)


@admin_settings_bp.route('/gdrive/callback')
@admin_required
def gdrive_callback():
    import gdrive
    from gdrive import GoogleDriveError

    expected = session.pop(_STATE_SESSION_KEY, None)
    error = request.args.get('error')
    if error:
        flash(f"Connexion Google annulée ou refusée ({error}).", 'danger')
        return redirect(url_for('admin_settings.index'))

    state = request.args.get('state')
    if not expected or state != expected:
        flash("Échec de la vérification de sécurité (state). Réessayez la connexion.", 'danger')
        return redirect(url_for('admin_settings.index'))

    try:
        gdrive.exchange_code(request.args.get('code'), _callback_url())
    except GoogleDriveError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_settings.index'))

    flash("Google Drive connecté. L'import est disponible dans l'éditeur d'articles.", 'success')
    return redirect(url_for('admin_settings.index'))


@admin_settings_bp.route('/gdrive/disconnect', methods=['POST'])
@admin_required
def gdrive_disconnect():
    import gdrive
    gdrive.disconnect()
    flash("Google Drive déconnecté.", 'info')
    return redirect(url_for('admin_settings.index'))
