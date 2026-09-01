#!/usr/bin/env bash
#
# Prepare a fresh Ubuntu server (ARM or x86) to run the MLS platform.
#
#   sudo bash deploy/setup-server.sh
#
# Idempotent: safe to re-run. Installs Docker, Python and the system
# packages the app needs, builds the grader image, and creates the
# service user. It does NOT start the app -- see deploy/README.md.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi

# Resolve the project root from this script's location, so the script
# does not care where the repository was cloned.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$HERE")"

SERVICE_USER="${SERVICE_USER:-mls}"

echo "==> Project root: $PROJECT"
echo "==> Architecture: $(uname -m)"

# ------------------------------------------------------------
# System packages
# ------------------------------------------------------------

echo "==> Installing system packages"

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-dev \
    build-essential \
    git \
    sqlite3 \
    ca-certificates \
    curl \
    gnupg

# ------------------------------------------------------------
# Docker
# ------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1; then

    echo "==> Installing Docker"

    install -m 0755 -d /etc/apt/keyrings

    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg

    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y docker-ce docker-ce-cli containerd.io

else
    echo "==> Docker already installed: $(docker --version)"
fi

systemctl enable --now docker

# ------------------------------------------------------------
# Service user
# ------------------------------------------------------------

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    echo "==> Creating service user: $SERVICE_USER"
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
    echo "==> Service user already exists: $SERVICE_USER"
fi

# The app launches grading containers, so it needs the Docker socket.
# This is root-equivalent access to the host: it is why the app runs as
# a dedicated unprivileged user and not as a login account.
usermod -aG docker "$SERVICE_USER"

# ------------------------------------------------------------
# Python environment
# ------------------------------------------------------------

echo "==> Creating virtualenv"

if [[ ! -d "$PROJECT/.venv" ]]; then
    python3 -m venv "$PROJECT/.venv"
fi

"$PROJECT/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT/.venv/bin/pip" install --quiet -r "$PROJECT/requirements.txt"

# ------------------------------------------------------------
# Grader image
# ------------------------------------------------------------
#
# Built from source on this machine, so it matches the host
# architecture. On ARM (Oracle Ampere) every dependency resolves to an
# arm64 wheel; build-essential is in the image as a fallback for any
# that must compile.

GRADER_IMAGE="${GRADER_IMAGE:-mls-grader:1.0}"

echo "==> Building grader image: $GRADER_IMAGE (this takes a few minutes)"

docker build \
    -f "$PROJECT/Dockerfile.grader" \
    -t "$GRADER_IMAGE" \
    "$PROJECT"

# ------------------------------------------------------------
# Data directories
# ------------------------------------------------------------

mkdir -p "$PROJECT/data/submissions"
mkdir -p "$PROJECT/backups"

chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT"

# ------------------------------------------------------------
# Done
# ------------------------------------------------------------

cat <<DONE

==> Setup complete.

Next:
  1. Create $PROJECT/.env from .env.example and fill it in.
     Generate a secret with:
       python3 -c "import secrets; print(secrets.token_hex(32))"

  2. Install the service:
       sudo cp deploy/mls.service /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now mls

  3. Set up the Cloudflare Tunnel (see deploy/README.md).

  4. Install the backup cron:
       sudo cp deploy/backup-db.sh /usr/local/bin/mls-backup
       sudo chmod +x /usr/local/bin/mls-backup
       sudo cp deploy/mls-backup.cron /etc/cron.d/mls-backup

DONE
