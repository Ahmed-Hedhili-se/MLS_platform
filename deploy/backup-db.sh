#!/usr/bin/env bash
#
# Back up the MLS SQLite database.
#
# Uses `sqlite3 .backup`, not cp: the database is in WAL mode and
# being written to while this runs, so copying the file directly can
# capture a torn state. .backup takes a proper consistent snapshot.
#
#   mls-backup [destination-dir]

set -euo pipefail

PROJECT="${MLS_PROJECT:-/opt/MLS_platform/Mls-Platform}"
DEST="${1:-$PROJECT/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"

# Read the configured database path rather than guessing, so this
# keeps working if DATABASE_URL changes.
DB="$(
    cd "$PROJECT" && "$PROJECT/.venv/bin/python" - "$PROJECT" <<'PY'
import sys
# cron does not run from the project directory, so put it on the path
# explicitly rather than relying on the working directory.
sys.path.insert(0, sys.argv[1])
from webapp.database import DATABASE_PATH
print(DATABASE_PATH)
PY
)"

if [[ ! -f "$DB" ]]; then
    echo "No database at $DB" >&2
    exit 1
fi

mkdir -p "$DEST"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/app-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"

gzip -9 "$OUT"

echo "Backed up to $OUT.gz"

# Prune old snapshots.
find "$DEST" -name 'app-*.db.gz' -mtime "+$KEEP_DAYS" -delete

echo "Kept the last $KEEP_DAYS days."
