#!/usr/bin/env bash
#
# teardown.sh
# Wipe the standalone compose stack on $STANDALONE_HOST back to a
# clean slate. Safe to run before a deploy to guarantee no state
# leaks between HT.14 regression runs, and safe to run repeatedly.
#
# What it removes:
#   - Both containers (server + worker)
#   - Named volumes (esphome-dist-data, esphome-versions) — this wipes
#     the git-versioning history, firmware archive, settings.json
#   - The config-esphome bind-mount directory
#   - The compose-dir .env file
#
# What it intentionally keeps:
#   - The compose dir itself (cheap to recreate; saves one rsync)
#   - The local $STANDALONE_TOKEN_FILE (next deploy reuses the token
#     by design — matches scripts/haos/ onboarding conventions)
#
# Env overrides:
#   STANDALONE_HOST         (default docker-pve-1)
#   STANDALONE_COMPOSE_DIR  (default /opt/esphome-fleet)

set -euo pipefail

STANDALONE_HOST="${STANDALONE_HOST:-docker-pve-1}"
STANDALONE_COMPOSE_DIR="${STANDALONE_COMPOSE_DIR:-/opt/esphome-fleet}"

SSH_CTRL="$(mktemp -u -t standalone-ssh.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$SSH_CTRL" -o ControlPersist=60s)
trap 'ssh "${SSH_OPTS[@]}" -O exit "$STANDALONE_HOST" 2>/dev/null || true; rm -f "$SSH_CTRL"' EXIT
rsh() { ssh "${SSH_OPTS[@]}" "$STANDALONE_HOST" "$@"; }

echo "==> Tearing down $STANDALONE_HOST:$STANDALONE_COMPOSE_DIR ..."

# compose down -v is the single step that stops containers AND deletes
# the named volumes in one shot. `|| true` because a fresh host where
# the compose dir doesn't even exist yet should still be a successful
# no-op teardown.
rsh bash -s "$STANDALONE_COMPOSE_DIR" <<'REMOTE'
set -eu
compose_dir="$1"
if [[ -f "$compose_dir/docker-compose.yml" ]]; then
  cd "$compose_dir"
  docker compose down -v --remove-orphans 2>&1 || true
  rm -rf config-esphome .env
fi
# Belt-and-suspenders cleanup. If a previous run died before compose
# finished wiring up the project, containers created with fixed
# container_name: persist outside the project and collide on the next
# deploy. Same story for named volumes if the project name shifts.
for c in esphome-fleet-server esphome-fleet-worker esphome-dist-client; do
  docker rm -f "\$c" 2>/dev/null || true
done
docker volume rm esphome-dist-data esphome-versions 2>/dev/null || true
REMOTE

echo "==> Teardown complete."
