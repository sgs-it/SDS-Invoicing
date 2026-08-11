"""Workflow-engine unit tests for the SDS Invoicing tracker.

Covered:
  * invoice-cycle creation auto-generates the document checklist
  * document advancement follows the per-document-type workflow (SDS)
  * bottleneck detection points at the blocking document
  * cycle status transitions from preparation to paid
  * the permission matrix gates prepare vs approve moves
  * the scheduled sweep raises reminders and escalations
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import create_app  # noqa: E402
from web.db import db as _db  # noqa: E402
from web.models import (  # noqa: E402
    Client,
    CycleDocument,
    Notification,
    Project,
    ProjectDocumentRequirement,
    SubmissionMethod,
    User,
    UserDocumentPermission,
)
from web.seed import ensure_master_data, ensure_admin_user  # noqa: E402
from web.workflow import (  # noqa: E402
    advance_document,
    create_invoice_cycle,
    find_bottleneck,
    generate_monthly_cycles,
    month_calendar,
    next_action,
    project_covers_month,
    recompute_cycle,
    scheduled_sweep,
    user_can_approve,
    user_can_prepare,
)


@pytest.fixture
def app(tmp_path):
    class TestConfig:
        SECRET_KEY = "test-secret"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'test.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False}}
        UPLOAD_FOLDER = str(tmp_path / "uploads")
        SCHEDULER_ENABLED = False
        SCHEDULER_INTERVAL_SECONDS = 3600
        CURRENCY = "AED"

    app = create_app(TestConfig)
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


@pytest.fixture
def admin(ctx):
    ensure_master_data()
    ensure_admin_user()
    return User.query.filter_by(username="admin").first()


def _make_project(name="Emrill test project", method_code="PORTAL",
                  doc_codes=("INVOICE", "SDS", "GRN", "PO")):
    from web.models import DocumentType
    client = Client(name="Emrill")  # type: ignore
    method = SubmissionMethod.query.filter_by(code=method_code).first()
    _db.session.add(client)
    _db.session.flush()
    project = Project(
        client_id=client.id, name=name,  # type: ignore
        submission_method_id=method.id, default_team_code="CSD",  # type: ignore
    )
    _db.session.add(project)
    _db.session.flush()
    for i, code in enumerate(doc_codes):
        dt = DocumentType.query.filter_by(code=code).first()
        _db.session.add(ProjectDocumentRequirement(
            project_id=project.id, doc_type_id=dt.id, sequence=i))  # type: ignore
    _db.session.commit()
    return project


def _doc(cycle, code):
    return next(d for d in cycle.documents if d.doc_type.code == code)


# --------------------------------------------------------------------------- #
# 1. Cycle creation auto-generates the checklist
# --------------------------------------------------------------------------- #
def test_create_cycle_generates_documents(ctx, admin):
    project = _make_project(doc_codes=("INVOICE", "SDS", "GRN", "PO"))
    cycle = create_invoice_cycle(
        project, "2026-08", admin, target_submit_date=date(2026, 8, 20))

    assert len(cycle.documents) == 4
    assert {d.doc_type.code for d in cycle.documents} == {
        "INVOICE", "SDS", "GRN", "PO"}
    assert cycle.status_code == "IN_PREPARATION"
    # Every doc starts on its template's first step.
    for d in cycle.documents:
        assert d.status_code in ("NOT_STARTED", "PREPARING")


# --------------------------------------------------------------------------- #
# 2. SDS advancement
# --------------------------------------------------------------------------- #
def test_advance_sds_through_workflow(ctx, admin):
    project = _make_project(doc_codes=("SDS",))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    doc = _doc(cycle, "SDS")

    for expected in ("PREPARING", "INTERNAL_REVIEW", "SENT_CLIENT",
                     "WAITING_CLIENT", "APPROVED", "COMPLETED"):
        ok, msg = advance_document(doc, admin)
        assert ok, msg
        assert doc.status_code == expected

    assert doc.is_completed()
    assert doc.completed_at is not None
    assert doc.approval_status == "APPROVED"


def test_advance_sets_waiting_for(ctx, admin):
    project = _make_project(doc_codes=("SDS",))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    doc = _doc(cycle, "SDS")
    advance_document(doc, admin, note="prep done")          # -> PREPARING
    advance_document(doc, admin)                            # -> INTERNAL_REVIEW
    advance_document(doc, admin)                            # -> SENT_CLIENT
    assert doc.waiting_for and "Client" in doc.waiting_for


# --------------------------------------------------------------------------- #
# 3. Bottleneck detection
# --------------------------------------------------------------------------- #
def test_bottleneck_waiting_client(ctx, admin):
    project = _make_project(doc_codes=("SDS",))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    sds = _doc(cycle, "SDS")
    advance_document(sds, admin)   # PREPARING
    advance_document(sds, admin)   # INTERNAL_REVIEW
    advance_document(sds, admin)   # SENT_CLIENT -> waiting on client
    advance_document(sds, admin)   # WAITING_CLIENT

    doc, desc = find_bottleneck(cycle)
    assert doc is sds
    assert "client" in desc.lower()


def test_bottleneck_not_started_priority(ctx, admin):
    project = _make_project(doc_codes=("INVOICE", "SDS"))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    sds = _doc(cycle, "SDS")
    advance_document(sds, admin)   # move SDS forward; invoice still NOT_STARTED
    doc, desc = find_bottleneck(cycle)
    assert doc.doc_type.code == "INVOICE"
    assert "not started" in desc.lower()


# --------------------------------------------------------------------------- #
# 4. Cycle status transitions
# --------------------------------------------------------------------------- #
def test_cycle_status_reaches_ready(ctx, admin):
    project = _make_project(doc_codes=("SDS",))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    doc = _doc(cycle, "SDS")
    for _ in range(6):
        advance_document(doc, admin)
    recompute_cycle(cycle)
    assert cycle.status_code == "READY_FOR_SUBMISSION"
    assert "submission" in (next_action(cycle) or "").lower()


def test_cycle_status_overdue(ctx, admin):
    project = _make_project(doc_codes=("SDS",))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    doc = _doc(cycle, "SDS")
    doc.due_date = date.today() - timedelta(days=3)
    recompute_cycle(cycle)
    assert cycle.status_code == "OVERDUE"


# --------------------------------------------------------------------------- #
# 5. Permission matrix
# --------------------------------------------------------------------------- #
def test_user_permission_matrix(ctx, admin):
    from web.models import DocumentType
    ensure_master_data()
    sds = DocumentType.query.filter_by(code="SDS").first()
    po = DocumentType.query.filter_by(code="PO").first()

    u = User(username="sgs_ops", role="SGS", active=True)  # type: ignore
    u.set_password("secret1")
    _db.session.add(u)
    _db.session.flush()
    _db.session.add(UserDocumentPermission(
        user_id=u.id, doc_type_id=sds.id, can_prepare=True, can_approve=False))  # type: ignore
    _db.session.commit()

    # Admin bypasses.
    assert user_can_prepare(admin, sds.id) and user_can_approve(admin, sds.id)
    # The matrix applies to a normal user.
    assert user_can_prepare(u, sds.id) is True
    assert user_can_approve(u, sds.id) is False
    assert user_can_prepare(u, po.id) is False   # no row -> no access
    assert user_can_approve(u, po.id) is False


def test_advance_blocked_without_permission(ctx, admin):
    from web.models import DocumentType
    ensure_master_data()
    sds = DocumentType.query.filter_by(code="SDS").first()

    u = User(username="no_rights", role="Employee", active=True)  # type: ignore
    u.set_password("secret1")
    _db.session.add(u)
    _db.session.commit()

    project = _make_project(doc_codes=("SDS",))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    doc = _doc(cycle, "SDS")

    ok, msg = advance_document(doc, u)
    assert not ok
    assert "preparation" in msg.lower()
    assert doc.status_code == "NOT_STARTED"


# --------------------------------------------------------------------------- #
# 6. Scheduled sweep / escalation
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 7. Monthly recurrence + dashboard calendar + cascade delete
# --------------------------------------------------------------------------- #
def test_contract_covers_month(ctx, admin):
    from web.models import Project
    project = Project(name="Contract window", client_id=_make_project().client_id,  # type: ignore
                      submission_method_id=_make_project().submission_method_id,  # type: ignore
                      contract_start=date(2026, 1, 1),  # type: ignore
                      contract_end=date(2026, 6, 30))  # type: ignore
    _db.session.add(project)
    _db.session.commit()
    assert project_covers_month(project, "2026-03") is True
    assert project_covers_month(project, "2026-06") is True   # whole month inside
    assert project_covers_month(project, "2026-07") is False  # after contract end


def test_generate_monthly_cycles_is_idempotent(ctx, admin):
    p1 = _make_project(name="Active project")
    p1.contract_start = date(2026, 1, 1)
    p1.contract_end = None            # ongoing
    p2 = _make_project(name="Expired project")
    p2.contract_start = date(2026, 1, 1)
    p2.contract_end = date(2026, 8, 31)   # ends before September
    _db.session.commit()

    created = generate_monthly_cycles("2026-09", admin)
    assert created == 1               # only the ongoing project qualifies
    again = generate_monthly_cycles("2026-09", admin)
    assert again == 0                 # idempotent — no duplicates


def test_month_calendar_places_cycles_on_days(ctx, admin):
    project = _make_project(doc_codes=("SDS",))
    cycle = create_invoice_cycle(
        project, "2026-09", admin, target_submit_date=date(2026, 9, 15))
    cal = month_calendar("2026-09")
    assert cal["month"] == "2026-09"
    assert cal["cycle_count"] == 1
    assert cycle.id in [c.id for c in cal["by_day"][15]]
    assert cal["prev_month"] == "2026-08"
    assert cal["next_month"] == "2026-10"


def test_project_delete_cascades_details(ctx, admin):
    project = _make_project(doc_codes=("SDS", "INVOICE"))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    assert len(cycle.documents) == 2
    _db.session.delete(project)
    _db.session.commit()
    from web.models import InvoiceCycle, CycleDocument
    assert InvoiceCycle.query.count() == 0
    assert CycleDocument.query.count() == 0


def test_scheduled_sweep_escalates_overdue(ctx, admin):
    # An employee with a linked user account owns the overdue document.
    from web.models import Department, Employee
    dept = Department.query.filter_by(code="CSD").first()
    emp = Employee(name="Noufal", department_id=dept.id)  # type: ignore
    _db.session.add(emp)
    _db.session.flush()
    owner = User(username="csd_owner", role="Employee", active=True,  # type: ignore
                 employee_id=emp.id)  # type: ignore
    owner.set_password("secret1")
    _db.session.add(owner)
    _db.session.commit()

    project = _make_project(doc_codes=("SDS",))
    cycle = create_invoice_cycle(project, "2026-08", admin)
    doc = _doc(cycle, "SDS")
    doc.employee_id = emp.id
    doc.due_date = date.today() - timedelta(days=5)
    _db.session.commit()

    created = scheduled_sweep(today=date.today())
    assert created >= 1
    n = Notification.query.filter_by(ntype="ESCALATION").first()
    assert n is not None
    assert n.user_id == owner.id
    assert "OVERDUE" in n.message
