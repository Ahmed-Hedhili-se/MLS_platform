"""
Background grading queue.

Grading a submission means starting a Docker container and waiting up
to fifteen minutes for it. Doing that inside the request meant the
worker thread handling the click was blocked for the whole run -- at a
deadline, when submissions arrive together, that is exactly when the
platform stops responding for everyone else.

Jobs are handed to a small pool of daemon threads instead. The request
returns as soon as the job is queued, and the submission's status in
the database ("grading" -> "graded" / "grading_error") is what the
teacher's page reads to show progress.

Deliberately in-process and not durable: a restart mid-run loses the
queue, and any submission left in "grading" is reset on the next start
by recover_interrupted_jobs() so it can simply be graded again.
"""

import logging
import os
import queue
import threading

from . import grading_mode


log = logging.getLogger(__name__)


# Concurrency is capped because each job is a container holding 2 CPUs
# and 2 GB. Two at a time keeps the host usable; raise it only if the
# machine actually has the headroom.
def _worker_count():
    try:
        configured = int(os.environ.get("GRADING_WORKERS", "2"))
    except ValueError:
        return 2

    return max(1, min(configured, 8))


WORKER_COUNT = _worker_count()

_jobs = queue.Queue()

# submission ids currently queued or running, so a double-click does
# not start the same container twice.
_active = set()
_active_lock = threading.Lock()

_started = False
_start_lock = threading.Lock()


def is_active(submission_id):
    with _active_lock:
        return submission_id in _active


def active_count():
    with _active_lock:
        return len(_active)


def _run(handler, submission_id, args):

    try:
        message, status = handler(submission_id, *args)

        if status >= 400:
            log.error(
                "Grading job for submission %s finished with %s: %s",
                submission_id, status, message,
            )
        else:
            log.info(
                "Grading job for submission %s finished: %s",
                submission_id, message,
            )

    except Exception:
        # A crash here must never kill the worker thread -- it would
        # silently drain the pool and every later job would hang.
        log.exception(
            "Grading job for submission %s raised", submission_id,
        )

    finally:
        with _active_lock:
            _active.discard(submission_id)


def _worker_loop():

    while True:
        handler, submission_id, args = _jobs.get()

        try:
            _run(handler, submission_id, args)
        finally:
            _jobs.task_done()


def _mark_awaiting_manual_grading(submission_id):
    """
    Park a submission until the teacher's offline grading run.

    Returning True from enqueue() without this would leave the
    submission reading "submitted" forever, with nothing on the
    teacher's page to show it still needs grading.
    """

    from .database import SessionLocal
    from .models import Submission

    try:
        with SessionLocal() as db:

            submission = db.get(Submission, submission_id)

            if submission is None:
                log.warning(
                    "Cannot mark submission %s as awaiting manual "
                    "grading: not found",
                    submission_id,
                )
                return False

            submission.status = grading_mode.AWAITING_STATUS
            db.commit()

            return True

    except Exception:
        log.exception(
            "Could not mark submission %s as awaiting manual grading",
            submission_id,
        )
        return False


def start(app=None):
    """Start the worker threads once per process."""

    global _started

    if grading_mode.is_manual():

        if app is not None:
            app.logger.info(
                "Grading queue not started: MLS_GRADING_MODE=manual",
            )

        return

    with _start_lock:

        if _started:
            return

        for index in range(WORKER_COUNT):
            threading.Thread(
                target=_worker_loop,
                name=f"grading-worker-{index}",
                daemon=True,
            ).start()

        _started = True

    if app is not None:
        app.logger.info(
            "Grading queue started with %s worker(s)", WORKER_COUNT,
        )


def enqueue(handler, submission_id, *args):
    """
    Queue a submission for grading.

    Extra positional arguments are passed through to the handler
    after the submission id.

    Returns False when that submission is already queued or running,
    so callers can tell the user rather than starting a second
    container for the same work. Deduplication is per submission on
    purpose: grading one notebook while the whole submission is being
    graded would have both runs writing the same rows.
    """

    if grading_mode.is_manual():
        # No container is going to run here. Park the submission for
        # the teacher's offline pass and report success -- from the
        # caller's point of view the work has been accepted, it just
        # happens later and elsewhere.
        return _mark_awaiting_manual_grading(submission_id)

    with _active_lock:

        if submission_id in _active:
            return False

        _active.add(submission_id)

    start()

    _jobs.put((handler, submission_id, args))

    return True


def recover_interrupted_jobs():
    """
    Reset submissions left mid-grade by a restart.

    Without this a submission whose container was killed by a deploy
    stays "grading" forever and the teacher has no way to retry it.
    """

    from .database import SessionLocal
    from .models import Submission

    try:
        with SessionLocal() as db:

            stranded = (
                db.query(Submission)
                .filter(Submission.status == "grading")
                .all()
            )

            for submission in stranded:
                submission.status = "grading_error"

            if stranded:
                db.commit()

            return len(stranded)

    except Exception:
        log.exception("Could not reset interrupted grading jobs")
        return 0
