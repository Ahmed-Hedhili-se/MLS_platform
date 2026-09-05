"""
grade_offline.py -- grade a batch of submissions away from the server.

Manual grading mode splits the platform in two: the server collects
submissions, this script grades them somewhere that has Docker, and the
results are uploaded back.

    1. Teacher's dashboard -> Export pending  -> pending.json
    2. This script                            -> results.json
    3. Teacher's dashboard -> Import results

Run it inside the grader image, not on your own Python. Student
notebooks are arbitrary code and this executes them:

    docker run --rm \
        --cap-drop ALL \
        --security-opt no-new-privileges \
        --memory 2g --cpus 2 --pids-limit 128 \
        -v "$PWD:/work" \
        --entrypoint python \
        mls-grader:1.0 /grader/grade_offline.py \
            --input /work/pending.json \
            --output /work/results.json

Each submission is cloned fresh and checked out at the commit SHA the
server recorded, so a student pushing after the deadline changes
nothing about what is graded.

Usage:
    python grade_offline.py --input pending.json --output results.json
    python grade_offline.py --input pending.json --output out.json --lab lab1
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

from datetime import datetime, timezone
from pathlib import Path

from grade import grade_student_lab


# Must match webapp/offline_grading.py FORMAT_VERSION.
FORMAT_VERSION = 1

CLONE_TIMEOUT_SECONDS = 300


# ============================================================
# Fetching
# ============================================================

def authenticated_url(repo_url):
    """
    Add a token to the clone URL when GH_TOKEN is set.

    Private student repositories cannot be cloned anonymously. Grading
    offline is the right place to hold that credential: it lives on the
    teacher's machine for the length of one grading run, instead of
    sitting in the environment of a public web server.

    Without GH_TOKEN the URL is returned unchanged, so public
    repositories keep working with no configuration.
    """

    token = (os.environ.get("GH_TOKEN") or "").strip()

    if not token:
        return repo_url

    if not repo_url.startswith("https://"):
        return repo_url

    return repo_url.replace(
        "https://",
        f"https://x-access-token:{token}@",
        1,
    )


def redact(text, repo_url):
    """
    Keep the token out of anything a student might later read.

    Git echoes the remote URL in its error messages, so an
    authentication failure would otherwise write the credential
    straight into results.json and from there into the database.
    """

    token = (os.environ.get("GH_TOKEN") or "").strip()

    if token and token in text:
        text = text.replace(token, "***")

    return text


def fetch_at_commit(repo_url, commit_sha, destination):
    """
    Clone a repository and check out one specific commit.

    A shallow clone of the default branch is not enough: the recorded
    commit may be several pushes back by the time grading runs. Fetch
    the exact object instead, and fall back to a normal clone only when
    no SHA was recorded.
    """

    repo_url = authenticated_url(repo_url)

    destination = Path(destination)

    if commit_sha:

        subprocess.run(
            ["git", "init", "--quiet", str(destination)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

        subprocess.run(
            ["git", "remote", "add", "origin", repo_url],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(destination),
        )

        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", commit_sha],
            check=True,
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_SECONDS,
            cwd=str(destination),
        )

        subprocess.run(
            ["git", "checkout", "--quiet", "FETCH_HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(destination),
        )

        return commit_sha

    subprocess.run(
        [
            "git", "clone",
            "--depth", "1",
            "--no-tags",
            "--no-recurse-submodules",
            repo_url,
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT_SECONDS,
    )

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(destination),
    )

    return head.stdout.strip()


# ============================================================
# Grading one submission
# ============================================================

def grade_one(item):
    """
    Grade a single work-file entry.

    Never raises: a repository that has been deleted or made private
    must not stop the other two hundred submissions.
    """

    entry = {
        "submission_id": item["submission_id"],
        "student": item["student"],
        "lab": item["lab"],
        "commit_sha": item.get("commit_sha"),
        "notebooks": [],
    }

    with tempfile.TemporaryDirectory() as workdir:

        try:
            entry["commit_sha"] = fetch_at_commit(
                item["repo_url"],
                item.get("commit_sha"),
                workdir,
            )

        except subprocess.TimeoutExpired:
            entry["notebooks"] = [
                {
                    "notebook": "(fetch)",
                    "score": 0.0,
                    "error": "Timed out fetching the repository.",
                    "checks": [],
                }
            ]
            return entry

        except subprocess.CalledProcessError as exc:
            detail = redact(
                (exc.stderr or "").strip(),
                item["repo_url"],
            ).splitlines()

            entry["notebooks"] = [
                {
                    "notebook": "(fetch)",
                    "score": 0.0,
                    "error": (
                        "Could not fetch the repository: "
                        + (detail[-1] if detail else "git failed")
                    ),
                    "checks": [],
                }
            ]
            return entry

        results = grade_student_lab(
            item["student"],
            workdir,
            item["lab"],
            submission_id=item["submission_id"],
        )

        for result in results:
            entry["notebooks"].append(
                {
                    "notebook": result["notebook"],
                    "score": result.get("score", 0.0),
                    "error": result.get("error"),
                    "checks": result.get("checks", []),
                }
            )

    return entry


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Grade an exported batch of submissions offline."
        ),
    )

    parser.add_argument(
        "--input",
        required=True,
        help="pending.json exported from the teacher dashboard",
    )

    parser.add_argument(
        "--output",
        required=True,
        help="where to write results.json",
    )

    parser.add_argument(
        "--lab",
        help="only grade submissions for this lab id",
    )

    parser.add_argument(
        "--student",
        help="only grade this student's submissions",
    )

    args = parser.parse_args()

    work = json.loads(
        Path(args.input).read_text(encoding="utf-8")
    )

    if work.get("format_version") != FORMAT_VERSION:
        print(
            f"Unsupported work file version "
            f"{work.get('format_version')!r}; expected "
            f"{FORMAT_VERSION}.",
            file=sys.stderr,
        )
        return 1

    items = work.get("submissions", [])

    if args.lab:
        items = [i for i in items if i["lab"] == args.lab]

    if args.student:
        items = [i for i in items if i["student"] == args.student]

    if not items:
        print("Nothing to grade.", file=sys.stderr)
        return 1

    print(f"Grading {len(items)} submission(s).\n")

    results = []

    for index, item in enumerate(items, start=1):

        print(
            f"[{index}/{len(items)}] "
            f"{item['student']} / {item['lab']} "
            f"(submission {item['submission_id']}) ... ",
            end="",
            flush=True,
        )

        entry = grade_one(item)

        scores = [
            notebook["score"]
            for notebook in entry["notebooks"]
        ]

        average = (
            round(sum(scores) / len(scores), 4)
            if scores
            else 0.0
        )

        failed = [
            notebook
            for notebook in entry["notebooks"]
            if notebook["error"]
        ]

        if failed and len(failed) == len(entry["notebooks"]):
            print(f"ERROR: {failed[0]['error']}")
        else:
            print(f"{average:.2%}")

        results.append(entry)

    payload = {
        "format_version": FORMAT_VERSION,
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "results": results,
    }

    Path(args.output).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    print(f"\nWrote {len(results)} result(s) to {args.output}")
    print("Upload it from the teacher dashboard to publish the grades.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
