"""Management reports.

Nine reports, each with filters, a printable view and a CSV export:
  invoice-status   Invoice Status
  document-pending Document Pending
  client-waiting   Client Waiting
  dept-performance Department / Employee Performance
  submissions      Submission
  client-delay     Client Delay
  payment-aging    Payment Aging
  outstanding      Outstanding Payment
  monthly          Monthly Invoice / Revenue
"""
import csv
import io
from datetime import date, datetime

from flask import Blueprint, Response, redirect, render_template, request, url_for
from flask_login import login_required

from web.db import db
from web.models import (
    CYCLE_STATUSES,
    Client,
    CycleDocument,
    Department,
    DocumentType,
    Employee,
    InvoiceCycle,
    Payment,
    Project,
    Submission,
    SubmissionMethod,
    SUBMISSION_STATUSES,
)
from web.workflow import (
    DOC_STATUS_LABELS,
    compute_cycle_counts,
    cycle_status_css,
    cycle_status_label,
    doc_status_css,
    doc_status_label,
)

_DOC_STATUS_CODES = list(DOC_STATUS_LABELS.keys())

bp = Blueprint("reports", __name__)

REPORTS = [
    ("invoice-status", "Invoice Status", "Every invoice cycle with live status, counts and bottleneck."),
    ("document-pending", "Document Pending", "All incomplete documents with owner, due date and waiting-for."),
    ("client-waiting", "Client Waiting", "What the company is currently waiting on from each client."),
    ("dept-performance", "Department / Employee Performance", "Workload, completion and overdue rates per team."),
    ("submissions", "Submission", "All submissions with reference numbers, method and approval status."),
    ("client-delay", "Client Delay", "Submissions and approvals that slipped past target dates."),
    ("payment-aging", "Payment Aging", "Unpaid invoices bucketed by days overdue (30 / 60 / 90+)."),
    ("outstanding", "Outstanding Payment", "Every payment still outstanding with total due."),
    ("monthly", "Monthly Invoice / Revenue", "Invoiced vs received amounts per month."),
]


@bp.route("/reports")
@login_required
def index():
    return render_template("reports/index.html", reports=REPORTS)


def _filters():
    """Parse the shared filter querystring into a dict for templates."""
    def _get_int(name):
        val = request.args.get(name, type=int)
        return val or None

    f = {
        "client_id": _get_int("client_id"),
        "project_id": _get_int("project_id"),
        "department_id": _get_int("department_id"),
        "employee_id": _get_int("employee_id"),
        "doc_type_id": _get_int("doc_type_id"),
        "method_id": _get_int("method_id"),
        "month": request.args.get("month", "") or "",
        "status": request.args.get("status", "") or "",
    }
    return f


def _filter_rows(query, f, join_client=True):
    """Apply the shared filters to a model query."""
    if join_client:
        query = query.join(Client)
    if f["client_id"]:
        query = query.filter(Client.id == f["client_id"])
    if f["project_id"]:
        query = query.filter(Project.id == f["project_id"])
    if f["department_id"]:
        query = query.filter(Department.id == f["department_id"])
    if f["employee_id"]:
        query = query.filter(Employee.id == f["employee_id"])
    if f["doc_type_id"]:
        query = query.filter(DocumentType.id == f["doc_type_id"])
    if f["method_id"]:
        query = query.filter(SubmissionMethod.id == f["method_id"])
    if f["month"]:
        query = query.filter(InvoiceCycle.invoice_month == f["month"])
    return query


def _filter_context(f, statuses=None):
    return {
        "clients": Client.query.order_by(Client.name).all(),
        "projects": Project.query.order_by(Project.name).all(),
        "departments": Department.query.order_by(Department.code).all(),
        "employees": Employee.query.order_by(Employee.name).all(),
        "doc_types": DocumentType.query.order_by(DocumentType.sort_order,
                                                 DocumentType.name).all(),
        "methods": SubmissionMethod.query.order_by(SubmissionMethod.name).all(),
        "months": _cycle_months(),
        "statuses": statuses,
        "f": f,
    }


def _cycle_months():
    rows = db.session.query(InvoiceCycle.invoice_month).distinct().all()
    return sorted([r[0] for r in rows if r[0]], reverse=True)


