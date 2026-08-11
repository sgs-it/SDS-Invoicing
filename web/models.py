"""SQLAlchemy models for the SDS Invoice & Client Payment Tracking System.

Master data (clients, projects, departments, employees, users, document types,
submission methods, workflow templates/steps, user document permissions) plus
operational data (invoice cycles, per-document tracking, history, submissions,
payments, notifications, audit log, system settings).
"""
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from web.db import db


# --------------------------------------------------------------------------- #
# Enumerated strings reused across the app
# --------------------------------------------------------------------------- #
ROLES = ("Admin", "Manager", "CSD", "SGS", "Finance", "Operations", "Employee")

# Waiting-for taxonomy: who/what the company is currently blocked on.
WAIT_TYPES = ("NONE", "EMPLOYEE", "DEPARTMENT", "CLIENT", "DOCUMENT")

APPROVAL_STATUSES = ("NONE", "PENDING", "APPROVED", "REJECTED")

# Cycle-level status codes (auto-computed from documents/submission/payment)
CYCLE_STATUSES = (
    "IN_PREPARATION",
    "WAITING_CLIENT",
    "OVERDUE",
    "READY_FOR_SUBMISSION",
    "SUBMITTED",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "PAYMENT_PENDING",
    "PAID",
)

PAYMENT_STATUSES = ("PENDING", "PARTIAL", "PAID", "OVERDUE")

SUBMISSION_STATUSES = (
    "SUBMITTED",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "RESUBMITTED",
)


# --------------------------------------------------------------------------- #
# Master data
# --------------------------------------------------------------------------- #
class Client(db.Model):
    __tablename__ = "clients"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(60))
    address = db.Column(db.String(300))
    active = db.Column(db.Boolean, default=True, nullable=False)
    projects = db.relationship(
        "Project", backref="client", lazy="dynamic", cascade="all, delete-orphan"
    )

    def __repr__(self):  # pragma: no cover - debug aid
        return f"<Client {self.id} {self.name}>"


class Department(db.Model):
    __tablename__ = "departments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(30), unique=True, nullable=False)
    employees = db.relationship(
        "Employee", backref="department", lazy="dynamic"
    )

    def __repr__(self):  # pragma: no cover
        return f"<Department {self.code}>"


class Employee(db.Model):
    __tablename__ = "employees"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(120))
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"))
    active = db.Column(db.Boolean, default=True, nullable=False)

    def __repr__(self):  # pragma: no cover
        return f"<Employee {self.name}>"


class SubmissionMethod(db.Model):
    __tablename__ = "submission_methods"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)          # e.g. "Online - Portal"
    code = db.Column(db.String(30), unique=True, nullable=False)  # DIRECT / PORTAL / ...
    description = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)
    projects = db.relationship("Project", backref="submission_method", lazy="dynamic")

    def __repr__(self):  # pragma: no cover
        return f"<SubmissionMethod {self.code}>"


class DocumentType(db.Model):
    __tablename__ = "document_types"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)     # "Service Delivery Sheet (SDS)"
    code = db.Column(db.String(30), unique=True, nullable=False)  # "SDS"
    description = db.Column(db.Text)
    requires_client_sign = db.Column(db.Boolean, default=False)
    requires_grn = db.Column(db.Boolean, default=False)
    icon = db.Column(db.String(8), default="📄")
    sort_order = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True, nullable=False)

    def __repr__(self):  # pragma: no cover
        return f"<DocumentType {self.code}>"


class Project(db.Model):
    __tablename__ = "projects"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    name = db.Column(db.String(250), nullable=False, index=True)
    job_code = db.Column(db.String(120), index=True)
    submission_method_id = db.Column(
        db.Integer, db.ForeignKey("submission_methods.id"), nullable=False
    )
    # Free-text criteria captured from the Excel workbook (for reference).
    common_criteria = db.Column(db.Text)   # e.g. "GRN Required"
    sds_criteria = db.Column(db.Text)      # e.g. "The SDS document must be signed by the client."
    target_date_rule = db.Column(db.Text)  # e.g. "Before 10th of every month"
    step_narrative = db.Column(db.Text)    # full step-by-step procedure from Excel
    default_team_code = db.Column(db.String(30))  # primary responsible team (dept code)
    # Contract period: the monthly invoicing procedure runs for every month
    # inside [contract_start, contract_end]. A null end means "ongoing".
    contract_start = db.Column(db.Date)
    contract_end = db.Column(db.Date)
    active = db.Column(db.Boolean, default=True, nullable=False)
    requirements = db.relationship(
        "ProjectDocumentRequirement",
        backref="project",
        cascade="all, delete-orphan",
        order_by="ProjectDocumentRequirement.sequence",
    )
    cycles = db.relationship(
        "InvoiceCycle", backref="project", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):  # pragma: no cover
        return f"<Project {self.job_code} {self.name}>"


