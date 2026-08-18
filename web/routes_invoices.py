"""Invoice cycles and per-document tracking.

Handles cycle creation (which auto-generates the required-document checklist),
document advancement along its type's workflow, assignment, dates, remarks,
attachments and the document history / detail view.
"""
import os
import uuid
from datetime import date, datetime

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from web.auth import role_required

from web import db
from web.models import (
    CycleDocument,
    Department,
    DocumentHistory,
    Employee,
    InvoiceCycle,
    Payment,
    Project,
    Submission,
    WorkflowStep,
)
from web.workflow import (
    CYCLE_STATUS_LABELS,
    advance_document,
    audit,
    CallableDict,
    compute_cycle_counts,
    cycle_status_css,
    delivery_stages,
    doc_status_css,
    doc_status_label,
    doc_steps,
    find_bottleneck,
    next_action,
    recompute_cycle,
)

bp = Blueprint("invoices", __name__)


def _cycle_status_helpers():
    return cycle_status_css, CallableDict(CYCLE_STATUS_LABELS)


@bp.route("/invoices/generate-monthly", methods=["GET", "POST"])
@role_required("Admin", "Manager")
def generate_monthly():
    if request.method == "POST":
        target_month = request.form.get("target_month", "").strip()
        if not target_month:
            flash("Target month is required.", "danger")
            return redirect(url_for("invoices.generate_monthly"))
            
        from datetime import datetime
        try:
            # target_month is YYYY-MM
            dt = datetime.strptime(target_month, "%Y-%m").date()
        except ValueError:
            flash("Invalid month format.", "danger")
            return redirect(url_for("invoices.generate_monthly"))
            
        projects = Project.query.filter_by(active=True).all()
        created = 0
        skipped = 0
        
        from web.workflow import create_invoice_cycle
        from datetime import date
        import calendar
        
        # End of the selected month
        last_day = calendar.monthrange(dt.year, dt.month)[1]
        month_end = date(dt.year, dt.month, last_day)
        
        for p in projects:
            # Check contract validity
            if p.contract_start and p.contract_start > month_end:
                skipped += 1
                continue
            if p.contract_end and p.contract_end < dt:
                skipped += 1
                continue
                
            # Check if cycle already exists
            existing = InvoiceCycle.query.filter_by(project_id=p.id, invoice_month=target_month).first()
            if existing:
                skipped += 1
                continue
                
            # Create cycle
            create_invoice_cycle(p, target_month, current_user, target_submit_date=month_end)
            created += 1
            
        db.session.commit()
        flash(f"Successfully generated {created} invoice cycles for {target_month}. (Skipped {skipped} inactive/existing).", "success")
        return redirect(url_for("invoices.list_cycles", month=target_month))
        
    return render_template("generate_monthly.html")

# --------------------------------------------------------------------------- #
# Cycle list + create
# --------------------------------------------------------------------------- #
@bp.route("/invoices")
@login_required
def list_cycles():
    f = {
        "project_id": request.args.get("project_id", type=int),
        "month": request.args.get("month", "") or "",
        "status": request.args.get("status", "") or "",
    }
    q = InvoiceCycle.query.order_by(InvoiceCycle.created_at.desc())
    if f["project_id"]:
        q = q.filter(InvoiceCycle.project_id == f["project_id"])
    if f["month"]:
        q = q.filter(InvoiceCycle.invoice_month == f["month"])
    if f["status"]:
        q = q.filter(InvoiceCycle.status_code == f["status"])
    cycles = q.all()
    
    if request.args.get('export') == '1':
        import csv
        import io
        from flask import Response
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Cycle Name', 'Project', 'Month', 'Invoice Number', 'Amount', 'Docs Completed', 'Docs Total', 'Status', 'Next Action'])
        
        for c in cycles:
            c_completed = sum(1 for d in c.documents if d.is_completed())
            c_total = len(c.documents)
            status_label = CYCLE_STATUS_LABELS.get(c.status_code, c.status_code)
            
            writer.writerow([
                c.cycle_name,
                c.project.name,
                c.invoice_month,
                c.invoice_number or '',
                str(c.invoice_amount) if c.invoice_amount else '',
                c_completed,
                c_total,
                status_label,
                c.next_action_text or ''
            ])
            
        csv_data = output.getvalue()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=invoice_cycles_export.csv"}
        )

    projects = Project.query.filter_by(active=True).order_by(Project.name).all()
    months = [r[0] for r in db.session.query(
        InvoiceCycle.invoice_month).distinct()
        .order_by(InvoiceCycle.invoice_month.desc()).all() if r[0]]
    css, label = _cycle_status_helpers()
    return render_template("invoice_cycles.html", cycles=cycles,
                           projects=projects, months=months, filters=f,
                           CYCLE_CSS=css, CYCLE_STATUS=label)