def _send_csv(slug, filename, rows, header):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for r in rows:
        writer.writerow([("" if v is None else v) for v in r])
    data = "﻿" + out.getvalue()   # BOM so Excel reads UTF-8
    return Response(
        data, mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename={slug}-{filename}.csv"})


# =========================================================================== #
# 1. Invoice Status
# =========================================================================== #
def _invoice_status_rows(f):
    q = (InvoiceCycle.query.join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id)
         .join(SubmissionMethod, Project.submission_method_id ==
               SubmissionMethod.id))
    q = _filter_rows(q, f, join_client=False)
    if f["status"]:
        q = q.filter(InvoiceCycle.status_code == f["status"])
    rows = []
    for c in q.order_by(InvoiceCycle.created_at.desc()).all():
        counts = compute_cycle_counts(c)
        rows.append({
            "cycle": c, "counts": counts,
            "status_label": cycle_status_label(c.status_code),
            "status_css": cycle_status_css(c.status_code),
        })
    return rows


@bp.route("/reports/invoice-status")
@login_required
def invoice_status():
    f = _filters()
    rows = _invoice_status_rows(f)
    return render_template("reports/invoice_status.html", rows=rows,
                           **_filter_context(f, statuses=CYCLE_STATUSES))


@bp.route("/reports/invoice-status.csv")
@login_required
def invoice_status_csv():
    f = _filters()
    out = []
    for r in _invoice_status_rows(f):
        c = r["cycle"]
        out.append([c.cycle_name, c.project.client.name, c.project.name,
                    c.project.submission_method.name, c.invoice_month,
                    c.invoice_amount, c.status_code,
                    r["counts"]["total"], r["counts"]["completed"],
                    r["counts"]["in_preparation"], r["counts"]["waiting_client"],
                    r["counts"]["overdue"], c.bottleneck_text])
    return _send_csv("invoice-status", "invoice-status", out,
                     ["Cycle", "Client", "Project", "Method", "Month",
                      "Amount", "Status", "Total Docs", "Completed",
                      "In Prep", "Waiting Client", "Overdue", "Bottleneck"])


# =========================================================================== #
# 2. Document Pending
# =========================================================================== #
def _document_pending_rows(f):
    q = (CycleDocument.query.join(InvoiceCycle,
                                  CycleDocument.invoice_cycle_id ==
                                  InvoiceCycle.id)
         .join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id)
         .join(DocumentType, CycleDocument.doc_type_id == DocumentType.id)
         .outerjoin(Department, CycleDocument.department_id == Department.id)
         .outerjoin(Employee, CycleDocument.employee_id == Employee.id))
    q = q.filter(CycleDocument.completed_at.is_(None))
    q = _filter_rows(q, f, join_client=False)
    if f["status"]:
        q = q.filter(CycleDocument.status_code == f["status"])
    return q.order_by(CycleDocument.due_date.asc()).all()


@bp.route("/reports/document-pending")
@login_required
def document_pending():
    f = _filters()
    rows = _document_pending_rows(f)
    doc_statuses = [s for s in _DOC_STATUS_CODES]
    return render_template("reports/document_pending.html", rows=rows,
                           today=date.today(),
                           **_filter_context(f, statuses=doc_statuses))


@bp.route("/reports/document-pending.csv")
@login_required
def document_pending_csv():
    f = _filters()
    out = []
    for d in _document_pending_rows(f):
        out.append([d.cycle.cycle_name, d.cycle.project.client.name,
                    d.cycle.project.name, d.doc_type.name, d.status_code,
                    d.department.code if d.department else "",
                    d.employee.name if d.employee else "",
                    d.due_date, d.waiting_for, d.attachment_path or ""])
    return _send_csv("document-pending", "document-pending", out,
                     ["Cycle", "Client", "Project", "Document", "Status",
                      "Department", "Employee", "Due Date", "Waiting For",
                      "Attachment"])


# =========================================================================== #
# 3. Client Waiting
# =========================================================================== #
WAITING_CODES = ("SENT_CLIENT", "WAITING_CLIENT", "REQUESTED", "GRN_REQUESTED")


@bp.route("/reports/client-waiting")
@login_required
def client_waiting():
    f = _filters()
    q = (CycleDocument.query.join(InvoiceCycle,
                                  CycleDocument.invoice_cycle_id ==
                                  InvoiceCycle.id)
         .join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id)
         .join(DocumentType, CycleDocument.doc_type_id == DocumentType.id))
    q = _filter_rows(q, f, join_client=False)
    q = q.filter(CycleDocument.status_code.in_(WAITING_CODES))
    rows = q.order_by(Client.name, CycleDocument.due_date.asc()).all()
    return render_template("reports/client_waiting.html", rows=rows,
                           **_filter_context(f))


