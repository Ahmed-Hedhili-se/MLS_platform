
# .env must be loaded before anything else imports, because modules
# such as webapp.database read os.environ at import time. Loading it
# further down -- as this file used to -- meant DATABASE_URL and
# friends were always already too late to have any effect.
from pathlib import Path as _Path

from dotenv import load_dotenv

load_dotenv(_Path(__file__).resolve().parent.parent / ".env")

import os  # noqa: E402
import re  # noqa: E402
import json  # noqa: E402
import smtplib  # noqa: E402
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_from_directory,
    session,
    abort,
    flash,
    jsonify,
)
import shutil
import subprocess
from pathlib import Path
from webapp.email_utils import send_grade_published_email
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
# NOTE: grade.grade_submission is deliberately NOT imported here.
# It executes student notebooks in the calling process. Every
# grading path in this app goes through the Docker sandbox in
# _grade_submission_blocking instead.
from authlib.integrations.flask_client import OAuth
from sqlalchemy.orm import joinedload

from .database import SessionLocal
from .models import (
    Lab,
    Student,
    Submission,
    Grade,
    NotebookResult,
    Notification,
)
from .grading_db import (
    save_grading_result,
    finalize_lab_grade,
)
from . import grading_queue
from .snapshots import create_repository_snapshot, validate_repo_url
from .submission_rules import (
    MAX_RESUBMISSION_REJECTIONS,
    MAX_SUBMISSION_ATTEMPTS,
    NotEligible,
    evaluate_submission_eligibility,
    retire_previous_submission,
)
from urllib.parse import urlparse
import subprocess
import sys
from pathlib import Path

# Overridable so a rebuilt grader can be rolled out by tag
# without editing code.
GRADER_IMAGE = os.environ.get("GRADER_IMAGE") or "mls-grader:1.0"

# Resource cap per grading container. The defaults suit the current
# labs -- sklearn on a few tens of thousands of rows -- and let the
# platform run on a small server. Raise them if a lab starts training
# something genuinely heavy; the container is killed if it exceeds
# the memory limit.
GRADER_MEMORY = os.environ.get("GRADER_MEMORY") or "1g"
GRADER_CPUS = os.environ.get("GRADER_CPUS") or "1"

# The repository-URL rules live in snapshots.py so the JSON API can share
# them. Kept under the original name for the callers below.
validate_submission_repo_url = validate_repo_url

#helper 
def ensure_utc(dt):
    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(timezone.utc)

# ============================================================
# Notification Helper
# ============================================================

def create_notification(
    db,
    student_id,
    notification_type,
    title,
    message,
    lab_id=None,
    submission_id=None,
):
    notification = Notification(
        student_id=student_id,
        lab_id=lab_id,
        submission_id=submission_id,
        notification_type=notification_type,
        title=title,
        message=message,
        read=False,
        read_at=None,
        created_at=datetime.now(timezone.utc),
    )

    db.add(notification)

    return notification

# ============================================================
# Authentication decorators
# ============================================================

def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "student_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


def teacher_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "student_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "teacher":
            return abort(403)
        return view(*args, **kwargs)
    return wrapped_view


# ============================================================
# Flask application
# ============================================================

app = Flask(__name__)

app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
)
@app.context_processor
def inject_notification_count():

    unread_notification_count = 0

    if session.get("student_id"):

        with SessionLocal() as db:

            unread_notification_count = (
                db.query(Notification)
                .filter(
                    Notification.student_id
                    == session["student_id"],
                    Notification.read == False,
                )
                .count()
            )

    return {
        "unread_notification_count":
            unread_notification_count
    }

# ============================================================
# Template filter: safe external links
# ============================================================

@app.template_filter("external_url")
def external_url(value):
    """
    Return the value only when it is a plain http(s) URL.

    Lab URLs (assignment, solution, video) are free text entered
    by a teacher and are rendered directly into href attributes.
    Without this guard a value such as "javascript:..." or
    "data:text/html,..." would execute in the browser of every
    student opening the lab.

    An empty string is returned for anything else so templates
    can hide the link entirely.
    """

    if not value:
        return ""

    candidate = str(value).strip()

    parsed = urlparse(candidate)

    if parsed.scheme not in ("http", "https"):
        return ""

    if not parsed.netloc:
        return ""

    return candidate


# ============================================================
# Flask session security
# ============================================================

app.secret_key = os.environ["FLASK_SECRET_KEY"]

# MLS_INSECURE_COOKIES=1 is for plain-HTTP local development only.
# Everywhere else the session cookie is HTTPS-only, so it is never
# put on the wire in clear text.
_insecure_cookies = (
    os.environ.get("MLS_INSECURE_COOKIES", "").strip().lower()
    in ("1", "true", "yes")
)

app.config.update(
    # Not readable from JavaScript, so an XSS bug cannot steal the
    # session outright.
    SESSION_COOKIE_HTTPONLY=True,

    # Only sent over HTTPS unless explicitly relaxed for local dev.
    SESSION_COOKIE_SECURE=not _insecure_cookies,

    # "Lax" still allows the top-level GET redirect back from the
    # GitHub OAuth callback, which "Strict" would break.
    SESSION_COOKIE_SAMESITE="Lax",

    # Sessions expire rather than living forever in a stale browser.
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),

    # Reject oversized request bodies before they are buffered.
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)


# ============================================================
# Background grading queue
# ============================================================

# Any submission still marked "grading" belongs to a container that
# died with the previous process; reset it so it can be retried.
_recovered = grading_queue.recover_interrupted_jobs()

if _recovered:
    app.logger.warning(
        "Reset %s submission(s) left mid-grade by a restart",
        _recovered,
    )

grading_queue.start(app)


# ============================================================
# CSRF protection
# ============================================================
#
# Every state-changing route -- publishing grades, deleting labs,
# overriding scores -- is a plain POST guarded only by the session
# cookie. Without a CSRF token any page a signed-in teacher visits
# could forge those requests on their behalf.
#
# Server-rendered forms carry a hidden csrf_token field. The React
# client reads the token from the non-HttpOnly XSRF-TOKEN cookie set
# below and echoes it back in the X-CSRFToken header.

csrf = CSRFProtect(app)

app.config["WTF_CSRF_TIME_LIMIT"] = None


@app.after_request
def set_csrf_cookie(response):
    """
    Publish the CSRF token to the React client.

    Deliberately readable by JavaScript: the SPA has to be able to
    read it to echo it back in a header. That is safe -- the token
    is not a credential on its own, and an attacker on another
    origin still cannot read this cookie.
    """

    if response.status_code < 400:

        response.set_cookie(
            "XSRF-TOKEN",
            generate_csrf(),
            secure=not _insecure_cookies,
            httponly=False,
            samesite="Lax",
        )

    return response

@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    """
    A rejected token means a forged request or a stale open tab.

    The API answers in JSON so the React client can show the real
    message; server-rendered pages get a flash and a redirect so a
    teacher who left a tab open overnight is told to retry rather
    than shown a bare 400.
    """

    if request.path.startswith("/api/"):
        return jsonify({
            "error": "csrf_failed",
            "message": (
                "Your session expired. Reload the page and try again."
            ),
        }), 400

    flash(
        "Your session expired. Please try that again.",
        "error",
    )

    return redirect(request.referrer or url_for("home")), 303


# ============================================================
# GitHub OAuth
# ============================================================

oauth = OAuth(app)

github = oauth.register(
    name="github",
    client_id=os.environ["GITHUB_CLIENT_ID"],
    client_secret=os.environ["GITHUB_CLIENT_SECRET"],
    authorize_url="https://github.com/login/oauth/authorize",
    access_token_url="https://github.com/login/oauth/access_token",
    api_base_url="https://api.github.com/",
    client_kwargs={
        "scope": "read:user user:email",
    },
)

# ============================================================
# Helpers
# ============================================================

def clean_terminal_text(text):
    if not text:
        return text
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ============================================================
# Email
# ============================================================

def send_grade_email(
    student_email,
    student_username,
    lab_name,
    teacher_score,
    feedback,
):
    """
    Legacy local SMTP helper.
    Publication routes currently use
    send_grade_published_email from webapp.email_utils.
    """
    if not student_email:
        return False

    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(
        os.environ.get(
            "SMTP_PORT",
            "587",
        )
    )
    smtp_username = os.environ.get("SMTP_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    sender_email = os.environ.get(
        "MAIL_FROM",
        smtp_username,
    )

    missing = []

    if not smtp_host:
        missing.append("SMTP_HOST")
    if not smtp_username:
        missing.append("SMTP_USERNAME")
    if not smtp_password:
        missing.append("SMTP_PASSWORD")
    if not sender_email:
        missing.append("MAIL_FROM")

    if missing:
        raise RuntimeError(
            "Missing SMTP environment variables: "
            + ", ".join(missing)
        )

    message = EmailMessage()
    message["Subject"] = f"Grade published — {lab_name}"
    message["From"] = sender_email
    message["To"] = student_email

    feedback_text = (
        feedback
        if feedback
        else "No feedback provided."
    )

    message.set_content(
        f"""
Hello {student_username},

Your grade for the lab "{lab_name}" has been published.

Teacher grade: {teacher_score:.2f}%

Feedback:

{feedback_text}

Please log in to the Lab Grading System
to view your complete submission and results.

Best regards,
Lab Grading System
""".strip()
    )

    with smtplib.SMTP(
        smtp_host,
        smtp_port,
        timeout=20,
    ) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(
            smtp_username,
            smtp_password,
        )
        smtp.send_message(message)

    return True


