"""Workflow engine for the SDS Invoice & Client Payment Tracking System.

Central logic that the routes / UI / API all share:

  * document advancement along per-document-type workflow templates
  * permission checks (Admin/Manager bypass; otherwise per-doc can_prepare /
    can_approve from UserDocumentPermission)
  * auto-computed invoice-cycle status, document counts, bottleneck and next
    action
  * delivery-style timeline stage resolution
  * notifications, audit log, and the scheduled due-date / escalation sweep
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func

from web.db import db
from web.models import (
    AuditLog,
    CycleDocument,
    DocumentHistory,
    InvoiceCycle,
    Notification,
    Payment,
    Project,
    SystemSetting,
    User,
    UserDocumentPermission,
    WorkflowTemplate,
)

# --------------------------------------------------------------------------- #
# Labels / styling
# --------------------------------------------------------------------------- #
DOC_STATUS_LABELS = {
    "NOT_STARTED": "Not Started",
    "PREPARING": "Preparing",
    "INTERNAL_REVIEW": "Internal Review",
    "SENT_CLIENT": "Sent to Client",
    "WAITING_CLIENT": "Waiting for Client",
    "GRN_REQUESTED": "GRN Requested",
    "REQUESTED": "Requested",
    "RECEIVED": "Received",
    "VERIFIED": "Verified",
    "ADDED_INVOICE": "Added to Invoice",
    "READY": "Ready for Submission",
    "SUBMITTED": "Submitted",
    "APPROVED": "Approved",
    "REJECTED": "Rejected",
    "COMPLETED": "Completed",
}

DOC_STATUS_CSS = {
    "NOT_STARTED": "grey",
    "PREPARING": "blue",
    "INTERNAL_REVIEW": "blue",
    "SENT_CLIENT": "amber",
    "WAITING_CLIENT": "amber",
    "GRN_REQUESTED": "amber",
    "REQUESTED": "amber",
    "RECEIVED": "teal",
    "VERIFIED": "teal",
    "ADDED_INVOICE": "teal",
    "READY": "teal",
    "SUBMITTED": "blue",
    "APPROVED": "green",
    "REJECTED": "red",
    "COMPLETED": "green",
}

CYCLE_STATUS_LABELS = {
    "IN_PREPARATION": "In Preparation",
    "WAITING_CLIENT": "Waiting on Client",
    "OVERDUE": "Overdue",
    "READY_FOR_SUBMISSION": "Ready for Submission",
    "SUBMITTED": "Submitted",
    "PENDING_APPROVAL": "Pending Approval",
    "APPROVED": "Approved",
    "REJECTED": "Rejected",
    "PAYMENT_PENDING": "Payment Pending",
    "PAID": "Paid",
}

CYCLE_STATUS_CSS = {
    "IN_PREPARATION": "blue",
    "WAITING_CLIENT": "amber",
    "OVERDUE": "red",
    "READY_FOR_SUBMISSION": "teal",
    "SUBMITTED": "blue",
    "PENDING_APPROVAL": "amber",
    "APPROVED": "green",
    "REJECTED": "red",
    "PAYMENT_PENDING": "amber",
    "PAID": "green",
}


def doc_status_label(code):
    return DOC_STATUS_LABELS.get(code, code or "—")


def doc_status_css(code):
    return DOC_STATUS_CSS.get(code, "grey")


def cycle_status_label(code):
    return CYCLE_STATUS_LABELS.get(code, code or "—")


def cycle_status_css(code):
    return CYCLE_STATUS_CSS.get(code, "blue")


class CallableDict(dict):
    """A dict that can also be called to look up a value.

    Lets templates use one variable both as an iterable map
    (``CYCLE_STATUS.items()``) and as a label function
    (``CYCLE_STATUS(code)``).
    """
    def __call__(self, key, default=None):
        return self.get(key, key if default is None else default)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
_SETTINGS_CACHE = {}


def get_setting(key, default=None):
    """Get a system setting, caching the result to avoid DB queries on every page render."""
    if key in _SETTINGS_CACHE:
        return _SETTINGS_CACHE[key]

    s = SystemSetting.query.filter_by(key=key).first()
    val = s.value if s else default
    _SETTINGS_CACHE[key] = val
    return val


def set_setting(key, value):
    """Set a system setting and update the cache."""
    s = SystemSetting.query.filter_by(key=key).first()
    if s:
        s.value = value
    else:
        db.session.add(SystemSetting(key=key, value=value))
    _SETTINGS_CACHE[key] = value
    db.session.commit()
    return s


# --------------------------------------------------------------------------- #
# Notifications / audit
# --------------------------------------------------------------------------- #
def notify(user_id, title, message, ntype="SYSTEM"):
    n = Notification(user_id=user_id, title=title, message=message, ntype=ntype)
    db.session.add(n)
    return n


def notify_users(role, title, message, ntype="SYSTEM"):
    """Notify every active user with the given role."""
    users = User.query.filter_by(role=role, active=True).all()
    for u in users:
        notify(u.id, title, message, ntype)
    return users


def audit(user, action, entity_type, entity_id, details=""):
    a = AuditLog(
        user_id=user.id if user else None,
        username=(user.username if user else "system"),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )
    db.session.add(a)
    db.session.flush()
    return a


def notify_assignee(doc, title, message, ntype="STATUS"):
    """Notify the user attached to a document's employee, if any."""
    for u in _responsible_users(doc):
        notify(u.id, title, message, ntype)


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def user_can_prepare(user, doc_type_id):
    if user is None or not user.active:
        return False
    if user.role in ("Admin", "Manager"):
        return True
    p = UserDocumentPermission.query.filter_by(
        user_id=user.id, doc_type_id=doc_type_id).first()
    return bool(p and p.can_prepare)


