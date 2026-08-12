"""Central application configuration.

All paths are relative to the project root so the app works regardless of the
current working directory. Overridable via environment variables.
"""
import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "sds.db")

# Use /tmp for uploads on Vercel due to read-only filesystem
if os.environ.get("VERCEL") == "1":
    UPLOAD_DIR = "/tmp/sds_uploads"
else:
    UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "sds-change-me-secret-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get("SUPABASE_DB_URI") or "sqlite:///" + DB_PATH
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Only use sqlite-specific connect args if using sqlite
    if SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {}

    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

    UPLOAD_FOLDER = UPLOAD_DIR
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MB per attachment

    # Currency symbol used across money displays (editable later via SystemSettings)
    CURRENCY = "AED"

    # Notification / escalation tuning
    DUE_ALERT_DAYS = (7, 3, 1)      # alert these many days before a due date
    ESCALATION_AFTER_DAYS = 1       # escalate this many days after an overdue date
    
    # Feature flags
    # Disable scheduler in Vercel's serverless environment
    SCHEDULER_ENABLED = True
    if os.environ.get("VERCEL") == "1":
        SCHEDULER_ENABLED = False
    
    SCHEDULER_INTERVAL_SECONDS = 3600  # run due-date / escalation sweep every hour

    # Optional SMTP (email notifications). Leave SMTP_HOST empty to disable email.
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "SDS Invoicing <no-reply@sds.local>")
    SMTP_USE_TLS = True