# ============================================================
# Home
# ============================================================

@app.route("/")
def home():
    with SessionLocal() as db:
        labs = (
            db.query(Lab)
            .filter_by(
                archived=False,
                visible=True,
            )
            .order_by(
                Lab.display_order.asc(),
                Lab.id.asc(),
            )
            .all()
        )
        # The React client is the platform. Teachers continue to the
        # server-rendered dashboard, which is still the only place the
        # grading tools exist.
        if session.get("role") == "teacher":
            return redirect(url_for("teacher_dashboard"))

        return redirect("/app/labs")

# ============================================================
# Lab Details
# ============================================================

@app.route(
    "/lab/<int:lab_id>/details"
)
@login_required
def lab_details(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(
                id=lab_id
            )
            .first()
        )

        if lab is None:
            return (
                "Lab not found",
                404,
            )

        # ----------------------------------------------------
        # Students cannot access archived/hidden labs
        # ----------------------------------------------------

        if session.get("role") != "teacher":

            if lab.archived or not lab.visible:
                return (
                    "This laboratory is not available.",
                    404,
                )

        if session.get("role") != "teacher":
            return redirect(f"/app/labs/{lab.id}")

        return render_template(
            "labs/detail.html",
            lab=lab,
        )
# ============================================================
# Edit Lab
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/edit",
    methods=["GET", "POST"],
)
@teacher_required
def edit_lab(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(
                id=lab_id
            )
            .first()
        )

        if lab is None:
            return (
                "Lab not found",
                404,
            )

        if request.method == "POST":

            # ------------------------------------------------
            # Basic information
            # ------------------------------------------------

            name = request.form.get(
                "name",
                "",
            ).strip()

            description = request.form.get(
                "description",
                "",
            ).strip()

            github_url = request.form.get(
                "github_url",
                "",
            ).strip()

            category = request.form.get(
                "category",
                "",
            ).strip()

            # ------------------------------------------------
            # Organization
            # ------------------------------------------------

            display_order = request.form.get(
                "display_order",
                0,
                type=int,
            )

            # ------------------------------------------------
            # Learning content
            # ------------------------------------------------

            solution_url = request.form.get(
                "solution_url",
                "",
            ).strip()

            video_url = request.form.get(
                "video_url",
                "",
            ).strip()

            teacher_notes = request.form.get(
                "teacher_notes",
                "",
            ).strip()

            # ------------------------------------------------
            # Validation
            # ------------------------------------------------

            if not name:

                flash(
                    "Lab name is required.",
                    "warning",
                )

                return redirect(
                    url_for(
                        "edit_lab",
                        lab_id=lab.id,
                    )
                )

            if not github_url:

                flash(
                    "GitHub assignment URL is required.",
                    "warning",
                )

                return redirect(
                    url_for(
                        "edit_lab",
                        lab_id=lab.id,
                    )
                )

            # ------------------------------------------------
            # Check duplicate name
            # ------------------------------------------------

            duplicate = (
                db.query(Lab)
                .filter(
                    Lab.name == name,
                    Lab.id != lab.id,
                )
                .first()
            )

            if duplicate:

                flash(
                    "Another lab already uses this name.",
                    "warning",
                )

                return redirect(
                    url_for(
                        "edit_lab",
                        lab_id=lab.id,
                    )
                )

            # ------------------------------------------------
            # Update Lab
            # ------------------------------------------------

            lab.name = name
            lab.description = (
                description or None
            )
            lab.github_url = github_url
            lab.category = (
                category or None
            )

            lab.display_order = (
                display_order
            )

            lab.solution_url = (
                solution_url or None
            )

            lab.video_url = (
                video_url or None
            )

            lab.teacher_notes = (
                teacher_notes or None
            )

            db.commit()

            flash(
                "Lab updated successfully.",
                "success",
            )

            return redirect(
                url_for(
                    "lab_details",
                    lab_id=lab.id,
                )
            )

        return render_template(
            "labs/edit.html",
            lab=lab,
        )

# ============================================================
# Lab page
# ============================================================

@app.route(
    "/lab/<int:lab_id>"
)
@login_required
def lab_page(lab_id):

    with SessionLocal() as db:

        # ----------------------------------------------------
        # Load lab
        # ----------------------------------------------------

        lab = (
            db.query(Lab)
            .filter_by(
                id=lab_id
            )
            .first()
        )

        if lab is None:
            return (
                "Lab not found",
                404,
            )

        # ----------------------------------------------------
        # Current time
        # ----------------------------------------------------

        now = datetime.now(
            timezone.utc
        )

        # ----------------------------------------------------
        # Normalize deadline
        # ----------------------------------------------------

        if lab.submission_deadline is not None:

            lab.submission_deadline = (
                ensure_utc(
                    lab.submission_deadline
                )
            )

        # ----------------------------------------------------
        # Load submissions
        # ----------------------------------------------------

        query = (
            db.query(Submission)
            .options(
                joinedload(
                    Submission.student
                ),
                joinedload(
                    Submission.grade
                ),
            )
            .filter_by(
                lab_id=lab.id
            )
        )

        # ----------------------------------------------------
        # Students only see their own submissions
        # ----------------------------------------------------

        if session.get("role") != "teacher":

            query = query.filter(
                Submission.student_id
                == session["student_id"]
            )

        # ----------------------------------------------------
        # Order submissions
        # ----------------------------------------------------

        submissions = (
            query
            .order_by(
                Submission.submitted_at.desc()
            )
            .all()
        )

        # ----------------------------------------------------
        # Select template
        # ----------------------------------------------------

        # ----------------------------------------------------
        # Students use the React client
        # ----------------------------------------------------

        if session.get("role") != "teacher":
            return redirect(f"/app/labs/{lab.id}")

        # ----------------------------------------------------
        # Teachers keep the lab management view
        # ----------------------------------------------------

        return render_template(
            "labs/teacher.html",
            lab=lab,
            submissions=submissions,
            now=now,
        )


# ============================================================
# Submit Lab
# ============================================================

@app.route(
    "/lab/<int:lab_id>/submit",
    methods=["POST"],
)
@login_required
def submit_lab(lab_id):

    # --------------------------------------------------------
    # Get repository URL
    # --------------------------------------------------------

    repo_url = request.form.get(
        "repo_url",
        "",
    ).strip()


    # --------------------------------------------------------
    # Validate repository URL
    # --------------------------------------------------------

    if not repo_url:

        return (
            "Repository URL is required.",
            400,
        )


    if not validate_submission_repo_url(
        repo_url
    ):

        flash(
            "Please submit a valid HTTPS GitHub repository URL.",
            "warning",
        )

        return redirect(
            url_for(
                "lab_page",
                lab_id=lab_id,
            )
        )


    with SessionLocal() as db:

        # ====================================================
        # LOAD LAB
        # ====================================================

        lab = (
            db.query(Lab)
            .filter_by(
                id=lab_id
            )
            .first()
        )

        if lab is None:

            return (
                "Lab not found",
                404,
            )


        # ====================================================
        # GET STUDENT
        # ====================================================

        student = (
            db.query(Student)
            .filter_by(
                id=session["student_id"]
            )
            .first()
        )

        if student is None:

            session.clear()

            return redirect(
                url_for("login")
            )


        # ====================================================
        # MAY THIS STUDENT SUBMIT?
        # ====================================================

        try:

            current_submission, attempt_number = (
                evaluate_submission_eligibility(db, student, lab)
            )

        except NotEligible as blocked:

            return _eligibility_response(blocked, lab)


        # ====================================================
        # CREATE NEW SUBMISSION
        # ====================================================

        submission = Submission(

            student_id=student.id,

            lab_id=lab.id,

            repo_url=repo_url,

            status="submitted",

            attempt_number=attempt_number,

            snapshot_path=None,

            commit_sha=None,

            is_current=True,

            resubmission_allowed=False,

            resubmission_requested=False,

            resubmission_message=None,

        )

        db.add(
            submission
        )


        # ----------------------------------------------------
        # Get submission ID before snapshot creation
        # ----------------------------------------------------

        db.flush()


        # ====================================================
        # CREATE IMMUTABLE REPOSITORY SNAPSHOT
        # ====================================================

        try:

            snapshot_path, commit_sha = (
                create_repository_snapshot(
                    repo_url=repo_url,
                    submission_id=submission.id,
                )
            )

        except Exception as e:

            # ------------------------------------------------
            # Roll back database changes.
            # ------------------------------------------------
            #
            # For a resubmission, the previous submission
            # remains current.
            #

            db.rollback()

            app.logger.exception(
                "Failed to create repository snapshot "
                "for submission %s",
                submission.id,
            )

            return (
                "Could not create a snapshot of the "
                "repository. Please verify that the "
                "repository is accessible and try again.",
                500,
            )


        # ====================================================
        # PREVIOUS CURRENT SUBMISSION BECOMES HISTORICAL
        # ====================================================

        retire_previous_submission(current_submission)


        # ====================================================
        # SAVE SNAPSHOT INFORMATION
        # ====================================================

        submission.snapshot_path = (
            snapshot_path
        )

        submission.commit_sha = (
            commit_sha
        )


        # ====================================================
        # SAVE DATABASE CHANGES
        # ====================================================

        db.commit()

        db.refresh(
            submission
        )


        # ====================================================
        # LOG SUCCESS
        # ====================================================

        app.logger.info(
            "Submission %s created successfully. "
            "Student=%s Lab=%s Attempt=%s "
            "Commit=%s Snapshot=%s",
            submission.id,
            student.github_username,
            lab.name,
            submission.attempt_number,
            submission.commit_sha,
            submission.snapshot_path,
        )


        # ====================================================
        # REDIRECT
        # ====================================================

        return redirect(
            url_for(
                "submission_page",
                submission_id=submission.id,
            )
        )



