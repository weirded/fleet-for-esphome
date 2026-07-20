#!/usr/bin/env bash
#
# push-to-standalone.sh
# Deploy the compose stack to a clean Docker host and run the
# standalone-portable subset of the Playwright suite against it.
#
# HT.14. This is the regression guard for every SI.* polish item and
# for bug #83 (bearer-auth 401 page). Without it, the non-HA deployment
# shape rots silently between releases.
#
# Counterpart to:
#   push-to-hass-4.sh  — HA add-on on the author's warm hass-4 box
#   push-to-haos.sh    — HA add-on on a fresh HAOS VM
#   push-to-standalone — server outside HA, compose-only (this script)
#
# Prerequisites:
#   - SSH to $STANDALONE_HOST (docker + docker compose on PATH)
#   - SSH to $FLEET_SOURCE_HOST (default hass-4) with read access to
#     the esphome config dir, for the fixture seed
#   - GHCR pull works on the remote (anonymous for public images is fine)
#
# Usage:
#   ./scripts/push-to-standalone.sh
#   STANDALONE_HOST=docker-optiplex-5 ./scripts/push-to-standalone.sh
#   SKIP_TEARDOWN=1 SKIP_SMOKE=1 ./scripts/push-to-standalone.sh   # dry
#
# Env:
#   STANDALONE_HOST        ssh alias          (default docker-pve)
#   STANDALONE_URL         http base          (default http://<host>:8765)
#   STANDALONE_COMPOSE_DIR remote path        (default /opt/esphome-fleet)
#   STANDALONE_TOKEN_FILE  local token cache  (default ~/.config/distributed-esphome/standalone-token)
#   TAG                    ghcr tag           (default develop)
#   FLEET_SOURCE_HOST      fleet src          (default hass-4)
#   ESPHOME_VERSION        pinned version     (default 2026.7.0)
#   SKIP_TEARDOWN          set to 1 to reuse existing state
#   SKIP_INSTALL           set to 1 to skip deploy.sh
#   SKIP_BOOTSTRAP         set to 1 to skip esphome install
#   SKIP_SEED              set to 1 to skip fleet seed
#   SKIP_SMOKE             set to 1 to skip Playwright run

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

STANDALONE_HOST="${STANDALONE_HOST:-docker-pve}"
TAG="${TAG:-develop}"
FLEET_SOURCE_HOST="${FLEET_SOURCE_HOST:-hass-4}"
STANDALONE_COMPOSE_DIR="${STANDALONE_COMPOSE_DIR:-/opt/esphome-fleet}"
STANDALONE_URL="${STANDALONE_URL:-http://${STANDALONE_HOST}:8765}"
STANDALONE_URL="${STANDALONE_URL%/}"

