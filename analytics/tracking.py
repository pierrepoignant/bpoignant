import hashlib
import re
from datetime import datetime, timedelta

from flask import Blueprint, current_app, render_template, request
from flask_login import current_user
from sqlalchemy import func

from init_db import db
from analytics.models import PageView
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


def register_tracking(app):
    """Wire the after_request hook. Called from create_app()."""
    app.after_request(_log_view)


# ─── ADMIN DASHBOARD ────────────────────────────────────────

def _parse_days():
    try:
        days = int(request.args.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    return max(1, min(days, 365))


@analytics_bp.route('/')
@admin_required
def dashboard():
    days = _parse_days()
    since = datetime.utcnow() - timedelta(days=days)

    total_views = db.session.query(func.count(PageView.id)).filter(
        PageView.created_at >= since
    ).scalar() or 0

    unique_visitors = db.session.query(
        func.count(func.distinct(PageView.visitor_hash))
    ).filter(PageView.created_at >= since).scalar() or 0

    top_pages = db.session.query(
        PageView.path, func.count(PageView.id).label('n')
    ).filter(PageView.created_at >= since).group_by(PageView.path).order_by(
        func.count(PageView.id).desc()
    ).limit(15).all()

    top_referrers = db.session.query(
        PageView.referrer, func.count(PageView.id).label('n')
    ).filter(
        PageView.created_at >= since, PageView.referrer.isnot(None)
    ).group_by(PageView.referrer).order_by(
        func.count(PageView.id).desc()
    ).limit(10).all()

    # Daily series: views + unique visitors per day.
    rows = db.session.query(
        func.date(PageView.created_at).label('d'),
        func.count(PageView.id).label('views'),
        func.count(func.distinct(PageView.visitor_hash)).label('uniques'),
    ).filter(PageView.created_at >= since).group_by('d').order_by('d').all()

    by_day = {str(r.d): (r.views, r.uniques) for r in rows}
    series = []
    today = datetime.utcnow().date()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        v, u = by_day.get(str(d), (0, 0))
        series.append({'date': d, 'views': v, 'uniques': u})
    max_views = max((p['views'] for p in series), default=0) or 1

    recent = PageView.query.order_by(PageView.created_at.desc()).limit(25).all()

    # Quick country breakdown when available.
    country_rows = db.session.query(
        PageView.country, func.count(PageView.id).label('n')
    ).filter(
        PageView.created_at >= since, PageView.country.isnot(None)
    ).group_by(PageView.country).order_by(func.count(PageView.id).desc()).limit(10).all()

    return render_template(
        'analytics_dashboard.html',
        days=days,
        total_views=total_views,
        unique_visitors=unique_visitors,
        top_pages=top_pages,
        top_referrers=top_referrers,
        series=series,
        max_views=max_views,
        recent=recent,
        countries=country_rows,
    )
