import hashlib
import re
from datetime import datetime, date, time, timedelta

from flask import Blueprint, current_app, render_template, request
from flask_login import current_user
from sqlalchemy import func

from init_db import db
from analytics.models import PageView, SearchQuery
from auth import admin_required


analytics_bp = Blueprint(
    'analytics', __name__, url_prefix='/admin/analytics', template_folder='templates'
)


_BOT_RE = re.compile(
    r'bot|crawl|spider|slurp|bingpreview|facebookexternalhit|whatsapp|telegrambot|'
    r'discordbot|linkedinbot|preview|monitor|uptime|pingdom|lighthouse|headless',
    re.IGNORECASE,
)

# Endpoints whose paths we never log (admin/auth/static/health/tracking itself).
_SKIP_BLUEPRINTS = {'auth', 'admin_articles', 'admin_authors', 'admin_subscribers',
                    'analytics', 'static'}
_SKIP_PATH_PREFIXES = ('/admin', '/static', '/healthz', '/newsletter/unsubscribe')


def _client_ip():
    # ProxyFix already rewrote remote_addr from X-Forwarded-For.
    return request.remote_addr or ''


def _visitor_hash(ip, user_agent):
    """Daily-rotating hash so we can count unique visitors per day without
    storing raw IPs. Salt rotates each UTC day."""
    salt = current_app.config.get('SECRET_KEY', '') + datetime.utcnow().strftime('%Y-%m-%d')
    digest = hashlib.sha256(f"{ip}|{user_agent}|{salt}".encode('utf-8')).hexdigest()
    return digest[:32]


def _should_skip():
    if request.method != 'GET':
        return True
    if request.path.startswith(_SKIP_PATH_PREFIXES):
        return True
    if request.endpoint:
        bp = request.endpoint.split('.', 1)[0]
        if bp in _SKIP_BLUEPRINTS:
            return True
    if getattr(current_user, 'is_authenticated', False) and getattr(current_user, 'is_admin', False):
        # Don't pollute stats with admin browsing.
        return True
    ua = request.headers.get('User-Agent', '')
    if not ua or _BOT_RE.search(ua):
        return True
    return False


def _log_view(response):
    try:
        if response.status_code >= 400:
            return response
        if _should_skip():
            return response

        ua = request.headers.get('User-Agent', '')[:512]
        ref = request.referrer or None
        # Drop self-referrals so internal navigation doesn't dominate the
        # "top referrers" list.
        if ref and request.host_url and ref.startswith(request.host_url):
            ref = None

        view = PageView(
            path=request.path[:512],
            referrer=(ref[:512] if ref else None),
            visitor_hash=_visitor_hash(_client_ip(), ua),
            user_agent=ua or None,
            country=(request.headers.get('CF-IPCountry') or
                     request.headers.get('X-Country-Code') or None),
        )
        db.session.add(view)
        db.session.commit()
    except Exception as exc:
        current_app.logger.warning(f"analytics: failed to log view: {exc}")
        db.session.rollback()
    return response


def log_search(query, results_count):
    """Record a public search. Applies the same bot/admin filtering as page
    views, so the two datasets stay comparable and admin testing doesn't show
    up as reader demand. Never raises: a failed log must not break the page."""
    try:
        if _should_skip():
            return
        normalised = re.sub(r'\s+', ' ', (query or '')).strip().lower()
        if not normalised:
            return
        db.session.add(SearchQuery(term=normalised[:200],
                                   results_count=results_count))
        db.session.commit()
    except Exception as exc:
        current_app.logger.warning(f"analytics: failed to log search: {exc}")
        db.session.rollback()


def register_tracking(app):
    """Wire the after_request hook. Called from create_app()."""
    app.after_request(_log_view)


# ─── ADMIN DASHBOARD ────────────────────────────────────────

_FR_MONTHS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.',
              'août', 'sept.', 'oct.', 'nov.', 'déc.']


