"""Admin console: user accounts, the per-user document access matrix,
system settings and the audit log. Admin role only.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from web import db
from web.auth import admin_required
from web.models import (
    AuditLog,
    Department,
    DocumentType,
    Employee,
    ROLES,
    User,
    UserDocumentPermission,
)
from web.workflow import audit, set_setting

bp = Blueprint("admin", __name__)


def _flash_success(message):
    flash(message, "success")


# =========================================================================== #
# User accounts
# =========================================================================== #
@bp.route("/admin/users")
@admin_required
def users():
    rows = User.query.order_by(User.username).all()
    return render_template("admin/users.html", users=rows, ROLES=ROLES)


@bp.route("/admin/users/new", methods=["GET", "POST"])
@admin_required
def user_new():
    employees = Employee.query.order_by(Employee.name).all()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "Employee")
        if not username or len(password) < 6:
            flash("Username and a password of at least 6 characters are "
                  "required.", "danger")
            return redirect(url_for("admin.user_new"))
        if User.query.filter_by(username=username).first():
            flash("That username is already taken.", "danger")
            return redirect(url_for("admin.user_new"))
        if role not in ROLES:
            role = "Employee"
        emp_id = request.form.get("employee_id", type=int) or None
        if emp_id and Employee.query.get(emp_id) is None:
            emp_id = None
        u = User(
            username=username, role=role, employee_id=emp_id,
            active=bool(request.form.get("active")),
            must_change_password=bool(request.form.get("must_change_password")),
        )
        u.set_password(password)
        db.session.add(u)
        db.session.flush()
        _sync_permissions(u, request.form)
        audit(_user(), f"Created user: {username}", "User", u.id)
        db.session.commit()
        _flash_success("User created.")
        return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", user=None,
                           employees=employees, ROLES=ROLES,
                           doc_types=DocumentType.query.order_by(
                               DocumentType.sort_order,
                               DocumentType.name).all(), perms={})


@bp.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def user_edit(user_id):
    u = db.get_or_404(User, user_id)
    employees = Employee.query.order_by(Employee.name).all()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            flash("Username is required.", "danger")
            return redirect(url_for("admin.user_edit", user_id=u.id))
        clash = User.query.filter(User.username == username,
                                  User.id != u.id).first()
        if clash:
            flash("That username is already taken.", "danger")
            return redirect(url_for("admin.user_edit", user_id=u.id))
        u.username = username
        u.role = request.form.get("role", u.role) if request.form.get(
            "role", u.role) in ROLES else u.role
        emp_id = request.form.get("employee_id", type=int) or None
        if emp_id and Employee.query.get(emp_id) is None:
            emp_id = None
        u.employee_id = emp_id
        u.active = bool(request.form.get("active"))
        u.must_change_password = bool(request.form.get("must_change_password"))
        password = request.form.get("password", "")
        if password:
            if len(password) < 6:
                flash("New password must be at least 6 characters.", "danger")
                return redirect(url_for("admin.user_edit", user_id=u.id))
            u.set_password(password)
        _sync_permissions(u, request.form)
        audit(_user(), f"Updated user: {u.username}", "User", u.id)
        db.session.commit()
        _flash_success("User updated.")
        return redirect(url_for("admin.users"))
    perms = {p.doc_type_id: p for p in u.permissions}
    return render_template("admin/user_form.html", user=u,
                           employees=employees, ROLES=ROLES, perms=perms,
                           doc_types=DocumentType.query.order_by(
                               DocumentType.sort_order,
                               DocumentType.name).all())


@bp.route("/admin/users/<int:user_id>/toggle")
@admin_required
def user_toggle(user_id):
    u = db.get_or_404(User, user_id)
    if u.id == _user().id:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("admin.users"))
    u.active = not u.active
    audit(_user(), f"{'Deactivated' if not u.active else 'Activated'} "
                   f"user: {u.username}", "User", u.id)
    db.session.commit()
    _flash_success("User updated.")
    return redirect(url_for("admin.users"))


def _sync_permissions(user, form):
    """Replace the user's document access matrix from submitted checkboxes.

    Per the admin requirement: for every document type, toggle "Can Prepare"
    and "Can Approve". Admin/Manager roles bypass the matrix anyway.
    """
    UserDocumentPermission.query.filter_by(user_id=user.id).delete()
    doc_types = DocumentType.query.all()
    for dt in doc_types:
        prepare = form.get(f"prep_{dt.code}") == "1"
        approve = form.get(f"appr_{dt.code}") == "1"
        if prepare or approve:
            db.session.add(UserDocumentPermission(
                user_id=user.id, doc_type_id=dt.id,
                can_prepare=prepare, can_approve=approve))


# =========================================================================== #
# Document access matrix (standalone screen)
# =========================================================================== #
@bp.route("/admin/users/<int:user_id>/permissions", methods=["GET", "POST"])
@admin_required
def user_permissions(user_id):
    u = db.get_or_404(User, user_id)
    doc_types = DocumentType.query.order_by(DocumentType.sort_order,
                                            DocumentType.name).all()
    if request.method == "POST":
        UserDocumentPermission.query.filter_by(user_id=u.id).delete()
        for dt in doc_types:
            prepare = request.form.get(f"prep_{dt.code}") == "1"
            approve = request.form.get(f"appr_{dt.code}") == "1"
            if prepare or approve:
                db.session.add(UserDocumentPermission(
                    user_id=u.id, doc_type_id=dt.id,
                    can_prepare=prepare, can_approve=approve))
        audit(_user(), f"Updated document permissions for {u.username}",
              "User", u.id)
        db.session.commit()
        _flash_success("Document permissions updated.")
        return redirect(url_for("admin.users"))
    perms = {p.doc_type_id: p for p in u.permissions}
    return render_template("admin/user_permissions.html", user=u,
                           doc_types=doc_types, perms=perms)


# =========================================================================== #
# System settings
# =========================================================================== #
@bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def settings():
    from web.workflow import get_setting
    if request.method == "POST":
        set_setting("currency", request.form.get("currency", "AED").strip())
        set_setting("smtp_host", request.form.get("smtp_host", "").strip())
        set_setting("smtp_port", request.form.get("smtp_port", "587").strip())
        set_setting("smtp_user", request.form.get("smtp_user", "").strip())
        smtp_pass = request.form.get("smtp_password", "")
        if smtp_pass:   # keep existing password if left blank
            set_setting("smtp_password", smtp_pass)
        set_setting("smtp_from", request.form.get("smtp_from",
                                                  "SDS Invoicing").strip())
        set_setting("smtp_use_tls", "1" if request.form.get("smtp_use_tls")
                    else "")
        audit(_user(), "Updated system settings", "SystemSetting", None)
        db.session.commit()
        _flash_success("Settings saved.")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html",
                           currency=get_setting("currency", "AED"),
                           smtp_host=get_setting("smtp_host", ""),
                           smtp_port=get_setting("smtp_port", "587"),
                           smtp_user=get_setting("smtp_user", ""),
                           smtp_from=get_setting("smtp_from", "SDS Invoicing"),
                           smtp_use_tls=get_setting("smtp_use_tls", "") == "1")


# =========================================================================== #
# Audit log
# =========================================================================== #
@bp.route("/admin/audit")
@admin_required
def audit_log():
    rows = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all()
    return render_template("admin/audit_log.html", rows=rows)


def _user():
    from flask_login import current_user
    return current_user
