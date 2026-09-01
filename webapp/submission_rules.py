"""
submission_rules.py

The rules that decide whether a student may create a new attempt.

Kept free of Flask response objects so the same function backs the HTML
pages and the JSON API. Callers translate NotEligible into whatever their
transport needs -- a redirect and a flash, or a status code and a body.

There must only ever be one copy of these rules. A deadline enforced on
one route and not another is a hole, and that is exactly the kind of gap
that opens when the logic is pasted twice.
"""

from datetime import datetime, timezone


MAX_SUBMISSION_ATTEMPTS = 3

MAX_RESUBMISSION_REJECTIONS = 3


class NotEligible(Exception):
    """
    Why this student may not submit.

    `code` is stable and machine-readable; `message` is shown to the
    student as written.
    """

    def __init__(self, code, message, status=403):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def ensure_utc(value):

    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def evaluate_submission_eligibility(db, student, lab):
    """
    Apply launch state, the attempt ceiling, teacher-approved
    resubmission, the manual open/closed switch and the deadline.

    Returns (current_submission, attempt_number).
    Raises NotEligible otherwise.
    """

    # Imported here so this module stays importable from anywhere.
    from .models import Submission

    if not lab.launched:
        raise NotEligible(
            "not_launched",
            "This laboratory has not been launched yet.",
        )

    current_submission = (
        db.query(Submission)
        .filter_by(
            student_id=student.id,
            lab_id=lab.id,
            is_current=True,
        )
        .order_by(
            Submission.attempt_number.desc()
        )
        .first()
    )

    # --------------------------------------------------------
    # First submission
    # --------------------------------------------------------

    if current_submission is None:

        is_resubmission = False
        attempt_number = 1

    # --------------------------------------------------------
    # Existing current submission
    # --------------------------------------------------------

    else:

        is_resubmission = current_submission.resubmission_allowed

        if (
            current_submission.attempt_number
            >= MAX_SUBMISSION_ATTEMPTS
        ):
            raise NotEligible(
                "max_attempts",
                f"You have reached the maximum of "
                f"{MAX_SUBMISSION_ATTEMPTS} attempts for this lab.",
            )

        if not is_resubmission:
            raise NotEligible(
                "needs_approval",
                "You have already submitted this lab. "
                "Please request permission from the teacher "
                "to resubmit.",
            )

        attempt_number = current_submission.attempt_number + 1

    # --------------------------------------------------------
    # Normal submission window
    # --------------------------------------------------------
    #
    # Teacher-approved resubmissions are allowed even when the normal
    # submission window is closed.

    if not is_resubmission:

        if not lab.submission_open:
            raise NotEligible(
                "closed",
                "Submissions are currently closed for this lab.",
            )

        deadline = ensure_utc(lab.submission_deadline)

        if (
            deadline is not None
            and datetime.now(timezone.utc) >= deadline
        ):
            raise NotEligible(
                "deadline_passed",
                "The submission deadline for this lab has passed.",
            )

    return current_submission, attempt_number


def retire_previous_submission(current_submission):
    """
    A new attempt supersedes the old one, which keeps its grade but stops
    being the submission under consideration.
    """

    if current_submission is None:
        return

    current_submission.is_current = False
    current_submission.resubmission_allowed = False
    current_submission.resubmission_requested = False
    current_submission.resubmission_message = None