# ============================================================
# Eligibility -> HTTP
# ============================================================

def _eligibility_response(blocked, lab):
    """
    Turn a NotEligible into the response the lab pages give: the attempt
    ceiling flashes and returns you to the lab, every other reason is a
    plain 403.
    """

    if blocked.code == "max_attempts":

        flash(blocked.message, "warning")

        return redirect(url_for("lab_page", lab_id=lab.id))

    return (blocked.message, blocked.status)


# ============================================================
# Student submission page
# ============================================================

@app.route(
    "/submission/<int:submission_id>"
)
@login_required
def submission_page(submission_id):
    with SessionLocal() as db:
        submission = (
            db.query(Submission)
            .options(
                joinedload(Submission.student),
                joinedload(
                    Submission.grade
                )
                .selectinload(
                    Grade.notebooks
                )
                .selectinload(
                    NotebookResult.checks
                ),
            )
            .filter_by(id=submission_id)
            .first()
        )

        if submission is None:
            return (
                "Submission not found",
                404,
            )

        if (
            session.get("role") != "teacher"
            and submission.student_id
            != session["student_id"]
        ):
            return abort(403)

        if submission.grade:
            for notebook in submission.grade.notebooks:
                for check in notebook.checks:
                    check.message = clean_terminal_text(
                        check.message
                    )

        if session.get("role") != "teacher":
            return redirect("/app/submissions")

        return render_template(
            "submission.html",
            submission=submission,
        )

#reject allsubmm 
# ============================================================
# Reject All Pending Resubmission Requests
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/reject-resubmit-all",
    methods=["POST"],
)
@teacher_required
def reject_resubmit_all(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(
                id=lab_id
            )
            .first()
        )

        if lab is None:
            return (
                "Lab not found",
                404,
            )

        submissions = (
            db.query(Submission)
            .filter_by(
                lab_id=lab.id,
                is_current=True,
                resubmission_requested=True,
            )
            .all()
        )

        rejected_count = 0

        for submission in submissions:

            submission.resubmission_requested = False
            submission.resubmission_allowed = False
            submission.resubmission_rejections += 1

            if (
                submission.resubmission_rejections
                >= MAX_RESUBMISSION_REJECTIONS
            ):

                submission.resubmission_message = (
                    f"Your resubmission request has been rejected "
                    f"{MAX_RESUBMISSION_REJECTIONS} times. "
                    "No further resubmission requests are allowed "
                    "for this attempt."
                )

            else:

                remaining = (
                    MAX_RESUBMISSION_REJECTIONS
                    - submission.resubmission_rejections
                )

                submission.resubmission_message = (
                    "Your resubmission request was rejected "
                    "by the teacher. "
                    f"You have {remaining} rejection request"
                    f"{'s' if remaining != 1 else ''} remaining."
                )

            # ------------------------------------------------
            # Notification
            # ------------------------------------------------

            create_notification(
                db=db,
                student_id=submission.student_id,
                notification_type="resubmission",
                title="Resubmission Request Rejected",
                message=submission.resubmission_message,
                lab_id=submission.lab_id,
                submission_id=submission.id,
            )

            rejected_count += 1

        db.commit()

        app.logger.info(
            "Rejected %s resubmission requests in lab %s",
            rejected_count,
            lab.id,
        )

        flash(
            f"{rejected_count} resubmission request(s) rejected.",
            "warning",
        )

        return redirect(
            url_for(
                "teacher_lab_submissions",
                lab_id=lab.id,
            )
        )
# ============================================================
# Teacher Grading Page
# ============================================================

@app.route(
    "/teacher/submission/<int:submission_id>",
    methods=["GET", "POST"],
)
@teacher_required
def teacher_submission_page(submission_id):

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .options(
                joinedload(
                    Submission.student
                ),
                joinedload(
                    Submission.lab
                ),
                joinedload(
                    Submission.grade
                )
                .selectinload(
                    Grade.notebooks
                )
                .selectinload(
                    NotebookResult.checks
                ),
            )
            .filter_by(
                id=submission_id
            )
            .first()
        )

        if submission is None:

            return (
                "Submission not found",
                404,
            )

        # ----------------------------------------------------
        # Create Grade object if necessary
        # ----------------------------------------------------

        if submission.grade is None:

            grade = Grade(
                submission_id=submission.id,
                automatic_score=None,
                teacher_score=None,
                feedback=None,
                published_at=None,
            )

            db.add(grade)

            db.commit()

            db.refresh(grade)

            submission.grade = grade

        else:

            grade = submission.grade

        # ====================================================
        # POST
        # ====================================================

        if request.method == "POST":

            teacher_score = request.form.get(
                "teacher_score",
                type=float,
            )

            feedback = request.form.get(
                "feedback",
                "",
            ).strip()

            action = request.form.get(
                "action"
            )

            # ------------------------------------------------
            # Validate teacher score
            # ------------------------------------------------

            if teacher_score is None:

                return (
                    "Teacher score is required.",
                    400,
                )

            if not 0 <= teacher_score <= 100:

                return (
                    "Teacher score must be between 0 and 100.",
                    400,
                )

            # =================================================
            # SAVE DRAFT
            # =================================================

            if action == "save":

                grade.teacher_score = (
                    teacher_score
                )

                grade.feedback = (
                    feedback
                )

                db.commit()

                return redirect(
                    url_for(
                        "teacher_submission_page",
                        submission_id=submission.id,
                    )
                )

            # =================================================
            # PUBLISH
            # =================================================

            if action == "publish":

                # --------------------------------------------
                # Submission must be graded
                # --------------------------------------------

                if submission.status not in (
                    "graded",
                    "published",
                ):

                    return (
                        "This submission must be graded before "
                        "it can be published.",
                        400,
                    )

                # --------------------------------------------
                # Automatic grade required
                # --------------------------------------------

                if (
                    grade.automatic_score
                    is None
                ):

                    return (
                        "Automatic grading must be completed "
                        "before publishing.",
                        400,
                    )

                # --------------------------------------------
                # Calculate final score
                # --------------------------------------------

                final_score = (
                    0.7
                    * grade.automatic_score
                    +
                    0.3
                    * teacher_score
                )

                # --------------------------------------------
                # Update grade
                # --------------------------------------------

                grade.teacher_score = (
                    teacher_score
                )

                grade.feedback = (
                    feedback
                )

                # --------------------------------------------
                # First publication?
                # --------------------------------------------

                first_publication = (
                    grade.published_at is None
                )

                if first_publication:

                    grade.published_at = (
                        datetime.now(
                            timezone.utc
                        )
                    )

                # --------------------------------------------
                # Update submission state
                # --------------------------------------------

                submission.status = (
                    "published"
                )

                # --------------------------------------------
                # Create notification ONLY on
                # first publication
                # --------------------------------------------

                if first_publication:

                    notification_message = (
                        f"Your final grade for "
                        f"{submission.lab.name} "
                        f"has been published. "
                        f"Your final score is "
                        f"{final_score:.2f}/100."
                    )

                    create_notification(
                        db=db,
                        student_id=(
                            submission.student_id
                        ),
                        notification_type="grade",
                        title="Grade Published",
                        message=notification_message,
                        lab_id=(
                            submission.lab_id
                        ),
                        submission_id=(
                            submission.id
                        ),
                    )

                # --------------------------------------------
                # Save grade + publication +
                # notification together
                # --------------------------------------------

                db.commit()

                # --------------------------------------------
                # Email notification
                # --------------------------------------------

                email_sent = False
                email_error = False

                if (
                    first_publication
                    and submission.student.email
                ):

                    try:

                        email_sent = (
                            send_grade_published_email(
                                student_email=(
                                    submission.student.email
                                ),
                                student_username=(
                                    submission.student.github_username
                                ),
                                lab_name=(
                                    submission.lab.name
                                ),
                                score=final_score,
                                feedback=feedback,
                            )
                        )

                    except Exception as e:

                        email_error = True

                        app.logger.exception(
                            "Failed to send grade email: %s",
                            e,
                        )

                # --------------------------------------------
                # Clean terminal output
                # --------------------------------------------

                for notebook in grade.notebooks:

                    for check in notebook.checks:

                        check.message = (
                            clean_terminal_text(
                                check.message
                            )
                        )

                return render_template(
                    "teacher_submission.html",
                    submission=submission,
                    email_sent=email_sent,
                    email_error=email_error,
                )

            return (
                "Invalid action.",
                400,
            )

        # ====================================================
        # GET
        # ====================================================

        for notebook in grade.notebooks:

            for check in notebook.checks:

                check.message = (
                    clean_terminal_text(
                        check.message
                    )
                )

        return render_template(
            "teacher_submission.html",
            submission=submission,
            email_sent=False,
            email_error=False,
        )