@bp.route("/invoices/new", methods=["GET", "POST"])
@login_required
def cycle_new():
    if request.method == "POST":
        project = Project.query.get(request.form.get("project_id", type=int))
        if not project:
            flash("Please choose a project.", "danger")
            return redirect(url_for("invoices.cycle_new"))
        month = request.form.get("invoice_month", "").strip()
        if not month:
            flash("Invoice month is required (format YYYY-MM).", "danger")
            return redirect(url_for("invoices.cycle_new"))
        target = _parse_date(request.form.get("target_submit_date"))
        from web.workflow import create_invoice_cycle
        cycle = create_invoice_cycle(
            project, month, current_user,
            invoice_number=request.form.get("invoice_number") or None,
            invoice_amount=_parse_amount(
                request.form.get("invoice_amount")),
            target_submit_date=target,
            cycle_name=request.form.get("cycle_name") or None,
        )
        flash(f"Invoice cycle created with {len(cycle.documents)} "
              f"required documents.", "success")
        return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))

    project_id = request.args.get("project_id", type=int)
    projects = (Project.query.join(Project.client)
                .order_by(Project.client_id, Project.name).all())
    selected = Project.query.get(project_id) if project_id else None
    return render_template("invoice_cycle_form.html", projects=projects,
                           selected=selected)


# --------------------------------------------------------------------------- #
# Cycle detail (timeline + document checklist)
# --------------------------------------------------------------------------- #
@bp.route("/invoices/<int:cycle_id>")
@login_required
def cycle_detail(cycle_id):
    cycle = db.get_or_404(InvoiceCycle, cycle_id)
    counts = compute_cycle_counts(cycle)
    bottleneck, _ = find_bottleneck(cycle)
    stages = delivery_stages(cycle)
    css, label = _cycle_status_helpers()
    doc_css = doc_status_css
    doc_label = doc_status_label
    departments = Department.query.order_by(Department.code).all()
    employees = Employee.query.order_by(Employee.name).all()
    # latest submission + payment for the action links
    submission = cycle.latest_submission()
    payment = cycle.payments[-1] if cycle.payments else None
    return render_template(
        "invoice_cycle_detail.html", cycle=cycle, counts=counts,
        bottleneck=bottleneck, stages=stages, CYCLE_CSS=css,
        CYCLE_STATUS=label, DOC_CSS=doc_css, DOC_STATUS=doc_label,
        departments=departments, employees=employees,
        submission=submission, payment=payment, today=date.today(),
    )


@bp.route("/invoices/<int:cycle_id>/delete", methods=["POST"])
@login_required
def cycle_delete(cycle_id):
    cycle = db.get_or_404(InvoiceCycle, cycle_id)
    name = cycle.cycle_name
    db.session.delete(cycle)
    audit(current_user, f"Deleted invoice cycle: {name}",
          "InvoiceCycle", cycle_id)
    db.session.commit()
    flash("Invoice cycle deleted.", "info")
    return redirect(url_for("invoices.list_cycles"))


# --------------------------------------------------------------------------- #
# Document detail + actions
# --------------------------------------------------------------------------- #
@bp.route("/documents/<int:doc_id>")
@login_required
def document_detail(doc_id):
    from web.workflow import user_can_prepare, user_can_approve
    doc = db.get_or_404(CycleDocument, doc_id)
    steps = doc_steps(doc)
    departments = Department.query.order_by(Department.code).all()
    employees = Employee.query.order_by(Employee.name).all()
    can_prepare = user_can_prepare(current_user, doc.doc_type_id)
    can_approve = user_can_approve(current_user, doc.doc_type_id)
    return render_template("document_detail.html", doc=doc, steps=steps,
                           departments=departments, employees=employees,
                           can_prepare=can_prepare, can_approve=can_approve,
                           DOC_CSS=doc_status_css, DOC_STATUS=doc_status_label)