def user_can_approve(user, doc_type_id):
    if user is None or not user.active:
        return False
    if user.role in ("Admin", "Manager"):
        return True
    p = UserDocumentPermission.query.filter_by(
        user_id=user.id, doc_type_id=doc_type_id).first()
    return bool(p and p.can_approve)


def get_visible_documents(docs, user=None):
    """Return only documents the user is allowed to see."""
    if user is None:
        from flask_login import current_user
        if not current_user or not current_user.is_authenticated:
            return []
        user = current_user
        
    if user.role in ("Admin", "Manager"):
        return list(docs)
        
    visible = []
    # Pre-fetch permissions for performance if not Admin/Manager
    from web.models import UserDocumentPermission
    perms = UserDocumentPermission.query.filter_by(user_id=user.id).all()
    allowed_doc_types = {p.doc_type_id for p in perms if p.can_prepare or p.can_approve}
    
    for d in docs:
        if d.doc_type_id in allowed_doc_types:
            visible.append(d)
    return visible

def get_assigned_user_names(doc_type_id):
    from web.models import UserDocumentPermission, User
    perms = UserDocumentPermission.query.filter_by(doc_type_id=doc_type_id).all()
    user_ids = [p.user_id for p in perms if p.can_prepare or p.can_approve]
    if not user_ids:
        return "Unassigned"
    users = User.query.filter(User.id.in_(user_ids), User.active == True).all()
    if not users:
        return "Unassigned"
    return ", ".join(u.name for u in users)


# --------------------------------------------------------------------------- #
# Document workflows
# --------------------------------------------------------------------------- #
def doc_template(doc_type_id):
    return WorkflowTemplate.query.filter_by(
        doc_type_id=doc_type_id, active=True).first()


def doc_steps(doc):
    tpl = doc_template(doc.doc_type_id)
    return tpl.steps if tpl else []


def initial_step(doc):
    steps = doc_steps(doc)
    return steps[0] if steps else None


def next_step(doc):
    """The step after the current one (in template order)."""
    steps = doc_steps(doc)
    if not steps:
        return None
    if doc.current_step_id is None:
        return steps[0]
    idx = next((i for i, s in enumerate(steps) if s.id == doc.current_step_id), -1)
    return steps[idx + 1] if 0 <= idx < len(steps) - 1 else None