# ============================================================
# Grade One Submission
# ============================================================

def _grade_submission_blocking(submission_id, notebook_filename=None):
    """
    Run one submission through the Docker grader, start to finish.

    notebook_filename limits the run to a single notebook; None grades
    the whole lab.

    Blocks for as long as the container does, so it is called on a
    grading-queue worker thread, never inside a request. Returns a
    (message, status_code) pair purely so the queue can log what
    happened; nothing here touches the Flask request context.
    """

    # --------------------------------------------------------
    # Load submission information
    # --------------------------------------------------------

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .options(
                joinedload(
                    Submission.student
                ),
                joinedload(
                    Submission.lab
                ),
            )
            .filter_by(
                id=submission_id
            )
            .first()
        )

        if submission is None:

            return (
                "Submission not found",
                404,
            )

        if not submission.snapshot_path:

            return (
                "This submission has no frozen repository snapshot. "
                "It cannot be graded.",
                400,
            )

        student_username = (
            submission.student.github_username
        )

        snapshot_path = (
            submission.snapshot_path
        )

        lab_name = (
            submission.lab.name
        )

        lab_id = lab_name

    # --------------------------------------------------------
    # Verify snapshot exists on the host
    # --------------------------------------------------------

    snapshot = Path(
        snapshot_path
    ).resolve()

    if not snapshot.exists():

        app.logger.error(
            "Snapshot does not exist for submission %s: %s",
            submission_id,
            snapshot,
        )

        return (
            "The frozen repository snapshot could not be found.",
            500,
        )

    # --------------------------------------------------------
    # Docker command
    # --------------------------------------------------------

    command = [
        "docker",
        "run",
        "--rm",

        # -----------------------------------------------
        # Network isolation
        # -----------------------------------------------

        "--network",
        "none",

        # -----------------------------------------------
        # Read-only container filesystem
        # -----------------------------------------------

        "--read-only",

        # -----------------------------------------------
        # No Linux capabilities
        # -----------------------------------------------

        "--cap-drop",
        "ALL",

        # -----------------------------------------------
        # Prevent privilege escalation
        # -----------------------------------------------

        "--security-opt",
        "no-new-privileges",

        # -----------------------------------------------
        # Resource limits
        # -----------------------------------------------

        "--memory",
        GRADER_MEMORY,

        "--cpus",
        GRADER_CPUS,

        "--pids-limit",
        "128",

        # -----------------------------------------------
        # Writable temporary filesystem only
        # -----------------------------------------------

        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=512m",

        # -----------------------------------------------
        # Student snapshot ONLY
        #
        # Read-only mount.
        # -----------------------------------------------

        "--mount",
        (
            "type=bind,"
            f"source={snapshot},"
            "target=/grader/submission,"
            "readonly"
        ),

        # -----------------------------------------------
        # Image
        # -----------------------------------------------

        GRADER_IMAGE,

        # -----------------------------------------------
        # Worker arguments
        # -----------------------------------------------

        "--student-id",
        student_username,

        "--lab-id",
        lab_id,

        "--snapshot-path",
        "/grader/submission",

        "--submission-id",
        str(submission_id),
    ]

    if notebook_filename:

        command += [
            "--notebook",
            notebook_filename,
        ]

    app.logger.info(
        "Starting Docker grading container "
        "for submission %s",
        submission_id,
    )

    # --------------------------------------------------------
    # Run Docker
    # --------------------------------------------------------

    try:

        result = subprocess.run(
            command,
            cwd=str(
                Path(__file__)
                .resolve()
                .parent
                .parent
            ),
            capture_output=True,
            text=True,
            timeout=15 * 60,
            check=False,
        )

    except subprocess.TimeoutExpired:

        app.logger.error(
            "Docker grading timed out "
            "for submission %s",
            submission_id,
        )

        with SessionLocal() as db:

            submission = (
                db.query(Submission)
                .filter_by(
                    id=submission_id
                )
                .first()
            )

            if submission:

                submission.status = (
                    "grading_error"
                )

                db.commit()

        return (
            "Automatic grading timed out.",
            500,
        )

    except Exception as e:

        app.logger.exception(
            "Could not start Docker grading "
            "for submission %s",
            submission_id,
        )

        with SessionLocal() as db:

            submission = (
                db.query(Submission)
                .filter_by(
                    id=submission_id
                )
                .first()
            )

            if submission:

                submission.status = (
                    "grading_error"
                )

                db.commit()

        return (
            "Could not start the grading container.",
            500,
        )

    # --------------------------------------------------------
    # Log worker output
    # --------------------------------------------------------

    if result.stdout:

        app.logger.info(
            "Docker grader stdout for submission %s:\n%s",
            submission_id,
            result.stdout[-10000:],
        )

    if result.stderr:

        app.logger.warning(
            "Docker grader stderr for submission %s:\n%s",
            submission_id,
            result.stderr[-10000:],
        )

    # --------------------------------------------------------
    # Docker process failed
    # --------------------------------------------------------

    if result.returncode != 0:

        app.logger.error(
            "Docker grader failed for submission %s "
            "with return code %s",
            submission_id,
            result.returncode,
        )

        with SessionLocal() as db:

            submission = (
                db.query(Submission)
                .filter_by(
                    id=submission_id
                )
                .first()
            )

            if submission:

                submission.status = (
                    "grading_error"
                )

                db.commit()

        return (
            "Automatic grading failed.",
            500,
        )

    # ========================================================
    # Parse grading JSON
    # ========================================================

    marker = "GRADING_RESULT_JSON:"

    result_payload = None

    for line in reversed(
        result.stdout.splitlines()
    ):

        if not line.startswith(marker):
            continue

        json_payload = (
            line[len(marker):].strip()
        )

        try:

            result_payload = json.loads(
                json_payload
            )

        except json.JSONDecodeError:

            app.logger.exception(
                "Invalid grading JSON for submission %s",
                submission_id,
            )

        break

    # --------------------------------------------------------
    # No JSON result
    # --------------------------------------------------------

    if result_payload is None:

        app.logger.error(
            "No valid grading result returned "
            "for submission %s",
            submission_id,
        )

        with SessionLocal() as db:

            submission = (
                db.query(Submission)
                .filter_by(
                    id=submission_id
                )
                .first()
            )

            if submission:

                submission.status = (
                    "grading_error"
                )

                db.commit()

        return (
            "The grading container returned no valid result.",
            500,
        )

    # --------------------------------------------------------
    # Worker reported failure
    # --------------------------------------------------------

    if not result_payload.get(
        "success",
        False,
    ):

        app.logger.error(
            "Grading worker reported failure "
            "for submission %s",
            submission_id,
        )

        with SessionLocal() as db:

            submission = (
                db.query(Submission)
                .filter_by(
                    id=submission_id
                )
                .first()
            )

            if submission:

                submission.status = (
                    "grading_error"
                )

                db.commit()

        return (
            "Automatic grading failed.",
            500,
        )

    # ========================================================
    # Save grading results into application database
    # ========================================================

    grading_results = (
        result_payload.get(
            "results",
            []
        )
    )

    if not grading_results:

        app.logger.error(
            "No notebook results returned "
            "for submission %s",
            submission_id,
        )

        with SessionLocal() as db:

            submission = (
                db.query(Submission)
                .filter_by(
                    id=submission_id
                )
                .first()
            )

            if submission:

                submission.status = (
                    "grading_error"
                )

                db.commit()

        return (
            "Grading produced no notebook results.",
            500,
        )

    try:

        # ----------------------------------------------------
        # Save notebook-level results
        # ----------------------------------------------------

        for grading_result in grading_results:

            save_grading_result(
                grading_result,
                submission_id=submission_id,
            )

        # ----------------------------------------------------
        # Calculate overall automatic grade
        # ----------------------------------------------------

        finalize_lab_grade(
            student_username,
            lab_name,
            submission_id=submission_id,
        )

    except Exception:

        app.logger.exception(
            "Failed to save grading results "
            "for submission %s",
            submission_id,
        )

        with SessionLocal() as db:

            submission = (
                db.query(Submission)
                .filter_by(
                    id=submission_id
                )
                .first()
            )

            if submission:

                submission.status = (
                    "grading_error"
                )

                db.commit()

        return (
            "Grading completed, but the results "
            "could not be saved.",
            500,
        )

    # ========================================================
    # Mark submission as graded
    # ========================================================

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .filter_by(
                id=submission_id
            )
            .first()
        )

        if submission is None:

            return (
                "Submission not found after grading.",
                404,
            )

        submission.status = (
            "graded"
        )

        db.commit()

        app.logger.info(
            "Submission %s successfully graded "
            "inside Docker sandbox.",
            submission_id,
        )

    return ("Graded.", 200)


