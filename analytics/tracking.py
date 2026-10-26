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
        # Un lien que nous avons tagué (utm_source) l'emporte sur le référent
        # HTTP : c'est l'attribution qu'on a posée nous-mêmes en partageant, et
        # elle apparaît telle quelle dans la liste des sources (« facebook »,
        # « threads », « x »…).
        utm = (request.args.get('utm_source') or '').strip()
        if utm:
            ref = utm[:120]

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


# Ramener un référent (URL complète ou jeton utm que nous avons posé) à un nom
# de source propre, pour que « X », « Threads », « Facebook »… se regroupent au
# lieu de s'éparpiller en t.co, lnkd.in, l.facebook.com, etc.
# Jetons utm que nous posons nous-mêmes : correspondance EXACTE (le référent
# vaut « x », « threads »…), jamais une sous-chaîne — sinon « x » attraperait
# n'importe quelle URL contenant la lettre x.
_SOURCES_UTM = {
    'x': 'X', 'twitter': 'X',
    'threads': 'Threads',
    'facebook': 'Facebook', 'facebook-profil': 'Facebook (profil)',
    'instagram': 'Instagram', 'linkedin': 'LinkedIn',
    'youtube': 'YouTube', 'tiktok': 'TikTok',
}
# Hôtes (sous-chaîne du domaine) : correspondance par morceau de nom de domaine.
_SOURCES_HOTE = [
    (('t.co', 'twitter.com', 'x.com'), 'X'),
    (('threads.net', 'threads.com'), 'Threads'),
    (('l.facebook', 'lm.facebook', 'm.facebook', 'facebook.com', 'fb.me', 'fb.com'), 'Facebook'),
    (('l.instagram', 'instagram.com'), 'Instagram'),
    (('linkedin.com', 'lnkd.in'), 'LinkedIn'),
    (('youtube.com', 'youtu.be'), 'YouTube'),
    (('tiktok.com',), 'TikTok'),
    (('google.',), 'Google'),
    (('bing.com',), 'Bing'),
    (('duckduckgo',), 'DuckDuckGo'),
    (('yahoo.',), 'Yahoo'),
    (('t.me',), 'Telegram'),
    (('mail.', 'webmail', 'outlook.', 'gmail.'), 'E-mail'),
]


def source_name(referrer):
    """Nom de source lisible pour un référent, ou « Direct » s'il n'y en a pas."""
    r = (referrer or '').strip().lower()
    if not r:
        return 'Direct'
    # Jeton utm posé par nous : égalité stricte.
    if r in _SOURCES_UTM:
        return _SOURCES_UTM[r]
    # URL : on regarde le domaine.
    from urllib.parse import urlparse
    hote = urlparse(r if '//' in r else '//' + r).netloc.lower()
    cible = hote or r
    for cles, nom in _SOURCES_HOTE:
        if any(c in cible for c in cles):
            return nom
    return hote[4:] if hote.startswith('www.') else (hote or 'Autre')


@analytics_bp.app_template_filter('source_trafic')
def _source_trafic(referrer):
    return source_name(referrer)


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

    # Toutes les visites de la fenêtre, regroupées par référent brut, puis
    # repliées sur un nom de source propre (X, Threads, Facebook…). « Direct »
    # — sans référent — est compté aussi : c'est une source à part entière.
    refs_bruts = db.session.query(
        PageView.referrer, func.count(PageView.id).label('n')
    ).filter(*window).group_by(PageView.referrer).all()
    _par_source = {}
    for ref, n in refs_bruts:
        nom = source_name(ref)
        _par_source[nom] = _par_source.get(nom, 0) + n
    top_referrers = [{'source': nom, 'n': n}
                     for nom, n in sorted(_par_source.items(), key=lambda kv: kv[1], reverse=True)[:12]]

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

    # Clics sur le lien Amazon du livre (via la redirection suivie
    # /articles/livre/amazon) sur la fenêtre : total et visiteurs uniques.
    amazon_window = window + [PageView.path == '/articles/livre/amazon']
    amazon_clicks = db.session.query(func.count(PageView.id)).filter(*amazon_window).scalar() or 0
    amazon_uniques = db.session.query(
        func.count(func.distinct(PageView.visitor_hash))
    ).filter(*amazon_window).scalar() or 0

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
        amazon_clicks=amazon_clicks,
        amazon_uniques=amazon_uniques,
        countries=country_rows,
        top_searches=top_searches,
        empty_searches=empty_searches,
        total_searches=total_searches,
    )


