"""
snapshots.py

Building the immutable directory the grader reads.

The grader takes a path, joins the expected notebook filename onto it and
executes. Git is not the source of truth for grading -- it is one of two
ways of filling that directory:

    create_repository_snapshot()   clone a student's GitHub repo
    create_upload_snapshot()       copy notebooks the student uploaded

Both produce data/submissions/<id>/repo/ with identical structure, so
everything downstream is unaware of which path was taken.
"""

import logging
import os
import re
import shutil
import stat
import subprocess
import sys

from pathlib import Path
from urllib.parse import urlparse


log = logging.getLogger(__name__)


# ============================================================
# Deleting a git working copy
# ============================================================

def _clear_readonly(func, path, exc):
    """
    Git stores objects read-only. On Windows that makes os.unlink fail
    with PermissionError, so clear the bit and retry.
    """

    try:
        os.chmod(path, stat.S_IWRITE)

    except OSError:
        raise exc

    func(path)


def remove_tree(path, ignore_errors=False):
    """
    shutil.rmtree that can actually delete a repository on Windows.
    """

    path = Path(path)

    if not path.exists():
        return

    try:

        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_clear_readonly)

        else:  # pragma: no cover - the grader image is 3.12
            shutil.rmtree(path, onerror=_clear_readonly)

    except Exception:

        if not ignore_errors:
            raise

        log.warning("Could not fully remove %s", path)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _submissions_root():
    """
    Where student snapshots are written.

    SUBMISSION_STORAGE_ROOT lets a deployment put them on a larger or
    separately backed-up volume. A relative value is resolved against
    the project root so it does not depend on the working directory.
    """

    configured = (
        os.environ.get("SUBMISSION_STORAGE_ROOT") or ""
    ).strip()

    if not configured:
        return PROJECT_ROOT / "data" / "submissions"

    root = Path(configured)

    return root if root.is_absolute() else PROJECT_ROOT / root


SUBMISSIONS_ROOT = _submissions_root()


# Git operations must never wait on a human or a hung network.
CLONE_TIMEOUT_SECONDS = 120

GIT_TIMEOUT_SECONDS = 30


# ============================================================
# Restricted git environment
# ============================================================

def git_environment():
    """
    An environment in which git cannot prompt, cannot be redirected by
    user configuration, and cannot speak anything but HTTPS.
    """

    env = os.environ.copy()

    # Never allow Git to prompt for username/password.
    env["GIT_TERMINAL_PROMPT"] = "0"

    # Only allow HTTPS for automated clone/fetch operations.
    env["GIT_ALLOW_PROTOCOL"] = "https"

    # Do not let protocols configured as "user" be activated by
    # automated Git operations.
    env["GIT_PROTOCOL_FROM_USER"] = "0"

    # Ignore system Git configuration for this subprocess.
    env["GIT_CONFIG_NOSYSTEM"] = "1"

    # Prevent a user's global Git configuration from changing URL
    # rewriting, credential helpers, etc.
    env["GIT_CONFIG_GLOBAL"] = os.devnull

    return env


def git_security_config():

    # os.devnull is "NUL" on Windows and "/dev/null" on POSIX. Hard-coding
    # either one silently disables the protection on the other platform.
    return [
        "-c", "protocol.ext.allow=never",
        "-c", "protocol.file.allow=never",
        "-c", "protocol.ssh.allow=never",
        "-c", "protocol.git.allow=never",
        "-c", f"core.hooksPath={os.devnull}",
    ]


def run_git(arguments, timeout=GIT_TIMEOUT_SECONDS, cwd=None):

    return subprocess.run(
        ["git", *git_security_config(), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=git_environment(),
        cwd=str(cwd) if cwd else None,
    )


# ============================================================
# Paths
# ============================================================

def snapshot_root_for(submission_id):

    return SUBMISSIONS_ROOT / str(submission_id)


def repo_path_for(submission_id):

    return snapshot_root_for(submission_id) / "repo"


def prepare_snapshot_directory(submission_id):
    """
    Return an empty repo directory for this submission, discarding any
    remains of a previous attempt at the same id.
    """

    root = snapshot_root_for(submission_id)

    remove_tree(root)

    repo = root / "repo"

    repo.mkdir(parents=True, exist_ok=True)

    return repo


def drafts_root_for(student_id, lab_id):

    return (
        PROJECT_ROOT
        / "data"
        / "drafts"
        / str(student_id)
        / str(lab_id)
    )


# ============================================================
# Commit SHA
# ============================================================

def read_commit_sha(repo_path):

    result = run_git(["-C", str(repo_path), "rev-parse", "HEAD"])

    commit_sha = result.stdout.strip()

    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit_sha):
        raise RuntimeError("Git returned an invalid commit SHA.")

    return commit_sha


# ============================================================
# Path A -- clone a student repository
# ============================================================

