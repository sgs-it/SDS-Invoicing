"""In-app notification center."""
from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from web import db
from web.models import Notification

bp = Blueprint("notifications", __name__)


@bp.route("/notifications")
@login_required
def index():
    ntype = request.args.get("type", "") or ""
    q = (Notification.query
         .filter_by(user_id=current_user.id)
         .order_by(Notification.created_at.desc()))
    if ntype:
        q = q.filter(Notification.ntype == ntype)
    rows = q.all()
    unread = sum(1 for n in rows if not n.is_read)
    return render_template("notifications.html", rows=rows, unread=unread,
                           ntype=ntype)


@bp.route("/notifications/mark-all", methods=["POST"])
@login_required
def mark_all():
    for n in Notification.query.filter_by(user_id=current_user.id,
                                          is_read=False).all():
        n.is_read = True
    db.session.commit()
    return redirect(url_for("notifications.index"))


@bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def mark_read(notification_id):
    n = db.get_or_404(Notification, notification_id)
    if n.user_id == current_user.id:
        n.is_read = True
        db.session.commit()
    return redirect(url_for("notifications.index"))