def default_grouping(days):
    """Sensible bucket size for a window: short spans by day, ~quarter by
    week, a year by month."""
    if days <= 31:
        return 'day'
    if days <= 92:
        return 'week'
    return 'month'


def stat_buckets(days, group, today):
    """Ordered time buckets covering the last ``days`` days, each a dict with an
    exclusive [start, end) date range and display labels. Weeks start on Monday;
    months on the 1st. Boundary buckets may reach just before the window start so
    a partial week/month is still shown whole."""
    start = today - timedelta(days=days - 1)
    buckets = []
    if group == 'week':
        cur = start - timedelta(days=start.weekday())  # back to Monday
        while cur <= today:
            end = cur + timedelta(days=7)
            buckets.append({'start': cur, 'end': end,
                            'label': cur.strftime('%d/%m'),
                            'full': 'Semaine du ' + cur.strftime('%d/%m/%Y')})
            cur = end
    elif group == 'month':
        cur = date(start.year, start.month, 1)
        while cur <= today:
            end = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
            buckets.append({'start': cur, 'end': end,
                            'label': _FR_MONTHS[cur.month - 1],
                            'full': _FR_MONTHS[cur.month - 1] + ' ' + str(cur.year)})
            cur = end
    else:  # day
        cur = start
        while cur <= today:
            end = cur + timedelta(days=1)
            buckets.append({'start': cur, 'end': end,
                            'label': cur.strftime('%d/%m'),
                            'full': cur.strftime('%d/%m/%Y')})
            cur = end
    return buckets


def bucketed_series(base_window, days, group, today):
    """Views + unique visitors per bucket for the given base filter
    ``base_window`` (a list of SQLAlchemy conditions, e.g. a path restriction).

    Day grouping runs one grouped query; week/month count uniques per bucket
    over each bucket's own range, since distinct visitors can't be summed from
    daily counts. Buckets are few for week/month, so it stays cheap."""
    buckets = stat_buckets(days, group, today)
    if not buckets:
        return []

    if group == 'day':
        lo = datetime.combine(buckets[0]['start'], time.min)
        hi = datetime.combine(buckets[-1]['end'], time.min)
        rows = db.session.query(
            func.date(PageView.created_at).label('d'),
            func.count(PageView.id),
            func.count(func.distinct(PageView.visitor_hash)),
        ).filter(*base_window, PageView.created_at >= lo,
                 PageView.created_at < hi).group_by('d').all()
        by_day = {str(r[0]): (r[1], r[2]) for r in rows}
        return [
            {'label': b['label'], 'full': b['full'],
             'views': by_day.get(str(b['start']), (0, 0))[0],
             'uniques': by_day.get(str(b['start']), (0, 0))[1]}
            for b in buckets
        ]

    series = []
    for b in buckets:
        row = db.session.query(
            func.count(PageView.id),
            func.count(func.distinct(PageView.visitor_hash)),
        ).filter(
            *base_window,
            PageView.created_at >= datetime.combine(b['start'], time.min),
            PageView.created_at < datetime.combine(b['end'], time.min),
        ).one()
        series.append({'label': b['label'], 'full': b['full'],
                       'views': row[0] or 0, 'uniques': row[1] or 0})
    return series


