"""Dashboard: real-time monitoring tiles, filters, bottleneck panel,
overdue / upcoming lists and recent activity."""
from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from web import db
from web.models import (
    Client,
    CycleDocument,
    DocumentHistory,
    InvoiceCycle,
    Project,
    SubmissionMethod,
)
from web.workflow import (
    CYCLE_STATUS_LABELS,
    CallableDict,
    cycle_status_css,
    doc_status_css,
    doc_status_label,
    generate_monthly_cycles,
    month_calendar,
)

bp = Blueprint("dashboard", __name__)


def _active_buckets(doc):
    """Classify a single document into a dashboard bucket."""
    if doc.is_completed():
        return "completed"
    if doc.is_waiting_client() or doc.status_code in ("REQUESTED",
                                                      "GRN_REQUESTED"):
        return "waiting_client"
    if doc.status_code in ("PREPARING", "INTERNAL_REVIEW", "VERIFIED",
                           "READY", "ADDED_INVOICE", "RECEIVED"):
        return "internal"
    return "not_started"


def _filtered_cycles(f):
    """Invoice cycles matching the dashboard filters."""
    q = (InvoiceCycle.query
         .options(
             joinedload(InvoiceCycle.documents).joinedload(CycleDocument.doc_type),
             joinedload(InvoiceCycle.payments),
             joinedload(InvoiceCycle.project).joinedload(Project.client),
             joinedload(InvoiceCycle.project).joinedload(Project.submission_method)
         )
         .join(Project)
         .join(Client, Client.id == Project.client_id)
         .join(SubmissionMethod,
               SubmissionMethod.id == Project.submission_method_id))
    if f.get("client_id"):
        q = q.filter(Client.id == f["client_id"])
    if f.get("project_id"):
        q = q.filter(InvoiceCycle.project_id == f["project_id"])
    if f.get("from_date"):
        q = q.filter(InvoiceCycle.invoice_month >= f["from_date"][:7])
    if f.get("to_date"):
        q = q.filter(InvoiceCycle.invoice_month <= f["to_date"][:7])
    if f.get("method_id"):
        q = q.filter(Project.submission_method_id == f["method_id"])
    if f.get("status"):
        q = q.filter(InvoiceCycle.status_code == f["status"])
    return q.all()


def _month_list():
    return [r[0] for r in db.session.query(
        InvoiceCycle.invoice_month).distinct()
        .order_by(InvoiceCycle.invoice_month.desc()).all() if r[0]]


@bp.route("/dashboard")
@login_required
def home():
    f = {
        "client_id": request.args.get("client_id", type=int),
        "project_id": request.args.get("project_id", type=int),
        "from_date": request.args.get("from_date", "") or "",
        "to_date": request.args.get("to_date", "") or "",
        "method_id": request.args.get("method_id", type=int),
        "status": request.args.get("status", "") or "",
    }
    cycles = _filtered_cycles(f)

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
            headers={"Content-Disposition": "attachment;filename=dashboard_export.csv"}
        )

    # ----- monthly calendar (specific-month details only) -----
    cal_month = request.args.get("cal_month", "") or ""
    if not cal_month:
        cal_month = date.today().strftime("%Y-%m")
    cal = month_calendar(cal_month)

    # ----- aggregate document buckets across filtered cycles -----
    agg = {"total": 0, "completed": 0, "waiting_client": 0,
           "internal": 0, "not_started": 0, "overdue_docs": 0}
    for c in cycles:
        for d in c.documents:
            agg["total"] += 1
            agg[_active_buckets(d)] += 1
            if d.due_date and d.due_date < date.today() and not d.is_completed():
                agg["overdue_docs"] += 1

    # ----- cycle / payment derived stats -----
    ready = sum(1 for c in cycles if c.status_code == "READY_FOR_SUBMISSION")
    submitted = sum(1 for c in cycles if c.status_code == "SUBMITTED")
    approved = sum(1 for c in cycles if c.status_code == "APPROVED")
    pay_pending = sum(1 for c in cycles if c.status_code == "PAYMENT_PENDING")
    overdue_payments = sum(
        1 for c in cycles
        for p in c.payments if p.payment_status == "OVERDUE")
    active_cycles = sum(1 for c in cycles if c.status_code != "PAID")

    outstanding = 0
    for c in cycles:
        if not c.payments:
            if c.invoice_amount:
                outstanding += float(c.invoice_amount or 0)
            continue
        p = c.payments[-1]
        if p.payment_status in ("PAID",):
            continue
        outstanding += float(p.outstanding_amount if p.outstanding_amount
                             is not None else (c.invoice_amount or 0))

    today = date.today()
    # ----- bottleneck panel (active cycles, most recent first) -----
    bottlenecks = [c for c in cycles if c.status_code != "PAID"]
    bottlenecks.sort(key=lambda c: c.created_at or today, reverse=True)
    bottlenecks = bottlenecks[:8]

    overdue_docs = (CycleDocument.query
                    .filter(CycleDocument.due_date.isnot(None),
                            CycleDocument.due_date < today,
                            CycleDocument.completed_at.is_(None))
                    .order_by(CycleDocument.due_date.asc())
                    .limit(10).all())
    upcoming_docs = (CycleDocument.query
                     .filter(CycleDocument.due_date.isnot(None),
                             CycleDocument.due_date >= today,
                             CycleDocument.due_date <= today + timedelta(days=7),
                             CycleDocument.completed_at.is_(None))
                     .order_by(CycleDocument.due_date.asc())
                     .limit(10).all())
    activity = (DocumentHistory.query
                .options(joinedload(DocumentHistory.user))
                .order_by(DocumentHistory.created_at.desc())
                .limit(12).all())

    clients = Client.query.filter_by(active=True).order_by(Client.name).all()
    projects = Project.query.filter_by(active=True).order_by(Project.name).all()
    methods = SubmissionMethod.query.filter_by(active=True).order_by(
        SubmissionMethod.name).all()

    return render_template(
        "dashboard.html",
        agg=agg, cycles=cycles, ready=ready, submitted=submitted,
        approved=approved, pay_pending=pay_pending,
        overdue_payments=overdue_payments, outstanding=round(outstanding, 2),
        active_cycles=active_cycles, completed_cycles=len(cycles)-active_cycles, bottlenecks=bottlenecks,
        overdue_docs=overdue_docs, upcoming_docs=upcoming_docs,
        activity=activity, clients=clients, projects=projects,
        methods=methods, months=_month_list(),
        filters=f, cal=cal,
        CYCLE_STATUS=CallableDict(CYCLE_STATUS_LABELS),
        CYCLE_CSS=cycle_status_css, DOC_STATUS=doc_status_label,
        DOC_CSS=doc_status_css, today=today,
    )


@bp.route("/dashboard/generate-month", methods=["POST"])
@login_required
def generate_month():
    """One-click: create the invoice cycle for every active project for a month.

    The monthly invoicing procedure repeats every month until each project's
    contract expires (contract_start/contract_end). Safe to press repeatedly —
    projects that already have a cycle for the month are skipped.
    """
    month = request.form.get("cal_month", "").strip()
    if not month:
        month = date.today().strftime("%Y-%m")
    created = generate_monthly_cycles(month, current_user)
    if created:
        flash(f"Created {created} invoice cycle(s) for {month}.", "success")
    else:
        flash(f"No new cycles needed for {month} — all active projects already "
              f"have one or their contract does not cover the month.", "info")
    return redirect(url_for("dashboard.home", cal_month=month))