# ============================================================
# Grade One Submission (queued)
# ============================================================

@app.route(
    "/teacher/submission/<int:submission_id>/grade",
    methods=["POST"],
)
@teacher_required
def grade_entire_submission(submission_id):
    """
    Hand the submission to the grading queue and return immediately.

    The teacher's page polls the submission status, so the click no
    longer holds a worker thread for the fifteen minutes a container
    may take.
    """

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .filter_by(id=submission_id)
            .first()
        )

        if submission is None:
            return ("Submission not found", 404)

        if not submission.snapshot_path:
            flash(
                "This submission has no frozen repository snapshot. "
                "It cannot be graded.",
                "error",
            )
            return redirect(
                url_for(
                    "teacher_submission_page",
                    submission_id=submission_id,
                )
            )

        # Marked before queueing so the page reflects it on the very
        # next load, even if every worker is busy.
        submission.status = "grading"

        db.commit()

    queued = grading_queue.enqueue(
        _grade_submission_blocking,
        submission_id,
    )

    if queued:
        flash(
            "Grading started. This page will update when it finishes.",
            "info",
        )
    else:
        flash(
            "This submission is already being graded.",
            "info",
        )

    return redirect(
        url_for(
            "teacher_submission_page",
            submission_id=submission_id,
        )
    )
# ============================================================
# Student resubmission Requist 
# ============================================================
@app.route(
    "/submission/<int:submission_id>/request-resubmit",
    methods=["POST"],
)
@login_required
def request_resubmission(submission_id):
    with SessionLocal() as db:
        submission = (
            db.query(Submission)
            .filter_by(id=submission_id)
            .first()
        )

        if submission is None:
            return "Submission not found", 404

        if submission.student_id != session["student_id"]:
            return abort(403)

        if not submission.is_current:
            return (
                "Only the current submission can request a resubmission.",
                400,
            )

        # ----------------------------------------------------
        # Maximum attempts reached
        # ----------------------------------------------------

        if submission.attempt_number >= MAX_SUBMISSION_ATTEMPTS:
            submission.resubmission_requested = False
            submission.resubmission_allowed = False
            submission.resubmission_message = (
                f"You have already reached the maximum of "
                f"{MAX_SUBMISSION_ATTEMPTS} attempts for this lab."
            )
            db.commit()

            flash(
                f"You have reached the maximum of "
                f"{MAX_SUBMISSION_ATTEMPTS} attempts for this lab.",
                "warning",
            )

            return redirect(
                url_for(
                    "lab_page",
                    lab_id=submission.lab_id,
                )
            )

        # ----------------------------------------------------
        # Teacher manually blocked resubmission requests
        # ----------------------------------------------------

        if submission.resubmission_blocked:
            submission.resubmission_requested = False
            submission.resubmission_allowed = False
            submission.resubmission_message = (
                "Resubmission requests are currently blocked "
                "by the teacher."
            )
            db.commit()

            flash(
                "Resubmission requests are currently blocked by the teacher.",
                "warning",
            )

            return redirect(
                url_for(
                    "lab_page",
                    lab_id=submission.lab_id,
                )
            )

        # ----------------------------------------------------
        # Maximum rejected requests
        # ----------------------------------------------------

        if (
            submission.resubmission_rejections
            >= MAX_RESUBMISSION_REJECTIONS
        ):
            submission.resubmission_requested = False
            submission.resubmission_allowed = False
            submission.resubmission_message = (
                f"Your resubmission request has been rejected "
                f"{MAX_RESUBMISSION_REJECTIONS} times. "
                "No further resubmission requests are allowed "
                "for this attempt."
            )
            db.commit()

            flash(
                "You have reached the maximum number of "
                "rejected resubmission requests.",
                "warning",
            )

            return redirect(
                url_for(
                    "lab_page",
                    lab_id=submission.lab_id,
                )
            )

        # ----------------------------------------------------
        # Already approved
        # ----------------------------------------------------

        if submission.resubmission_allowed:
            flash(
                "A resubmission has already been approved.",
                "info",
            )

            return redirect(
                url_for(
                    "lab_page",
                    lab_id=submission.lab_id,
                )
            )

        # ----------------------------------------------------
        # Request already pending
        # ----------------------------------------------------

        if submission.resubmission_requested:
            flash(
                "Your resubmission request is already pending.",
                "info",
            )

            return redirect(
                url_for(
                    "lab_page",
                    lab_id=submission.lab_id,
                )
            )

        # ----------------------------------------------------
        # Create request
        # ----------------------------------------------------

        submission.resubmission_requested = True
        submission.resubmission_allowed = False
        submission.resubmission_message = None

        db.commit()

        flash(
            "Your resubmission request has been sent to the teacher.",
            "success",
        )

        return redirect(
            url_for(
                "lab_page",
                lab_id=submission.lab_id,
            )
        )

#block 
@app.route(
    "/teacher/submission/<int:submission_id>/block-resubmit",
    methods=["POST"],
)
@teacher_required
def block_resubmission(submission_id):

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .filter_by(id=submission_id)
            .first()
        )

        if submission is None:
            return "Submission not found", 404

        if not submission.is_current:
            return (
                "Only the current submission can be modified.",
                400,
            )

        submission.resubmission_blocked = True
        submission.resubmission_requested = False
        submission.resubmission_allowed = False
        submission.resubmission_message = (
            "Resubmission requests have been blocked by the teacher."
        )
        create_notification(
            db=db,
            student_id=submission.student_id,
            notification_type="resubmission",
            title="Resubmission Requests Blocked",
            message=(
                "The teacher has blocked resubmission requests "
                "for this submission."
            ),
            lab_id=submission.lab_id,
            submission_id=submission.id,
        )

        db.commit()

        return redirect(
            url_for(
                "lab_page",
                lab_id=submission.lab_id,
            )
        )

#unblock one student 

@app.route(
    "/teacher/submission/<int:submission_id>/unblock-resubmit",
    methods=["POST"],
)
@teacher_required
def unblock_resubmission(submission_id):

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .filter_by(id=submission_id)
            .first()
        )

        if submission is None:
            return "Submission not found", 404

        if not submission.is_current:
            return (
                "Only the current submission can be modified.",
                400,
            )

        submission.resubmission_blocked = False
        submission.resubmission_message = (
            "Resubmission requests have been re-enabled by the teacher."
        )

        db.commit()

        return redirect(
            url_for(
                "lab_page",
                lab_id=submission.lab_id,
            )
        )

#block all 
# ============================================================
# Block Resubmission Requests For All Current Students
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/block-resubmit-all",
    methods=["POST"],
)
@teacher_required
def block_resubmit_all(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        submissions = (
            db.query(Submission)
            .filter_by(
                lab_id=lab.id,
                is_current=True,
            )
            .all()
        )

        blocked_count = 0

        for submission in submissions:

            submission.resubmission_blocked = True
            submission.resubmission_requested = False
            submission.resubmission_allowed = False

            submission.resubmission_message = (
                "Resubmission requests have been blocked "
                "by the teacher."
            )

            create_notification(
                db=db,
                student_id=submission.student_id,
                notification_type="resubmission",
                title="Resubmission Requests Blocked",
                message=submission.resubmission_message,
                lab_id=submission.lab_id,
                submission_id=submission.id,
            )

            blocked_count += 1

        db.commit()

        flash(
            f"{blocked_count} student(s) now have resubmission "
            "requests blocked.",
            "warning",
        )

        return redirect(
            url_for(
                "teacher_lab_submissions",
                lab_id=lab.id,
            )
        )
#unblock alll
# ============================================================
# Unblock Resubmission Requests For All Current Students
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/unblock-resubmit-all",
    methods=["POST"],
)
@teacher_required
def unblock_resubmit_all(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        submissions = (
            db.query(Submission)
            .filter_by(
                lab_id=lab.id,
                is_current=True,
                resubmission_blocked=True,
            )
            .all()
        )

        unblocked_count = 0

        for submission in submissions:

            submission.resubmission_blocked = False

            submission.resubmission_message = (
                "Resubmission requests have been re-enabled "
                "by the teacher."
            )

            create_notification(
                db=db,
                student_id=submission.student_id,
                notification_type="resubmission",
                title="Resubmission Requests Re-enabled",
                message=submission.resubmission_message,
                lab_id=submission.lab_id,
                submission_id=submission.id,
            )

            unblocked_count += 1

        db.commit()

        flash(
            f"{unblocked_count} student(s) can request "
            "resubmission again.",
            "success",
        )

        return redirect(
            url_for(
                "teacher_lab_submissions",
                lab_id=lab.id,
            )
        )
# ============================================================
# Allow Resubmission For One Student
# ============================================================

@app.route(
    "/teacher/submission/<int:submission_id>/allow-resubmit",
    methods=["POST"],
)
@teacher_required
def allow_resubmission(submission_id):

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .filter_by(
                id=submission_id,
            )
            .first()
        )

        if submission is None:
            return (
                "Submission not found",
                404,
            )

        if not submission.is_current:
            return (
                "Only the current submission can be modified.",
                400,
            )

        if submission.attempt_number >= MAX_SUBMISSION_ATTEMPTS:
            return (
                f"This student has already reached the maximum "
                f"of {MAX_SUBMISSION_ATTEMPTS} attempts.",
                400,
            )

        if not submission.resubmission_requested:
            return (
                "There is no pending resubmission request.",
                400,
            )

        # ----------------------------------------------------
        # Approve resubmission
        # ----------------------------------------------------

        submission.resubmission_requested = False
        submission.resubmission_allowed = True

        submission.resubmission_message = (
            "Your resubmission request was approved by the teacher. "
            "You may submit one new attempt."
        )

        # ----------------------------------------------------
        # Create notification
        # ----------------------------------------------------

        create_notification(
            db=db,
            student_id=submission.student_id,
            notification_type="resubmission",
            title="Resubmission Request Approved",
            message=submission.resubmission_message,
            lab_id=submission.lab_id,
            submission_id=submission.id,
        )

        # ----------------------------------------------------
        # Save both changes together
        # ----------------------------------------------------

        db.commit()

        app.logger.info(
            "Approved resubmission request for submission %s",
            submission.id,
        )

        return redirect(
            url_for(
                "lab_page",
                lab_id=submission.lab_id,
            )
        )
    