@bp.route("/documents/<int:doc_id>/advance", methods=["POST"])
@login_required
def document_advance(doc_id):
    doc = db.get_or_404(CycleDocument, doc_id)
    to_step_id = request.form.get("to_step_id", type=int)
    note = request.form.get("note", "").strip()
    ok, message = advance_document(doc, current_user, to_step_id, note)
    if not ok:
        flash(message, "danger")
    else:
        flash(f"Document advanced: {message}.", "success")
    return redirect(url_for("invoices.document_detail", doc_id=doc.id))


@bp.route("/documents/<int:doc_id>/edit", methods=["POST"])
@login_required
def document_edit(doc_id):
    doc = db.get_or_404(CycleDocument, doc_id)
    doc.department_id = request.form.get("department_id", type=int) or None
    doc.employee_id = request.form.get("employee_id", type=int) or None
    doc.preparation_date = _parse_date(request.form.get("preparation_date"))
    doc.due_date = _parse_date(request.form.get("due_date"))
    doc.remarks = request.form.get("remarks") or None
    db.session.add(DocumentHistory(
        cycle_document_id=doc.id, user_id=current_user.id,
        action="Details updated", from_status=doc.status_code,
        to_status=doc.status_code,
        note=f"Owner/date/remarks changed by {current_user.username}",
    ))
    audit(current_user, f"Updated document details: {doc.doc_type.code}",
          "CycleDocument", doc.id)
    recompute_cycle(doc.cycle)
    db.session.commit()
    flash("Document details updated.", "success")
    return redirect(url_for("invoices.document_detail", doc_id=doc.id))


@bp.route("/documents/<int:doc_id>/upload", methods=["POST"])
@login_required
def document_upload(doc_id):
    doc = db.get_or_404(CycleDocument, doc_id)
    file = request.files.get("attachment")
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1]
        fname = f"{uuid.uuid4().hex}{ext}"
        file.save(os.path.join(current_app.config["UPLOAD_FOLDER"], fname))
        doc.attachment_path = fname
        db.session.add(DocumentHistory(
            cycle_document_id=doc.id, user_id=current_user.id,
            action="Attachment uploaded", from_status=doc.status_code,
            to_status=doc.status_code, note=file.filename,
        ))
        audit(current_user,
              f"Uploaded attachment for {doc.doc_type.code}: {file.filename}",
              "CycleDocument", doc.id)
        db.session.commit()
        flash("Attachment uploaded.", "success")
    else:
        flash("No file selected.", "warning")
    return redirect(url_for("invoices.document_detail", doc_id=doc.id))


@bp.route("/documents/<int:doc_id>/attachment")
@login_required
def document_attachment(doc_id):
    doc = db.get_or_404(CycleDocument, doc_id)
    if not doc.attachment_path:
        flash("No attachment on file.", "warning")
        return redirect(url_for("invoices.document_detail", doc_id=doc.id))
    return send_from_directory(current_app.config["UPLOAD_FOLDER"],
                               doc.attachment_path, as_attachment=True)


@bp.route("/documents/<int:doc_id>/remove-attachment", methods=["POST"])
@login_required
def document_remove_attachment(doc_id):
    doc = db.get_or_404(CycleDocument, doc_id)
    doc.attachment_path = None
    db.session.add(DocumentHistory(
        cycle_document_id=doc.id, user_id=current_user.id,
        action="Attachment removed", from_status=doc.status_code,
        to_status=doc.status_code))
    db.session.commit()
    flash("Attachment removed.", "info")
    return redirect(url_for("invoices.document_detail", doc_id=doc.id))


