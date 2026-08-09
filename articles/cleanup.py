"""Article HTML normaliser. Runs after bleach to tidy what Bernard
actually sees: paragraphs instead of <br>, single spaces, comma+space.
Decimals like "3,14" are left intact (comma followed by a digit is
never expanded).
"""

import re

from bs4 import BeautifulSoup


# Two-letter top-level domains we must NOT mistake for a glued sentence
# (so real links like "exemple.fr" or "site.io" are left alone).
_TLD2 = {
    'fr', 'be', 'ch', 'ca', 'io', 'co', 'uk', 'us', 'de', 'es', 'it', 'nl',
    'pt', 'eu', 're', 'tv', 'me', 'ru', 'cn', 'jp', 'au', 'at', 'se', 'no',
    'fi', 'dk', 'pl', 'gr', 'ie', 'lu', 'in', 'br', 'ma',
}

# A dot glued to a 2-letter word that isn't a TLD — e.g. "fin.Le", "texte.Il".
# Google Docs turns such a token into a hyperlink when the space after the
# period is missing; we want to undo that and restore the space.
_DOT_GLUE = re.compile(r'\.([A-Za-zÀ-ÿ]{2})(?![A-Za-zÀ-ÿ])')


def _fix_dot_glue(text: str) -> str:
    return _DOT_GLUE.sub(
        lambda m: m.group(0) if m.group(1).lower() in _TLD2 else '. ' + m.group(1),
        text,
    )


def _strip_glued_autolinks(soup):
    """Undo Google Docs' accidental auto-links: when text like "fin.Le" (a
    period stuck to a 2-letter word) gets turned into a link, drop the link
    and put the missing space back ("fin. Le")."""
    for a in list(soup.find_all('a')):
        text = a.get_text()
        m = _DOT_GLUE.search(text)
        if not m or m.group(1).lower() in _TLD2:
            continue
        # Only touch auto-generated links: the href is just the text treated
        # as a domain. Leave deliberately-added links untouched.
        href = (a.get('href') or '')
        host = re.sub(r'^[a-z]+://', '', href, flags=re.I).split('/')[0].strip().lower()
        if host and host == text.strip().lower():
            a.replace_with(_fix_dot_glue(text))


def clean_article_html(html: str) -> str:
    if not html:
        return html or ''

    soup = BeautifulSoup(html, 'html.parser')

    _strip_glued_autolinks(soup)

    for p in list(soup.find_all('p')):
        _split_on_br(p, soup)

    # Any <br> left at the top level (outside <p>) is dropped — the
    # paragraph split above is the only paragraph-break mechanism we keep.
    for br in list(soup.find_all('br')):
        br.decompose()

    for node in list(soup.find_all(string=True)):
        original = str(node)
        text = re.sub(r' {2,}', ' ', original)
        text = re.sub(r',(?=[^\s\d])', ', ', text)
        # French typography: a semicolon is always wrapped in spaces.
        text = re.sub(r'\s*;\s*', ' ; ', text)
        if text != original:
            node.replace_with(text)

    for p in list(soup.find_all('p')):
        if not p.get_text(strip=True) and not p.find('img'):
            p.decompose()

    return str(soup)


def _split_on_br(p, soup):
    if not p.find('br'):
        return

    groups = [[]]
    for child in list(p.children):
        if getattr(child, 'name', None) == 'br':
            groups.append([])
        else:
            groups[-1].append(child)

    new_ps = []
    for group in groups:
        if not _group_has_visible_content(group):
            continue
        new_p = soup.new_tag('p')
        for k, v in p.attrs.items():
            new_p[k] = v
        for child in group:
            child.extract()
            new_p.append(child)
        new_ps.append(new_p)

    for np in new_ps:
        p.insert_before(np)
    p.decompose()


def _group_has_visible_content(group):
    for c in group:
        if isinstance(c, str):
            if c.strip():
                return True
        elif c.get_text(strip=True) or c.name == 'img':
            return True
    return False


def clean_text(text: str) -> str:
    """Apply the same inline typography tidies as `clean_article_html` to a
    plain string (titles, etc.): collapse whitespace, add a space after a
    comma that isn't followed by a digit, and wrap semicolons in spaces
    (French typography). Decimals like "3,14" are left intact."""
    if not text:
        return text or ''
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r',(?=[^\s\d])', ', ', text)
    text = re.sub(r'\s*;\s*', ' ; ', text)
    return text.strip()


def summarize_html(html: str, max_len: int = 180) -> str:
    """Propose a one-line summary from an article's HTML body: the opening
    sentence when it fits, otherwise the text trimmed at a word boundary
    with an ellipsis. Returns '' when there's no usable text."""
    soup = BeautifulSoup(html or '', 'html.parser')
    # Prefer the first real paragraph so a leading heading doesn't bleed in;
    # fall back to the whole body when there are no <p> tags.
    first_p = next((p for p in soup.find_all('p') if p.get_text(strip=True)), None)
    source = first_p if first_p is not None else soup
    text = re.sub(r'\s+', ' ', source.get_text(separator=' ')).strip()
    if not text:
        return ''

    # Prefer ending neatly at the first sentence boundary when it's not too
    # long; gives a natural "résumé" rather than an arbitrary cut.
    m = re.search(r'(.+?[.!?])(?:\s|$)', text)
    if m and len(m.group(1)) <= max_len:
        return m.group(1).strip()

    if len(text) <= max_len:
        return text

    cut = text[:max_len].rsplit(' ', 1)[0].rstrip(' ,;:—-')
    return (cut + '…') if cut else text[:max_len].rstrip() + '…'
