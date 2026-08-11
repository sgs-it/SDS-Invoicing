"""Authentication: login / logout / change password, plus a role gate."""
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, url_for, current_app
from flask_login import current_user, login_required, login_user, logout_user
from authlib.integrations.flask_client import OAuth
import os

from web import login_manager
from web.db import db
from web.models import User
from web.workflow import audit

bp = Blueprint("auth", __name__)
oauth = OAuth()


def role_required(*roles):
    """Route decorator: restrict access to the given roles."""
    def wrapper(view):
        @wraps(view)
        @login_required
        def inner(*args, **kwargs):
            if current_user.role not in roles:
                flash("You do not have permission to view that page.",
                      "danger")
                return redirect(url_for("dashboard.home"))
            return view(*args, **kwargs)
        return inner
    return wrapper


def admin_required(view):
    return role_required("Admin")(view)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user is None or not user.active or not user.check_password(password):
            flash("Invalid username or password.", "danger")
        else:
            login_user(user)
            audit(user, "Logged in", "User", user.id)
            if user.must_change_password:
                flash("Please set a new password to continue.", "warning")
                return redirect(url_for("auth.change_password"))
            nxt = request.args.get("next")
            return redirect(nxt or url_for("dashboard.home"))
    return render_template("login.html")


@bp.route('/login/google')
def login_google():
    if not oauth.google:
        flash("Google OAuth is not configured.", "danger")
        return redirect(url_for('auth.login'))
    redirect_uri = url_for('auth.auth_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route('/oauth/callback')
def auth_callback():
    if not oauth.google:
        flash("Google OAuth is not configured.", "danger")
        return redirect(url_for('auth.login'))
    
    token = oauth.google.authorize_access_token()
    userinfo = token.get('userinfo')
    
    if userinfo:
        email = userinfo['email']
        username = email.split('@')[0]
        
        user = User.query.filter_by(username=username).first()
        if not user:
            # Create a new user with 'Employee' role and set active=False (Pending Admin Approval)
            user = User(
                username=username, 
                role='Employee', 
                active=False,
                must_change_password=False
            )
            # Set a random strong password since they won't use it to log in
            import secrets
            user.set_password(secrets.token_urlsafe(32))
            db.session.add(user)
            db.session.commit()
            audit(user, f"Registered via Google OAuth: {email}", "User", user.id)
            
        if not user.active:
            flash("Your account is pending Admin approval.", "warning")
            return redirect(url_for('auth.login'))
            
        login_user(user)
        audit(user, "Logged in via Google", "User", user.id)
        return redirect(url_for("dashboard.home"))
        
    flash("Failed to authenticate with Google.", "danger")
    return redirect(url_for('auth.login'))


@bp.route("/logout")
@login_required
def logout():
    audit(current_user, "Logged out", "User", current_user.id)
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current):
            flash("Current password is incorrect.", "danger")
        elif len(new) < 6:
            flash("New password must be at least 6 characters.", "danger")
        elif new != confirm:
            flash("New passwords do not match.", "danger")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            db.session.commit()
            audit(current_user, "Changed password", "User", current_user.id)
            flash("Password updated.", "success")
            return redirect(url_for("dashboard.home"))
    return render_template("change_password.html")
