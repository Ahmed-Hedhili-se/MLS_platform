"""
grading_mode.py

Where grading actually runs.

    auto    -- the server grades. Every submission is cloned into a
               snapshot directory and handed to the background queue,
               which starts a locked-down Docker container per job.
               This needs a host with the Docker socket and disk for
               the snapshots.

    manual  -- the server only collects. A submission records the repo
               URL and the commit SHA it pointed at, and stops there.
               The teacher exports the pending submissions, grades them
               on a machine that does have Docker, and uploads the
               results back.

Manual mode exists because a host with a Docker socket is not always
available. It costs students their immediate feedback, so prefer auto
wherever the machine allows it.

Set MLS_GRADING_MODE in .env. Anything other than "manual" is auto, so
an unset or misspelled value fails towards the safer behaviour.
"""

import os


AUTO = "auto"
MANUAL = "manual"


# Status a submission carries while it waits for the teacher's offline
# grading run. Distinct from "grading", which means a container is
# actually running and which recover_interrupted_jobs() resets on
# startup -- these submissions must survive a restart untouched.
AWAITING_STATUS = "awaiting_manual_grading"


def current_mode():

    configured = (
        os.environ.get("MLS_GRADING_MODE") or ""
    ).strip().lower()

    return MANUAL if configured == MANUAL else AUTO


def is_manual():

    return current_mode() == MANUAL


def is_auto():

    return current_mode() == AUTO