@bp.route("/reports/client-waiting.csv")
@login_required
def client_waiting_csv():
    f = _filters()
    q = (CycleDocument.query.join(InvoiceCycle,
                                  CycleDocument.invoice_cycle_id ==
                                  InvoiceCycle.id)
         .join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id)
         .join(DocumentType, CycleDocument.doc_type_id == DocumentType.id))
    q = _filter_rows(q, f, join_client=False)
    q = q.filter(CycleDocument.status_code.in_(WAITING_CODES))
    out = [[d.cycle.project.client.name, d.cycle.cycle_name, d.doc_type.name,
            d.status_code, d.waiting_for, d.due_date,
            (d.due_date - date.today()).days if d.due_date else ""]
           for d in q.order_by(Client.name).all()]
    return _send_csv("client-waiting", "client-waiting", out,
                     ["Client", "Cycle", "Document", "Status", "Waiting For",
                      "Due Date", "Days Left"])


# =========================================================================== #
# 4. Department / Employee Performance
# =========================================================================== #
@bp.route("/reports/dept-performance")
@login_required
def dept_performance():
    f = _filters()
    dept_rows = []
    q = CycleDocument.query.outerjoin(
        Department, CycleDocument.department_id == Department.id)
    if f["department_id"]:
        q = q.filter(Department.id == f["department_id"])
    for dept in Department.query.order_by(Department.code).all():
        if f["department_id"] and dept.id != f["department_id"]:
            continue
        docs = [d for d in q.filter(Department.id == dept.id).all()]
        total = len(docs)
        completed = sum(1 for d in docs if d.is_completed())
        overdue = sum(1 for d in docs if not d.is_completed() and d.due_date
                      and d.due_date < date.today())
        dept_rows.append({
            "dept": dept, "total": total, "completed": completed,
            "pending": total - completed, "overdue": overdue,
            "pct": (completed / total * 100) if total else 0,
        })
    emp_rows = []
    employees = Employee.query.order_by(Employee.name).all()
    if f["department_id"]:
        employees = [e for e in employees if e.department_id == f["department_id"]]
    for emp in employees:
        docs = CycleDocument.query.filter_by(employee_id=emp.id).all()
        total = len(docs)
        completed = sum(1 for d in docs if d.is_completed())
        overdue = sum(1 for d in docs if not d.is_completed() and d.due_date
                      and d.due_date < date.today())
        emp_rows.append({
            "emp": emp, "total": total, "completed": completed,
            "pending": total - completed, "overdue": overdue,
            "pct": (completed / total * 100) if total else 0,
        })
    return render_template("reports/dept_performance.html",
                           dept_rows=dept_rows, emp_rows=emp_rows,
                           **_filter_context(f))


@bp.route("/reports/dept-performance.csv")
@login_required
def dept_performance_csv():
    f = _filters()
    out = []
    for r in _dept_summary():
        if f["department_id"] and r["dept"].id != f["department_id"]:
            continue
        out.append([r["dept"].code, r["dept"].name, r["total"],
                    r["completed"], r["pending"], r["overdue"],
                    f"{r['pct']:.0f}%"])
    return _send_csv("dept-performance", "dept-performance", out,
                     ["Code", "Department", "Assigned", "Completed",
                      "Pending", "Overdue", "Completion Rate"])


def _dept_summary():
    rows = []
    for dept in Department.query.order_by(Department.code).all():
        docs = CycleDocument.query.filter_by(department_id=dept.id).all()
        total = len(docs)
        completed = sum(1 for d in docs if d.is_completed())
        overdue = sum(1 for d in docs if not d.is_completed() and d.due_date
                      and d.due_date < date.today())
        rows.append({
            "dept": dept, "total": total, "completed": completed,
            "pending": total - completed, "overdue": overdue,
            "pct": (completed / total * 100) if total else 0,
        })
    return rows


# =========================================================================== #
# 5. Submission
# =========================================================================== #
def _submission_rows(f):
    q = (Submission.query.join(InvoiceCycle,
                               Submission.invoice_cycle_id ==
                               InvoiceCycle.id)
         .join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id)
         .join(SubmissionMethod,
               Submission.submission_method_id == SubmissionMethod.id))
    q = _filter_rows(q, f, join_client=False)
    if f["status"]:
        q = q.filter(Submission.status == f["status"])
    return q.order_by(Submission.submission_date.desc()).all()


