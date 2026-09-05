"""
offline_grading.py

Moving grading off the server and back again.

In manual mode the server never executes a notebook. The round trip is:

    1. export_pending()   -> a work file the teacher downloads
    2. grade_offline.py   -> run on a machine with Docker; produces a
                             results file
    3. import_results()   -> the teacher uploads that file and grades
                             land in the database

The work file carries the submission id and the commit SHA recorded at
submission time, so the teacher grades exactly what was submitted even
if the student has pushed since. Everything in it is already visible to
the teacher in the web UI -- it is a convenience format, not a secret.

Results are applied through the same grading_db functions the container
path uses, so a manually graded submission is indistinguishable from an
automatically graded one once it lands.
"""

import logging

from datetime import datetime, timezone

from .database import SessionLocal
from .grading_db import finalize_lab_grade, save_grading_result
from .models import Lab, Student, Submission


log = logging.getLogger(__name__)


# Bumped only if the file format changes incompatibly. import_results()
# refuses anything it does not recognise rather than half-applying it.
FORMAT_VERSION = 1


# Statuses that mean "this submission still needs a grading run".
# grading_error is included so a failed automatic run can be picked up
# by an offline pass rather than being stuck.
PENDING_STATUSES = (
    "awaiting_manual_grading",
    "submitted",
    "grading_error",
)


# ============================================================
# Export
# ============================================================

def export_pending(lab_id=None, include_graded=False):
    """
    Build the work file for an offline grading run.

    lab_id
        Restrict to one lab. None exports every lab.

    include_graded
        Re-export submissions that already have a grade, for a re-run
        after the hidden tests change.
    """

    with SessionLocal() as db:

        query = (
            db.query(Submission)
            .filter(Submission.is_current.is_(True))
        )

        if lab_id is not None:
            query = query.filter(Submission.lab_id == lab_id)

        if not include_graded:
            query = query.filter(
                Submission.status.in_(PENDING_STATUSES)
            )

        submissions = query.order_by(Submission.id).all()

        items = []

        for submission in submissions:

            student = db.get(Student, submission.student_id)
            lab = db.get(Lab, submission.lab_id)

            if student is None or lab is None:
                log.warning(
                    "Skipping submission %s: missing student or lab",
                    submission.id,
                )
                continue

            items.append(
                {
                    "submission_id": submission.id,
                    "student": student.github_username,
                    "lab": lab.name,
                    "lab_slug": lab.slug,
                    "repo_url": submission.repo_url,
                    "commit_sha": submission.commit_sha,
                    "attempt": submission.attempt_number,
                    "status": submission.status,
                }
            )

        return {
            "format_version": FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "lab_id": lab_id,
            "count": len(items),
            "submissions": items,
        }


# ============================================================
# Import
# ============================================================

def import_results(payload):
    """
    Apply an offline grading run to the database.

    Returns (applied, errors) where errors is a list of human-readable
    strings. One bad row does not abort the rest -- a run of 205
    submissions should not be lost because one repository was deleted.
    """

    if not isinstance(payload, dict):
        raise ValueError("Results file must be a JSON object.")

    version = payload.get("format_version")

    if version != FORMAT_VERSION:
        raise ValueError(
            f"Unsupported results format version {version!r}; "
            f"expected {FORMAT_VERSION}."
        )

    results = payload.get("results")

    if not isinstance(results, list):
        raise ValueError("Results file has no 'results' list.")

    applied = 0
    errors = []

    # Grades are per notebook; the overall submission grade is only
    # correct once every notebook for that submission is stored, so
    # finalize is deferred until the whole file has been applied.
    to_finalize = {}

    for entry in results:

        submission_id = entry.get("submission_id")

        try:
            for notebook_result in entry.get("notebooks", []):

                record = {
                    "student": entry["student"],
                    "lab": entry["lab"],
                    "notebook": notebook_result["notebook"],
                    "score": notebook_result.get("score", 0.0),
                    "error": notebook_result.get("error"),
                    "checks": notebook_result.get("checks", []),
                }

                save_grading_result(
                    record,
                    submission_id=submission_id,
                )

                applied += 1

            to_finalize[submission_id] = (
                entry["student"],
                entry["lab"],
            )

        except Exception as exc:
            log.exception(
                "Could not apply offline result for submission %s",
                submission_id,
            )
            errors.append(
                f"submission {submission_id}: {exc}"
            )

    # --------------------------------------------------------
    # Overall grade and final status
    # --------------------------------------------------------

    for submission_id, (student_name, lab_name) in to_finalize.items():

        try:
            finalize_lab_grade(
                student_name,
                lab_name,
                submission_id=submission_id,
            )

            _set_status(submission_id)

        except Exception as exc:
            log.exception(
                "Could not finalize submission %s", submission_id,
            )
            errors.append(
                f"submission {submission_id} (finalize): {exc}"
            )

    return applied, errors


def _set_status(submission_id):
    """
    Move a submission out of the awaiting state once its results are in.

    A submission whose notebooks all failed to execute is marked
    grading_error, matching what the container path records, so the
    teacher's existing filters still find it.
    """

    with SessionLocal() as db:

        submission = db.get(Submission, submission_id)

        if submission is None:
            return

        grade = submission.grade

        failed = (
            grade is not None
            and grade.notebooks
            and all(
                notebook.error for notebook in grade.notebooks
            )
        )

        submission.status = "grading_error" if failed else "graded"

        db.commit()
