"""SendGrid wrapper.

We call the SendGrid v3 REST API directly with `requests` rather than the
official Python SDK to avoid an extra dependency. The whole surface is
one function — `send_email()` — that the rest of the app uses.

Required env vars (loaded via config/env_loader.py from bpoignant.json
locally or BPOIGNANT_SECRETS_JSON in production):
  SENDGRID__API_KEY      Twilio SendGrid API key
  MAIL_FROM_EMAIL        Verified sender (Single Sender or domain-auth address)
  MAIL_FROM_NAME         Display name shown in the recipient's inbox

Reply-To is fixed to bernard.poignant@gmail.com (see REPLY_TO below) so
replies always reach Bernard, regardless of environment configuration.
"""

import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)

SENDGRID_URL = 'https://api.sendgrid.com/v3/mail/send'

# Where replies to newsletter e-mails go. Hard-coded (not from the
# environment) so a stray MAIL_REPLY_TO can't redirect replies elsewhere.
REPLY_TO = 'bernard.poignant@gmail.com'


def _config():
    """Read mail config from env. Resolved at call time so tests can patch."""
    api_key = os.environ.get('SENDGRID__API_KEY', '')
    return {
        'api_key': api_key,
        'from_email': os.environ.get('MAIL_FROM_EMAIL', 'noreply@bernardpoignant.fr'),
        'from_name': os.environ.get('MAIL_FROM_NAME', 'Bernard Poignant'),
        'reply_to': REPLY_TO,
    }


def is_configured() -> bool:
    return bool(_config()['api_key'])


def send_email(
    to_email: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    to_name: Optional[str] = None,
) -> bool:
    """Send one email via SendGrid. Returns True on 2xx, False otherwise.

    Returns False (and logs) if SendGrid isn't configured, rather than
    raising — callers don't need to check is_configured() first.
    """
    cfg = _config()
    if not cfg['api_key']:
        log.warning("send_email skipped — SENDGRID__API_KEY not set (to=%s, subject=%s)", to_email, subject)
        return False

    to_entry = {'email': to_email}
    if to_name:
        to_entry['name'] = to_name

    body = {
        'personalizations': [{'to': [to_entry], 'subject': subject}],
        'from': {'email': cfg['from_email'], 'name': cfg['from_name']},
        'content': [
            {'type': 'text/plain', 'value': text or _html_to_text(html)},
            {'type': 'text/html',  'value': html},
        ],
    }
    if cfg['reply_to']:
        body['reply_to'] = {'email': cfg['reply_to']}

    try:
        resp = requests.post(
            SENDGRID_URL,
            json=body,
            headers={'Authorization': f"Bearer {cfg['api_key']}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        log.error("send_email network error (to=%s): %s", to_email, exc)
        return False

    if 200 <= resp.status_code < 300:
        return True

    log.error(
        "send_email failed: status=%s to=%s body=%s",
        resp.status_code, to_email, resp.text[:500],
    )
    return False


def _html_to_text(html: str) -> str:
    """Crude HTML-to-plain fallback so we always send a multipart message.
    Good enough for our hand-written templates."""
    import re
    # Drop <style>…</style> first
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.S | re.I)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'</p>', '\n\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    # Collapse whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
