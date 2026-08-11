"""Submission tracking: direct / portal / email submission, reference &
confirmation numbers, rejection & resubmission, and approval."""
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from web import db
from web.models import InvoiceCycle, Payment, Project, Submission, SubmissionMethod
from web.workflow import audit, notify_assignee, recompute_cycle

bp = Blueprint("submissions", __name__)


@bp.route("/submissions")
@login_required
def list_submissions():
    f = {
        "status": request.args.get("status", "") or "",
        "method_id": request.args.get("method_id", type=int),
    }
    q = Submission.query.order_by(Submission.submission_date.desc())
    if f["status"]:
        q = q.filter(Submission.status == f["status"])
    if f["method_id"]:
        q = q.filter(Submission.submission_method_id == f["method_id"])
    rows = q.all()
    methods = SubmissionMethod.query.order_by(SubmissionMethod.name).all()
    return render_template("submissions.html", rows=rows, methods=methods,
                           filters=f)


@bp.route("/submissions/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        cycle = InvoiceCycle.query.get(request.form.get("cycle_id", type=int))
        method = SubmissionMethod.query.get(
            request.form.get("submission_method_id", type=int))
        if not cycle or not method:
            flash("Cycle and submission method are required.", "danger")
            return redirect(url_for("submissions.new"))
        sub = Submission(
            invoice_cycle_id=cycle.id,
            submission_method_id=method.id,
            submission_date=_parse_date(request.form.get("submission_date"))
            or date.today(),
            submitted_by_id=current_user.id,
            reference_no=request.form.get("reference_no") or None,
            confirmation_no=request.form.get("confirmation_no") or None,
            status=request.form.get("status", "SUBMITTED") or "SUBMITTED",
            rejection_reason=request.form.get("rejection_reason") or None,
            remarks=request.form.get("remarks") or None,
        )
        db.session.add(sub)
        recompute_cycle(cycle)
        audit(current_user, f"Registered submission for {cycle.cycle_name}",
              "Submission", None, method.name)
        db.session.commit()
        flash("Submission registered.", "success")
        return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))

    cycle_id = request.args.get("cycle_id", type=int)
    cycles = (InvoiceCycle.query.join(Project)
              .order_by(InvoiceCycle.created_at.desc()).all())
    selected = InvoiceCycle.query.get(cycle_id) if cycle_id else None
    methods = SubmissionMethod.query.order_by(SubmissionMethod.name).all()
    return render_template("submission_form.html", cycles=cycles,
                           selected=selected, methods=methods)


@bp.route("/submissions/<int:submission_id>")
@login_required
def detail(submission_id):
    sub = db.get_or_404(Submission, submission_id)
    return render_template("submission_detail.html", sub=sub)


@bp.route("/submissions/<int:submission_id>/approve", methods=["POST"])
@login_required
def approve(submission_id):
    sub = db.get_or_404(Submission, submission_id)
    sub.status = "APPROVED"
    sub.approval_date = date.today()
    sub.approved_by_id = current_user.id
    cycle = sub.cycle
    # Auto-create a payment record when none exists yet.
    if not cycle.payments:
        db.session.add(Payment(
            invoice_cycle_id=cycle.id,
            invoice_amount=cycle.invoice_amount,
            outstanding_amount=cycle.invoice_amount,
            payment_status="PENDING",
        ))
    recompute_cycle(cycle)
    audit(current_user, f"Approved submission for {cycle.cycle_name}",
          "Submission", sub.id)
    db.session.commit()
    flash("Submission approved. A payment record was opened.", "success")
    return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))


@bp.route("/submissions/<int:submission_id>/reject", methods=["POST"])
@login_required
def reject(submission_id):
    sub = db.get_or_404(Submission, submission_id)
    sub.status = "REJECTED"
    sub.rejection_reason = request.form.get("rejection_reason") or "Not specified"
    sub.approval_date = None
    recompute_cycle(sub.cycle)
    audit(current_user, f"Rejected submission for {sub.cycle.cycle_name}",
          "Submission", sub.id, sub.rejection_reason)
    db.session.commit()
    flash("Submission marked as rejected. Resubmit when ready.", "warning")
    return redirect(url_for("invoices.cycle_detail", cycle_id=sub.cycle_id))


@bp.route("/submissions/<int:submission_id>/resubmit", methods=["POST"])
@login_required
def resubmit(submission_id):
    sub = db.get_or_404(Submission, submission_id)
    sub.status = "RESUBMITTED"
    sub.approval_date = None
    recompute_cycle(sub.cycle)
    audit(current_user, f"Marked submission as resubmitted: "
                        f"{sub.cycle.cycle_name}", "Submission", sub.id)
    db.session.commit()
    flash("Submission flagged for resubmission.", "info")
    return redirect(url_for("invoices.cycle_detail", cycle_id=sub.cycle_id))


@bp.route("/submissions/<int:submission_id>/edit", methods=["POST"])
@login_required
def edit(submission_id):
    sub = db.get_or_404(Submission, submission_id)
    sub.reference_no = request.form.get("reference_no") or None
    sub.confirmation_no = request.form.get("confirmation_no") or None
    sub.submission_date = _parse_date(request.form.get("submission_date")) \
        or sub.submission_date
    sub.remarks = request.form.get("remarks") or None
    audit(current_user, "Updated submission details", "Submission", sub.id)
    recompute_cycle(sub.cycle)
    db.session.commit()
    flash("Submission updated.", "success")
    return redirect(url_for("invoices.cycle_detail", cycle_id=sub.cycle_id))


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