def _target_step(doc, to_step_id):
    steps = doc_steps(doc)
    if to_step_id:
        for s in steps:
            if s.id == int(to_step_id):
                return s
        return None
    return next_step(doc)


def advance_document(doc, user, to_step_id=None, note=""):
    """Advance a document to a workflow step. Returns (ok, message)."""
    target = _target_step(doc, to_step_id)
    if target is None:
        return False, "No next workflow step available for this document."

    # Determine the required permission for this move.
    approve_move = target.is_terminal or target.status_code == "APPROVED"
    if approve_move:
        if not user_can_approve(user, doc.doc_type_id):
            return False, ("You do not have approval rights for "
                           f"{doc.doc_type.name}.")
    else:
        if not user_can_prepare(user, doc.doc_type_id):
            return False, ("You do not have preparation rights for "
                           f"{doc.doc_type.name}.")

    old_status = doc.status_code
    from_label = doc_status_label(old_status)

    doc.current_step_id = target.id
    doc.status_code = target.status_code

    # Waiting-for text from the step definition.
    if target.waiting_type == "CLIENT":
        doc.waiting_for = f"Waiting on {target.waiting_on or 'the client'}"
    elif target.waiting_type == "EMPLOYEE":
        doc.waiting_for = f"Waiting on {target.waiting_on or 'assigned employee'}"
    elif target.waiting_type == "DEPARTMENT":
        doc.waiting_for = f"Waiting on {target.waiting_on or 'department'}"
    elif target.waiting_type == "DOCUMENT":
        doc.waiting_for = f"Waiting on {target.waiting_on or 'document'}"
    else:
        doc.waiting_for = None

    # Approval status bookkeeping.
    if target.status_code == "REJECTED":
        doc.approval_status = "REJECTED"
    elif target.status_code == "APPROVED" or target.is_terminal:
        if doc.approval_status != "REJECTED":
            doc.approval_status = "APPROVED"
    elif target.waiting_type == "CLIENT":
        doc.approval_status = "PENDING"

    # Dates.
    if target.status_code in ("PREPARING", "PREPARED") and not doc.preparation_date:
        doc.preparation_date = date.today()
    if target.is_terminal:
        doc.completed_at = datetime.utcnow()
    else:
        doc.completed_at = None

    # History + audit + notification.
    to_label = doc_status_label(target.status_code)
    if target.is_terminal:
        action = f"Completed"
    elif target.status_code == "APPROVED":
        action = "Approved"
    elif target.status_code == "REJECTED":
        action = "Rejected"
    else:
        action = f"Advanced to {to_label}"
    db.session.add(DocumentHistory(
        cycle_document_id=doc.id, user_id=user.id if user else None,
        action=action, from_status=old_status, to_status=target.status_code,
        note=note,
    ))
    audit(user, f"Document {action}: {doc.doc_type.code}",
          "CycleDocument", doc.id, note)
    notify_assignee(doc,
                    f"{doc.doc_type.name} updated",
                    f"{doc.cycle.cycle_name}: {action} "
                    f"(from {from_label} to {to_label}).")

    db.session.flush()
    recompute_cycle(doc.cycle)
    db.session.commit()
    return True, action


# --------------------------------------------------------------------------- #
# Invoice-cycle creation + document checklist generation
# --------------------------------------------------------------------------- #
def create_invoice_cycle(project, invoice_month, user,
                         invoice_number=None, invoice_amount=None,
                         target_submit_date=None, cycle_name=None):
    """Create a cycle and auto-generate its required-document checklist."""
    cycle = InvoiceCycle(
        project_id=project.id,
        cycle_name=cycle_name or f"{invoice_month} — {project.name}",
        invoice_month=invoice_month,
        invoice_number=invoice_number,
        invoice_amount=invoice_amount,
        target_submit_date=target_submit_date,
        created_by_id=user.id if user else None,
        status_code="IN_PREPARATION",
    )
    db.session.add(cycle)
    db.session.flush()
    create_cycle_documents(cycle)
    audit(user, f"Created invoice cycle: {cycle.cycle_name}",
          "InvoiceCycle", cycle.id, f"Month {invoice_month}")
    recompute_cycle(cycle)
    db.session.commit()
    return cycle