# ============================================================
# Allow Resubmission For All Eligible Students
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/allow-resubmit-all",
    methods=["POST"],
)
@teacher_required
def allow_resubmit_all(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(
                id=lab_id
            )
            .first()
        )

        if lab is None:
            return (
                "Lab not found",
                404,
            )

        submissions = (
            db.query(Submission)
            .filter_by(
                lab_id=lab.id,
                is_current=True,
                resubmission_requested=True,
            )
            .all()
        )

        allowed_count = 0

        for submission in submissions:

            if (
                submission.attempt_number
                >= MAX_SUBMISSION_ATTEMPTS
            ):
                continue

            submission.resubmission_requested = False
            submission.resubmission_allowed = True

            submission.resubmission_message = (
                "Your resubmission request was approved by the teacher. "
                "You may submit one new attempt."
            )

            # ------------------------------------------------
            # Notification
            # ------------------------------------------------

            create_notification(
                db=db,
                student_id=submission.student_id,
                notification_type="resubmission",
                title="Resubmission Request Approved",
                message=(
                    "Your resubmission request was approved by the teacher. "
                    "You may submit one new attempt."
                ),
                lab_id=submission.lab_id,
                submission_id=submission.id,
            )

            allowed_count += 1

        db.commit()

        app.logger.info(
            "Allowed resubmission for %s students in lab %s",
            allowed_count,
            lab.id,
        )

        flash(
            f"{allowed_count} resubmission request(s) approved.",
            "success",
        )

        return redirect(
            url_for(
                "teacher_lab_submissions",
                lab_id=lab.id,
            )
        )


# ============================================================
# Teacher Reject Resubmission
# ============================================================
@app.route(
    "/teacher/submission/<int:submission_id>/reject-resubmit",
    methods=["POST"],
)
@teacher_required
def reject_resubmission(submission_id):

    print(
        "================ REJECT ROUTE ================="
    )

    print(
        "Received submission_id:",
        submission_id,
    )

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .filter_by(
                id=submission_id,
            )
            .first()
        )

        print(
            "Submission found:",
            submission is not None,
        )

        if submission is None:
            return "Submission not found", 404

        print(
            "is_current:",
            submission.is_current,
        )

        print(
            "resubmission_requested:",
            submission.resubmission_requested,
        )

        if not submission.is_current:
            return (
                "Only the current submission can be modified.",
                400,
            )

        if not submission.resubmission_requested:
            return (
                "There is no pending resubmission request.",
                400,
            )

        submission.resubmission_requested = False
        submission.resubmission_allowed = False
        submission.resubmission_rejections += 1

        if (
            submission.resubmission_rejections
            >= MAX_RESUBMISSION_REJECTIONS
        ):

            submission.resubmission_message = (
                f"Your resubmission request has been rejected "
                f"{MAX_RESUBMISSION_REJECTIONS} times. "
                "No further resubmission requests are allowed "
                "for this attempt."
            )

        else:

            remaining = (
                MAX_RESUBMISSION_REJECTIONS
                - submission.resubmission_rejections
            )

            submission.resubmission_message = (
                "Your resubmission request was rejected "
                "by the teacher. "
                f"You have {remaining} rejection request"
                f"{'s' if remaining != 1 else ''} remaining."
            )

        print(
            "Creating notification for student:",
            submission.student_id,
        )

        notification = create_notification(
            db=db,
            student_id=submission.student_id,
            notification_type="resubmission",
            title="Resubmission Request Rejected",
            message=submission.resubmission_message,
            lab_id=submission.lab_id,
            submission_id=submission.id,
        )

        print(
            "Notification object:",
            notification,
        )

        db.commit()

        print(
            "COMMITTED REJECTION + NOTIFICATION"
        )

        print(
            "================================================"
        )

        return redirect(
            url_for(
                "lab_page",
                lab_id=submission.lab_id,
            )
        )


# ============================================================
# Grade All Submissions For A Lab
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/grade-all",
    methods=["POST"],
)
@teacher_required
def grade_all_submissions(lab_id):
    """
    Queue every current submission for the lab.

    Each one is graded in its own sandboxed container by the grading
    queue. Previously this looped over the submissions calling
    grade_submission() inline: the request was held for the sum of
    every run, and each of those ran the student's notebook in the
    Flask process rather than in the sandbox.
    """

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return (
                "Lab not found",
                404,
            )

        submissions = (
            db.query(Submission)
            .filter_by(
                lab_id=lab.id,
                is_current=True,
            )
            .order_by(
                Submission.submitted_at.asc()
            )
            .all()
        )

        skipped_no_snapshot = 0
        already_running = 0

        # Decided once. Ids are collected before the session closes
        # because the queue runs on other threads and must not share
        # this session.
        to_queue = []

        for submission in submissions:

            if not submission.snapshot_path:
                skipped_no_snapshot += 1
                continue

            if grading_queue.is_active(submission.id):
                already_running += 1
                continue

            submission.status = "grading"
            to_queue.append(submission.id)

        db.commit()

    queued = len(to_queue)

    for submission_id in to_queue:
        grading_queue.enqueue(
            _grade_submission_blocking,
            submission_id,
        )

    app.logger.info(
        "Queued %s submission(s) for grading on lab %s",
        queued,
        lab_id,
    )

    parts = [f"Queued {queued} submission(s) for grading."]

    if already_running:
        parts.append(
            f"{already_running} already grading."
        )

    if skipped_no_snapshot:
        parts.append(
            f"{skipped_no_snapshot} skipped with no snapshot."
        )

    flash(" ".join(parts), "info")

    return redirect(
        url_for(
            "lab_page",
            lab_id=lab_id,
        )
    )


# ============================================================
# Publish All Ready Results For A Lab
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/publish-all",
    methods=["POST"],
)
@teacher_required
def publish_all_ready_results(lab_id):
    published_count = 0
    already_published_count = 0
    not_ready_count = 0
    email_error_count = 0

    with SessionLocal() as db:
        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        submissions = (
            db.query(Submission)
            .options(
                joinedload(Submission.student),
                joinedload(Submission.lab),
                joinedload(Submission.grade),
            )
            .filter_by(
                lab_id=lab.id
            )
            .order_by(
                Submission.submitted_at.asc()
            )
            .all()
        )

        for submission in submissions:
            grade = submission.grade

            if submission.status == "published":
                already_published_count += 1
                continue

            if grade is None:
                not_ready_count += 1
                continue

            if grade.automatic_score is None:
                not_ready_count += 1
                continue

            if grade.teacher_score is None:
                not_ready_count += 1
                continue

            if submission.status != "graded":
                not_ready_count += 1
                continue

            final_score = (
                0.7 * grade.automatic_score
                + 0.3 * grade.teacher_score
            )

            grade.published_at = (
                datetime.now(timezone.utc)
            )

            submission.status = "published"

            db.commit()

            published_count += 1

            if submission.student.email:
                try:
                    send_grade_published_email(
                        student_email=(
                            submission.student.email
                        ),
                        student_username=(
                            submission.student.github_username
                        ),
                        lab_name=(
                            submission.lab.name
                        ),
                        score=final_score,
                        feedback=(
                            grade.feedback or ""
                        ),
                    )

                except Exception as e:
                    email_error_count += 1
                    app.logger.exception(
                        "Failed to send publication email "
                        "for submission %s: %s",
                        submission.id,
                        e,
                    )

    return redirect(
        url_for(
            "lab_page",
            lab_id=lab_id,
            published=published_count,
            already_published=(
                already_published_count
            ),
            not_ready=not_ready_count,
            email_errors=email_error_count,
        )
    )