class ProjectDocumentRequirement(db.Model):
    __tablename__ = "project_doc_requirements"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False
    )
    doc_type_id = db.Column(
        db.Integer, db.ForeignKey("document_types.id"), nullable=False
    )
    sequence = db.Column(db.Integer, default=0)
    required = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text)
    doc_type = db.relationship("DocumentType")

    __table_args__ = (
        db.UniqueConstraint("project_id", "doc_type_id", name="uq_project_doc"),
    )


class WorkflowTemplate(db.Model):
    """A workflow template.

    If `doc_type_id` is set the template drives a *document-type* workflow
    (e.g. the SDS lifecycle). If it is null the template models an
    *invoice-level* pipeline (per submission method) used for the timeline.
    """
    __tablename__ = "workflow_templates"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    doc_type_id = db.Column(db.Integer, db.ForeignKey("document_types.id"))
    description = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)
    steps = db.relationship(
        "WorkflowStep",
        backref="template",
        cascade="all, delete-orphan",
        order_by="WorkflowStep.sequence",
    )


class WorkflowStep(db.Model):
    __tablename__ = "workflow_steps"
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(
        db.Integer, db.ForeignKey("workflow_templates.id"), nullable=False
    )
    sequence = db.Column(db.Integer, default=0)
    name = db.Column(db.String(120), nullable=False)
    status_code = db.Column(db.String(40), nullable=False)
    # What the step is waiting on: EMPLOYEE / DEPARTMENT / CLIENT / DOCUMENT / NONE
    waiting_type = db.Column(db.String(20), default="NONE")
    waiting_on = db.Column(db.String(120))   # e.g. "Client signature", "GRN", "Finance"
    is_terminal = db.Column(db.Boolean, default=False)
    is_blocking = db.Column(db.Boolean, default=True)

    __table_args__ = (
        db.UniqueConstraint("template_id", "sequence", name="uq_template_seq"),
    )


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"))
    employee = db.relationship("Employee")
    role = db.Column(db.String(30), default="Employee", nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    permissions = db.relationship(
        "UserDocumentPermission",
        backref="user",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    def __repr__(self):  # pragma: no cover
        return f"<User {self.username} ({self.role})>"


class UserDocumentPermission(db.Model):
    """Per-user, per-document-type access matrix assigned by an Admin.

    can_prepare -> user may create/advance the document through preparation.
    can_approve -> user may approve/complete the document.
    Admin and Manager roles bypass this matrix.
    """
    __tablename__ = "user_doc_permissions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    doc_type_id = db.Column(
        db.Integer, db.ForeignKey("document_types.id"), nullable=False
    )
    can_prepare = db.Column(db.Boolean, default=False)
    can_approve = db.Column(db.Boolean, default=False)
    doc_type = db.relationship("DocumentType")

    __table_args__ = (
        db.UniqueConstraint("user_id", "doc_type_id", name="uq_user_doc"),
    )


# --------------------------------------------------------------------------- #
# Operational data
# --------------------------------------------------------------------------- #
class InvoiceCycle(db.Model):
    __tablename__ = "invoice_cycles"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    cycle_name = db.Column(db.String(150), nullable=False)
    invoice_month = db.Column(db.String(7), index=True)   # "2026-06"
    invoice_number = db.Column(db.String(80))
    invoice_amount = db.Column(db.Numeric(14, 2))
    target_submit_date = db.Column(db.Date)
    status_code = db.Column(db.String(30), default="IN_PREPARATION", index=True)
    bottleneck_text = db.Column(db.Text)
    next_action_text = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by = db.relationship("User")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    documents = db.relationship(
        "CycleDocument",
        backref="cycle",
        cascade="all, delete-orphan",
        order_by="CycleDocument.sequence",
    )
    submissions = db.relationship(
        "Submission", backref="cycle", cascade="all, delete-orphan"
    )
    payments = db.relationship(
        "Payment", backref="cycle", cascade="all, delete-orphan"
    )

    def latest_submission(self):
        return max(self.submissions, key=lambda s: s.submission_date or s.id) \
            if self.submissions else None


class CycleDocument(db.Model):
    __tablename__ = "cycle_documents"
    id = db.Column(db.Integer, primary_key=True)
    invoice_cycle_id = db.Column(
        db.Integer, db.ForeignKey("invoice_cycles.id"), nullable=False
    )
    doc_type_id = db.Column(
        db.Integer, db.ForeignKey("document_types.id"), nullable=False
    )
    sequence = db.Column(db.Integer, default=0)
    current_step_id = db.Column(db.Integer, db.ForeignKey("workflow_steps.id"))
    current_step = db.relationship("WorkflowStep")
    status_code = db.Column(db.String(40), default="NOT_STARTED", index=True)
    # Ownership
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"))
    department = db.relationship("Department")
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"))
    employee = db.relationship("Employee")
    # Dates
    preparation_date = db.Column(db.Date)
    due_date = db.Column(db.Date)
    completed_at = db.Column(db.DateTime)
    # Workflow state
    waiting_for = db.Column(db.String(200))      # human readable "Client signature (GRN)"
    approval_status = db.Column(db.String(20), default="NONE")
    submission_status = db.Column(db.String(30), default="NONE")
    attachment_path = db.Column(db.String(300))
    remarks = db.Column(db.Text)

    doc_type = db.relationship("DocumentType")
    history = db.relationship(
        "DocumentHistory",
        backref="document",
        cascade="all, delete-orphan",
        order_by="DocumentHistory.created_at",
    )

    def is_completed(self):
        return self.status_code in ("COMPLETED",)

    def is_blocking(self):
        """True when this document still blocks invoice submission."""
        return not self.is_completed()

    def is_waiting_client(self):
        return self.status_code in ("SENT_CLIENT", "WAITING_CLIENT")

    def is_internal(self):
        return self.status_code in (
            "PREPARING", "INTERNAL_REVIEW", "VERIFIED", "GRN_REQUESTED",
            "REQUESTED", "ADDED_INVOICE", "READY",
        )


class DocumentHistory(db.Model):
    """Full audit trail for every document action."""
    __tablename__ = "document_history"
    id = db.Column(db.Integer, primary_key=True)
    cycle_document_id = db.Column(
        db.Integer, db.ForeignKey("cycle_documents.id"), nullable=False
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship("User")
    action = db.Column(db.String(120), nullable=False)
    from_status = db.Column(db.String(40))
    to_status = db.Column(db.String(40))
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Submission(db.Model):
    __tablename__ = "submissions"
    id = db.Column(db.Integer, primary_key=True)
    invoice_cycle_id = db.Column(
        db.Integer, db.ForeignKey("invoice_cycles.id"), nullable=False
    )
    submission_method_id = db.Column(
        db.Integer, db.ForeignKey("submission_methods.id"), nullable=False
    )
    submission_method = db.relationship("SubmissionMethod")
    submission_date = db.Column(db.Date, nullable=False)
    submitted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    submitted_by = db.relationship("User", foreign_keys=[submitted_by_id])
    reference_no = db.Column(db.String(120))
    confirmation_no = db.Column(db.String(120))
    status = db.Column(db.String(30), default="SUBMITTED")
    rejection_reason = db.Column(db.Text)
    approval_date = db.Column(db.Date)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    remarks = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_cycle_id = db.Column(
        db.Integer, db.ForeignKey("invoice_cycles.id"), nullable=False
    )
    invoice_amount = db.Column(db.Numeric(14, 2))
    payment_due_date = db.Column(db.Date)
    expected_payment_date = db.Column(db.Date)
    actual_payment_date = db.Column(db.Date)
    outstanding_amount = db.Column(db.Numeric(14, 2))
    payment_status = db.Column(db.String(20), default="PENDING")
    remarks = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user = db.relationship("User")
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text)
    ntype = db.Column(db.String(30), default="SYSTEM")  # REMINDER / ESCALATION / STATUS / SYSTEM
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    username = db.Column(db.String(80))
    action = db.Column(db.String(150), nullable=False)
    entity_type = db.Column(db.String(60))
    entity_id = db.Column(db.Integer)
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SystemSetting(db.Model):
    __tablename__ = "system_settings"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text)