def create_cycle_documents(cycle):
    """Auto-generate CycleDocument based on the most recent cycle, or fallback to project requirements."""
    last_cycle = InvoiceCycle.query.filter(
        InvoiceCycle.project_id == cycle.project_id,
        InvoiceCycle.id != cycle.id
    ).order_by(InvoiceCycle.invoice_month.desc()).first()

    if last_cycle and last_cycle.documents:
        for old_doc in last_cycle.documents:
            step = None
            tpl = doc_template(old_doc.doc_type_id)
            if tpl and tpl.steps:
                step = tpl.steps[0]
            doc = CycleDocument(
                invoice_cycle_id=cycle.id,
                doc_type_id=old_doc.doc_type_id,
                sequence=old_doc.sequence,
                current_step_id=step.id if step else None,
                status_code=step.status_code if step else "NOT_STARTED",
                department_id=old_doc.department_id,
                due_date=cycle.target_submit_date,
                waiting_for=None,
                approval_status="NONE",
                submission_status="NONE",
            )
            db.session.add(doc)
    else:
        for req in cycle.project.requirements:
            step = None
            tpl = doc_template(req.doc_type_id)
            if tpl and tpl.steps:
                step = tpl.steps[0]
            doc = CycleDocument(
                invoice_cycle_id=cycle.id,
                doc_type_id=req.doc_type_id,
                sequence=req.sequence,
                current_step_id=step.id if step else None,
                status_code=step.status_code if step else "NOT_STARTED",
                department_id=cycle.project.default_team_code and _dept_by_code(
                    cycle.project.default_team_code),
                due_date=cycle.target_submit_date,
                waiting_for=None,
                approval_status="NONE",
                submission_status="NONE",
            )
            db.session.add(doc)
    db.session.flush()


def _dept_by_code(code):
    from web.models import Department
    return Department.query.filter_by(code=code).first().id \
        if Department.query.filter_by(code=code).first() else None


# --------------------------------------------------------------------------- #
# Cycle status computation
# --------------------------------------------------------------------------- #
def compute_cycle_counts(cycle):
    """Counts across the cycle's documents."""
    docs = cycle.documents
    total = len(docs)
    completed = sum(1 for d in docs if d.is_completed())
    not_started = sum(1 for d in docs if d.status_code == "NOT_STARTED")
    in_preparation = sum(1 for d in docs
                         if d.status_code in (
                             "PREPARING", "INTERNAL_REVIEW", "VERIFIED",
                             "READY", "ADDED_INVOICE", "RECEIVED",
                         ))
    waiting_client = sum(1 for d in docs
                         if d.status_code in (
                             "SENT_CLIENT", "WAITING_CLIENT",
                             "REQUESTED", "GRN_REQUESTED",
                         ))
    overdue = sum(1 for d in docs
                  if d.due_date and d.due_date < date.today()
                  and not d.is_completed())
    return {
        "total": total, "completed": completed, "not_started": not_started,
        "in_preparation": in_preparation, "waiting_client": waiting_client,
        "overdue": overdue,
    }


def compute_cycle_status(cycle, counts=None):
    """Derive the overall invoice-cycle status from live data."""
    counts = counts or compute_cycle_counts(cycle)

    for p in cycle.payments:
        if p.payment_status == "PAID":
            return "PAID"

    latest = cycle.latest_submission()
    if latest:
        if latest.status == "APPROVED":
            if any(p.payment_status == "PAID" for p in cycle.payments):
                return "PAID"
            if cycle.payments:
                return "PAYMENT_PENDING"
            return "APPROVED"
        if latest.status == "REJECTED":
            return "REJECTED"
        if latest.status == "PENDING_APPROVAL":
            return "PENDING_APPROVAL"
        return "SUBMITTED"

    if counts["total"] and counts["completed"] == counts["total"]:
        if cycle.payments:
            return "PAYMENT_PENDING"
        return "READY_FOR_SUBMISSION"
    if counts["overdue"]:
        return "OVERDUE"
    if counts["waiting_client"]:
        return "WAITING_CLIENT"
    return "IN_PREPARATION"