# ============================================================
# Lab Launch Settings
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/launch",
    methods=["POST"],
)
@teacher_required
def update_lab_launch(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        action = request.form.get(
            "action",
            "",
        )

        if action == "launch":

            lab.launched = True

        elif action == "unlaunch":

            lab.launched = False

        else:

            return (
                "Invalid launch action.",
                400,
            )

        db.commit()

        return redirect(
            url_for(
                "lab_page",
                lab_id=lab.id,
            )
        )

# ============================================================
# Submission Window Settings
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/submission-settings",
    methods=["POST"],
)
@teacher_required
def update_submission_settings(lab_id):
    with SessionLocal() as db:
        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        action = request.form.get(
            "action",
            "",
        )

        deadline_text = request.form.get(
            "submission_deadline",
            "",
        ).strip()

        if deadline_text:
            try:
                naive_deadline = datetime.strptime(
                    deadline_text,
                    "%Y-%m-%dT%H:%M",
                )

                tunisian_time = naive_deadline.replace(
                    tzinfo=ZoneInfo("Africa/Tunis")
                )

                lab.submission_deadline = (
                    tunisian_time.astimezone(
                        timezone.utc
                    )
                )

            except ValueError:
                return (
                    "Invalid deadline format.",
                    400,
                )

        else:
            lab.submission_deadline = None

        now = datetime.now(timezone.utc)

        if action == "open":
            deadline = ensure_utc(
                lab.submission_deadline
            )

            if (
                deadline is not None
                and now >= deadline
            ):
                return (
                    "Cannot open submissions because "
                    "the deadline has already passed.",
                    400,
                )

            lab.submission_open = True

        elif action == "close":
            lab.submission_open = False

        elif action == "save_deadline":
            pass

        else:
            return (
                "Invalid submission settings action.",
                400,
            )

        db.commit()

        return redirect(
            url_for(
                "lab_page",
                lab_id=lab.id,
            )
        )


# ============================================================
# Grade One Notebook
# ============================================================

@app.route(
    "/teacher/submission/<int:submission_id>/grade-notebook",
    methods=["POST"],
)
@teacher_required
def grade_one_notebook(submission_id):
    """
    Re-grade a single notebook of a submission.

    Goes through the same queued Docker path as a full grade. It used
    to call grade_submission() directly, which executes the student's
    notebook in the Flask process itself -- outside the sandbox, with
    the server's access to app.db and .env.
    """

    notebook_filename = request.form.get(
        "notebook",
        "",
    ).strip()

    if not notebook_filename:
        return (
            "Notebook filename is required.",
            400,
        )

    # A filename is all that is wanted here. Anything with a path
    # separator would be resolved inside the container against the
    # mounted snapshot, so refuse it rather than pass it through.
    if Path(notebook_filename).name != notebook_filename:
        return (
            "Invalid notebook filename.",
            400,
        )

    with SessionLocal() as db:

        submission = (
            db.query(Submission)
            .filter_by(id=submission_id)
            .first()
        )

        if submission is None:
            return (
                "Submission not found",
                404,
            )

        if not submission.snapshot_path:
            flash(
                "This submission has no frozen repository snapshot. "
                "It cannot be graded.",
                "error",
            )
            return redirect(
                url_for(
                    "teacher_submission_page",
                    submission_id=submission_id,
                )
            )

        submission.status = "grading"

        db.commit()

    queued = grading_queue.enqueue(
        _grade_submission_blocking,
        submission_id,
        notebook_filename,
    )

    if queued:
        flash(
            f"Grading {notebook_filename}. This page will update "
            "when it finishes.",
            "info",
        )
    else:
        flash(
            "This submission is already being graded.",
            "info",
        )

    return redirect(
        url_for(
            "teacher_submission_page",
            submission_id=submission_id,
        )
    )


# ============================================================
# Teacher Dashboard
# ============================================================

@app.route("/teacher")
@teacher_required
def teacher_dashboard():

    with SessionLocal() as db:

        labs = (
            db.query(Lab)
            .filter(
                Lab.archived.is_(False)
            )
            .order_by(
                Lab.display_order.asc(),
                Lab.id.asc(),
            )
            .all()
        )

        submissions = (
            db.query(Submission)
            .options(
                joinedload(Submission.student),
                joinedload(Submission.lab),
                joinedload(Submission.grade),
            )
            .order_by(
                Submission.submitted_at.desc()
            )
            .all()
        )

        return render_template(
            "teacher_dashboard.html",
            labs=labs,
            submissions=submissions,
        )
# ============================================================
# Archived Labs
# ============================================================

@app.route("/teacher/archived-labs")
@teacher_required
def teacher_archived_labs():

    with SessionLocal() as db:

        archived_labs = (
            db.query(Lab)
            .filter(
                Lab.archived.is_(True)
            )
            .order_by(
                Lab.updated_at.desc(),
                Lab.id.desc(),
            )
            .all()
        )

        return render_template(
            "teacher_archived_labs.html",
            archived_labs=archived_labs,
        )
# ============================================================
# GitHub Login
# ============================================================

@app.route("/login")
def login():
    redirect_uri = url_for(
        "github_callback",
        _external=True,
    )

    # Remember where to land afterwards. Only same-site paths are kept,
    # so this cannot be used to bounce a signed-in student off-site.
    destination = request.args.get("next", "")

    if destination.startswith("/") and not destination.startswith("//"):
        session["login_next"] = destination
    else:
        session.pop("login_next", None)

    # Without real credentials GitHub answers an unknown client_id with
    # its own 404, which looks like the platform is broken. Say what is
    # actually wrong instead.
    client_id = os.environ.get("GITHUB_CLIENT_ID", "")

    if not client_id or client_id == "replace-me":

        app.logger.error(
            "GITHUB_CLIENT_ID is not configured; refusing to start OAuth."
        )

        return (
            "GitHub sign-in is not configured yet.\n\n"
            "1. Go to github.com > Settings > Developer settings >\n"
            "   OAuth Apps > New OAuth App\n"
            "2. Homepage URL:      "
            f"{request.host_url.rstrip('/')}\n"
            "   Authorization callback URL:\n"
            f"                      {redirect_uri}\n"
            "3. Put the Client ID and a generated Client Secret into\n"
            "   Mls-Platform/.env as GITHUB_CLIENT_ID and\n"
            "   GITHUB_CLIENT_SECRET\n"
            "4. Restart the server.\n",
            503,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    app.logger.info("Starting GitHub OAuth, callback=%s", redirect_uri)

    return github.authorize_redirect(
        redirect_uri
    )


# ============================================================
# GitHub OAuth Callback
# ============================================================

@app.route("/auth/github/callback")
def github_callback():
    token = github.authorize_access_token()

    user_response = github.get(
        "user",
        token=token,
    )

    github_user = user_response.json()

    github_id = str(
        github_user["id"]
    )

    github_username = (
        github_user["login"]
    )

    email = github_user.get("email")

    if not email:
        email_response = github.get(
            "user/emails",
            token=token,
        )

        emails = email_response.json()

        for item in emails:
            if (
                item.get("primary")
                and item.get("verified")
            ):
                email = item.get("email")
                break

    with SessionLocal() as db:
        student = (
            db.query(Student)
            .filter_by(
                github_id=github_id
            )
            .first()
        )

        if student is None:
            student = Student(
                github_username=github_username,
                github_id=github_id,
                email=email,
                role="student",
            )

            db.add(student)
            db.commit()
            db.refresh(student)

        else:
            student.github_username = (
                github_username
            )

            if email:
                student.email = email

            db.commit()

        session["student_id"] = student.id
        session["github_username"] = (
            student.github_username
        )
        session["role"] = student.role

    destination = session.pop("login_next", None)

    if destination:
        return redirect(destination)

    return redirect(
        url_for("home")
    )


# ============================================================
# Logout
# ============================================================

@app.route("/logout")
def logout():
    session.clear()

    return redirect(
        url_for("home")
    )

# ============================================================
# Create New Lab
# ============================================================

@app.route(
    "/teacher/labs/new",
    methods=["GET", "POST"],
)
@teacher_required
def create_lab():

    with SessionLocal() as db:

        if request.method == "POST":

            name = request.form.get(
                "name",
                "",
            ).strip()

            description = request.form.get(
                "description",
                "",
            ).strip()

            github_url = request.form.get(
                "github_url",
                "",
            ).strip()

            category = request.form.get(
                "category",
                "",
            ).strip()

            display_order = request.form.get(
                "display_order",
                0,
                type=int,
            )

            solution_url = request.form.get(
                "solution_url",
                "",
            ).strip()

            video_url = request.form.get(
                "video_url",
                "",
            ).strip()

            teacher_notes = request.form.get(
                "teacher_notes",
                "",
            ).strip()

            if not name:

                flash(
                    "Lab name is required.",
                    "warning",
                )

                return redirect(
                    url_for("create_lab")
                )

            if not github_url:

                flash(
                    "GitHub repository URL is required.",
                    "warning",
                )

                return redirect(
                    url_for("create_lab")
                )

            existing_lab = (
                db.query(Lab)
                .filter_by(
                    name=name
                )
                .first()
            )

            if existing_lab:

                flash(
                    "A lab with this name already exists.",
                    "warning",
                )

                return redirect(
                    url_for("create_lab")
                )

            # ------------------------------------------------
            # Generate unique slug
            # ------------------------------------------------

            slug_base = re.sub(
                r"[^a-z0-9]+",
                "-",
                name.lower(),
            ).strip("-")

            if not slug_base:
                slug_base = "lab"

            slug = slug_base
            counter = 2

            while (
                db.query(Lab)
                .filter_by(slug=slug)
                .first()
                is not None
            ):

                slug = (
                    f"{slug_base}-{counter}"
                )

                counter += 1

            # ------------------------------------------------
            # Create lab
            # ------------------------------------------------

            lab = Lab(
                name=name,
                slug=slug,
                description=description or None,
                github_url=github_url,
                display_order=display_order,
                category=category or None,
                archived=False,
                visible=True,
                launched=False,
                submission_open=False,
                submission_deadline=None,
                solution_url=solution_url or None,
                video_url=video_url or None,
                teacher_notes=teacher_notes or None,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )

            db.add(lab)

            db.commit()

            db.refresh(lab)

            flash(
                f'Lab "{lab.name}" created successfully.',
                "success",
            )

            return redirect(
                url_for(
                    "lab_details",
                    lab_id=lab.id,
                )
            )

        return render_template(
            "labs/new.html"
        )

# ============================================================
# Archive Lab
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/archive",
    methods=["POST"],
)
@teacher_required
def archive_lab(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        lab.archived = True

        # An archived lab should not remain publicly accessible.
        lab.visible = False

        # Stop new submissions.
        lab.submission_open = False

        db.commit()

        flash(
            f'Lab "{lab.name}" has been archived.',
            "success",
        )

        return redirect(
            url_for("teacher_dashboard")
        )


# ============================================================
# Restore Lab
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/restore",
    methods=["POST"],
)
@teacher_required
def restore_lab(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        lab.archived = False

        # Restoring does not automatically launch the lab
        # or open submissions.
        lab.visible = True
        lab.launched = False
        lab.submission_open = False

        db.commit()

        flash(
            f'Lab "{lab.name}" has been restored.',
            "success",
        )

        return redirect(
            url_for("teacher_dashboard")
        )


# ============================================================
# Hide Lab
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/hide",
    methods=["POST"],
)
@teacher_required
def hide_lab(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        lab.visible = False

        db.commit()

        flash(
            f'Lab "{lab.name}" is now hidden from students.',
            "success",
        )

        return redirect(
            url_for("teacher_dashboard")
        )


# ============================================================
# Show Lab
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/show",
    methods=["POST"],
)
@teacher_required
def show_lab(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(id=lab_id)
            .first()
        )

        if lab is None:
            return "Lab not found", 404

        if lab.archived:
            flash(
                "Archived labs cannot be shown. Restore the lab first.",
                "warning",
            )

            return redirect(
                url_for("teacher_dashboard")
            )

        lab.visible = True

        db.commit()

        flash(
            f'Lab "{lab.name}" is now visible to students.',
            "success",
        )

        return redirect(
            url_for("teacher_dashboard")
        )