# ─── Statistiques agrégées par réseau ───────────────────────
#
# YouTube, Facebook, Instagram et Threads ne donnent qu'un total courant, sans
# historique (contrairement au site, dont on garde chaque lecture). On présente
# donc des totaux et un classement des publications, pas une courbe dans le
# temps — c'est tout ce que les API offrent sans relevés quotidiens conservés.

def _reseau_data(reseau):
    from tiktok.models import TikTokPost
    from articles.models import Article

    def somme(rows, attr):
        return sum((getattr(r, attr) or 0) for r in rows)

    if reseau == 'youtube':
        vids = TikTokPost.query.filter(TikTokPost.youtube_video_id.isnot(None)).all()
        items = sorted(vids, key=lambda v: (v.youtube_views or 0), reverse=True)
        return {
            'titre': 'YouTube', 'icone': 'fa-brands fa-youtube', 'couleur': '#FF0000',
            'totaux': [{'label': 'vidéos', 'valeur': len(vids)},
                       {'label': 'vues', 'valeur': somme(vids, 'youtube_views')},
                       {'label': "j'aime", 'valeur': somme(vids, 'youtube_likes')},
                       {'label': 'commentaires', 'valeur': somme(vids, 'youtube_comments')}],
            'posts': [{'titre': v.title, 'url': v.youtube_url, 'date': v.youtube_posted_at,
                       'metriques': [{'label': 'vues', 'valeur': v.youtube_views},
                                     {'label': "j'aime", 'valeur': v.youtube_likes},
                                     {'label': 'comm.', 'valeur': v.youtube_comments}]} for v in items],
        }
    if reseau == 'instagram':
        vids = TikTokPost.query.filter(TikTokPost.instagram_media_id.isnot(None)).all()
        items = sorted(vids, key=lambda v: (v.instagram_views or 0), reverse=True)
        return {
            'titre': 'Instagram', 'icone': 'fa-brands fa-instagram', 'couleur': '#E4405F',
            'totaux': [{'label': 'Reels', 'valeur': len(vids)},
                       {'label': 'vues', 'valeur': somme(vids, 'instagram_views')},
                       {'label': "j'aime", 'valeur': somme(vids, 'instagram_likes')},
                       {'label': 'commentaires', 'valeur': somme(vids, 'instagram_comments')}],
            'posts': [{'titre': v.title, 'url': v.instagram_url, 'date': v.instagram_posted_at,
                       'metriques': [{'label': 'vues', 'valeur': v.instagram_views},
                                     {'label': "j'aime", 'valeur': v.instagram_likes},
                                     {'label': 'comm.', 'valeur': v.instagram_comments}]} for v in items],
        }
    if reseau == 'threads':
        arts = Article.query.filter(Article.threads_post_id.isnot(None)).all()
        items = sorted(arts, key=lambda a: (a.threads_views or 0), reverse=True)
        return {
            'titre': 'Threads', 'icone': 'fa-brands fa-threads', 'couleur': '#000',
            'totaux': [{'label': 'articles', 'valeur': len(arts)},
                       {'label': 'vues', 'valeur': somme(arts, 'threads_views')},
                       {'label': "j'aime", 'valeur': somme(arts, 'threads_likes')},
                       {'label': 'réponses', 'valeur': somme(arts, 'threads_replies')},
                       {'label': 'reposts', 'valeur': somme(arts, 'threads_reposts')}],
            'posts': [{'titre': a.title, 'url': a.threads_post_url, 'date': a.threads_posted_at,
                       'metriques': [{'label': 'vues', 'valeur': a.threads_views},
                                     {'label': "j'aime", 'valeur': a.threads_likes},
                                     {'label': 'réponses', 'valeur': a.threads_replies},
                                     {'label': 'reposts', 'valeur': a.threads_reposts}]} for a in items],
        }
    if reseau == 'facebook':
        # Facebook mêle deux choses : les vidéos de la Page (vues) et les
        # publications de lien des articles (j'aime, commentaires).
        vids = TikTokPost.query.filter(TikTokPost.fb_post_id.isnot(None)).all()
        arts = Article.query.filter(Article.fb_page_post_id.isnot(None)).all()
        items = ([{'titre': v.title, 'url': v.facebook_url, 'date': v.fb_posted_at, 'genre': 'vidéo',
                   'metriques': [{'label': 'vues', 'valeur': v.fb_views}]} for v in vids]
                 + [{'titre': a.title, 'url': a.fb_page_post_url, 'date': a.fb_page_posted_at, 'genre': 'article',
                     'metriques': [{'label': "j'aime", 'valeur': a.fb_page_likes},
                                   {'label': 'commentaires', 'valeur': a.fb_page_comments},
                                   {'label': 'partages', 'valeur': a.fb_page_shares}]} for a in arts])
        items.sort(key=lambda i: i['date'] or datetime.min, reverse=True)
        return {
            'titre': 'Facebook (Page)', 'icone': 'fa-brands fa-facebook', 'couleur': '#1877F2',
            'totaux': [{'label': 'vidéos', 'valeur': len(vids)},
                       {'label': 'vues vidéos', 'valeur': somme(vids, 'fb_views')},
                       {'label': 'articles', 'valeur': len(arts)},
                       {'label': "j'aime articles", 'valeur': somme(arts, 'fb_page_likes')},
                       {'label': 'commentaires articles', 'valeur': somme(arts, 'fb_page_comments')}],
            'posts': items,
            'note': 'Le profil personnel de Bernard est suivi à part, sur la fiche de chaque article.',
        }
    return None