@bp.route("/reports/submissions")
@login_required
def submissions():
    f = _filters()
    rows = _submission_rows(f)
    return render_template("reports/submissions.html", rows=rows,
                           **_filter_context(f, statuses=SUBMISSION_STATUSES))


@bp.route("/reports/submissions.csv")
@login_required
def submissions_csv():
    f = _filters()
    out = []
    for s in _submission_rows(f):
        out.append([s.cycle.project.client.name, s.cycle.cycle_name,
                    s.submission_method.name, s.submission_date,
                    s.submitted_by.username if s.submitted_by else "",
                    s.reference_no, s.confirmation_no, s.status,
                    s.approval_date, s.rejection_reason])
    return _send_csv("submissions", "submissions", out,
                     ["Client", "Cycle", "Method", "Submission Date",
                      "Submitted By", "Reference No", "Confirmation No",
                      "Status", "Approval Date", "Rejection Reason"])


# =========================================================================== #
# 6. Client Delay
# =========================================================================== #
@bp.route("/reports/client-delay")
@login_required
def client_delay():
    f = _filters()
    rows = []
    subs = _submission_rows(f)
    for s in subs:
        target = s.cycle.target_submit_date
        delay = (s.submission_date - target).days \
            if target and s.submission_date > target else 0
        approval_delay = None
        if s.status == "APPROVED" and s.approval_date and s.submission_date:
            approval_delay = (s.approval_date - s.submission_date).days
        if delay > 0 or (approval_delay and approval_delay > 0):
            rows.append({"sub": s, "delay": delay,
                         "approval_delay": approval_delay})
    rows.sort(key=lambda r: -(r["delay"] or 0))
    return render_template("reports/client_delay.html", rows=rows,
                           **_filter_context(f))


@bp.route("/reports/client-delay.csv")
@login_required
def client_delay_csv():
    f = _filters()
    out = []
    for s in _submission_rows(f):
        target = s.cycle.target_submit_date
        delay = (s.submission_date - target).days \
            if target and s.submission_date > target else 0
        approval_delay = (s.approval_date - s.submission_date).days \
            if s.status == "APPROVED" and s.approval_date \
            and s.submission_date else 0
        if delay > 0 or approval_delay > 0:
            out.append([s.cycle.project.client.name, s.cycle.cycle_name,
                        s.submission_date, target, delay, s.approval_date,
                        approval_delay])
    return _send_csv("client-delay", "client-delay", out,
                     ["Client", "Cycle", "Submitted", "Target", "Delay (days)",
                      "Approved", "Approval Lag (days)"])


# =========================================================================== #
# 7. Payment Aging
# =========================================================================== #
def _aging_bucket(pay):
    if pay.payment_status == "PAID":
        return "PAID"
    if not pay.payment_due_date:
        return "NO_DUE_DATE"
    days = (date.today() - pay.payment_due_date).days
    if days <= 0:
        return "CURRENT"
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


@bp.route("/reports/payment-aging")
@login_required
def payment_aging():
    f = _filters()
    q = (Payment.query.join(InvoiceCycle,
                            Payment.invoice_cycle_id == InvoiceCycle.id)
         .join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id))
    q = _filter_rows(q, f, join_client=False)
    rows = q.order_by(Payment.payment_due_date.asc()).all()
    buckets = {"CURRENT": [], "0-30": [], "31-60": [], "61-90": [],
               "90+": [], "NO_DUE_DATE": [], "PAID": []}
    for p in rows:
        buckets[_aging_bucket(p)].append(p)
    totals = {}
    for key, pays in buckets.items():
        totals[key] = round(sum(
            float(p.outstanding_amount if p.outstanding_amount is not None
                  else 0) for p in pays), 2)
    order = ["CURRENT", "0-30", "31-60", "61-90", "90+", "PAID", "NO_DUE_DATE"]
    return render_template("reports/payment_aging.html", buckets=buckets,
                           totals=totals, order=order, today=date.today(),
                           **_filter_context(f))


@bp.route("/reports/payment-aging.csv")
@login_required
def payment_aging_csv():
    f = _filters()
    q = (Payment.query.join(InvoiceCycle,
                            Payment.invoice_cycle_id == InvoiceCycle.id)
         .join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id))
    q = _filter_rows(q, f, join_client=False)
    out = []
    for p in q.order_by(Payment.payment_due_date.asc()).all():
        out.append([p.cycle.project.client.name, p.cycle.cycle_name,
                    p.invoice_amount, p.payment_due_date,
                    p.actual_payment_date, p.outstanding_amount,
                    _aging_bucket(p)])
    return _send_csv("payment-aging", "payment-aging", out,
                     ["Client", "Cycle", "Amount", "Due Date", "Paid Date",
                      "Outstanding", "Aging Bucket"])