def _bottleneck_candidate(doc):
    """Sort key + description for a document in the bottleneck search."""
    if doc.is_completed():
        return None
    # Overdue documents take priority.
    if doc.due_date and doc.due_date < date.today():
        return 0
    return 1


def find_bottleneck(cycle):
    """Return (document, description) for the document currently blocking
    the invoice, or (None, None) when nothing is left to do."""
    active = [d for d in cycle.documents if not d.is_completed()]
    if not active:
        return None, None
    active.sort(key=lambda d: (_bottleneck_candidate(d), d.sequence))
    doc = active[0]

    if doc.status_code in ("SENT_CLIENT", "WAITING_CLIENT",
                           "REQUESTED", "GRN_REQUESTED"):
        desc = (f"Blocked by {doc.doc_type.name} — "
                f"{doc.waiting_for or 'waiting on client'}")
    elif doc.status_code == "NOT_STARTED":
        desc = f"Not started: {doc.doc_type.name} not yet prepared"
    elif doc.status_code in ("PREPARING", "INTERNAL_REVIEW", "VERIFIED",
                             "READY", "ADDED_INVOICE", "RECEIVED"):
        owner = get_assigned_user_names(doc.doc_type_id)
        if doc.due_date and doc.due_date < date.today():
            desc = (f"Overdue: {doc.doc_type.name} ({owner}) — "
                    f"due {doc.due_date:%d %b %Y}")
        else:
            desc = f"In progress: {doc.doc_type.name} — {owner}"
    else:
        desc = f"{doc.doc_type.name} — {doc_status_label(doc.status_code)}"
    return doc, desc


def next_action(cycle):
    """Human-readable 'what must happen next' for a cycle."""
    bottleneck, desc = find_bottleneck(cycle)
    if bottleneck is None:
        latest = cycle.latest_submission()
        if latest and latest.status == "APPROVED":
            return "Follow up on client payment."
        if cycle.payments and cycle.payments[-1].payment_status == "PAID":
            return "Invoice fully paid."
        return "Invoice ready — proceed with submission."
    s = bottleneck.status_code
    if s in ("SENT_CLIENT", "WAITING_CLIENT", "REQUESTED", "GRN_REQUESTED"):
        return f"Follow up with the client for {bottleneck.waiting_for or 'response'}."
    if s == "NOT_STARTED":
        return f"Start preparing the {bottleneck.doc_type.name}."
    if s in ("PREPARING", "INTERNAL_REVIEW"):
        return f"Complete and verify the {bottleneck.doc_type.name}."
    if s == "VERIFIED":
        return f"Finalise the {bottleneck.doc_type.name}."
    if s in ("READY", "ADDED_INVOICE", "RECEIVED"):
        return (f"Proceed to submission via "
                f"{cycle.project.submission_method.name}.")
    return desc or "Continue the invoice workflow."


def recompute_cycle(cycle):
    """Refresh stored status, counts text, bottleneck and next action."""
    counts = compute_cycle_counts(cycle)
    cycle.status_code = compute_cycle_status(cycle, counts)
    bottleneck, desc = find_bottleneck(cycle)
    cycle.bottleneck_text = desc
    cycle.next_action_text = next_action(cycle)
    return cycle


def refresh_all_cycles():
    for cycle in InvoiceCycle.query.all():
        recompute_cycle(cycle)
    db.session.commit()


# --------------------------------------------------------------------------- #
# Delivery-style timeline
# --------------------------------------------------------------------------- #
def _doc_in_status(cycle, codes):
    return [d for d in cycle.documents if d.status_code in codes]


