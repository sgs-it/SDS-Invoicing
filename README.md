# SDS Invoice & Client Payment Tracking System

A self-contained, offline-capable web app that turns the company's invoicing
procedure (previously a single Excel workbook) into configurable digital
workflows. For every client project it tracks the required documents, the
current bottleneck, the invoice status, a delivery-style timeline, submissions,
client approval and payments — with dashboards, notifications, escalation,
audit trail and management reports.

**Stack:** Python + Flask + SQLite (no external database, no internet needed).

---

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000** and sign in with the seeded admin account:

| Username | Password |
|----------|----------|
| `admin`  | `admin123` |

You will be prompted to change the password on first login.

> The database file lives at `data/sds.db` and is created automatically on
> first run, along with all master data (document types, workflows, submission
> methods, departments).

---

## Importing the existing Excel data (one-time)

The migration script parses `SDS Invoicing_Procedure_.xlsx` into clients,
projects, required-document checklists and procedures:

```bash
python migrate_import.py
```

- Idempotent: re-running skips projects it already imported (matched by job code).
- Add `--reset` to drop and rebuild the imported data first.
- To run the import on a fresh database: `python migrate_import.py` then start
  the app. You can import first and use the app right after.

---

## Running the tests

```bash
pip install pytest
python -m pytest tests/ -q
```

The suite covers the workflow engine: cycle creation, document advancement,
bottleneck detection, cycle-status computation, the per-user document
permission matrix, and the scheduled due-date / escalation sweep.

---

## Roles & permissions

| Role        | Can do                                                                  |
|-------------|-------------------------------------------------------------------------|
| **Admin**   | Everything — users, permissions matrix, workflow templates, settings, audit log |
| **Manager** | All views, reports, approvals; bypasses the document permission matrix  |
| **CSD / SGS / Finance / Operations** | Documents, submissions and payments for their area |
| **Employee**| Their assigned documents and notifications                              |

**Document permission matrix (Admin → Users → Permissions):** for every
document type you can grant a user *Can prepare* (create / advance a document
through preparation) and *Can approve* (approve / complete it). A user with no
entry for a document type cannot act on it. The workflow engine enforces these
checks on every move, in both the UI and the API.

---

## How the workflow works

1. **Create an invoice cycle** for a project → the app auto-generates one
   *document card* per required document (Invoice, PO, SDS, GRN, WDA, KPI, IAF,
   Monthly Report, Attendance, Service Report, DO, Service Sheet, Completion
   Form).
2. **Advance each document** through its own configurable workflow
   (e.g. SDS: Not Started → Preparing → Internal Review → Sent to Client →
   Waiting for Client Signature → Approved → Completed). Each move is
   permission-gated and recorded in the document's full history.
3. **The system derives everything else automatically:**
   - the **invoice status** from the documents / submission / payment
     (e.g. "8 documents required, 5 completed, 1 in preparation, 2 waiting"),
   - the **bottleneck** — what the company is waiting on, and from whom,
   - the **next action**,
   - the **delivery-style timeline** from document prep → submission → client
     approval → payment received.
4. **Register a submission** (direct / portal / email, with reference and
   confirmation numbers), reject / resubmit, approve.
5. **Record payments** — due / expected / actual dates, outstanding amount,
   aging, and auto-computed payment status.
6. The **background scheduler** (hourly) raises due-date reminders and overdue
   escalations into the in-app notification center.

---

## Reports

All reports support filters, a print view (🖨) and CSV export (⬇):

1. Invoice Status
2. Document Pending
3. Client Waiting
4. Department / Employee Performance
5. Submission
6. Client Delay
7. Payment Aging (30 / 60 / 90+)
8. Outstanding Payment
9. Monthly Invoice / Revenue

---

## Admin

- **Users** — create accounts, assign roles, link to employees, force a
  password change, and assign the per-document prepare/approve matrix.
- **Settings** — currency symbol and optional SMTP for email notifications
  (in-app notifications always work).
- **Audit log** — the last 500 system actions, permanently recorded.

## Project structure

```
app.py                  Entry point (python app.py)
config.py               Settings (DB path, uploads, scheduler, SMTP)
requirements.txt
README.md
migrate_import.py       Phase 1: Excel -> structured seed data (run once)
data/                   SQLite DB + uploads (created at runtime)
web/
  __init__.py           create_app() factory + scheduler thread
  models.py             All SQLAlchemy models
  workflow.py           Workflow engine (status, bottleneck, timeline, sweep)
  seed.py               Master-data seeding + default admin account
  auth.py               Login / logout / change password / role gates
  routes_*.py           Dashboard, masters, invoices, submissions, payments,
                        reports, notifications, admin
  templates/            Jinja2 pages (offline, custom CSS/JS)
  static/               app.css + app.js (no CDN)
tests/
  test_workflow.py      pytest suite for the workflow engine
```