def _parse_days():
    try:
        days = int(request.args.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    return max(1, min(days, 365))


def _parse_group(days):
    group = request.args.get('group')
    if group not in ('day', 'week', 'month'):
        group = default_grouping(days)
    return group


@analytics_bp.route('/')
@admin_required
def dashboard():
    days = _parse_days()
    group = _parse_group(days)
    today = datetime.utcnow().date()

    # Build the window for the KPI totals. days == 1 is the "Hier" shortcut:
    # the full previous calendar day, bounded above so today's partial data
    # doesn't leak in. Everything else is a rolling "last N days" window
    # ending today.
    if days == 1:
        start = today - timedelta(days=1)
        since = datetime.combine(start, datetime.min.time())
        until = datetime.combine(today, datetime.min.time())
        group = 'day'
    else:
        since = datetime.utcnow() - timedelta(days=days)
        until = None

    window = [PageView.created_at >= since]
    if until is not None:
        window.append(PageView.created_at < until)

    total_views = db.session.query(func.count(PageView.id)).filter(
        *window
    ).scalar() or 0

    unique_visitors = db.session.query(
        func.count(func.distinct(PageView.visitor_hash))
    ).filter(*window).scalar() or 0

    top_pages = db.session.query(
        PageView.path, func.count(PageView.id).label('n')
    ).filter(*window).group_by(PageView.path).order_by(
        func.count(PageView.id).desc()
    ).limit(15).all()

    top_referrers = db.session.query(
        PageView.referrer, func.count(PageView.id).label('n')
    ).filter(
        *window, PageView.referrer.isnot(None)
    ).group_by(PageView.referrer).order_by(
        func.count(PageView.id).desc()
    ).limit(10).all()

    # Searches over the same window. Two lists on purpose: the popular terms
    # say what readers come for, the empty-handed ones say what the archive is
    # missing — which is the list worth acting on.
    search_window = [SearchQuery.created_at >= since]
    if until is not None:
        search_window.append(SearchQuery.created_at < until)

    top_searches = db.session.query(
        SearchQuery.term,
        func.count(SearchQuery.id).label('n'),
        func.max(SearchQuery.results_count).label('results'),
    ).filter(*search_window).group_by(SearchQuery.term).order_by(
        func.count(SearchQuery.id).desc()
    ).limit(15).all()

    empty_searches = db.session.query(
        SearchQuery.term, func.count(SearchQuery.id).label('n')
    ).filter(*search_window, SearchQuery.results_count == 0).group_by(
        SearchQuery.term
    ).order_by(func.count(SearchQuery.id).desc()).limit(15).all()

    total_searches = db.session.query(func.count(SearchQuery.id)).filter(
        *search_window
    ).scalar() or 0

    # Series: views + unique visitors per bucket (day / week / month). The "Hier"
    # shortcut keeps its single upper-bounded day; everything else is bucketed
    # over the window, with whole boundary weeks/months shown.
    if days == 1:
        series = [{'label': start.strftime('%d/%m'), 'full': start.strftime('%d/%m/%Y'),
                   'views': total_views, 'uniques': unique_visitors}]
    else:
        series = bucketed_series([], days, group, today)
    max_views = max((p['views'] for p in series), default=0) or 1

    # Per-bucket averages shown on the KPI cards.
    n_buckets = len(series)
    avg_views = round(total_views / n_buckets, 1) if n_buckets else 0
    avg_uniques = round(unique_visitors / n_buckets, 1) if n_buckets else 0

    # Compact, JSON-serialisable copy of the series for the client-side chart.
    # Carries all three switchable metrics (trafic / visiteurs / pages par
    # visiteur) plus pre-formatted labels.
    chart_data = [
        {
            'label': p['label'],
            'full': p['full'],
            'views': p['views'],
            'uniques': p['uniques'],
            'ratio': round(p['views'] / p['uniques'], 1) if p['uniques'] else 0,
        }
        for p in series
    ]

    recent = PageView.query.order_by(PageView.created_at.desc()).limit(25).all()

    # Quick country breakdown when available.
    country_rows = db.session.query(
        PageView.country, func.count(PageView.id).label('n')
    ).filter(
        *window, PageView.country.isnot(None)
    ).group_by(PageView.country).order_by(func.count(PageView.id).desc()).limit(10).all()

    return render_template(
        'analytics_dashboard.html',
        days=days,
        group=group,
        total_views=total_views,
        unique_visitors=unique_visitors,
        avg_views=avg_views,
        avg_uniques=avg_uniques,
        top_pages=top_pages,
        top_referrers=top_referrers,
        series=series,
        chart_data=chart_data,
        max_views=max_views,
        recent=recent,
        countries=country_rows,
        top_searches=top_searches,
        empty_searches=empty_searches,
        total_searches=total_searches,
    )
