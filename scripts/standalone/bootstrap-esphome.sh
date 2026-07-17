#!/usr/bin/env bash
#
# bootstrap-esphome.sh
# Pick an ESPHome version for the standalone server and wait for the
# lazy install to finish before the smoke suite tries to compile.
#
# HT.14 / HT.13b-equivalent. On a fresh standalone boot there's no
# SUPERVISOR_TOKEN, so scanner.ensure_esphome_installed falls back to
# PyPI latest. That works but takes 1–3 minutes; /ui/api/server-info
# returns esphome_install_status="installing" until it's done. If the
# smoke enqueues a compile before that transition, the worker claims
# the job and hangs waiting for the version dir to exist.
#
# POSTing an explicit version here lets us pin to exactly the release
# compile-test.yml exercises — same version matrix across regression
# guards. Polling the install_status field is how the UI banner's
# "Retry" button knows when to clear.
#
# Prerequisites:
#   - deploy.sh has run (server responding on :8765)
#
# Env overrides:
#   STANDALONE_HOST       (default docker-pve)
#   ESPHOME_VERSION       (default 2026.7.0 — pinned to compile-test.yml)
#   BOOTSTRAP_TIMEOUT     (default 600 seconds = 10 min)

set -euo pipefail

STANDALONE_HOST="${STANDALONE_HOST:-docker-pve}"
STANDALONE_COMPOSE_DIR="${STANDALONE_COMPOSE_DIR:-/opt/esphome-fleet}"
ESPHOME_VERSION="${ESPHOME_VERSION:-2026.7.0}"
BOOTSTRAP_TIMEOUT="${BOOTSTRAP_TIMEOUT:-600}"

STANDALONE_TOKEN_FILE="${STANDALONE_TOKEN_FILE:-$HOME/.config/distributed-esphome/standalone-token}"
TOKEN=""
[[ -s "$STANDALONE_TOKEN_FILE" ]] && TOKEN=$(cat "$STANDALONE_TOKEN_FILE")

SSH_CTRL="$(mktemp -u -t standalone-ssh.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$SSH_CTRL" -o ControlPersist=60s)
trap 'ssh "${SSH_OPTS[@]}" -O exit "$STANDALONE_HOST" 2>/dev/null || true; rm -f "$SSH_CTRL"' EXIT
rsh() { ssh "${SSH_OPTS[@]}" "$STANDALONE_HOST" "$@"; }

# All curls run on the remote so we don't have to care about whether
# the server is reachable from the laptop (see the docker-pve dual-homing
# note in the plan). Standalone default is require_ha_auth=false so
# the token is optional; we send it only if we have one.
AUTH_HEADER=""
[[ -n "$TOKEN" ]] && AUTH_HEADER="-H 'Authorization: Bearer $TOKEN'"

echo "==> Bootstrap target:  $STANDALONE_HOST"
echo "==> ESPHome version:   $ESPHOME_VERSION"
echo "==> Timeout:           ${BOOTSTRAP_TIMEOUT}s"

# -----------------------------------------------------------------------
# 1. Tell the server which version to install.
# -----------------------------------------------------------------------
echo ""
echo "==> POST /ui/api/esphome-version ..."
# Single-quoted heredoc on the remote so $TOKEN and $ESPHOME_VERSION
# don't get re-expanded there; we interpolate them on this side.
if ! rsh "curl -sf --max-time 10 $AUTH_HEADER -H 'Content-Type: application/json' --data '{\"version\":\"$ESPHOME_VERSION\"}' -X POST http://127.0.0.1:8765/ui/api/esphome-version" >/dev/null; then
  echo "    ERROR: /ui/api/esphome-version POST failed" >&2
  rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose logs --tail 30 server" >&2 || true
  exit 1
fi
echo "    Requested version: $ESPHOME_VERSION"

# -----------------------------------------------------------------------
# 2. Poll /ui/api/server-info.esphome_install_status until it flips to
#    "ready" — or "failed", or we time out.
# -----------------------------------------------------------------------
echo ""
echo "==> Polling esphome_install_status ..."
START_TS=$(date +%s)
LAST_STATUS=""
while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TS))
  if (( ELAPSED > BOOTSTRAP_TIMEOUT )); then
    echo "    TIMEOUT after ${ELAPSED}s (last status: '$LAST_STATUS')" >&2
    echo "    Last 40 server log lines:" >&2
    rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose logs --tail 40 server" >&2 || true
    exit 124
  fi
  RESP=$(rsh "curl -sf --max-time 10 $AUTH_HEADER http://127.0.0.1:8765/ui/api/server-info" 2>/dev/null || true)
  STATUS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('esphome_install_status',''))" 2>/dev/null || echo "")
  VER=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('esphome_server_version',''))" 2>/dev/null || echo "")
  if [[ "$STATUS" != "$LAST_STATUS" ]]; then
    echo "    [${ELAPSED}s] status=$STATUS version=$VER"
    LAST_STATUS="$STATUS"
  fi
  case "$STATUS" in
    ready)
      echo "    ESPHome $VER installed and ready."
      exit 0
      ;;
    failed)
      echo "    ESPHome install FAILED. Server logs tail:" >&2
      rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose logs --tail 60 server" >&2 || true
      exit 2
      ;;
  esac
  sleep 5
done
