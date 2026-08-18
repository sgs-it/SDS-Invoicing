"""Master data management: clients, projects, departments, employees,
document types, submission methods and workflow templates.
Admin + Manager only.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from web import db
from web.auth import role_required
from web.models import (
    Client,
    Department,
    DocumentType,
    Employee,
    InvoiceCycle,
    Project,
    ProjectDocumentRequirement,
    SubmissionMethod,
    WorkflowStep,
    WorkflowTemplate,
)
from web.workflow import audit

bp = Blueprint("masters", __name__)


def _flash_success(message):
    flash(message, "success")


# =========================================================================== #
# Projects
# =========================================================================== #
@bp.route("/projects")
@role_required("Admin", "Manager")
def projects():
    from datetime import date
    from sqlalchemy.orm import joinedload
    rows = (Project.query.options(
                joinedload(Project.client),
                joinedload(Project.submission_method),
                joinedload(Project.requirements).joinedload(ProjectDocumentRequirement.doc_type),
                joinedload(Project.default_team)
            )
            .join(Client)
            .order_by(Client.name, Project.name).all())
    return render_template("projects.html", projects=rows,
                           today=date.today())


@bp.route("/projects/<int:project_id>")
@role_required("Admin", "Manager")
def project_detail(project_id):
    project = db.get_or_404(Project, project_id)
    from web.workflow import cycle_status_css, cycle_status_label
    return render_template("project_detail.html", project=project,
                           CYCLE_CSS=cycle_status_css,
                           CYCLE_STATUS=cycle_status_label)


@bp.route("/projects/new", methods=["GET", "POST"])
@role_required("Admin", "Manager")
def project_new():
    if request.method == "POST":
        client = Client.query.get(request.form.get("client_id", type=int))
        method = SubmissionMethod.query.get(
            request.form.get("submission_method_id", type=int))
        if not client or not method:
            flash("Client and submission method are required.", "danger")
            return redirect(url_for("masters.project_new"))
        p = Project(
            client_id=client.id, submission_method_id=method.id,
            name=request.form.get("name", "").strip(),
            job_code=request.form.get("job_code", "").strip() or None,
            common_criteria=request.form.get("common_criteria") or None,
            sds_criteria=request.form.get("sds_criteria") or None,
            target_date_rule=request.form.get("target_date_rule") or None,
            default_team_code=request.form.get("default_team_code") or None,
            contract_start=_parse_date(request.form.get("contract_start")),
            contract_end=_parse_date(request.form.get("contract_end")),
        )
        db.session.add(p)
        db.session.flush()
        _add_requirements_from_form(p, request.form)
        audit(_user(), f"Created project: {p.name}", "Project", p.id)
        db.session.commit()
        _flash_success("Project created.")
        return redirect(url_for("masters.project_detail", project_id=p.id))
    clients = Client.query.order_by(Client.name).all()
    methods = SubmissionMethod.query.order_by(SubmissionMethod.name).all()
    doc_types = DocumentType.query.order_by(DocumentType.sort_order,
                                            DocumentType.name).all()
    return render_template("project_form.html", project=None,
                           clients=clients, methods=methods,
                           doc_types=doc_types, selected_docs=[])


@bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@role_required("Admin", "Manager")
def project_edit(project_id):
    project = db.get_or_404(Project, project_id)
    if request.method == "POST":
        client = Client.query.get(request.form.get("client_id", type=int))
        method = SubmissionMethod.query.get(
            request.form.get("submission_method_id", type=int))
        if not client or not method:
            flash("Client and submission method are required.", "danger")
            return redirect(url_for("masters.project_edit",
                                    project_id=project.id))
        project.client_id = client.id
        project.submission_method_id = method.id
        project.name = request.form.get("name", "").strip()
        project.job_code = request.form.get("job_code", "").strip() or None
        project.common_criteria = request.form.get("common_criteria") or None
        project.sds_criteria = request.form.get("sds_criteria") or None
        project.target_date_rule = request.form.get("target_date_rule") or None
        project.default_team_code = request.form.get("default_team_code") or None
        project.contract_start = _parse_date(request.form.get("contract_start"))
        project.contract_end = _parse_date(request.form.get("contract_end"))
        # replace requirements
        ProjectDocumentRequirement.query.filter_by(
            project_id=project.id).delete()
        _add_requirements_from_form(project, request.form)
        audit(_user(), f"Updated project: {project.name}", "Project", project.id)
        db.session.commit()
        _flash_success("Project updated.")
        return redirect(url_for("masters.project_detail", project_id=project.id))
    clients = Client.query.order_by(Client.name).all()
    methods = SubmissionMethod.query.order_by(SubmissionMethod.name).all()
    doc_types = DocumentType.query.order_by(DocumentType.sort_order,
                                            DocumentType.name).all()
    selected_docs = [r.doc_type_id for r in project.requirements]
    return render_template("project_form.html", project=project,
                           clients=clients, methods=methods,
                           doc_types=doc_types, selected_docs=selected_docs)


@bp.route("/projects/<int:project_id>/renew", methods=["GET", "POST"])
@role_required("Admin", "Manager")
def project_renew(project_id):
    project = Project.query.get_or_404(project_id)
    if request.method == "POST":
        project.contract_start = _parse_date(request.form.get("contract_start"))
        project.contract_end = _parse_date(request.form.get("contract_end"))
        db.session.commit()
        _flash_success("Contract dates updated successfully.")
        return redirect(url_for("masters.project_detail", project_id=project.id))
    return render_template("project_renew.html", project=project)


def _add_requirements_from_form(project, form):
    codes = form.getlist("required_docs")
    for i, code in enumerate(codes):
        dt = DocumentType.query.filter_by(code=code).first()
        if dt:
            db.session.add(ProjectDocumentRequirement(
                project_id=project.id, doc_type_id=dt.id,
                sequence=i, required=True))


@bp.route("/projects/<int:project_id>/toggle")
@role_required("Admin", "Manager")
def project_toggle(project_id):
    project = db.get_or_404(Project, project_id)
    project.active = not project.active
    audit(_user(), f"{'Deactivated' if not project.active else 'Activated'} "
                   f"project: {project.name}", "Project", project.id)
    db.session.commit()
    _flash_success("Project updated.")
    return redirect(url_for("masters.projects"))


@bp.route("/projects/<int:project_id>/delete", methods=["POST"])
@role_required("Admin", "Manager")
def project_delete(project_id):
    """Permanently delete a project and every detail it owns.

    The monthly procedure keeps running until the project is deleted — after
    that all of its invoice cycles, documents, history, submissions and
    payments are removed too (cascade).
    """
    project = db.get_or_404(Project, project_id)
    name = project.name
    if project.cycles.count():
        audit(_user(),
              f"Deleted project with {project.cycles.count()} cycle(s): {name}",
              "Project", project.id)
    else:
        audit(_user(), f"Deleted project: {name}", "Project", project.id)
    db.session.delete(project)
    db.session.commit()
    _flash_success(f"Project '{name}' and all of its details were deleted.")
    return redirect(url_for("masters.projects"))


# =========================================================================== #
# Clients
# =========================================================================== #
@bp.route("/masters/clients")
@role_required("Admin", "Manager")
def clients():
    rows = (Client.query.outerjoin(Project).order_by(Client.name)
            .all())
    return render_template("clients.html", clients=rows)


@bp.route("/masters/clients/new", methods=["POST"])
@role_required("Admin", "Manager")
def client_new():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Client name is required.", "danger")
        return redirect(url_for("masters.clients"))
    c = Client(name=name, email=request.form.get("email") or None,
               phone=request.form.get("phone") or None,
               address=request.form.get("address") or None)
    db.session.add(c)
    audit(_user(), f"Created client: {name}", "Client", None)
    db.session.commit()
    _flash_success("Client created.")
    return redirect(url_for("masters.clients"))


@bp.route("/masters/clients/<int:client_id>/edit", methods=["POST"])
@role_required("Admin", "Manager")
def client_edit(client_id):
    c = db.get_or_404(Client, client_id)
    c.name = request.form.get("name", c.name).strip()
    c.email = request.form.get("email") or None
    c.phone = request.form.get("phone") or None
    c.address = request.form.get("address") or None
    audit(_user(), f"Updated client: {c.name}", "Client", c.id)
    db.session.commit()
    _flash_success("Client updated.")
    return redirect(url_for("masters.clients"))


@bp.route("/masters/clients/<int:client_id>/toggle")
@role_required("Admin", "Manager")
def client_toggle(client_id):
    c = db.get_or_404(Client, client_id)
    c.active = not c.active
    audit(_user(), f"{'Deactivated' if not c.active else 'Activated'} "
                   f"client: {c.name}", "Client", c.id)
    db.session.commit()
    _flash_success("Client updated.")
    return redirect(url_for("masters.clients"))


# =========================================================================== #
# Departments
# =========================================================================== #
@bp.route("/masters/departments")
@role_required("Admin", "Manager")
def departments():
    return render_template("departments.html",
                           rows=Department.query.order_by(Department.code).all())


@bp.route("/masters/departments/new", methods=["POST"])
@role_required("Admin", "Manager")
def department_new():
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip().upper()
    if not name or not code:
        flash("Name and code are required.", "danger")
        return redirect(url_for("masters.departments"))
    if Department.query.filter_by(code=code).first():
        flash("A department with that code already exists.", "danger")
        return redirect(url_for("masters.departments"))
    db.session.add(Department(name=name, code=code))
    db.session.commit()
    _flash_success("Department created.")
    return redirect(url_for("masters.departments"))


# =========================================================================== #
# Employees
# =========================================================================== #
@bp.route("/masters/employees")
@role_required("Admin", "Manager")
def employees():
    rows = (Employee.query.outerjoin(Department)
            .order_by(Employee.name).all())
    depts = Department.query.order_by(Department.code).all()
    return render_template("employees.html", employees=rows, depts=depts)


@bp.route("/masters/employees/new", methods=["POST"])
@role_required("Admin", "Manager")
def employee_new():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Employee name is required.", "danger")
        return redirect(url_for("masters.employees"))
    dept = request.form.get("department_id", type=int)
    db.session.add(Employee(
        name=name, email=request.form.get("email") or None,
        department_id=dept or None))
    db.session.commit()
    _flash_success("Employee created.")
    return redirect(url_for("masters.employees"))


@bp.route("/masters/employees/<int:employee_id>/edit", methods=["POST"])
@role_required("Admin", "Manager")
def employee_edit(employee_id):
    e = db.get_or_404(Employee, employee_id)
    e.name = request.form.get("name", e.name).strip()
    e.email = request.form.get("email") or None
    e.department_id = request.form.get("department_id", type=int) or None
    e.active = bool(request.form.get("active"))
    db.session.commit()
    _flash_success("Employee updated.")
    return redirect(url_for("masters.employees"))


# =========================================================================== #
# Document types
# =========================================================================== #
@bp.route("/masters/document-types")
@role_required("Admin", "Manager")
def document_types():
    rows = (DocumentType.query.order_by(DocumentType.sort_order,
                                        DocumentType.name).all())
    tpl_map = {t.doc_type_id: t for t in WorkflowTemplate.query
               .filter(WorkflowTemplate.doc_type_id.isnot(None)).all()}
    return render_template("document_types.html", rows=rows, tpl_map=tpl_map)


@bp.route("/masters/document-types/new", methods=["POST"])
@role_required("Admin", "Manager")
def document_type_new():
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    if not code or not name:
        flash("Code and name are required.", "danger")
        return redirect(url_for("masters.document_types"))
    if DocumentType.query.filter_by(code=code).first():
        flash("A document type with that code already exists.", "danger")
        return redirect(url_for("masters.document_types"))
    db.session.add(DocumentType(
        code=code, name=name,
        requires_client_sign=bool(request.form.get("requires_client_sign")),
        requires_grn=bool(request.form.get("requires_grn")),
        icon=request.form.get("icon") or "📄",
        description=request.form.get("description") or None))
    db.session.commit()
    _flash_success("Document type created.")
    return redirect(url_for("masters.document_types"))


# =========================================================================== #
# Submission methods
# =========================================================================== #
@bp.route("/masters/submission-methods")
@role_required("Admin", "Manager")
def submission_methods():
    rows = (SubmissionMethod.query.order_by(SubmissionMethod.name).all())
    templates = WorkflowTemplate.query.filter(
        WorkflowTemplate.doc_type_id.is_(None)).all()
    return render_template("submission_methods.html", rows=rows,
                           templates=templates)


# =========================================================================== #
# Workflow templates
# =========================================================================== #
@bp.route("/masters/workflows")
@role_required("Admin", "Manager")
def workflows():
    rows = (WorkflowTemplate.query.order_by(WorkflowTemplate.doc_type_id)
            .all())
    return render_template("workflows.html", rows=rows)


@bp.route("/masters/workflows/<int:template_id>", methods=["GET", "POST"])
@role_required("Admin", "Manager")
def workflow_edit(template_id):
    tpl = db.get_or_404(WorkflowTemplate, template_id)
    if request.method == "POST":
        tpl.name = request.form.get("name", tpl.name).strip()
        # steps: names[], status_codes[], waiting_types[], waiting_ons[], terminals[]
        names = request.form.getlist("step_name")
        codes = request.form.getlist("step_status")
        wtypes = request.form.getlist("step_waiting_type")
        wons = request.form.getlist("step_waiting_on")
        terms = request.form.getlist("step_terminal")
        WorkflowStep.query.filter_by(template_id=tpl.id).delete()
        for i, name in enumerate(names):
            if not name.strip():
                continue
            db.session.add(WorkflowStep(
                template_id=tpl.id, sequence=i, name=name.strip(),
                status_code=(codes[i] if i < len(codes) else "").strip() or
                f"STEP{i}",
                waiting_type=(wtypes[i] if i < len(wtypes) else "NONE") or "NONE",
                waiting_on=(wons[i] if i < len(wons) else "") or None,
                is_terminal=(terms[i] if i < len(terms) else "") == "1",
            ))
        audit(_user(), f"Updated workflow template: {tpl.name}",
              "WorkflowTemplate", tpl.id)
        db.session.commit()
        _flash_success("Workflow template updated.")
        return redirect(url_for("masters.workflow_edit", template_id=tpl.id))
    return render_template("workflow_form.html", tpl=tpl)


def _user():
    from flask_login import current_user
    return current_user


def _parse_date(value):
    from datetime import datetime
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