@bp.route("/documents/<int:doc_id>/quick-toggle", methods=["POST"])
@login_required
def document_quick_toggle(doc_id):
    from web.models import Notification
    doc = db.get_or_404(CycleDocument, doc_id)
    cycle = doc.cycle
    
    completed = request.form.get("completed") == "1"
    invoice_number = request.form.get("invoice_number", "").strip()
    
    old_status = doc.status_code
    
    if completed:
        doc.status_code = "COMPLETED"
        doc.completed_at = datetime.utcnow()
        if invoice_number and doc.doc_type.code == "INV":
            cycle.invoice_number = invoice_number
        action = "Quick marked as completed"
    else:
        if doc.is_completed() and current_user.role.name not in ("Admin", "Manager"):
            flash("You do not have permission to uncheck a completed document.", "danger")
            return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))
            
        doc.status_code = "PREPARING"
        doc.completed_at = None
        action = "Quick marked as pending"
        
        notify_user_id = doc.employee_id if doc.employee_id else None
        if notify_user_id and notify_user_id != current_user.id:
            db.session.add(Notification(
                user_id=notify_user_id,
                title="Document Re-opened",
                message=f"Your completed document '{doc.doc_type.name}' in cycle '{cycle.cycle_name}' was marked as pending by {current_user.username}.",
                ntype="SYSTEM"
            ))
        
    db.session.add(DocumentHistory(
        cycle_document_id=doc.id, user_id=current_user.id,
        action=action, from_status=old_status,
        to_status=doc.status_code))
        
    db.session.commit()
    recompute_cycle(cycle)
    db.session.commit()
    
    flash(f"{doc.doc_type.name} updated.", "success")
    return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))


@bp.route("/invoices/<int:cycle_id>/quick-submission", methods=["POST"])
@login_required
def cycle_quick_submission(cycle_id):
    from web.models import Notification
    cycle = db.get_or_404(InvoiceCycle, cycle_id)
    completed = request.form.get("completed") == "1"
    
    existing = cycle.latest_submission()
    
    if completed:
        if not existing:
            sub = Submission(
                invoice_cycle_id=cycle.id,
                submission_method_id=cycle.project.submission_method_id,
                submission_date=datetime.utcnow().date(),
                submitted_by_id=current_user.id,
                status="SUBMITTED"
            )
            db.session.add(sub)
            audit(current_user, f"Quick submitted cycle {cycle.cycle_name}", "Submission", cycle.id)
            flash("Submission recorded.", "success")
    else:
        if existing:
            if current_user.role.name not in ("Admin", "Manager"):
                flash("You do not have permission to uncheck the submission.", "danger")
                return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))
                
            db.session.delete(existing)
            audit(current_user, f"Quick removed submission for {cycle.cycle_name}", "Submission", cycle.id)
            flash("Submission removed.", "info")
            
            if existing.submitted_by_id and existing.submitted_by_id != current_user.id:
                db.session.add(Notification(
                    user_id=existing.submitted_by_id,
                    title="Submission Removed",
                    message=f"The submission you recorded for '{cycle.cycle_name}' was removed by {current_user.username}.",
                    ntype="SYSTEM"
                ))
            
    db.session.commit()
    recompute_cycle(cycle)
    db.session.commit()
    return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))


@bp.route("/invoices/<int:cycle_id>/quick-payment", methods=["POST"])
@login_required
def cycle_quick_payment(cycle_id):
    from web.models import Notification
    cycle = db.get_or_404(InvoiceCycle, cycle_id)
    completed = request.form.get("completed") == "1"
    
    existing = cycle.payments[-1] if cycle.payments else None
    
    if completed:
        if not existing:
            pay = Payment(
                invoice_cycle_id=cycle.id,
                invoice_amount=cycle.invoice_amount,
                actual_payment_date=datetime.utcnow().date(),
                outstanding_amount=0,
                payment_status="PAID"
            )
            db.session.add(pay)
            audit(current_user, f"Quick paid cycle {cycle.cycle_name}", "Payment", cycle.id)
            flash("Payment recorded.", "success")
    else:
        if existing:
            if current_user.role.name not in ("Admin", "Manager"):
                flash("You do not have permission to uncheck the payment.", "danger")
                return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))
                
            db.session.delete(existing)
            audit(current_user, f"Quick removed payment for {cycle.cycle_name}", "Payment", cycle.id)
            flash("Payment removed.", "info")
            
    db.session.commit()
    recompute_cycle(cycle)
    db.session.commit()
    return redirect(url_for("invoices.cycle_detail", cycle_id=cycle.id))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
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
        return float(value.replace(",", ""))
    except ValueError:
        return None
