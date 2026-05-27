"""Article HTML normaliser. Runs after bleach to tidy what Bernard
actually sees: paragraphs instead of <br>, single spaces, comma+space.
Decimals like "3,14" are left intact (comma followed by a digit is
never expanded).
"""

import re

from bs4 import BeautifulSoup


def clean_article_html(html: str) -> str:
    if not html:
        return html or ''

    soup = BeautifulSoup(html, 'html.parser')

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
