import os
import ssl
from datetime import datetime

from flask import Flask, render_template, redirect, url_for, jsonify
from flask_login import LoginManager, current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from init_db import db
from init_cache import cache
from config import initialize_config


def _build_database_uri(app, db_name):
    """Build a MySQL connection URI from the `database-<db_name>` config
    section. Falls back to a local SQLite file under instance/ only if no
    DB credentials are configured at all.

    `DATABASE_URL` env var takes precedence over everything — explicit
    escape hatch for local tests that must not touch the OVH MySQL.
    """
    explicit = os.environ.get('DATABASE_URL')
    if explicit:
        return explicit, {}

    section = f'database-{db_name}'
    db_config = app.config.get(section) or {}
    host = db_config.get('host')
    if not host:
        instance_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
        os.makedirs(instance_dir, exist_ok=True)
        return f"sqlite:///{os.path.join(instance_dir, 'bpoignant.db')}", {}

    uri = (
        f"mysql+pymysql://{db_config.get('user', '')}:{db_config.get('password', '')}"
        f"@{db_config.get('host', '')}:{db_config.get('port', '3306')}/{db_config.get('name', '')}"
    )
    engine_options = {
        'pool_size': 5,
        'max_overflow': 10,
        'pool_recycle': 3600,
        'pool_pre_ping': True,
        'pool_timeout': 30,
    }
    if db_name != 'local':
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        engine_options['connect_args'] = {'ssl': ssl_ctx}
    return uri, engine_options


def create_app(db_name='ovh'):
    from dotenv import load_dotenv
    load_dotenv()

    app = Flask(__name__, instance_relative_config=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev-change-me-in-production'),
        PREFERRED_URL_SCHEME='https',
        SITE_NAME=os.environ.get('SITE_NAME', 'Bernard Poignant'),
        SITE_TAGLINE=os.environ.get('SITE_TAGLINE', 'Articles & réflexions'),
        # Google Search Console verification token — set via env to enable
        # the <meta name="google-site-verification"> tag in base.html.
        GOOGLE_SITE_VERIFICATION=os.environ.get('GOOGLE_SITE_VERIFICATION', ''),
    )

    app.config['CACHE_TYPE'] = os.environ.get('CACHE_TYPE', 'SimpleCache')
    cache.init_app(app)

    initialize_config(app)

    uri, engine_options = _build_database_uri(app, db_name)
    app.config['SQLALCHEMY_DATABASE_URI'] = uri
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    if engine_options:
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = engine_options

    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    db.init_app(app)

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'

    from auth.models import User  # noqa: F401
    from auth import auth_bp
    app.register_blueprint(auth_bp)

    from articles.models import Article, Author  # noqa: F401
    from articles import articles_bp, admin_articles_bp, admin_authors_bp
    app.register_blueprint(articles_bp)
    app.register_blueprint(admin_articles_bp)
    app.register_blueprint(admin_authors_bp)

    from newsletter.models import Subscriber  # noqa: F401
    from newsletter import newsletter_bp, admin_subscribers_bp, admin_sends_bp
    app.register_blueprint(newsletter_bp)
    app.register_blueprint(admin_subscribers_bp)
    app.register_blueprint(admin_sends_bp)

    from engagement.models import Comment, Reaction  # noqa: F401
    from engagement import engagement_bp, admin_comments_bp
    app.register_blueprint(engagement_bp)
    app.register_blueprint(admin_comments_bp)

    from pages.models import Page  # noqa: F401
    from pages import pages_bp, admin_pages_bp
    app.register_blueprint(pages_bp)
    app.register_blueprint(admin_pages_bp)

    from analytics.models import PageView  # noqa: F401
    from analytics import analytics_bp, register_tracking
    app.register_blueprint(analytics_bp)
    register_tracking(app)

    from seo.models import SearchRun, GoogleUrl  # noqa: F401
    from seo import seo_bp
    app.register_blueprint(seo_bp)

    from settings.models import Config  # noqa: F401
    from settings import admin_settings_bp
    app.register_blueprint(admin_settings_bp)

    with app.app_context():
        db.create_all()
        _migrate_schema()
        _seed_admin_user()
        _seed_default_author()
        _seed_default_pages()

    @app.context_processor
    def inject_globals():
        return {
            'site_name': app.config['SITE_NAME'],
            'site_tagline': app.config['SITE_TAGLINE'],
            'google_site_verification': app.config.get('GOOGLE_SITE_VERIFICATION') or '',
            'now': datetime.utcnow(),
        }

    @app.route('/')
    def index():
        return redirect(url_for('articles.public_list'))

    @app.route('/robots.txt')
    def robots_txt():
        from flask import Response
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /admin/\n"
            "Disallow: /newsletter/\n"
            f"Sitemap: {url_for('sitemap_xml', _external=True)}\n"
        )
        return Response(body, mimetype='text/plain')

    @app.route('/sitemap.xml')
    def sitemap_xml():
        from flask import Response
        from articles.models import Article
        from pages.models import Page
        articles = (
            Article.query.filter_by(published=True)
            .order_by(Article.created_at.desc())
            .all()
        )
        urls = [{
            'loc': url_for('articles.public_list', _external=True),
            'lastmod': max((a.updated_at for a in articles), default=datetime.utcnow()).date().isoformat(),
            'changefreq': 'weekly',
            'priority': '1.0',
        }]
        for a in articles:
            urls.append({
                'loc': url_for('articles.public_show', slug=a.slug, _external=True),
                'lastmod': a.updated_at.date().isoformat(),
                'changefreq': 'monthly',
                'priority': '0.8',
            })
        for p in Page.query.filter_by(published=True).all():
            urls.append({
                'loc': url_for('pages.show', slug=p.slug, _external=True),
                'lastmod': p.updated_at.date().isoformat(),
                'changefreq': 'yearly',
                'priority': '0.4',
            })

        xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for u in urls:
            xml_lines.append('  <url>')
            xml_lines.append(f'    <loc>{u["loc"]}</loc>')
            xml_lines.append(f'    <lastmod>{u["lastmod"]}</lastmod>')
            xml_lines.append(f'    <changefreq>{u["changefreq"]}</changefreq>')
            xml_lines.append(f'    <priority>{u["priority"]}</priority>')
            xml_lines.append('  </url>')
        xml_lines.append('</urlset>')
        return Response('\n'.join(xml_lines), mimetype='application/xml')

    @app.route('/healthz')
    def health_check():
        try:
            from sqlalchemy import text
            db.session.execute(text('SELECT 1'))
            return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()}), 200
        except Exception as e:
            return jsonify({'status': 'unhealthy', 'error': str(e)}), 503

    @login_manager.user_loader
    def load_user(user_id):
        from auth.models import User
        return db.session.get(User, int(user_id))

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        try:
            db.session.remove()
        except Exception:
            pass

    @app.errorhandler(404)
    def not_found(e):
        return render_template('404.html'), 404

    return app


