"""
api.py

JSON API for the React client in mls-web/, served at /app.

Submission works exactly as the server-rendered pages do: the student
creates their own GitHub repository, pastes the URL, and the platform
clones it into an immutable snapshot for the grader. The platform never
creates or modifies anything in a student's GitHub account -- sign-in
asks only for read:user and user:email, which is how a submission gets
tied to a person and where the address for grade emails comes from.

This is a second presentation of the same application, not a second
application: every write goes through the shared helpers in
submission_rules.py and snapshots.py.
"""

import logging

from datetime import datetime, timezone
from functools import lru_cache, wraps

from flask import Blueprint, jsonify, request, session
from sqlalchemy.orm import joinedload

from config_loader import list_labs as list_lab_ids, load_lab_config

from .database import SessionLocal
from .models import Lab, Student, Submission
from .snapshots import create_repository_snapshot, validate_repo_url
from .submission_rules import (
    NotEligible,
    ensure_utc,
    evaluate_submission_eligibility,
    retire_previous_submission,
)


log = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")


# ============================================================
# Auth
# ============================================================

def login_required_json(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        if "student_id" not in session:
            return jsonify({"error": "not_authenticated"}), 401

        return view(*args, **kwargs)

    return wrapped


def current_student(db):

    return (
        db.query(Student)
        .filter_by(id=session.get("student_id"))
        .first()
    )


# ============================================================
# Lab configuration
# ============================================================

@lru_cache(maxsize=1)
def _configured_labs():
    """
    Lab ids that ship a config.yaml. Cached so a request does not walk
    the labs/ directory on every serialisation.
    """

    return frozenset(list_lab_ids())


@lru_cache(maxsize=None)
def _lab_config(lab_id):
    """
    Parsed labs/<lab_id>/config.yaml, cached per process -- the same
    lifetime the imported LABS dict used to have.
    """

    return load_lab_config(lab_id)


def lab_config_key(lab):
    """
    The lab-config id for a lab, or None when it has no grading
    configuration. app.py passes lab.name to the grader as --lab-id, so
    the name is the key.
    """

    return lab.name if lab is not None and lab.name in _configured_labs() else None


def expected_notebooks(lab):

    key = lab_config_key(lab)

    if key is None:
        return []

    return [
        entry["notebook_filename"]
        for entry in _lab_config(key).get("notebooks", [])
    ]


# ============================================================
# Serialisation
# ============================================================

def _epoch_ms(value):
    """The client works in epoch milliseconds throughout."""

    if value is None:
        return None

    return int(ensure_utc(value).timestamp() * 1000)


def _lab_status(lab):
    """Real launch/window state, mapped to the three states the UI draws."""

    if not lab.launched:
        return "upcoming"

    if not lab.submission_open:
        return "closed"

    deadline = ensure_utc(lab.submission_deadline)

    if deadline is not None and datetime.now(timezone.utc) >= deadline:
        return "closed"

    return "open"


# A student never sees a score before the teacher publishes it.
_SUBMISSION_STATUS = {
    "submitted": "submitted",
    "grading_error": "submitted",
    "graded": "under_review",
    "published": "reviewed",
}


def _lab_code(lab):

    digits = "".join(c for c in lab.name if c.isdigit())

    return f"L{int(digits):02d}" if digits else lab.name[:3].upper()


def _objectives(lab):
    """
    The real graded checks, read back from the lab config -- more honest as
    "objectives" than anything hand-written would be.
    """

    key = lab_config_key(lab)

    if key is None:
        return []

    seen = []

    for nb_cfg in _lab_config(key).get("notebooks", []):

        for check in nb_cfg.get("checks", []):

            pretty = check["name"].replace("_", " ").capitalize()

            if pretty not in seen:
                seen.append(pretty)

    return seen


def _materials(lab):

    key = lab_config_key(lab)

    if key is None:
        return []

    items = [
        {"name": name, "size": "", "kind": "notebook"}
        for name in expected_notebooks(lab)
    ]

    items += [
        {"name": entry["filename"], "size": "", "kind": "dataset"}
        for entry in _lab_config(key).get("required_files", [])
    ]

    return items


def serialise_lab(lab):

    notebooks = expected_notebooks(lab)

    if notebooks:
        blurb = (
            f"{len(notebooks)} notebook"
            f"{'s' if len(notebooks) > 1 else ''} to complete, "
            "then submit your repository."
        )
    else:
        blurb = "Graded manually by your teacher."

    return {
        "id": str(lab.id),
        "code": _lab_code(lab),
        "title": (lab.description or lab.name).strip().capitalize(),
        "blurb": blurb,
        "description": lab.teacher_notes or lab.description or "",
        "status": _lab_status(lab),
        "track": lab.category or "Machine Learning",
        "difficulty": None,
        "estimatedHours": None,
        "deadline": _epoch_ms(lab.submission_deadline),
        "publishedAt": _epoch_ms(lab.created_at) if lab.launched else None,
        "materials": _materials(lab),
        "objectives": _objectives(lab),
        "assignmentUrl": lab.github_url,
        "solutionUrl": lab.solution_url,
        "videoUrl": lab.video_url,
        "expectedNotebooks": notebooks,
    }


def serialise_submission(submission):

    grade = submission.grade

    published = submission.status == "published"

    return {
        "id": str(submission.id),
        "labId": str(submission.lab_id),
        "studentLogin": submission.student.github_username,
        "studentName": submission.student.github_username,
        "fileName": submission.repo_url or "",
        "fileSize": 0,
        "repoUrl": submission.repo_url or "",
        "notes": "",
        "status": _SUBMISSION_STATUS.get(submission.status, "submitted"),
        "rawStatus": submission.status,
        "attempt": submission.attempt_number,
        "isCurrent": submission.is_current,
        "commitSha": submission.commit_sha,
        "submittedAt": _epoch_ms(submission.submitted_at),
        "score": (
            (grade.teacher_score
             if grade.teacher_score is not None
             else grade.automatic_score)
            if published and grade else None
        ),
        "feedback": (grade.feedback or "") if published and grade else "",
    }


# ============================================================
# Session
# ============================================================

@api.get("/me")
def me():

    if "student_id" not in session:
        return jsonify({"user": None})

    with SessionLocal() as db:

        student = current_student(db)

        if student is None:
            session.clear()
            return jsonify({"user": None})

        return jsonify({
            "user": {
                "id": str(student.id),
                "login": student.github_username,
                "name": student.github_username,
                # The client's vocabulary for a teacher is "organizer".
                "role": (
                    "organizer"
                    if student.role == "teacher"
                    else "student"
                ),
                "email": student.email,
            }
        })


# ============================================================
# Labs
# ============================================================

@api.get("/labs")
@login_required_json
def list_labs():

    with SessionLocal() as db:

        query = db.query(Lab).filter_by(archived=False)

        if session.get("role") != "teacher":
            query = query.filter_by(visible=True)

        labs = query.order_by(Lab.display_order, Lab.id).all()

        return jsonify({"labs": [serialise_lab(lab) for lab in labs]})


@api.get("/labs/<int:lab_id>")
@login_required_json
def get_lab(lab_id):

    with SessionLocal() as db:

        lab = db.query(Lab).filter_by(id=lab_id).first()

        if lab is None:
            return jsonify({"error": "lab_not_found"}), 404

        student = current_student(db)

        submissions = (
            db.query(Submission)
            .options(
                joinedload(Submission.student),
                joinedload(Submission.grade),
            )
            .filter_by(student_id=student.id, lab_id=lab.id)
            .order_by(Submission.attempt_number.desc())
            .all()
        )

        payload = {
            "lab": serialise_lab(lab),
            "submissions": [serialise_submission(s) for s in submissions],
        }

        # Why the student can or cannot submit right now, so the client
        # never has to reimplement the rules to decide what to show.
        try:
            _, attempt_number = evaluate_submission_eligibility(
                db, student, lab
            )

            payload["canSubmit"] = True
            payload["nextAttempt"] = attempt_number
            payload["blockedReason"] = None

        except NotEligible as blocked:

            payload["canSubmit"] = False
            payload["nextAttempt"] = None
            payload["blockedReason"] = {
                "code": blocked.code,
                "message": blocked.message,
            }

        return jsonify(payload)


# ============================================================
# Submissions
# ============================================================

@api.get("/submissions")
@login_required_json
def my_submissions():

    with SessionLocal() as db:

        student = current_student(db)

        submissions = (
            db.query(Submission)
            .options(
                joinedload(Submission.student),
                joinedload(Submission.grade),
            )
            .filter_by(student_id=student.id)
            .order_by(Submission.submitted_at.desc())
            .all()
        )

        return jsonify({
            "submissions": [serialise_submission(s) for s in submissions]
        })


@api.post("/labs/<int:lab_id>/submit")
@login_required_json
def submit(lab_id):
    """
    Submit a GitHub repository the student created themselves.

    The repository is cloned into an immutable snapshot at this moment;
    whatever they push afterwards does not change what gets graded.
    """

    payload = request.get_json(silent=True) or {}

    repo_url = (payload.get("repoUrl") or "").strip()

    if not repo_url:
        return jsonify({
            "error": "missing_repo_url",
            "message": "Enter the URL of your GitHub repository.",
        }), 400

    if not validate_repo_url(repo_url):
        return jsonify({
            "error": "invalid_repo_url",
            "message": (
                "That is not a valid GitHub repository URL. It should "
                "look like https://github.com/your-name/your-repo"
            ),
        }), 400

    with SessionLocal() as db:

        lab = db.query(Lab).filter_by(id=lab_id).first()

        if lab is None:
            return jsonify({"error": "lab_not_found"}), 404

        student = current_student(db)

        try:
            current_submission, attempt_number = (
                evaluate_submission_eligibility(db, student, lab)
            )

        except NotEligible as blocked:
            return jsonify({
                "error": blocked.code,
                "message": blocked.message,
            }), blocked.status

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

        db.add(submission)

        db.flush()

        # ----------------------------------------------------
        # Freeze the repository
        # ----------------------------------------------------

        try:
            snapshot_path, commit_sha = create_repository_snapshot(
                repo_url=repo_url,
                submission_id=submission.id,
            )

        except Exception:

            db.rollback()

            log.exception(
                "Could not snapshot %s for student %s lab %s",
                repo_url,
                student.id,
                lab.id,
            )

            return jsonify({
                "error": "snapshot_failed",
                "message": (
                    "Could not read that repository. Check that it "
                    "exists and is public, then try again -- no attempt "
                    "was used."
                ),
            }), 502

        submission.snapshot_path = snapshot_path
        submission.commit_sha = commit_sha

        retire_previous_submission(current_submission)

        db.commit()
        db.refresh(submission)

        log.info(
            "Submission %s created via API. Student=%s Lab=%s Attempt=%s "
            "Commit=%s",
            submission.id,
            student.github_username,
            lab.name,
            submission.attempt_number,
            submission.commit_sha,
        )

        return jsonify({
            "submission": serialise_submission(submission)
        }), 201
