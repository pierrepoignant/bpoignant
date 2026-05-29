"""Run a SearchApi fetch and persist the results.

`run_and_store` must execute inside a Flask app context (it touches the DB).
`start_background_run` wraps it in a thread + app context so the admin can
click "Lancer" and keep browsing while ~50 API calls complete.
"""

import threading
from datetime import datetime

from init_db import db
from seo.models import SearchRun, GoogleUrl
from seo.searchapi import perform_search

DEFAULT_KEYWORD = "bernard poignant"
DEFAULT_MAX_RESULTS = 500


def has_running(keyword=None):
    """True if a run is currently in progress (optionally for one keyword)."""
    q = SearchRun.query.filter_by(status='running')
    if keyword:
        q = q.filter_by(keyword=keyword)
    return db.session.query(q.exists()).scalar()


def run_and_store(keyword=DEFAULT_KEYWORD, max_results=DEFAULT_MAX_RESULTS, run=None):
    """Execute one fetch synchronously and store every result against a
    SearchRun row. Creates the run if not passed one. Returns the SearchRun."""
    if run is None:
        run = SearchRun(keyword=keyword, requested=max_results, status='running')
        db.session.add(run)
        db.session.commit()

    def _progress(page, total):
        run.pages_fetched = page
        run.fetched = total
        db.session.commit()

    try:
        results = perform_search(keyword, max_results=max_results, on_page=_progress)
        for r in results:
            db.session.add(GoogleUrl(
                run_id=run.id,
                keyword=keyword,
                rank=r['rank'],
                page=r['page'],
                title=r['title'],
                link=r['link'],
                displayed_link=r['displayed_link'],
                domain=r['domain'],
                snippet=r['snippet'],
                result_date=r['result_date'],
            ))
        run.fetched = len(results)
        run.status = 'done'
        run.finished_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 — record the failure, don't crash the thread
        db.session.rollback()
        run.status = 'error'
        run.error = str(exc)[:2000]
        run.finished_at = datetime.utcnow()
        db.session.commit()

    return run


def start_background_run(app, keyword=DEFAULT_KEYWORD, max_results=DEFAULT_MAX_RESULTS):
    """Create the SearchRun now (so the UI sees it immediately) and finish the
    fetch in a background thread. Returns the SearchRun id."""
    run = SearchRun(keyword=keyword, requested=max_results, status='running')
    db.session.add(run)
    db.session.commit()
    run_id = run.id

    def _worker():
        with app.app_context():
            r = db.session.get(SearchRun, run_id)
            run_and_store(keyword=keyword, max_results=max_results, run=r)

    threading.Thread(target=_worker, name=f'seo-fetch-{run_id}', daemon=True).start()
    return run_id
