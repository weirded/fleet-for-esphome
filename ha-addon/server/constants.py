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
MIN_IMAGE_VERSION = "8"

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