# Node + Playwright resolve hostnames via the OS resolver which, on a
# VPN-dual-homed laptop, can end up binding to the wrong interface
# (see feedback_laptop_dual_homing.md / bug class). curl and ssh
# happily use the routing table; Node has its own DNS path that
# doesn't. Pre-resolve via `getent`/`dig` to a concrete IP so the
# Playwright run can't trip over this.
STANDALONE_URL_FOR_PLAYWRIGHT="$STANDALONE_URL"
if [[ "$STANDALONE_URL" =~ ^http://([^:/]+)(:[0-9]+)?(/.*)?$ ]]; then
  _host="${BASH_REMATCH[1]}"
  # Only resolve if it's clearly a name, not an IP literal.
  if [[ ! "$_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    # `ssh -G` walks ~/.ssh/config and emits the effective HostName;
    # that's authoritative when the target is an ssh alias (our common
    # case: docker-pve, docker-optiplex-5). Fall back to the OS
    # resolvers if ssh_config doesn't rewrite to an IP.
    _ip=$(ssh -G "$_host" 2>/dev/null | awk '/^hostname / {print $2; exit}')
    [[ "$_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || _ip=""
    [[ -z "$_ip" ]] && _ip=$(dscacheutil -q host -a name "$_host" 2>/dev/null | awk '/^ip_address:/ {print $2; exit}')
    [[ -z "$_ip" ]] && _ip=$(getent hosts "$_host" 2>/dev/null | awk '{print $1; exit}')
    [[ -n "$_ip" ]] && STANDALONE_URL_FOR_PLAYWRIGHT="${STANDALONE_URL//$_host/$_ip}"
  fi
fi

VERSION="$(cat "$REPO_ROOT/ha-addon/VERSION")"

echo "==> Target host:       $STANDALONE_HOST"
echo "==> Server URL:        $STANDALONE_URL"
echo "==> Image tag:         $TAG"
echo "==> App version:       $VERSION"
echo "==> Fleet source:      $FLEET_SOURCE_HOST"

export STANDALONE_HOST TAG STANDALONE_COMPOSE_DIR

# -----------------------------------------------------------------------
# 1. Teardown — guarantee fresh state.
# -----------------------------------------------------------------------
if [[ "${SKIP_TEARDOWN:-0}" != "1" ]]; then
  echo ""
  echo "==> Step 1 / Teardown ..."
  "$REPO_ROOT/scripts/standalone/teardown.sh"
else
  echo "==> SKIP_TEARDOWN=1 — reusing existing remote state"
fi

# -----------------------------------------------------------------------
# 2. Deploy — compose up, wait for HTTP 200.
# -----------------------------------------------------------------------
if [[ "${SKIP_INSTALL:-0}" != "1" ]]; then
  echo ""
  echo "==> Step 2 / Deploy ..."
  "$REPO_ROOT/scripts/standalone/deploy.sh"
else
  echo "==> SKIP_INSTALL=1 — skipping deploy"
fi

# -----------------------------------------------------------------------
# 3. Bootstrap ESPHome — POST version, wait for install.
# -----------------------------------------------------------------------
if [[ "${SKIP_BOOTSTRAP:-0}" != "1" ]]; then
  echo ""
  echo "==> Step 3 / Bootstrap ESPHome ..."
  "$REPO_ROOT/scripts/standalone/bootstrap-esphome.sh"
else
  echo "==> SKIP_BOOTSTRAP=1 — assuming ESPHome already ready"
fi

# -----------------------------------------------------------------------
# 4. Seed fleet fixtures.
# -----------------------------------------------------------------------
if [[ "${SKIP_SEED:-0}" != "1" ]]; then
  echo ""
  echo "==> Step 4 / Seed fleet from $FLEET_SOURCE_HOST ..."
  FLEET_SOURCE_HOST="$FLEET_SOURCE_HOST" "$REPO_ROOT/scripts/standalone/seed-fleet.sh"
else
  echo "==> SKIP_SEED=1 — skipping fleet seed"
fi

# -----------------------------------------------------------------------
# 5. Wait for the worker to register.
#    The worker service in docker-compose.yml comes up after the server
#    healthcheck passes, but its first heartbeat still needs a second
#    or two. Poll /ui/api/workers until the expected hostname shows
#    online.
# -----------------------------------------------------------------------
STANDALONE_TOKEN_FILE="${STANDALONE_TOKEN_FILE:-$HOME/.config/distributed-esphome/standalone-token}"
STANDALONE_TOKEN=""
[[ -s "$STANDALONE_TOKEN_FILE" ]] && STANDALONE_TOKEN=$(cat "$STANDALONE_TOKEN_FILE")

expected_worker="${STANDALONE_HOST}-worker"

echo ""
echo "==> Step 5 / Waiting for worker '$expected_worker' to register ..."
SSH_CTRL="$(mktemp -u -t standalone-ssh.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$SSH_CTRL" -o ControlPersist=60s)
trap 'ssh "${SSH_OPTS[@]}" -O exit "$STANDALONE_HOST" 2>/dev/null || true; rm -f "$SSH_CTRL"' EXIT

rsh() { ssh "${SSH_OPTS[@]}" "$STANDALONE_HOST" "$@"; }

fetch_workers() {
  local auth=""
  [[ -n "$STANDALONE_TOKEN" ]] && auth="-H 'Authorization: Bearer $STANDALONE_TOKEN'"
  rsh "curl -sf --max-time 5 $auth http://127.0.0.1:8765/ui/api/workers 2>/dev/null" || true
}

for i in $(seq 1 30); do
  WORKERS_JSON=$(fetch_workers)
  ONLINE=$(echo "$WORKERS_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    workers = data.get('workers', data) if isinstance(data, dict) else data
    for w in workers or []:
        if w.get('hostname') == '$expected_worker' and w.get('online'):
            print('yes'); break
    else:
        print('no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")
  if [[ "$ONLINE" == "yes" ]]; then
    echo "    Worker '$expected_worker' is online."
    break
  fi
  if [[ "$i" -eq 30 ]]; then
    echo "    Worker did not register in 60s. /ui/api/workers payload:" >&2
    echo "$WORKERS_JSON" >&2
    echo "    Worker container log tail:" >&2
    rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose logs --tail 40 worker" >&2 || true
    exit 1
  fi
  sleep 2
done

# -----------------------------------------------------------------------
# 6. Smoke.
# -----------------------------------------------------------------------
if [[ "${SKIP_SMOKE:-0}" == "1" ]]; then
  echo ""
  echo "==> SKIP_SMOKE=1 — done."
  exit 0
fi

cd "$REPO_ROOT/ha-addon/ui"

echo ""
echo "==> Step 6a / Mocked Playwright suite ..."
npm run test:e2e

echo ""
echo "==> Step 6b / Standalone-portable e2e-hass-4 subset ..."
# Excluded specs:
#   ha-services            — calls HA REST API directly
#   cyd-office-info        — real OTA to a physical device not on this LAN
#   incremental-build      — same dependency
#
# Included (standalone-safe):
#   direct-port-auth       — bug #82/#83 regression anchor
#   pinned-bulk-compile    — worker claim + job cancel path, no hardware
#   schedule-fires         — schedule → enqueue → cancel, no hardware
echo "    Playwright base URL: $STANDALONE_URL_FOR_PLAYWRIGHT"
HASS4_URL="$STANDALONE_URL_FOR_PLAYWRIGHT" \
HASS4_ADDON_TOKEN="$STANDALONE_TOKEN" \
HASS4_TARGET="${HASS4_TARGET:-cyd-world-clock.yaml}" \
  npx playwright test --config=e2e-hass-4/playwright.config.ts \
    --grep-invert '(ha-services|cyd-office-info|incremental-build)'

echo ""
echo "==> HT.14 regression run complete."