@analytics_bp.route('/reseau/<reseau>')
@admin_required
def reseau_stats(reseau):
    from flask import abort
    data = _reseau_data((reseau or '').lower())
    if data is None:
        abort(404)
    return render_template('reseau_stats.html', reseau=reseau.lower(), data=data)


# ─── Portée totale : vues cumulées tous réseaux, par publication ────
#
# Chaque réseau mesure une « vue » à sa façon (une lecture sur le site, une vue
# TikTok, une impression X…) : les additionner donne un ordre de grandeur de la
# portée, pas une mesure exacte. C'est assumé — une seule ligne, « combien de
# monde ce texte a-t-il touché, en gros ».

def _n(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _reach_videos():
    from tiktok.models import TikTokPost, VideoView
    cols = ['Site', 'TikTok', 'X', 'YouTube', 'Facebook', 'Instagram', 'Profil FB']
    vues_site = dict(db.session.query(VideoView.post_id, func.count(VideoView.id))
                     .group_by(VideoView.post_id).all())
    lignes = []
    for p in TikTokPost.query.filter(TikTokPost.video_url.isnot(None)).all():
        vals = [_n(vues_site.get(p.id)), _n(p.views), _n(p.x_view_count),
                _n(p.youtube_views), _n(p.fb_views), _n(p.instagram_views),
                _n(p.fb_profile_views)]
        lignes.append({'titre': p.title, 'url': None, 'date': p.posted_at,
                       'valeurs': vals, 'total': sum(vals)})
    return cols, lignes


def _reach_articles():
    from articles.models import Article
    from analytics.models import PageView
    cols = ['Site', 'X', 'Threads', 'Profil FB']
    vues_path = dict(db.session.query(PageView.path, func.count(PageView.id))
                     .group_by(PageView.path).all())
    lignes = []
    for a in Article.query.filter(Article.published.is_(True)).all():
        site = _n(vues_path.get(f'/articles/{a.slug}'))
        vals = [site, _n(a.x_view_count), _n(a.threads_views), _n(a.fb_profile_views)]
        lignes.append({'titre': a.title, 'url': None, 'date': a.created_at,
                       'valeurs': vals, 'total': sum(vals)})
    return cols, lignes


def _rendre_portee(titre, icone, cols, lignes, tri='total'):
    from datetime import datetime as _dt
    if tri == 'date':
        lignes.sort(key=lambda r: r['date'] or _dt.min, reverse=True)
    else:
        lignes.sort(key=lambda r: r['total'], reverse=True)
    totaux = [sum(r['valeurs'][i] for r in lignes) for i in range(len(cols))]
    return render_template('portee.html', titre=titre, icone=icone, colonnes=cols,
                           lignes=lignes, totaux=totaux,
                           total_general=sum(totaux))


@analytics_bp.route('/portee/videos')
@admin_required
def portee_videos():
    cols, lignes = _reach_videos()
    return _rendre_portee('La Minute — portée totale', 'fas fa-film', cols, lignes, tri='date')


@analytics_bp.route('/portee/articles')
@admin_required
def portee_articles():
    cols, lignes = _reach_articles()
    return _rendre_portee('Articles — portée totale', 'fas fa-newspaper', cols, lignes, tri='date')
