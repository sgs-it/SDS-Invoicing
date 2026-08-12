"""Application factory. Creates the Flask app, wires the database, login
manager, blueprints, and the scheduler thread.
"""
import os
import threading

from flask import Flask, redirect, url_for
from flask_login import LoginManager

from config import Config
from web.db import db


login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"


def _ensure_dirs(upload_folder):
    os.makedirs(upload_folder, exist_ok=True)


@login_manager.user_loader
def load_user(user_id):
    from web.models import User
    return db.session.get(User, int(user_id))


def create_app(config=Config):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(config)
    _ensure_dirs(app.config["UPLOAD_FOLDER"])

    db.init_app(app)
    login_manager.init_app(app)

    # Blueprints
    from web import auth
    auth.oauth.init_app(app)
    if app.config.get("GOOGLE_CLIENT_ID"):
        auth.oauth.register(
            name='google',
            server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
            client_kwargs={'scope': 'openid email profile'}
        )

    from web import routes_dashboard
    from web import routes_masters
    from web import routes_invoices
    from web import routes_submissions
    from web import routes_payments
    from web import routes_reports
    from web import routes_notifications
    from web import routes_admin

    app.register_blueprint(auth.bp)
    app.register_blueprint(routes_dashboard.bp)
    app.register_blueprint(routes_masters.bp)
    app.register_blueprint(routes_invoices.bp)
    app.register_blueprint(routes_submissions.bp)
    app.register_blueprint(routes_payments.bp)
    app.register_blueprint(routes_reports.bp)
    app.register_blueprint(routes_notifications.bp)
    app.register_blueprint(routes_admin.bp)

    @app.route("/")
    def index():
        return redirect(url_for("dashboard.home"))

    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        from web.models import Notification
        from web.workflow import get_setting, user_can_prepare, user_can_approve
        currency = get_setting("currency", "AED")
        unread = 0
        if current_user.is_authenticated:
            unread = Notification.query.filter_by(
                user_id=current_user.id, is_read=False).count()
        return {"CURRENCY": currency, "APP_NAME": "SDS Invoicing Tracker",
                "unread_count": unread, 
                "user_can_prepare": user_can_prepare, 
                "user_can_approve": user_can_approve}

    with app.app_context():
        db.create_all()
        _seed_defaults(app)

    _start_scheduler(app)

    return app


def _seed_defaults(app):
    """Create the default admin account and core master rows on first run."""
    from web.seed import ensure_master_data, ensure_admin_user
    ensure_master_data()
    ensure_admin_user()


def _start_scheduler(app):
    """Start the background due-date / escalation sweep."""
    if not app.config.get("SCHEDULER_ENABLED"):
        return
    from web.workflow import scheduled_sweep

    interval = app.config.get("SCHEDULER_INTERVAL_SECONDS", 3600)

    def loop():
        import time
        with app.app_context():
            try:
                scheduled_sweep()
            except Exception as exc:  # pragma: no cover - guard the loop
                app.logger.error("Scheduled sweep failed: %s", exc)
        time.sleep(interval)

    def runner():  # pragma: no cover - thread target
        while True:
            loop()

    t = threading.Thread(target=runner, name="sds-scheduler", daemon=True)
    t.start()