def delivery_stages(cycle):
    """Resolve the invoice-level pipeline into timeline stage statuses."""
    counts = compute_cycle_counts(cycle)
    method = cycle.project.submission_method.code
    tpl = WorkflowTemplate.query.filter_by(
        name=f"pipeline:{method}").first()
    stage_names = [s.name for s in tpl.steps] if tpl else [
        "Document Preparation", "Submission", "Client Approval", "Payment",
    ]

    latest = cycle.latest_submission()
    payment = cycle.payments[-1] if cycle.payments else None

    def _gather():
        docs = {d.doc_type.code: d for d in cycle.documents}

        def _doc_state(code):
            d = docs.get(code)
            if d is None:
                return None          # not applicable
            if d.is_completed():
                return "done"
            if d.is_waiting_client() or d.status_code in (
                    "REQUESTED", "GRN_REQUESTED", "RECEIVED"):
                return "active"
            if d.status_code in ("PREPARING", "INTERNAL_REVIEW", "VERIFIED",
                                 "READY", "ADDED_INVOICE"):
                return "active"
            return "pending"

        yield "Document Preparation", (
            "done" if counts["total"] and counts["completed"] == counts["total"]
            else "active" if counts["completed"] else "pending"
        ), f"{counts['completed']}/{counts['total']} documents ready"

        internals = _doc_in_status(
            cycle, ["PREPARING", "INTERNAL_REVIEW", "VERIFIED",
                    "READY", "ADDED_INVOICE", "RECEIVED"])
        yield "Internal Verification", (
            "done" if not internals and counts["completed"]
            else "active" if internals else "pending"
        ), "All internal checks done" if not internals else \
            f"{len(internals)} document(s) under internal processing"

        grn = docs.get("GRN")
        if method in ("PORTAL", "COMMON_GRN"):
            if grn:
                st = _doc_state("GRN")
                label = ("done" if st == "done" else "active" if st == "active"
                         else "pending")
                yield "GRN / Document Request", label, (
                    "GRN received & verified" if st == "done"
                    else "Waiting on client GRN" if st == "active"
                    else "GRN not started")
        else:
            yield "GRN / Document Request", "done", "Not required for this method"

        wda = docs.get("WDA")
        iaf = docs.get("IAF")
        kpi = docs.get("KPI")
        if wda or iaf or kpi:
            pending = [d for d in (wda, iaf, kpi) if d and not d.is_completed()]
            any_active = any(d and d.is_waiting_client() for d in (wda, iaf, kpi))
            yield "Client Approval Docs", (
                "done" if not pending else "active" if any_active else "pending"
            ), "All approval documents signed" if not pending else \
                f"{len(pending)} approval document(s) pending client"

        if latest:
            if latest.status == "APPROVED":
                sub_st = "done"
            elif latest.status in ("PENDING_APPROVAL", "SUBMITTED"):
                sub_st = "active"
            elif latest.status == "REJECTED":
                sub_st = "pending"
            else:
                sub_st = "active"
        else:
            sub_st = ("active" if counts["total"]
                      and counts["completed"] == counts["total"] else "pending")
        yield "Submission", sub_st, (
            latest.status if latest else "Not submitted yet")

        yield "Client Approval", (
            "done" if latest and latest.status == "APPROVED"
            else "active" if latest and latest.status in (
                "PENDING_APPROVAL", "SUBMITTED") else "pending"
        ), "Approved by client" if latest and latest.status == "APPROVED" \
            else "Awaiting client approval" if latest else "Not yet submitted"

        pay_st = "pending"
        pay_detail = "No payment recorded"
        if payment:
            if payment.payment_status == "PAID":
                pay_st = "done"
                pay_detail = f"Received {payment.actual_payment_date}"
            elif payment.payment_status == "OVERDUE":
                pay_st = "pending"
                pay_detail = f"Overdue since {payment.payment_due_date}"
            else:
                pay_st = "active"
                pay_detail = f"Due {payment.payment_due_date}"
        yield "Payment", pay_st, pay_detail

    return [{"name": name, "status": st, "detail": detail}
            for name, st, detail in _gather()]


