from collections import Counter

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, url_for
)
from sqlalchemy import func

from init_db import db
from auth import admin_required
from seo.models import SearchRun, GoogleUrl
from seo.searchapi import get_api_key
from seo.service import (
    start_background_run, has_running, DEFAULT_KEYWORD, DEFAULT_MAX_RESULTS,
)

# Bernard's own site — highlighted in the results so its ranking is obvious.
# IMPORTANT: bernardpoignant.fr is his; bernard-poignant.fr (with a hyphen) is
# a DIFFERENT site and must NOT be counted as his. So we match the exact host
# and its subdomains only — never a hyphen-insensitive stem.
OWN_DOMAIN = 'bernardpoignant.fr'

seo_bp = Blueprint('seo', __name__, url_prefix='/seo', template_folder='templates')


def _is_own(domain):
    if not domain:
        return False
    d = domain.lower().strip()
    if d.startswith('www.'):
        d = d[4:]
    return d == OWN_DOMAIN or d.endswith('.' + OWN_DOMAIN)


def _own_rank_in_run(run):
    """Best (lowest) rank Bernard's own site reached in a run, or None."""
    ranks = [r.rank for r in run.results.all() if _is_own(r.domain)]
    return min(ranks) if ranks else None


def _latest_run():
    """Most recent run that produced results; fall back to the very last run
    (e.g. one still running or that errored) so the UI always has something."""
    done = (
        SearchRun.query.filter_by(status='done')
        .order_by(SearchRun.started_at.desc())
        .first()
    )
    return done or SearchRun.query.order_by(SearchRun.started_at.desc()).first()


def _build_stats(run):
    """Aggregate everything the dashboard needs from a single run's rows."""
    if run is None:
        return None

    rows = run.results.order_by(GoogleUrl.rank.asc()).all()
    if not rows:
        return {'run': run, 'rows': [], 'empty': True}

    domain_counts = Counter(r.domain or '—' for r in rows)
    top_domains = domain_counts.most_common(15)

    # domain → its results (rows already ordered by rank asc), so the Domaines
    # tab can expand each domain to reveal the matching URLs.
    domain_urls = {}
    for r in rows:
        domain_urls.setdefault(r.domain or '—', []).append(r)

    # Bernard's own appearances + best rank.
    own = [r for r in rows if _is_own(r.domain)]
    own_best_rank = min((r.rank for r in own), default=None)

    # Results-per-page histogram (how deep into Google we went).
    page_counts = sorted(Counter(r.page or 0 for r in rows).items())
    max_page_count = max((n for _, n in page_counts), default=1) or 1

    return {
        'run': run,
        'rows': rows,
        'empty': False,
        'total': len(rows),
        'unique_domains': len(domain_counts),
        'top_domains': top_domains,
        'domain_urls': domain_urls,
        'max_domain_count': top_domains[0][1] if top_domains else 1,
        'own': own,
        'own_ranks': {r.rank for r in own},
        'own_best_rank': own_best_rank,
        'page_counts': page_counts,
        'max_page_count': max_page_count,
    }


@seo_bp.route('/')
@admin_required
def dashboard():
    run = _latest_run()
    stats = _build_stats(run)

    runs = SearchRun.query.order_by(SearchRun.started_at.desc()).limit(20).all()
    run_counts = dict(
        db.session.query(GoogleUrl.run_id, func.count(GoogleUrl.id))
        .group_by(GoogleUrl.run_id).all()
    )

    # Evolution of Bernard's own-site rank across completed runs (oldest →
    # newest) so the timestamps tell a story. Capped to the last 12 runs.
    completed = (
        SearchRun.query.filter_by(status='done')
        .order_by(SearchRun.started_at.desc()).limit(12).all()
    )
    evolution = [
        {'date': r.started_at, 'rank': _own_rank_in_run(r), 'total': run_counts.get(r.id, r.fetched)}
        for r in reversed(completed)
    ]
    evo_ranks = [e['rank'] for e in evolution if e['rank']]
    evo_worst = max(evo_ranks) if evo_ranks else 1

    return render_template(
        'seo_dashboard.html',
        stats=stats,
        runs=runs,
        run_counts=run_counts,
        evolution=evolution,
        evo_worst=evo_worst,
        running=has_running(),
        api_key_set=bool(get_api_key()),
        own_domain=OWN_DOMAIN,
        default_keyword=DEFAULT_KEYWORD,
        default_max=DEFAULT_MAX_RESULTS,
    )


@seo_bp.route('/run', methods=['POST'])
@admin_required
def launch():
    keyword = (request.form.get('keyword') or DEFAULT_KEYWORD).strip() or DEFAULT_KEYWORD
    try:
        max_results = int(request.form.get('max_results') or DEFAULT_MAX_RESULTS)
    except (TypeError, ValueError):
        max_results = DEFAULT_MAX_RESULTS
    max_results = max(10, min(max_results, 600))

    if not get_api_key():
        flash("SEARCHAPI__API_KEY n'est pas configurée.", 'danger')
        return redirect(url_for('seo.dashboard'))

    if has_running():
        flash("Une extraction est déjà en cours. Patientez qu'elle se termine.", 'warning')
        return redirect(url_for('seo.dashboard'))

    start_background_run(current_app._get_current_object(), keyword=keyword, max_results=max_results)
    flash(f"Extraction lancée pour « {keyword} » ({max_results} résultats visés). "
          "La page se rafraîchit automatiquement.", 'success')
    return redirect(url_for('seo.dashboard'))
