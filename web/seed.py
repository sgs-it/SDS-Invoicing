"""Idempotent seeding of master data: submission methods, departments,
document types, per-document-type workflow templates, invoice-level pipeline
templates, and the default admin account. Safe to run on every startup.
"""
from web.db import db
from web.models import (
    Department,
    DocumentType,
    SubmissionMethod,
    User,
    WorkflowStep,
    WorkflowTemplate,
)


# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #
SUBMISSION_METHODS = [
    ("Direct Submission", "DIRECT", "Documents handed over directly to the client office."),
    ("Online - Portal", "PORTAL", "Invoice and backups uploaded to the client's online portal."),
    ("Online Email", "EMAIL", "Documents emailed to the client's submission address."),
    ("Online Email + Direct", "EMAIL_DIRECT", "Email submission followed by direct office submission."),
    ("Common GRN Invoicing", "COMMON_GRN", "Portal workflow driven by GRN / service-sheet approval."),
]

DEPARTMENTS = [
    ("IP DXB - CSD", "CSD"),
    ("SGS", "SGS"),
    ("IP BPO", "BPO"),
    ("IPDXB Operations", "OPS"),
    ("Finance", "FIN"),
    ("Management", "MGMT"),
]

# code -> (name, icon, requires_client_sign, requires_grn, description)
DOCUMENT_TYPES = {
    "INVOICE": ("Invoice", "🧾", False, False, "Final invoice raised by Finance."),
    "PO": ("Purchase Order (PO)", "📋", False, False, "Purchase order / work order from the client."),
    "SDS": ("Service Delivery Sheet (SDS)", "📝", True, False, "Service delivery sheet, signed by the client."),
    "GRN": ("Goods Receipt Note (GRN)", "📦", True, True, "Client's goods receipt note approving the invoice."),
    "WDA": ("Work Done Acceptance (WDA)", "✍️", True, False, "Work done acceptance, signed by the client."),
    "KPI": ("KPI Score Card", "📊", False, False, "Key performance indicator score card from the client."),
    "IAF": ("Invoice Approval Form (IAF)", "✅", True, False, "Invoice approval form, signed by the client."),
    "MR": ("Monthly Report", "📰", False, False, "Monthly service report."),
    "ATT": ("Attendance Sheet", "👥", False, False, "Attendance sheet for the billing period."),
    "SR": ("Service Report", "🗒️", False, False, "Service report for the billing period."),
    "DO": ("Delivery Order (DO)", "🚚", True, False, "Delivery order approved by the client."),
    "SSHEET": ("Service Sheet", "🧾", True, False, "Service sheet stamped / signed by the client."),
    "CF": ("Completion Form", "🏁", True, False, "Project completion form, signed by the client."),
}