# --------------------------------------------------------------------------- #
# Monthly recurrence & dashboard calendar
# --------------------------------------------------------------------------- #
def parse_month(month_str):
    """Turn 'YYYY-MM' into (year, month). Returns (None, None) if invalid."""
    try:
        y, m = map(int, str(month_str).split("-"))
        if 1 <= m <= 12 and y > 0:
            return y, m
    except (ValueError, AttributeError):
        pass
    return None, None


def _add_months(month_str, delta):
    y, m = parse_month(month_str)
    if y is None:
        return ""
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _month_span(month_str):
    """First and last day of a 'YYYY-MM' month."""
    y, m = parse_month(month_str)
    if y is None:
        return None, None
    first = date(y, m, 1)
    last = date(y + 1, 1, 1) - timedelta(days=1) if m == 12 \
        else date(y, m + 1, 1) - timedelta(days=1)
    return first, last


def project_covers_month(project, month_str):
    """Does the project's contract period include this whole month?"""
    first, last = _month_span(month_str)
    if first is None:
        return False
    if project.contract_start and first < project.contract_start:
        return False
    if project.contract_end and last > project.contract_end:
        return False
    return True


def generate_monthly_cycles(month_str, user):
    """Create an invoice cycle for every active project for `month_str`.

    Idempotent (skips projects that already have a cycle for that month) and
    respects each project's contract period — the monthly procedure runs
    "every month until the contract expires". Returns the number created.
    """
    from web.models import Project
    first, last = _month_span(month_str)
    if first is None:
        return 0
    created = 0
    for project in Project.query.filter_by(active=True).order_by(Project.name).all():
        if not project_covers_month(project, month_str):
            continue
        if InvoiceCycle.query.filter_by(
                project_id=project.id, invoice_month=month_str).first():
            continue
        create_invoice_cycle(project, month_str, user)
        created += 1
    return created


def month_calendar(month_str):
    """Build the dashboard month-grid: which cycles fall on which day.

    A cycle lands on its ``target_submit_date`` day when that date is inside
    the month; otherwise it is shown in the "no target date" list so the
    month's details are always visible.
    """
    import calendar as _cal

    y, m = parse_month(month_str or "")
    today = date.today()
    if y is None:
        y, m = today.year, today.month
    month_str = f"{y:04d}-{m:02d}"
    first, last = _month_span(month_str)

    cycles = (InvoiceCycle.query
              .filter((InvoiceCycle.invoice_month == month_str) |
                      (InvoiceCycle.target_submit_date >= first) &
                      (InvoiceCycle.target_submit_date <= last))
              .join(Project)
              .order_by(Project.name).all())

    by_day = {}
    no_date = []
    for c in cycles:
        if c.target_submit_date and first <= c.target_submit_date <= last:
            by_day.setdefault(c.target_submit_date.day, []).append(c)
        else:
            no_date.append(c)

    # Build Sunday-first week rows. Each cell is a day-of-month int or None.
    _cal.setfirstweekday(_cal.SUNDAY)
    lead = (first.weekday() + 1) % 7   # leading blank cells
    weeks, week = [], [None] * lead
    for day in range(1, last.day + 1):
        week.append(day)
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))

    return {
        "month": month_str, "year": y, "month_num": m,
        "month_name": _cal.month_name[m],
        "first": first, "last": last, "weeks": weeks,
        "by_day": by_day, "no_date": no_date,
        "cycle_count": sum(len(v) for v in by_day.values()) + len(no_date),
        "prev_month": _add_months(month_str, -1),
        "next_month": _add_months(month_str, 1),
    }


