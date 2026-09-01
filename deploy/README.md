# Deploying the MLS platform

Target: one Ubuntu server (ARM or x86) running the Flask app under
systemd, with grading in Docker containers, exposed over HTTPS by a
Cloudflare Tunnel.

Sized for ~205 students on a self-hosted Linux machine.

---

## 0. Prepare the machine

Self-hosted on your own Linux box. No provisioning, no capacity
lottery — but the machine becomes infrastructure for 9 months, so a
few things are worth getting right before students depend on it.

### Hardware

| | Cores | RAM | Disk |
|---|---|---|---|
| Minimum | 4 | 8 GB | 100 GB |
| Comfortable | 8 | 16 GB | 250 GB |

Each grading container is capped at 2 CPUs / 2 GB, and 2 run
concurrently by default — so 4 cores and 8 GB is the real floor, with
headroom for the OS and the web app. More cores buys grading
throughput directly (see **Tuning** below).

**Disk**: submission snapshots are one-commit repos, so they are
compact, but nothing prunes them. Budget roughly 25–40 GB across
205 students × 4 labs × resubmissions over the mandate, plus the OS
and Docker images. 100 GB is safe; 250 GB means never thinking about
it.

### OS

Ubuntu 22.04 or 24.04 LTS (Server preferred — no desktop needed).
Both are supported well past your 9 months.

### Stop it going to sleep

This is the most common way a self-hosted service dies. On a desktop
install the machine will happily suspend and take the tunnel with it:

```bash
sudo systemctl mask sleep.target suspend.target     hibernate.target hybrid-sleep.target
```

If it is a laptop, also set it to ignore a closed lid — in
`/etc/systemd/logind.conf`:

```
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
```

then `sudo systemctl restart systemd-logind`.

### Survive reboots and power cuts

Both `mls` and `cloudflared` are installed as enabled systemd services
below, so they come back on their own after a reboot. What they cannot
survive is the machine not coming back:

- In BIOS/UEFI, set **restore power state after AC loss** to *On*, so
  a power cut does not leave it off until someone notices.
- A small UPS is worth it if the building's power is unreliable —
  SQLite in WAL mode handles a hard power loss well, but the OS
  filesystem may not.

### Unattended security updates, without surprise reboots

```bash
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Leave automatic reboots **off**. You want to choose when the platform
restarts, not discover it happened during a deadline.

### Network

No inbound ports, no port forwarding, no static IP, no firewall
changes. The Cloudflare Tunnel connects **outbound** — which is what
makes this work from behind university NAT.

Do check with SUP'COM IT that an outbound tunnel exposing an internal
service is acceptable policy. Better to have that conversation now
than after 205 students are using it.

---

## 1. Get the code onto the server

```bash
sudo mkdir -p /opt
sudo git clone <your-repo-url> /opt/MLS_platform
cd /opt/MLS_platform/Mls-Platform
```

Paths in `mls.service`, `mls-backup.cron` and `backup-db.sh` assume
`/opt/MLS_platform`. Edit them if you clone elsewhere.

## 2. Run the setup script

```bash
sudo bash deploy/setup-server.sh
```

Installs Docker, Python, the venv and system packages; creates the
`mls` service user; and builds the grader image **from source on this
machine**, so it matches the architecture.

Takes a few minutes, mostly compiling the grading image's scientific
stack. Re-run it any time — it is idempotent.

## 3. Configure the environment

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # FLASK_SECRET_KEY
nano .env
sudo chown mls:mls .env && sudo chmod 600 .env
```

Set at minimum `FLASK_SECRET_KEY`, `GITHUB_CLIENT_ID`,
`GITHUB_CLIENT_SECRET`, and:

```
MLS_URL_SCHEME=https      # required behind the tunnel
GRADING_WORKERS=2         # see "Tuning" below
```

Leave `MLS_INSECURE_COOKIES` unset. Setting it drops the Secure flag
from the session cookie.

## 4. Point GitHub OAuth at the real hostname

In your GitHub OAuth App settings, set the callback URL to:

```
https://mls.your-domain.example/auth/github/callback
```

This must match exactly, and it must be the tunnel hostname — not an
IP and not an ngrok URL. Changing it later logs everyone out mid-session.

## 5. Start the service

```bash
sudo cp deploy/mls.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mls
sudo systemctl status mls
journalctl -u mls -f
```

## 6. Cloudflare Tunnel

```bash
# Install cloudflared (amd64 for a standard x86 machine)
curl -L -o cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

cloudflared tunnel login
cloudflared tunnel create mls
cloudflared tunnel route dns mls mls.your-domain.example

sudo mkdir -p /etc/cloudflared
sudo cp deploy/cloudflared-config.yml /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml     # fill in TUNNEL-UUID + hostname

sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

You need a domain on Cloudflare (a free plan is fine). If you do not
have one, a cheap `.dev`/`.app` costs a few euros a year — worth it for
a stable OAuth callback over 9 months.

## 7. Backups

```bash
sudo cp deploy/backup-db.sh /usr/local/bin/mls-backup
sudo chmod +x /usr/local/bin/mls-backup
sudo cp deploy/mls-backup.cron /etc/cron.d/mls-backup
sudo -u mls /usr/local/bin/mls-backup     # verify it works now
```

Nightly at 02:30, 30 days retained, in `backups/`. Uses
`sqlite3 .backup`, which snapshots consistently while the app is
writing — `cp` on a WAL database can capture a torn file.

**Copy these off the server too.** A backup on the same disk as the
database does not survive losing the disk. `rclone` to any object
store, or a nightly `scp` from a machine you control.

---

## Tuning grading throughput

Each container is capped at 2 CPUs / 2 GB in `webapp/app.py`, and
`GRADING_WORKERS` (default 2) sets how many run at once.

On 4 OCPU, the default is 2 concurrent jobs. Notebook execution is
largely single-threaded, so you will usually get more throughput by
dropping `--cpus` to `"1"` in `app.py` and setting
`GRADING_WORKERS=3`.

**Measure before a deadline.** Grade ~20 real submissions and time
them:

```bash
journalctl -u mls | grep "Grading job for submission"
```

205 submissions at 2 min each with 2 workers is about 3.5 hours; at
4 min each it is about 7. That is the number that decides whether you
grade overnight or during a class.

## Updating

```bash
cd /opt/MLS_platform && sudo -u mls git pull
sudo -u mls ./Mls-Platform/.venv/bin/pip install -r Mls-Platform/requirements.txt
# Rebuild the grader only if grade.py, labs/ or requirements-grader.txt changed:
sudo docker build -f Mls-Platform/Dockerfile.grader -t mls-grader:1.0 Mls-Platform/
sudo systemctl restart mls
```

Restarting is safe mid-grade: submissions left in `grading` are reset
to `grading_error` on the next start so they can be re-queued.

## Health checks

```bash
systemctl status mls cloudflared         # both running?
docker ps                                # grading containers in flight
journalctl -u mls -n 100 --no-pager      # recent app log
sqlite3 webapp/app.db "PRAGMA journal_mode;"   # expect: wal
ls -lh backups/ | tail                   # backups actually landing
```