# Document-type workflows: code -> list of (step_name, status_code, waiting_type, waiting_on, terminal)
DOC_WORKFLOWS = {
    "SDS": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Preparing", "PREPARING", "EMPLOYEE", "Assigned employee", False),
        ("Internal Review", "INTERNAL_REVIEW", "DEPARTMENT", "Department review", False),
        ("Sent to Client", "SENT_CLIENT", "CLIENT", "Client", False),
        ("Waiting for Client Signature", "WAITING_CLIENT", "CLIENT", "Client signature", False),
        ("Approved", "APPROVED", "NONE", None, False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "GRN": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("GRN Requested", "GRN_REQUESTED", "CLIENT", "Client", False),
        ("Waiting for Client", "WAITING_CLIENT", "CLIENT", "GRN from client", False),
        ("GRN Received", "RECEIVED", "EMPLOYEE", "Assigned employee", False),
        ("Verified", "VERIFIED", "DEPARTMENT", "Finance / SGS", False),
        ("Added to Invoice", "ADDED_INVOICE", "EMPLOYEE", "Finance", False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "INVOICE": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Preparing", "PREPARING", "EMPLOYEE", "Finance", False),
        ("Internal Review", "INTERNAL_REVIEW", "DEPARTMENT", "Department review", False),
        ("Ready for Submission", "READY", "NONE", None, False),
        ("Submitted", "SUBMITTED", "NONE", None, False),
        ("Approved", "APPROVED", "NONE", None, False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "PO": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Requested", "REQUESTED", "CLIENT", "Client", False),
        ("Received", "RECEIVED", "EMPLOYEE", "Assigned employee", False),
        ("Verified", "VERIFIED", "DEPARTMENT", "Finance / SGS", False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "MR": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Preparing", "PREPARING", "EMPLOYEE", "Assigned employee", False),
        ("Sent to Client", "SENT_CLIENT", "CLIENT", "Client", False),
        ("Approved", "APPROVED", "NONE", None, False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "KPI": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Requested from Client", "REQUESTED", "CLIENT", "Client", False),
        ("Waiting for Client", "WAITING_CLIENT", "CLIENT", "KPI score from client", False),
        ("Received", "RECEIVED", "EMPLOYEE", "Assigned employee", False),
        ("Verified", "VERIFIED", "DEPARTMENT", "Department review", False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "WDA": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Sent to Client", "SENT_CLIENT", "CLIENT", "Client", False),
        ("Waiting for Client Signature", "WAITING_CLIENT", "CLIENT", "Client signature", False),
        ("Approved", "APPROVED", "NONE", None, False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "IAF": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Sent to Client", "SENT_CLIENT", "CLIENT", "Client", False),
        ("Waiting for Client Signature", "WAITING_CLIENT", "CLIENT", "Client signature", False),
        ("Approved", "APPROVED", "NONE", None, False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "ATT": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Preparing", "PREPARING", "EMPLOYEE", "Assigned employee", False),
        ("Verified", "VERIFIED", "DEPARTMENT", "Department review", False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "SR": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Preparing", "PREPARING", "EMPLOYEE", "Assigned employee", False),
        ("Sent to Client", "SENT_CLIENT", "CLIENT", "Client", False),
        ("Approved", "APPROVED", "NONE", None, False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "DO": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Sent to Client", "SENT_CLIENT", "CLIENT", "Client", False),
        ("Waiting for Client Approval", "WAITING_CLIENT", "CLIENT", "Client approval", False),
        ("Approved", "APPROVED", "NONE", None, False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "SSHEET": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Preparing", "PREPARING", "EMPLOYEE", "Assigned employee", False),
        ("Sent to Client", "SENT_CLIENT", "CLIENT", "Client", False),
        ("Client Sign / Stamp", "WAITING_CLIENT", "CLIENT", "Client signature / stamp", False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
    "CF": [
        ("Not Started", "NOT_STARTED", "NONE", None, False),
        ("Preparing", "PREPARING", "EMPLOYEE", "Assigned employee", False),
        ("Sent to Client", "SENT_CLIENT", "CLIENT", "Client", False),
        ("Client Sign", "WAITING_CLIENT", "CLIENT", "Client signature", False),
        ("Completed", "COMPLETED", "NONE", None, True),
    ],
}

# Invoice-level pipeline templates per submission method (timeline stages).
# These are stored as WorkflowTemplate rows with doc_type_id = NULL.
PIPELINE_TEMPLATES = {
    "DIRECT": [
        "Document Preparation", "Internal Verification", "Direct Submission",
        "Client Approval", "Payment",
    ],
    "PORTAL": [
        "Document Preparation", "Internal Verification", "GRN Request",
        "GRN Received", "Portal Upload", "Client Approval", "Payment",
    ],
    "EMAIL": [
        "Document Preparation", "Internal Verification", "Email Submission",
        "Client Approval", "Payment",
    ],
    "EMAIL_DIRECT": [
        "Document Preparation", "KPI / IAF Request", "Signed IAF Received",
        "Email Submission", "Direct Submission", "Client Approval", "Payment",
    ],
    "COMMON_GRN": [
        "Document Preparation", "Finance Invoice", "GRN / Service Sheet Request",
        "GRN Approved", "Portal Upload", "Client Approval", "Payment",
    ],
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _get_or_create(model, query_kwargs, **create_kwargs):
    obj = model.query.filter_by(**query_kwargs).first()
    if obj is None:
        obj = model(**query_kwargs, **create_kwargs)
        db.session.add(obj)
        db.session.flush()
    return obj


def ensure_master_data():
    for name, code, description in SUBMISSION_METHODS:
        _get_or_create(SubmissionMethod, {"code": code}, name=name, description=description)

    for name, code in DEPARTMENTS:
        _get_or_create(Department, {"code": code}, name=name)

    doc_types = {}
    for code, (name, icon, cs, grn, desc) in DOCUMENT_TYPES.items():
        dt = _get_or_create(
            DocumentType,
            {"code": code},
            name=name, icon=icon, requires_client_sign=cs,
            requires_grn=grn, description=desc,
        )
        doc_types[code] = dt

    # Document-type workflows
    for code, steps in DOC_WORKFLOWS.items():
        dt = doc_types[code]
        tpl = WorkflowTemplate.query.filter_by(doc_type_id=dt.id).first()
        if tpl is None:
            tpl = WorkflowTemplate(name=f"{dt.name} workflow", doc_type_id=dt.id)
            db.session.add(tpl)
            db.session.flush()
            for i, (sname, scode, wtype, won, term) in enumerate(steps):
                db.session.add(WorkflowStep(
                    template_id=tpl.id, sequence=i, name=sname,
                    status_code=scode, waiting_type=wtype, waiting_on=won,
                    is_terminal=term,
                ))

    # Invoice-level pipeline templates
    for code, stages in PIPELINE_TEMPLATES.items():
        method = SubmissionMethod.query.filter_by(code=code).first()
        if method is None:
            continue
        key = f"pipeline:{code}"
        tpl = WorkflowTemplate.query.filter_by(name=key).first()
        if tpl is None:
            tpl = WorkflowTemplate(name=key, doc_type_id=None,
                                   description=f"Invoice pipeline — {method.name}")
            db.session.add(tpl)
            db.session.flush()
            for i, stage in enumerate(stages):
                terminal = i == len(stages) - 1
                db.session.add(WorkflowStep(
                    template_id=tpl.id, sequence=i, name=stage,
                    status_code=f"STAGE{i}", waiting_type="NONE",
                    waiting_on=None, is_terminal=terminal, is_blocking=False,
                ))

    db.session.commit()


def ensure_admin_user():
    """Seed the default admin account (admin / admin123)."""
    if User.query.filter_by(username="admin").first():
        return
    admin = User(username="admin", role="Admin", active=True, must_change_password=True)
    admin.set_password("admin123")
    db.session.add(admin)
    db.session.commit()


def admin_credentials():
    return "admin", "admin123"
