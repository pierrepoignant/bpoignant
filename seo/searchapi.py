"""Thin SearchApi.io client for Google organic results.

SearchApi returns 10 organic results per page (Google removed `num=100` in
Sept 2025), so reaching N results means paginating with `&page=`. This module
is pure HTTP + parsing — it does no database work, so it can be unit-tested
and reused from both the web app and a CLI.

Docs: https://www.searchapi.io/docs/google
"""

import os
from urllib.parse import urlparse

import requests

ENDPOINT = "https://www.searchapi.io/api/v1/search"
RESULTS_PER_PAGE = 10
# Hard ceiling on pages so a bad loop can never hammer the paid API.
MAX_PAGES = 60


def get_api_key():
    """Resolve the SearchApi key from the env (loaded by python-dotenv /
    bpoignant.json). Returns '' when unset so callers can fail gracefully."""
    return (os.environ.get('SEARCHAPI__API_KEY') or '').strip()


def _domain_of(link):
    try:
        host = urlparse(link).netloc.lower()
        return host[4:] if host.startswith('www.') else host
    except Exception:
        return None


def fetch_page(keyword, page, api_key, timeout=30):
    """Fetch a single page of Google organic results. Returns the parsed JSON.
    Raises requests.HTTPError on a non-2xx response."""
    params = {
        'engine': 'google',
        'q': keyword,
        'page': page,
        'api_key': api_key,
        'gl': 'fr',   # country: France
        'hl': 'fr',   # interface language: French
    }
    resp = requests.get(ENDPOINT, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def perform_search(keyword, max_results=500, api_key=None, on_page=None):
    """Page through Google until `max_results` organic results are collected
    (or Google runs out of results / we hit MAX_PAGES).

    Returns a list of dicts with normalised fields. `on_page(page, total)` is
    an optional progress callback invoked after each page is parsed.
    """
    api_key = api_key or get_api_key()
    if not api_key:
        raise RuntimeError("SEARCHAPI__API_KEY is not set.")

    collected = []
    seen_links = set()
    page = 1

    while len(collected) < max_results and page <= MAX_PAGES:
        data = fetch_page(keyword, page, api_key)
        organic = data.get('organic_results') or []
        if not organic:
            break

        for item in organic:
            link = item.get('link')
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            collected.append({
                'rank': len(collected) + 1,
                'page': page,
                'title': item.get('title'),
                'link': link,
                'displayed_link': item.get('displayed_link'),
                'domain': item.get('domain') or _domain_of(link),
                'snippet': item.get('snippet'),
                'result_date': item.get('date'),
            })
            if len(collected) >= max_results:
                break

        if on_page:
            on_page(page, len(collected))

        # Stop early when Google signals there is no further page.
        pagination = data.get('pagination') or {}
        if not pagination.get('next') and len(organic) < RESULTS_PER_PAGE:
            break

        page += 1

    return collected
