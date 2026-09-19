"""Shared constants for the server application."""

# HA Supervisor internal IP — used for Ingress trust authentication
HA_SUPERVISOR_IP = "172.30.32.2"

# HTTP headers
HEADER_AUTHORIZATION = "Authorization"
HEADER_X_SERVER_VERSION = "X-Server-Version"
HEADER_X_CLIENT_ID = "X-Client-Id"
HEADER_X_WORKER_ID = "X-Worker-Id"
HEADER_X_INGRESS_PATH = "X-Ingress-Path"

# File names
SECRETS_YAML = "secrets.yaml"

# Minimum client Docker image version the server expects. Workers reporting an
# older image_version (or missing one) will be flagged in the UI and will NOT
# receive source-code auto-update payloads — updating .py files in place can't
# fix a stale image (missing system packages, old Python, old requirements).
# Bump this when a change in the client Dockerfile requires workers to rebuild
# their image (e.g. adding a new system dep or Python library).
MIN_IMAGE_VERSION = "12"

# Floor at which ``scanner.create_bundle`` switches between the
# modern, scoped-bundle path and the legacy full-config-dir tar
# (BD.2 / #131). ``ConfigBundleCreator`` lives in ``esphome.bundle``
# (landed ESPHome 2026.4): ≥ this floor → modern path; < this floor →
# legacy fallback. Pre-1.7.1 this was a hard install-time refusal,
# which blocked legitimate use cases (#130 / #131 — pinning 2026.3.3
# to dodge a 2026.4 YAML-parser regression, keeping older toolchains
# around). The legacy path ships the entire config directory, so users
# pinning below this floor lose bundle-scoping isolation; safe for
# single-user fleets, documented in DOCS.md / CHANGELOG.
MIN_ESPHOME_VERSION = "2026.4.0"

# Worker disk-pressure self-pause thresholds (#219). When a worker's
# heartbeat reports ``disk_used_pct`` at or above _ENTER_, the registry
# stamps ``health_blocked_reason = "disk_full"`` on it and the job-claim
# path returns 204 instead of assigning new work. The block clears when
# usage drops to or below _EXIT_. Two thresholds (hysteresis) keeps a job
# whose intermediate writes oscillate around a single line from flapping
# the gate. The 5-point band (95 → 90) covers the lifecycle of a typical
# PlatformIO toolchain extract (~600 MB) plus an ESPHome venv install
# (~1 GiB) on a 50 GiB worker rootfs without false trips.
WORKER_DISK_BLOCK_ENTER_PCT = 95
WORKER_DISK_BLOCK_EXIT_PCT = 90

# Absolute-space floor for the disk-pressure gate above (bug: a worker with
# a many-TB volume at >=95% used still has enormous headroom in absolute
# terms — e.g. 25.8 TB at 95% used leaves ~1.2 TB free, but a compile job
# only needs ~1.6 GB: ~600 MB PlatformIO toolchain extract + ~1 GiB ESPHome
# venv install. Blocking purely on percentage falsely starves huge volumes
# of work. The block now requires BOTH signals: percentage AND absolute
# free space below this floor. 15 GiB gives ~9x the single-job footprint —
# covers several concurrent build slots plus the fleet's own
# default_worker_disk_quota_bytes (10 GiB, settings.py) headroom, with
# margin for OS/logs/other containers sharing the host disk. On a typical
# worker in the tens-of-GB range this floor is *larger* than the 5% left
# free at the ENTER threshold (on a 50 GiB disk, 5% is 2.5 GiB — well
# under 15 GiB), so the floor is always satisfied whenever percentage
# crosses ENTER, i.e. behavior on small/typical disks is unchanged from
# before this fix. The floor only starts gating above ~300 GiB, which is
# exactly where a percentage-only rule stops being meaningful.
WORKER_DISK_FREE_FLOOR_BYTES = 15 * 1024 ** 3
# Server-side OTA push (api._server_ota_push) timeout thresholds. Thread OTA
# can legitimately take minutes for a sub-1MB image on a slow/lossy mesh
# (observed live: a 900KB image took 195s over a link with 0.9-2.4s ping
# RTT) — a flat total-duration timeout kills a transfer that's still
# actively progressing, just slowly. Progress-based instead: read the
# subprocess's output incrementally and reset the idle clock on every
# chunk received (including --dashboard's periodic "Uploading: NN%"
# frames — ESPHome's ProgressBar is otherwise a no-op over a piped,
# non-tty stdout). Only genuine silence for SERVER_OTA_IDLE_TIMEOUT
# seconds counts as stuck.
SERVER_OTA_IDLE_TIMEOUT = 90  # seconds — matches espota2.py's own 90s
                              # per-operation socket timeout during
                              # transfer, so waiting longer than that with
                              # zero bytes back is a reasonable "it's dead"
                              # signal, not just a slow link.
# Absolute safety ceiling even while progress keeps happening — a
# pathological case (a device dribbling output forever) shouldn't tie up
# the job indefinitely.
SERVER_OTA_ABSOLUTE_TIMEOUT = 1800  # seconds (30 minutes)
