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
# Where to land once the consent flow is over. Lets the article editor send the
# admin through the reconnect and get him back to the page he was writing on,
# instead of dumping him on the settings page.
_NEXT_SESSION_KEY = 'gdrive_oauth_next'


def _safe_next(raw):
    """Accept only same-site relative paths, so `?next=` can never be turned
    into an open redirect to another host."""
    if not raw or not raw.startswith('/') or raw.startswith('//') or '\\' in raw:
        return None
    return raw


def _after_connect():
    """Where the callback should send the admin: back where he came from when
    the editor asked for it, else the settings page."""
    return session.pop(_NEXT_SESSION_KEY, None) or url_for('admin_settings.index')


def _callback_url():
    # ProxyFix makes _external URLs come out as https behind the ingress.
    return url_for('admin_settings.gdrive_callback', _external=True)


@admin_settings_bp.route('/')
@admin_required
def index():
    import gdrive
    import apify
    import linkedin
    return render_template(
        'settings_index.html',
        linkedin_client_id=linkedin._client_id(),
        linkedin_has_secret=bool(linkedin._client_secret()),
        linkedin_connected=linkedin.is_configured(),
        linkedin_name=linkedin.display_name(),
        linkedin_days_left=linkedin.days_left(),
        linkedin_version=linkedin._version(),
        linkedin_redirect_uri=url_for('admin_settings.linkedin_callback', _external=True),
        apify_configured=apify.is_configured(),
        apify_actor=apify.actor(),
        apify_profile=apify.profile(),
        apify_default_actor=apify.DEFAULT_ACTOR,
        gdrive_client_id=gdrive._client_id(),
        gdrive_has_secret=bool(gdrive._client_secret()),
        gdrive_connected=gdrive.is_connected(),
        gdrive_redirect_uri=_callback_url(),
    )


@admin_settings_bp.route('/linkedin/credentials', methods=['POST'])
@admin_required
def linkedin_credentials():
    import linkedin
    linkedin.save_settings(request.form.get('client_id'),
                           request.form.get('client_secret'),
                           request.form.get('version'))
    flash("Identifiants LinkedIn enregistrés.", 'success')
    return redirect(url_for('admin_settings.index') + '#linkedin')


@admin_settings_bp.route('/linkedin/connect')
@admin_required
def linkedin_connect():
    import linkedin
    etat = secrets.token_urlsafe(24)
    session['linkedin_oauth_state'] = etat
    try:
        url = linkedin.authorization_url(
            url_for('admin_settings.linkedin_callback', _external=True), etat)
    except linkedin.LinkedInError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_settings.index') + '#linkedin')
    return redirect(url)


@admin_settings_bp.route('/linkedin/callback')
@admin_required
def linkedin_callback():
    import linkedin
    attendu = session.pop('linkedin_oauth_state', None)
    if not attendu or request.args.get('state') != attendu:
        flash("Réponse LinkedIn inattendue — recommencez la connexion.", 'danger')
        return redirect(url_for('admin_settings.index') + '#linkedin')
    if request.args.get('error'):
        flash(f"Connexion refusée : {request.args.get('error_description') or request.args['error']}",
              'danger')
        return redirect(url_for('admin_settings.index') + '#linkedin')
    try:
        linkedin.exchange_code(request.args.get('code'),
                               url_for('admin_settings.linkedin_callback', _external=True))
    except linkedin.LinkedInError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_settings.index') + '#linkedin')
    flash(f"LinkedIn connecté — {linkedin.display_name() or 'compte'}. "
          f"L'autorisation dure {linkedin.days_left()} jours.", 'success')
    return redirect(url_for('admin_settings.index') + '#linkedin')


@admin_settings_bp.route('/linkedin/verify', methods=['POST'])
@admin_required
def linkedin_verify():
    import linkedin
    ok, message = linkedin.verify_credentials()
    flash(message, 'success' if ok else 'danger')
    return redirect(url_for('admin_settings.index') + '#linkedin')


@admin_settings_bp.route('/linkedin/disconnect', methods=['POST'])
@admin_required
def linkedin_disconnect():
    import linkedin
    linkedin.disconnect()
    flash("LinkedIn déconnecté.", 'success')
    return redirect(url_for('admin_settings.index') + '#linkedin')


@admin_settings_bp.route('/apify/credentials', methods=['POST'])
@admin_required
def apify_credentials():
    import apify
    apify.save_settings(request.form.get('apify_token'),
                        request.form.get('apify_actor'),
                        request.form.get('apify_profile'))
    flash("Réglages Apify enregistrés.", 'success')
    return redirect(url_for('admin_settings.index'))


@admin_settings_bp.route('/apify/test', methods=['POST'])
@admin_required
def apify_test():
    """Check the token against Apify's own account endpoint — no actor run, so
    no credits are spent proving the configuration works."""
    import apify
    try:
        info = apify.account_info()
    except apify.ApifyError as exc:
        flash(f"Apify : {exc}", 'danger')
        return redirect(url_for('admin_settings.index'))
    plan = info.get('plan') or 'inconnu'
    flash(f"Apify OK — compte « {info.get('username') or '?'} », formule {plan}.", 'success')
    return redirect(url_for('admin_settings.index'))


@admin_settings_bp.route('/apify/disconnect', methods=['POST'])
@admin_required
def apify_disconnect():
    import apify
    apify.disconnect()
    flash("Jeton Apify supprimé.", 'info')
    return redirect(url_for('admin_settings.index'))


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


@admin_settings_bp.route('/gdrive/verifier', methods=['POST'])
@admin_required
def gdrive_verify():
    """Re-test the stored credential and clear the banner if it works.

    The banner is only lifted by a successful Drive call, so a failure that has
    since resolved kept warning until someone happened to open the import
    dialog. This asks Google directly, and costs one token request.
    """
    import gdrive

    ok, message = gdrive.health_check()
    flash("Google Drive répond — la connexion est valide." if ok
          else f"Google Drive ne répond toujours pas : {message}",
          'success' if ok else 'danger')
    return redirect(request.form.get('next') or url_for('admin_settings.index'))


@admin_settings_bp.route('/gdrive/connect')
@admin_required
def gdrive_connect():
    import gdrive
    from gdrive import GoogleDriveError
    state = secrets.token_urlsafe(24)
    session[_STATE_SESSION_KEY] = state
    # The editor passes ?next=<its own path> so the admin comes straight back
    # to his article once Google is done.
    nxt = _safe_next(request.args.get('next'))
    if nxt:
        session[_NEXT_SESSION_KEY] = nxt
    else:
        session.pop(_NEXT_SESSION_KEY, None)
    try:
        url = gdrive.authorization_url(_callback_url(), state)
    except GoogleDriveError as exc:
        flash(str(exc), 'danger')
        return redirect(_after_connect())
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
        return redirect(_after_connect())

    state = request.args.get('state')
    if not expected or state != expected:
        flash("Échec de la vérification de sécurité (state). Réessayez la connexion.", 'danger')
        return redirect(_after_connect())

    try:
        gdrive.exchange_code(request.args.get('code'), _callback_url())
    except GoogleDriveError as exc:
        flash(str(exc), 'danger')
        return redirect(_after_connect())

    flash("Google Drive reconnecté.", 'success')
    return redirect(_after_connect())


@admin_settings_bp.route('/gdrive/disconnect', methods=['POST'])
@admin_required
def gdrive_disconnect():
    import gdrive
    gdrive.disconnect()
    flash("Google Drive déconnecté.", 'info')
    return redirect(url_for('admin_settings.index'))
