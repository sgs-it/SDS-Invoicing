"""Payment tracking: invoice amount, due / expected / actual dates,
outstanding amount and payment status."""
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from web import db
from web.models import InvoiceCycle, Payment, Project
from web.workflow import audit, recompute_cycle

bp = Blueprint("payments", __name__)


def _compute_status(p):
    """Derive payment status from dates + outstanding, unless explicit PAID."""
    if p.payment_status == "PAID":
        return p.payment_status
    outstanding = p.outstanding_amount if p.outstanding_amount is not None \
        else (p.invoice_amount or 0)
    if p.actual_payment_date and outstanding <= 0:
        return "PAID"
    if p.payment_due_date and p.payment_due_date < date.today():
        return "OVERDUE"
    return "PENDING"


@bp.route("/payments")
@login_required
def list_payments():
    f = {"status": request.args.get("status", "") or "",
         "overdue_only": bool(request.args.get("overdue"))}
    q = Payment.query.order_by(Payment.payment_due_date.asc())
    if f["status"]:
        q = q.filter(Payment.payment_status == f["status"])
    rows = q.all()
    if f["overdue_only"]:
        rows = [p for p in rows if p.payment_status == "OVERDUE"]
    total = sum(float(p.outstanding_amount if p.outstanding_amount
                      is not None else 0) for p in rows)
    return render_template("payments.html", rows=rows, filters=f,
                           total=round(total, 2))


@bp.route("/payments/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        cycle = InvoiceCycle.query.get(request.form.get("cycle_id", type=int))
        if not cycle:
            flash("Please choose a cycle.", "danger")
            return redirect(url_for("payments.new"))
        amount = _parse_amount(request.form.get("invoice_amount"))
        p = Payment(
            invoice_cycle_id=cycle.id,
            invoice_amount=amount or cycle.invoice_amount,
            payment_due_date=_parse_date(request.form.get("payment_due_date")),
            expected_payment_date=_parse_date(
                request.form.get("expected_payment_date")),
            actual_payment_date=_parse_date(
                request.form.get("actual_payment_date")),
            outstanding_amount=_parse_amount(
                request.form.get("outstanding_amount")) or None,
            payment_status=request.form.get("payment_status", "PENDING"),
            remarks=request.form.get("remarks") or None,
        )
        if p.outstanding_amount is None:
            p.outstanding_amount = p.invoice_amount
        p.payment_status = _compute_status(p)
        db.session.add(p)
        recompute_cycle(cycle)
        audit(current_user, f"Added payment record for {cycle.cycle_name}",
              "Payment", None)
        db.session.commit()
        flash("Payment record added.", "success")
        return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))

    cycle_id = request.args.get("cycle_id", type=int)
    cycles = (InvoiceCycle.query.join(Project)
              .order_by(InvoiceCycle.created_at.desc()).all())
    selected = InvoiceCycle.query.get(cycle_id) if cycle_id else None
    return render_template("payment_form.html", cycles=cycles,
                           selected=selected)


@bp.route("/payments/<int:payment_id>/edit", methods=["POST"])
@login_required
def edit(payment_id):
    p = db.get_or_404(Payment, payment_id)
    p.invoice_amount = _parse_amount(request.form.get("invoice_amount")) \
        or p.invoice_amount
    p.payment_due_date = _parse_date(request.form.get("payment_due_date")) \
        or p.payment_due_date
    p.expected_payment_date = _parse_date(
        request.form.get("expected_payment_date"))
    p.actual_payment_date = _parse_date(request.form.get("actual_payment_date"))
    p.outstanding_amount = _parse_amount(
        request.form.get("outstanding_amount")) or p.outstanding_amount
    p.remarks = request.form.get("remarks") or None
    p.payment_status = _compute_status(p)
    audit(current_user, "Updated payment record", "Payment", p.id)
    recompute_cycle(p.cycle)
    db.session.commit()
    flash("Payment updated.", "success")
    return redirect(url_for("invoices.cycle_detail", cycle_id=p.cycle_id))


@bp.route("/payments/<int:payment_id>/mark-paid", methods=["POST"])
@login_required
def mark_paid(payment_id):
    p = db.get_or_404(Payment, payment_id)
    p.actual_payment_date = date.today()
    p.outstanding_amount = 0
    p.payment_status = "PAID"
    audit(current_user, f"Marked payment as PAID: {p.cycle.cycle_name}",
          "Payment", p.id)
    recompute_cycle(p.cycle)
    db.session.commit()
    flash("Payment marked as received.", "success")
    return redirect(url_for("invoices.cycle_detail", cycle_id=p.cycle_id))


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_amount(value):
    if not value:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None