# ============================================================
# Teacher Lab Submissions
# ============================================================

@app.route(
    "/teacher/lab/<int:lab_id>/submissions"
)
@teacher_required
def teacher_lab_submissions(lab_id):

    with SessionLocal() as db:

        lab = (
            db.query(Lab)
            .filter_by(
                id=lab_id
            )
            .first()
        )

        if lab is None:
            return (
                "Lab not found",
                404,
            )

        submissions = (
            db.query(Submission)
            .options(
                joinedload(
                    Submission.student
                ),
                joinedload(
                    Submission.grade
                ),
                joinedload(
                    Submission.lab
                ),
            )
            .filter(
                Submission.lab_id == lab.id,
                Submission.is_current == True,
            )
            .order_by(
                Submission.submitted_at.desc()
            )
            .all()
        )

        return render_template(
            "labs/submissions.html",
            lab=lab,
            submissions=submissions,
        )

# ============================================================
# Student Notifications
# ============================================================
# ============================================================
# Student Notifications
# ============================================================

@app.route("/notifications")
@login_required
def notifications():

    print("================================================")
    print("NOTIFICATION DEBUG")
    print("Session student_id:", session.get("student_id"))
    print("Session username:", session.get("github_username"))
    print("Session role:", session.get("role"))
    print("================================================")

    with SessionLocal() as db:

        student_id = session.get("student_id")

        notification_list = (
            db.query(Notification)
            .filter(
                Notification.student_id == student_id
            )
            .order_by(
                Notification.created_at.desc()
            )
            .all()
        )

        unread_count = (
            db.query(Notification)
            .filter(
                Notification.student_id == student_id,
                Notification.read == False,
            )
            .count()
        )

        print(
            "Notifications found:",
            len(notification_list)
        )

        print(
            "Unread notifications:",
            unread_count
        )

        for notification in notification_list:

            print(
                notification.id,
                notification.student_id,
                notification.title,
                notification.read,
            )

        return render_template(
            "notifications.html",
            notifications=notification_list,
            unread_count=unread_count,
        )

# ============================================================
# Mark Notification As Read
# ============================================================

@app.route(
    "/notifications/<int:notification_id>/read",
    methods=["POST"],
)
@login_required
def mark_notification_read(notification_id):

    with SessionLocal() as db:

        notification = (
            db.query(Notification)
            .filter_by(
                id=notification_id,
                student_id=session["student_id"],
            )
            .first()
        )

        if notification is None:
            return (
                "Notification not found",
                404,
            )

        if not notification.read:

            notification.read = True

            notification.read_at = (
                datetime.utcnow()
            )

            db.commit()

        if notification.submission_id:

            return redirect(
                url_for(
                    "submission_page",
                    submission_id=notification.submission_id,
                )
            )

        if notification.lab_id:

            return redirect(
                url_for(
                    "lab_page",
                    lab_id=notification.lab_id,
                )
            )

        return redirect(
            url_for("notifications")
        )

# ============================================================
# Mark All Notifications As Read
# ============================================================

@app.route(
    "/notifications/read-all",
    methods=["POST"],
)
@login_required
def mark_all_notifications_read():

    with SessionLocal() as db:

        now = datetime.utcnow()

        (
            db.query(Notification)
            .filter_by(
                student_id=session["student_id"],
                read=False,
            )
            .update(
                {
                    Notification.read: True,
                    Notification.read_at: now,
                },
                synchronize_session=False,
            )
        )

        db.commit()

        return redirect(
            url_for("notifications")
        )


# ============================================================
# React client (mls-web)
# ============================================================
#
# Mounted under /app so it sits beside the server-rendered pages
# rather than replacing them. Both share this session cookie and the
# same submission rules, so you can switch between them by URL.

from .api import api as api_blueprint  # noqa: E402

app.register_blueprint(api_blueprint)


SPA_DIST = (
    Path(__file__).resolve().parent.parent.parent
    / "mls-web"
    / "dist"
)


def _spa_build_problem(index):
    """
    Why the built client cannot be served, or None when it is fine.

    A stale build is refused rather than served: an older build may
    still be the standalone prototype running on mock data, and
    handing that out would show invented labs as though they were
    real.
    """

    if not index.exists():
        return "The React client has not been built yet."

    source = SPA_DIST.parent / "src"

    if not source.is_dir():
        return None

    newest = max(
        (path.stat().st_mtime for path in source.rglob("*")
         if path.is_file()),
        default=0,
    )

    if newest > index.stat().st_mtime:
        return (
            "The React client is out of date: its source has changed "
            "since the last build."
        )

    return None


@app.route("/app")
@app.route("/app/")
@app.route("/app/<path:requested>")
def spa(requested=""):
    """
    Serve the built client, letting React Router own everything under
    /app. Real files are served as-is; anything else falls through to
    index.html so a deep link works on a cold load.
    """

    index = SPA_DIST / "index.html"

    problem = _spa_build_problem(index)

    if problem:

        message = "\n".join([
            problem,
            "",
            "Build it with:",
            "",
            "    cd mls-web",
            "    npm install",
            "    npm run build",
            "",
            "The server-rendered pages are unaffected and "
            "remain at /.",
            "",
        ])

        return (
            message,
            503,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    if requested:

        candidate = (SPA_DIST / requested).resolve()

        # Never serve anything outside the build directory.
        if (
            candidate.is_file()
            and SPA_DIST.resolve() in candidate.parents
        ):
            return send_from_directory(SPA_DIST, requested)

    return send_from_directory(SPA_DIST, "index.html")


# ============================================================
# Server
# ============================================================

if __name__ == "__main__":

    host = os.environ.get("MLS_HOST", "0.0.0.0")
    port = int(os.environ.get("MLS_PORT", "5000"))

    # Werkzeug's server is single-process and explicitly not meant to
    # face real traffic. Waitress is a production WSGI server and runs
    # on Windows, where gunicorn does not.
    if os.environ.get("MLS_DEV_SERVER", "").strip().lower() in (
        "1", "true", "yes",
    ):

        app.logger.warning(
            "Starting the Werkzeug development server. "
            "Do not use this to serve students."
        )

        app.run(
            host=host,
            port=port,
            debug=False,
        )

    else:

        from waitress import serve

        app.logger.info(
            "Starting waitress on %s:%s", host, port,
        )

        serve(
            app,
            host=host,
            port=port,

            # Requests are short: the long work is on the grading
            # queue, not in the request.
            threads=int(os.environ.get("MLS_THREADS", "8")),

            # Set when running behind a reverse proxy or ngrok, so
            # ProxyFix above sees the forwarded headers.
            url_scheme=os.environ.get("MLS_URL_SCHEME", "http"),
        )