# =========================================================================== #
# 8. Outstanding Payment
# =========================================================================== #
@bp.route("/reports/outstanding")
@login_required
def outstanding():
    f = _filters()
    q = (Payment.query.join(InvoiceCycle,
                            Payment.invoice_cycle_id == InvoiceCycle.id)
         .join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id))
    q = _filter_rows(q, f, join_client=False)
    rows = []
    for p in q.order_by(Payment.payment_due_date.asc()).all():
        outstanding = float(p.outstanding_amount
                            if p.outstanding_amount is not None else 0)
        if outstanding <= 0 and p.payment_status != "PENDING":
            continue
        rows.append({"p": p, "outstanding": outstanding,
                     "days_overdue": (date.today() - p.payment_due_date).days
                     if p.payment_due_date and p.payment_due_date <
                     date.today() else 0})
    total = round(sum(r["outstanding"] for r in rows), 2)
    return render_template("reports/outstanding.html", rows=rows, total=total,
                           **_filter_context(f))


@bp.route("/reports/outstanding.csv")
@login_required
def outstanding_csv():
    f = _filters()
    q = (Payment.query.join(InvoiceCycle,
                            Payment.invoice_cycle_id == InvoiceCycle.id)
         .join(Project, InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id))
    q = _filter_rows(q, f, join_client=False)
    out = []
    for p in q.order_by(Payment.payment_due_date.asc()).all():
        outstanding = float(p.outstanding_amount
                            if p.outstanding_amount is not None else 0)
        if outstanding <= 0 and p.payment_status != "PENDING":
            continue
        out.append([p.cycle.project.client.name, p.cycle.cycle_name,
                    p.invoice_amount, p.payment_due_date, outstanding,
                    (date.today() - p.payment_due_date).days
                    if p.payment_due_date else ""])
    return _send_csv("outstanding", "outstanding", out,
                     ["Client", "Cycle", "Amount", "Due Date", "Outstanding",
                      "Days Overdue"])


# =========================================================================== #
# 9. Monthly Invoice / Revenue
# =========================================================================== #
@bp.route("/reports/monthly")
@login_required
def monthly():
    f = _filters()
    q = (InvoiceCycle.query.join(Project,
                                 InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id))
    q = _filter_rows(q, f, join_client=False)
    cycles = q.all()
    months = {}
    for c in cycles:
        m = c.invoice_month or "Unspecified"
        month = months.setdefault(m, {"invoiced": 0.0, "received": 0.0,
                                      "count": 0, "paid": 0})
        month["count"] += 1
        month["invoiced"] += float(c.invoice_amount or 0)
        for p in c.payments:
            if p.payment_status == "PAID":
                month["received"] += float(p.invoice_amount or 0)
                month["paid"] += 1
    keys = sorted(months.keys(), reverse=True)
    rows = [{"month": k, **months[k]} for k in keys]
    totals = {"invoiced": round(sum(r["invoiced"] for r in rows), 2),
              "received": round(sum(r["received"] for r in rows), 2),
              "count": sum(r["count"] for r in rows)}
    return render_template("reports/monthly.html", rows=rows, totals=totals,
                           **_filter_context(f))


@bp.route("/reports/monthly.csv")
@login_required
def monthly_csv():
    f = _filters()
    q = (InvoiceCycle.query.join(Project,
                                 InvoiceCycle.project_id == Project.id)
         .join(Client, Project.client_id == Client.id))
    q = _filter_rows(q, f, join_client=False)
    months = {}
    for c in q.all():
        m = c.invoice_month or "Unspecified"
        month = months.setdefault(m, {"invoiced": 0.0, "received": 0.0,
                                      "count": 0, "paid": 0})
        month["count"] += 1
        month["invoiced"] += float(c.invoice_amount or 0)
        for p in c.payments:
            if p.payment_status == "PAID":
                month["received"] += float(p.invoice_amount or 0)
                month["paid"] += 1
    out = [[k, months[k]["count"], round(months[k]["invoiced"], 2),
            months[k]["paid"], round(months[k]["received"], 2)]
           for k in sorted(months.keys(), reverse=True)]
    return _send_csv("monthly", "monthly", out,
                     ["Month", "Cycles", "Invoiced", "Paid Count", "Received"])