def _migrate_schema():
    """Lightweight forward-only migrations. SQLAlchemy's create_all only
    creates missing tables — it never ALTERs existing ones, so we run
    a few targeted ALTERs here for columns added after launch."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if 'users' in inspector.get_table_names():
        cols = {c['name'] for c in inspector.get_columns('users')}
        if 'email' not in cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL"))
            db.session.commit()

    if 'newsletter_campaigns' in inspector.get_table_names():
        cols = {c['name'] for c in inspector.get_columns('newsletter_campaigns')}
        if 'skipped_count' not in cols:
            db.session.execute(text(
                "ALTER TABLE newsletter_campaigns "
                "ADD COLUMN skipped_count INTEGER NOT NULL DEFAULT 0"
            ))
            db.session.commit()

    if 'articles' in inspector.get_table_names():
        cols = {c['name'] for c in inspector.get_columns('articles')}
        if 'newsletter_intro' not in cols:
            db.session.execute(text("ALTER TABLE articles ADD COLUMN newsletter_intro TEXT NULL"))
            db.session.commit()


def _seed_admin_user():
    """Create the bootstrap admin user if no users exist yet."""
    from auth.models import User

    if User.query.count() > 0:
        return

    username = os.environ.get('ADMIN_USERNAME', 'admin')
    password = os.environ.get('ADMIN_PASSWORD', '38HytheRoad$')

    admin = User(username=username, is_admin=True)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()


def _seed_default_author():
    """Create a default 'Bernard Poignant' author so the article form has a
    sensible option out of the box."""
    from articles.models import Author

    if Author.query.count() > 0:
        return
    db.session.add(Author(name='Bernard Poignant'))
    db.session.commit()


def _seed_default_pages():
    """Seed the three baseline editable pages on first boot: biographie,
    mentions-legales, confidentialite. Each is inserted only if its slug is
    missing — never overwritten — so the admin's edits are preserved.
    """
    from pages.models import Page
    from pages.seed_content import DEFAULT_PAGES

    for spec in DEFAULT_PAGES:
        if Page.query.filter_by(slug=spec['slug']).first():
            continue
        db.session.add(Page(
            slug=spec['slug'],
            title=spec['title'],
            meta_description=spec.get('meta_description'),
            content_html=spec['content_html'],
            published=True,
        ))
    db.session.commit()
