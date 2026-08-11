"""Heuristic spam scoring for newsletter signups.

The bots we've seen fill every field with random gibberish and use throwaway
domains, e.g.:

    gpltkjkg@immenseignite.info
    qwwxdtkmtt / omzeowqpys / liiiksjfef

`score_signup()` returns an integer risk score and the reasons behind it. The
caller turns a high score into a double opt-in requirement (a confirmation
e-mail) rather than an outright block — so a false positive on a real person
just adds one click, it never loses them.
"""

import re

# Score at/above which a signup should be forced through e-mail confirmation.
CONFIRM_THRESHOLD = 3

# Common webmail / ISP domains — a signup here is almost never a throwaway.
_TRUSTED_DOMAINS = {
    'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'hotmail.fr',
    'live.com', 'live.fr', 'yahoo.com', 'yahoo.fr', 'orange.fr', 'wanadoo.fr',
    'free.fr', 'sfr.fr', 'laposte.net', 'icloud.com', 'me.com', 'protonmail.com',
    'proton.me', 'gmx.fr', 'gmx.com', 'bbox.fr', 'numericable.fr', 'aol.com',
}

# Known disposable / throwaway domains seen in spam signups. Not exhaustive —
# the gibberish + TLD heuristics catch the long tail.
_DISPOSABLE_DOMAINS = {
    'immenseignite.info', 'mailinator.com', 'guerrillamail.com', 'yopmail.com',
    'sharklasers.com', 'trashmail.com', 'tempmail.com', 'temp-mail.org',
    'getnada.com', 'maildrop.cc', 'dispostable.com', 'fakeinbox.com',
    '10minutemail.com', 'throwawaymail.com', 'mvrht.net',
}

# TLDs that are cheap and heavily abused by signup bots.
_SUSPICIOUS_TLDS = {'info', 'xyz', 'top', 'online', 'site', 'club', 'live', 'buzz', 'click', 'work'}

_VOWELS = set('aeiouyàâäéèêëîïôöûü')


def looks_gibberish(s):
    """Heuristic: does this look like a random string rather than a real
    word/name? Tuned to flag bot gibberish (`qwwxdtkmtt`, `zpgmsygjyo`) while
    leaving normal French names (`bernard`, `poignant`, `quimper`) alone."""
    s = (s or '').strip().lower()
    if len(s) < 5:
        return False
    # Real names/words are letters (plus the odd hyphen/space/apostrophe);
    # gibberish bot strings are pure lowercase letters.
    if not re.fullmatch(r'[a-zàâäéèêëîïôöûüç]+', s):
        return False
    vowels = sum(c in _VOWELS for c in s)
    ratio = vowels / len(s)
    # Natural words sit around 35–45% vowels; very low means keyboard-mash.
    # Kept below 0.20 so real short place-names like "Brest" (0.20) pass.
    if ratio < 0.2:
        return True
    # A long run of consonants almost never occurs in real French words.
    if re.search(r'[bcdfghjklmnpqrstvwxzñ]{5,}', s):
        return True
    return False


def _domain(email):
    return email.rsplit('@', 1)[-1].strip().lower() if '@' in (email or '') else ''


def score_signup(email, prenom=None, nom=None, ville=None, honeypot=None):
    """Return ``(score, reasons)`` for a signup. Higher = more likely spam."""
    reasons = []
    score = 0

    email = (email or '').strip().lower()
    local = email.split('@', 1)[0] if '@' in email else email
    domain = _domain(email)
    tld = domain.rsplit('.', 1)[-1] if '.' in domain else ''

    trusted = domain in _TRUSTED_DOMAINS

    # Domain reputation.
    if domain in _DISPOSABLE_DOMAINS:
        score += 4
        reasons.append('domaine jetable connu')
    elif not trusted and tld in _SUSPICIOUS_TLDS:
        score += 2
        reasons.append(f'TLD à risque (.{tld})')

    # Gibberish local part (skip for trusted webmail — real gmail handles vary).
    if not trusted and looks_gibberish(local):
        score += 2
        reasons.append('adresse aléatoire')

    # Gibberish name / city fields — bots randomise these too.
    gib_fields = [f for f in (prenom, nom, ville) if looks_gibberish(f)]
    if len(gib_fields) >= 2:
        score += 3
        reasons.append('nom/ville aléatoires')
    elif len(gib_fields) == 1:
        score += 1
        reasons.append('champ nom/ville aléatoire')

    return score, reasons


def is_suspicious(email, prenom=None, nom=None, ville=None):
    """Convenience wrapper: True when the signup scores at/above the confirm
    threshold. Used to re-flag existing subscribers in the admin."""
    score, _ = score_signup(email, prenom, nom, ville)
    return score >= CONFIRM_THRESHOLD
