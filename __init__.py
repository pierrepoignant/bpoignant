import os
from datetime import datetime

from flask import Flask, render_template, redirect, url_for, jsonify, abort
from flask_login import LoginManager, current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from init_db import db
from init_cache import cache


def _build_database_uri():
    """Resolve the SQLAlchemy URI.

    Priority:
      1. `DATABASE_URL` env var (e.g. sqlite:////data/bpoignant.db, mysql+pymysql://...)
      2. Local SQLite file under instance/.
    """
    url = os.environ.get('DATABASE_URL')
    if url:
        return url
    instance_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
    os.makedirs(instance_dir, exist_ok=True)
    return f"sqlite:///{os.path.join(instance_dir, 'bpoignant.db')}"


def create_app():
    from dotenv import load_dotenv
    load_dotenv()

    app = Flask(__name__, instance_relative_config=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev-change-me-in-production'),
        PREFERRED_URL_SCHEME='https',
        SITE_NAME=os.environ.get('SITE_NAME', 'Bernard Poignant'),
        SITE_TAGLINE=os.environ.get('SITE_TAGLINE', 'Articles & réflexions'),
    )

    app.config['CACHE_TYPE'] = os.environ.get('CACHE_TYPE', 'SimpleCache')
    cache.init_app(app)

    app.config['SQLALCHEMY_DATABASE_URI'] = _build_database_uri()
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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

    from articles.models import Article  # noqa: F401
    from articles import articles_bp, admin_articles_bp
    app.register_blueprint(articles_bp)
    app.register_blueprint(admin_articles_bp)

    with app.app_context():
        db.create_all()
        _seed_admin_user()

    @app.context_processor
    def inject_globals():
        return {
            'site_name': app.config['SITE_NAME'],
            'site_tagline': app.config['SITE_TAGLINE'],
            'now': datetime.utcnow(),
        }

    @app.route('/')
    def index():
        return redirect(url_for('articles.public_list'))

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
