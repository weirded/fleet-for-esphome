#!/usr/bin/env bash
#
# deploy.sh
# Bring up the standalone docker-compose stack on $STANDALONE_HOST.
#
# HT.14. Analogue of scripts/haos/install-addon.sh for the non-HA
# deployment shape. Plain Linux + SSH + docker compose — no pvesh/qga
# middle hop, no Supervisor, just rsync the compose file and run it.
#
# Prerequisites:
#   - SSH access to $STANDALONE_HOST as a user with docker group
#   - Remote host has `docker` and `docker compose` on $PATH
#   - Internet access on remote to pull ghcr.io images
#
# Usage:
#   scripts/standalone/deploy.sh
#   STANDALONE_HOST=docker-pve-3 scripts/standalone/deploy.sh
#   TAG=1.6.2-dev.19 scripts/standalone/deploy.sh
#
# Env overrides:
#   STANDALONE_HOST         ssh alias (default docker-pve-1)
#   TAG                     image tag (default develop)
#   STANDALONE_COMPOSE_DIR  remote compose dir (default /opt/esphome-fleet)
#   STANDALONE_TOKEN_FILE   local token cache (default
#                           $HOME/.config/distributed-esphome/standalone-token)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

STANDALONE_HOST="${STANDALONE_HOST:-docker-pve-1}"
TAG="${TAG:-develop}"
STANDALONE_COMPOSE_DIR="${STANDALONE_COMPOSE_DIR:-/opt/esphome-fleet}"
# Notable constraint: the server does NOT honor a SERVER_TOKEN env
# var. It generates its own on first boot and persists it in
# /data/settings.json (the standalone source of truth). So we let it
# self-generate, then read the real token back out of the container
# and feed THAT to the worker — same trick push-to-hass-4.sh uses
# against the add-on. The local token cache on the driving machine is
# just for convenience (smoke suite reuse) and is refreshed on each
# deploy from the authoritative in-container copy.
STANDALONE_TOKEN_FILE="${STANDALONE_TOKEN_FILE:-$HOME/.config/distributed-esphome/standalone-token}"

# SSH multiplexing — one auth up front, all later calls reuse it. Same
# rationale as scripts/haos/install-addon.sh: a single ssh invocation
# with many agent-loaded identities burns through sshd's MaxAuthTries
# and trips fail2ban. One auth, many ops.
SSH_CTRL="$(mktemp -u -t standalone-ssh.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$SSH_CTRL" -o ControlPersist=60s)
trap 'ssh "${SSH_OPTS[@]}" -O exit "$STANDALONE_HOST" 2>/dev/null || true; rm -f "$SSH_CTRL"' EXIT
rsh() { ssh "${SSH_OPTS[@]}" "$STANDALONE_HOST" "$@"; }

echo "==> Deploy target:     $STANDALONE_HOST"
echo "==> Compose dir:       $STANDALONE_COMPOSE_DIR"
echo "==> Image tag:         $TAG"

# -----------------------------------------------------------------------
# 1. Push compose file and write a bootstrap .env on the remote.
#    SERVER_TOKEN here is a placeholder — compose requires it to
#    resolve, but the server will ignore it and mint its own. We
#    rewrite .env with the real token after step 4.
# -----------------------------------------------------------------------
mkdir -p "$(dirname "$STANDALONE_TOKEN_FILE")"
PLACEHOLDER_TOKEN="$(openssl rand -hex 16)"

echo ""
echo "==> Ensuring $STANDALONE_COMPOSE_DIR on remote ..."
rsh "mkdir -p '$STANDALONE_COMPOSE_DIR/config-esphome'"

echo "==> Copying docker-compose.yml ..."
scp "${SSH_OPTS[@]}" -q "$REPO_ROOT/docker-compose.yml" \
  "$STANDALONE_HOST:$STANDALONE_COMPOSE_DIR/docker-compose.yml"

echo "==> Writing bootstrap .env on remote ..."
rsh "cat > '$STANDALONE_COMPOSE_DIR/.env'" <<REMOTE_ENV
SERVER_TOKEN=$PLACEHOLDER_TOKEN
TAG=$TAG
WORKER_HOSTNAME=${STANDALONE_HOST}-worker
WORKER_MAX_JOBS=1
REMOTE_ENV
rsh "chmod 600 '$STANDALONE_COMPOSE_DIR/.env'"

# -----------------------------------------------------------------------
# 2. Pull images.
# -----------------------------------------------------------------------
echo ""
echo "==> docker compose pull ..."
rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose pull"

# -----------------------------------------------------------------------
# 3. Start the server alone. Bringing up the worker would race — it
#    would lock onto the placeholder token and get stuck in a 401 loop.
# -----------------------------------------------------------------------
echo ""
echo "==> docker compose up -d server ..."
rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose up -d --remove-orphans server"

# -----------------------------------------------------------------------
# 4. Wait for the server to report 200 on /ui/api/server-info.
#    (unauthenticated; standalone default is require_ha_auth=false)
# -----------------------------------------------------------------------
echo ""
echo "==> Waiting for server to respond at http://$STANDALONE_HOST:8765 ..."
for i in $(seq 1 60); do
  HTTP=$(rsh "curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8765/ui/api/server-info 2>/dev/null" || echo "000")
  if [[ "$HTTP" == "200" ]]; then
    echo "    Server HTTP 200."
    break
  fi
  if [[ "$i" -eq 60 ]]; then
    echo "    Timed out waiting for server to respond (last HTTP: '$HTTP')" >&2
    echo "    Last 20 log lines from server container:" >&2
    rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose logs --tail 20 server" >&2 || true
    exit 1
  fi
  sleep 2