# --------------------------------------------------------------------------- #
# Scheduled sweep: due-date alerts + escalation
# --------------------------------------------------------------------------- #
def scheduled_sweep(today=None):
    """Find docs approaching due / overdue and raise notifications.

    Runs on startup and hourly. Dedupes by (user, title, message, day).
    """
    from web.models import CycleDocument
    today = today or date.today()
    alert_days = (7, 3, 1)
    created = 0

    existing = {n.message for n in Notification.query.all()}
    sent_guard = set()

    def _guard(user_id, title, message):
        key = f"{user_id}|{title}|{message}"
        if key in sent_guard or message in existing:
            return False
        sent_guard.add(key)
        return True

    # --- document due-date alerts & escalation ---
    for doc in CycleDocument.query.filter(
            CycleDocument.completed_at.is_(None)).all():
        if not doc.due_date:
            continue
        diff = (doc.due_date - today).days
        if diff in alert_days and doc.due_date >= today:
            if doc.employee:
                u = User.query.filter_by(employee_id=doc.employee.id,
                                         active=True).first()
                if u and _guard(u.id, "Due reminder", _doc_due_msg(doc)):
                    notify(u.id, "Due reminder", _doc_due_msg(doc), "REMINDER")
                    created += 1
        elif diff < 0:
            for u in _responsible_users(doc):
                if _guard(u.id, "Escalation", _doc_overdue_msg(doc)):
                    notify(u.id, "Escalation", _doc_overdue_msg(doc),
                           "ESCALATION")
                    created += 1
            # Managers also get escalation visibility.
            for u in User.query.filter_by(role="Manager", active=True).all():
                if _guard(u.id, "Escalation", _doc_overdue_msg(doc)):
                    notify(u.id, "Escalation", _doc_overdue_msg(doc),
                           "ESCALATION")
                    created += 1

    # --- payment due reminders ---
    for pay in Payment.query.filter_by(payment_status="PENDING").all():
        if pay.payment_due_date and (pay.payment_due_date - today).days in alert_days:
            for u in User.query.filter_by(role="Finance", active=True).all():
                if _guard(u.id, "Payment reminder",
                          f"Payment {pay.payment_due_date} due for "
                          f"{pay.cycle.cycle_name}."):
                    notify(u.id, "Payment reminder",
                           f"Payment {pay.payment_due_date} due for "
                           f"{pay.cycle.cycle_name}.", "REMINDER")
                    created += 1

    # --- automatic cycle generation (end of month) ---
    import calendar
    from web.models import Project, InvoiceCycle
    last_day = calendar.monthrange(today.year, today.month)[1]
    
    if today.day == last_day:
        # Determine next month
        if today.month == 12:
            next_month_str = f"{today.year + 1}-01"
            next_month_date = date(today.year + 1, 1, 1)
        else:
            next_month_str = f"{today.year}-{today.month + 1:02d}"
            next_month_date = date(today.year, today.month + 1, 1)
            
        # For all active projects
        for p in Project.query.filter_by(active=True).all():
            # Check contract expiry
            if p.contract_end and p.contract_end < next_month_date:
                continue
                
            # Check if cycle already exists
            existing = InvoiceCycle.query.filter_by(project_id=p.id, invoice_month=next_month_str).first()
            if not existing:
                # We do not have a user in the background thread context, so we pass None or an Admin user
                sys_user = User.query.filter_by(role="Admin").first()
                # Target submit date is end of that next month
                next_last_day = calendar.monthrange(next_month_date.year, next_month_date.month)[1]
                target_date = date(next_month_date.year, next_month_date.month, next_last_day)
                
                create_invoice_cycle(p, next_month_str, sys_user, target_submit_date=target_date)
                created += 1

    if created:
        db.session.commit()
    return created


def _responsible_users(doc):
    from web.models import UserDocumentPermission, User
    perms = UserDocumentPermission.query.filter_by(doc_type_id=doc.doc_type_id).all()
    user_ids = [p.user_id for p in perms if p.can_prepare or p.can_approve]
    if not user_ids:
        return []
    return User.query.filter(User.id.in_(user_ids), User.active == True).all()


def _doc_due_msg(doc):
    return (f"'{doc.doc_type.name}' for {doc.cycle.cycle_name} is due on "
            f"{doc.due_date:%d %b %Y}. Please complete it on time.")


def _doc_overdue_msg(doc):
    return (f"'OVERDUE' {doc.doc_type.name} for {doc.cycle.cycle_name} "
            f"was due on {doc.due_date:%d %b %Y} and is still incomplete. "
            f"Please take action immediately.")