def create_repository_snapshot(repo_url, submission_id):
    """
    Clone a student's GitHub repository into an isolated submission
    directory and return (snapshot_path, commit_sha).

    Security properties:
    - No shell execution
    - HTTPS-only Git protocol
    - No interactive credential prompts
    - No submodule recursion
    - Git hooks disabled for the cloned repository
    - Shallow clone
    - Hard timeout on Git operations
    - Per-submission filesystem isolation
    """

    root = snapshot_root_for(submission_id)

    repo_path = prepare_snapshot_directory(submission_id)

    # git clone wants to create the directory itself.
    repo_path.rmdir()

    try:

        run_git(
            [
                "clone",
                "--depth", "1",
                "--no-tags",
                "--no-recurse-submodules",
                repo_url,
                str(repo_path),
            ],
            timeout=CLONE_TIMEOUT_SECONDS,
        )

        if not repo_path.exists():
            raise RuntimeError(
                "Git clone completed but "
                "the repository directory was not created."
            )

        return str(repo_path), read_commit_sha(repo_path)

    except subprocess.TimeoutExpired as exc:

        log.warning(
            "Git operation timed out for submission %s",
            submission_id,
        )

        remove_tree(root, ignore_errors=True)

        raise RuntimeError("Repository snapshot timed out.") from exc

    except subprocess.CalledProcessError as exc:

        # Don't expose the complete Git command or potentially sensitive
        # subprocess information to students.
        log.warning(
            "Git operation failed for submission %s: %s",
            submission_id,
            (exc.stderr or "").strip()[-2000:],
        )

        remove_tree(root, ignore_errors=True)

        raise RuntimeError("Could not create repository snapshot.") from exc

    except Exception:

        remove_tree(root, ignore_errors=True)

        raise


# ============================================================
# Path B -- uploaded notebooks
# ============================================================

def create_upload_snapshot(submission_id, staged_files, author=None):
    """
    Build a snapshot from files the student uploaded, then commit it.

    `staged_files` maps canonical filename -> path on disk. Filenames come
    from the lab config, never from the browser, so no traversal is possible
    here by construction.

    Returns (snapshot_path, commit_sha).

    Unlike the clone path this touches no network, so it cannot fail
    because GitHub is having a bad day.
    """

    if not staged_files:
        raise ValueError("A submission needs at least one file.")

    root = snapshot_root_for(submission_id)

    repo_path = prepare_snapshot_directory(submission_id)

    try:

        for filename, source_path in staged_files.items():

            # Defensive: a canonical filename is a bare name by
            # definition, so anything else means a caller bug.
            if Path(filename).name != filename:
                raise ValueError(f"unsafe filename: {filename!r}")

            shutil.copy2(source_path, repo_path / filename)

        commit_sha = commit_snapshot(
            repo_path,
            submission_id=submission_id,
            author=author,
        )

        return str(repo_path), commit_sha

    except Exception:

        remove_tree(root, ignore_errors=True)

        raise


def commit_snapshot(repo_path, submission_id, author=None):
    """
    Turn a plain directory into a one-commit repository.

    This is the whole archive when GitHub is switched off, and the thing
    the archive worker pushes when it is on. It needs no network, no
    credentials and no organisation to exist.
    """

    identity = [
        "-c", "user.name=MLS Platform",
        "-c", "user.email=platform@mls.local",
    ]

    subject = f"Submission {submission_id}"

    if author:
        subject += f" by {author}"

    subprocess.run(
        [
            "git",
            *git_security_config(),
            *identity,
            "-C", str(repo_path),
            "init",
            "--quiet",
            "--initial-branch=main",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env=git_environment(),
    )

    run_git(["-C", str(repo_path), "add", "--all"])

    subprocess.run(
        [
            "git",
            *git_security_config(),
            *identity,
            "-C", str(repo_path),
            "commit",
            "--quiet",
            "--no-verify",
            "-m", subject,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env=git_environment(),
    )

    return read_commit_sha(repo_path)


# ============================================================
# Repository URL validation
# ============================================================

ALLOWED_GITHUB_HOSTS = {
    "github.com",
    "www.github.com",
}


def validate_repo_url(repo_url):
    """
    Accept only a plain https://github.com/<owner>/<repo> URL.

    Lives here rather than in app.py so the JSON API can reuse it
    without importing the Flask module and creating a cycle.
    """

    repo_url = (repo_url or "").strip()

    parsed = urlparse(repo_url)

    if parsed.scheme != "https":
        return False

    if parsed.hostname not in ALLOWED_GITHUB_HOSTS:
        return False

    # Credentials embedded in the URL would be sent to git.
    if parsed.username or parsed.password:
        return False

    if parsed.port is not None:
        return False

    path = parsed.path.strip("/")

    parts = path.split("/")

    if len(parts) != 2:
        return False

    owner, repo = parts

    if not owner or not repo:
        return False

    if ".." in path:
        return False

    return True