done

VERSION_REPORTED=$(rsh "curl -sf --max-time 3 http://127.0.0.1:8765/ui/api/server-info 2>/dev/null" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('addon_version',''))" 2>/dev/null || echo "")
echo "==> Server reports addon_version=$VERSION_REPORTED"

# Sanity-check the SI.2 banner — the whole point of running this test
# against standalone is to catch regressions of the standalone-mode
# detection path.
if rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose logs server 2>&1 | grep -q 'Running in standalone mode'"; then
  echo "==> Standalone banner detected in server logs."
else
  echo "    WARNING: did NOT see 'Running in standalone mode' banner in server logs" >&2
  echo "    (SI.2 regression signal — HA_MODE=standalone should have been honoured.)" >&2
fi

# -----------------------------------------------------------------------
# 5. Read the server's self-generated token out of /data/settings.json,
#    save it locally, and rewrite the remote .env so the worker picks
#    up the matching value on step 6.
# -----------------------------------------------------------------------
echo ""
echo "==> Reading real SERVER_TOKEN from container ..."
REAL_TOKEN=$(rsh "docker exec esphome-fleet-server python3 -c \"import json; print(json.load(open('/data/settings.json'))['server_token'])\"" 2>/dev/null | tr -d '\r\n' || true)
if [[ -z "$REAL_TOKEN" ]]; then
  echo "    ERROR: couldn't read server_token from /data/settings.json" >&2
  rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose logs --tail 30 server" >&2 || true
  exit 1
fi
printf '%s\n' "$REAL_TOKEN" > "$STANDALONE_TOKEN_FILE"
chmod 600 "$STANDALONE_TOKEN_FILE"
echo "    Token cached at $STANDALONE_TOKEN_FILE (${#REAL_TOKEN} chars)"

echo "==> Rewriting remote .env with real token ..."
rsh "cat > '$STANDALONE_COMPOSE_DIR/.env'" <<REMOTE_ENV
SERVER_TOKEN=$REAL_TOKEN
TAG=$TAG
WORKER_HOSTNAME=${STANDALONE_HOST}-worker
WORKER_MAX_JOBS=1
REMOTE_ENV
rsh "chmod 600 '$STANDALONE_COMPOSE_DIR/.env'"

# -----------------------------------------------------------------------
# 6. Now bring up the worker with the correct token.
#    docker-compose.yml gives the worker a static container_name
#    (esphome-fleet-worker). If a foreign container owns that name — e.g.
#    from a previous run whose compose project was recreated, so compose
#    no longer tracks it — `compose up` (even with --force-recreate)
#    hits "container name already in use" and bails. Force-remove the
#    conflicting name first; harmless if absent.
# -----------------------------------------------------------------------
echo ""
echo "==> Removing any foreign esphome-fleet-worker container (if present)..."
rsh "docker rm -f esphome-fleet-worker 2>/dev/null || true" >/dev/null

echo ""
echo "==> docker compose up -d worker ..."
rsh "cd '$STANDALONE_COMPOSE_DIR' && docker compose up -d --force-recreate worker"

# -----------------------------------------------------------------------
# 7. Wait for the worker to register with the server. The e2e-hass-4
#    suite's compile / pinned-compile / live-log tests all assume at
#    least one online worker exists; launching Playwright before the
#    worker's first heartbeat arrives flakes them with "local-worker
#    must be online". ~10s is normally enough for the worker container
#    to boot + send its first heartbeat.
# -----------------------------------------------------------------------
echo ""
echo "==> Waiting for worker to register ..."
for i in $(seq 1 20); do
  N=$(rsh "curl -sf --max-time 3 -H 'Authorization: Bearer $REAL_TOKEN' http://127.0.0.1:8765/ui/api/workers 2>/dev/null | python3 -c \"import sys,json; print(sum(1 for w in json.load(sys.stdin) if w.get('online')))\" 2>/dev/null" || echo 0)
  if [[ "$N" -ge 1 ]]; then
    echo "    $N worker(s) online."
    break
  fi
  if [[ "$i" -eq 20 ]]; then
    echo "    WARNING: no worker registered after 20s — compile tests may flake" >&2
  fi
  sleep 1
done

# -----------------------------------------------------------------------
# 8. Seed the ESPHome fixture fleet so the e2e-hass-4 suite has real
#    device YAMLs to compile against. Skip with SKIP_SEED=1 on repeat
#    runs when you know the fleet is already there.
# -----------------------------------------------------------------------
if [[ "${SKIP_SEED:-0}" != "1" ]]; then
  echo ""
  echo "==> Seeding fixture fleet from ${FLEET_SOURCE_HOST:-hass-4} ..."
  STANDALONE_HOST="$STANDALONE_HOST" \
  STANDALONE_COMPOSE_DIR="$STANDALONE_COMPOSE_DIR" \
  FLEET_SOURCE_HOST="${FLEET_SOURCE_HOST:-hass-4}" \
    "$REPO_ROOT/scripts/standalone/seed-fleet.sh"
else
  echo "==> SKIP_SEED=1 — skipping fleet seed"
fi
