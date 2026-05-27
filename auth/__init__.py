import functools

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for, current_app
)
from flask_login import login_user, logout_user, login_required, current_user

from init_db import db
from auth.models import User

auth_bp = Blueprint('auth', __name__, url_prefix='/admin', template_folder='templates')


def admin_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login', next=request.url))
        if not getattr(current_user, 'is_admin', False):
            flash("Accès réservé aux administrateurs.", 'danger')
            return redirect(url_for('articles.public_list'))
        return view(**kwargs)
    return wrapped_view


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_articles.list_articles'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        user = User.query.filter_by(username=username).first()
        if user is None or not user.check_password(password):
            flash("Identifiants invalides.", 'danger')
            return render_template('login.html', username=username)

        user.update_last_login()
        login_user(user, remember=True)
        current_app.logger.info(f"Login successful for {username}")
        next_page = request.args.get('next') or url_for('admin_articles.list_articles')
        return redirect(next_page)

    return render_template('login.html', username='')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Vous êtes déconnecté.", 'info')
    return redirect(url_for('articles.public_list'))


@auth_bp.route('/users', methods=['GET'])
@admin_required
def list_users():
    users = User.query.order_by(User.username).all()
    return render_template('users.html', users=users)


@auth_bp.route('/users/new', methods=['GET', 'POST'])
@admin_required
def create_user():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        is_admin = request.form.get('is_admin') == 'on'

        if not username or not password:
            flash("Nom d'utilisateur et mot de passe requis.", 'danger')
            return redirect(url_for('auth.create_user'))

        if User.query.filter_by(username=username).first():
            flash(f'Utilisateur "{username}" déjà existant.', 'danger')
            return redirect(url_for('auth.create_user'))

        user = User(username=username, is_admin=is_admin)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash(f'Utilisateur "{username}" créé.', 'success')
        return redirect(url_for('auth.list_users'))

    return render_template('user_form.html', user=None)


@auth_bp.route('/users/<int:user_id>/password', methods=['POST'])
@admin_required
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    password = request.form.get('password') or ''
    if not password:
        flash("Mot de passe vide.", 'danger')
        return redirect(url_for('auth.list_users'))
    user.set_password(password)
    db.session.commit()
    flash(f'Mot de passe mis à jour pour {user.username}.', 'success')
    return redirect(url_for('auth.list_users'))


@auth_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Vous ne pouvez pas supprimer votre propre compte.", 'danger')
        return redirect(url_for('auth.list_users'))
    db.session.delete(user)
    db.session.commit()
    flash("Utilisateur supprimé.", 'success')
    return redirect(url_for('auth.list_users'))
