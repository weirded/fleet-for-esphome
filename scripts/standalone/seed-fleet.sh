#!/usr/bin/env bash
#
# seed-fleet.sh
# Copy the minimal ESPHome fixture fleet from $FLEET_SOURCE_HOST (default
# hass-4) into the standalone host's bind-mounted config-esphome/
# directory so the e2e-hass-4 Playwright subset has something to
# compile against.
#
# HT.14 analogue of scripts/haos/seed-fleet.sh. Plain Linux hosts on
# both ends, so this is just tar-over-SSH — no pvesh, no qga chunking.
# Env vars intentionally mirror the HAOS script so the two feel the
# same and a future HT.13c commit of scrubbed fixtures drops in for
# both call sites.
#
# Prerequisites:
#   - scripts/standalone/deploy.sh has run (compose dir + config-esphome/
#     bind-mount exist on remote)
#   - SSH access to FLEET_SOURCE_HOST and STANDALONE_HOST from this machine
#
# Usage:
#   scripts/standalone/seed-fleet.sh
#   FLEET_SOURCE_HOST=other-hass scripts/standalone/seed-fleet.sh
#
# Env overrides:
#   STANDALONE_HOST         (default docker-pve-1)
#   STANDALONE_COMPOSE_DIR  (default /opt/esphome-fleet)
#   FLEET_SOURCE_HOST       (default hass-4)
#   FLEET_SOURCE_DIR        (default /usr/share/hassio/homeassistant/esphome)
#   FLEET_TARGETS           (default: the same four files HAOS seeds)

set -euo pipefail

STANDALONE_HOST="${STANDALONE_HOST:-docker-pve-1}"
STANDALONE_COMPOSE_DIR="${STANDALONE_COMPOSE_DIR:-/opt/esphome-fleet}"
FLEET_SOURCE_HOST="${FLEET_SOURCE_HOST:-hass-4}"
FLEET_SOURCE_DIR="${FLEET_SOURCE_DIR:-/usr/share/hassio/homeassistant/esphome}"
# `fonts` + `images` are directories — tar recurses. cyd-world-clock.yaml
# references fonts/Arimo-Regular.ttf, fonts/JetBrainsMono-Bold.ttf, and
# images/flag_{us,in,eu}.png; without them the server-side validator
# rejects the bundle and the compile is marked failed before the worker
# ever sees it (#192). `includes/` carries `common.yaml` (and friends)
# which `garage-door-big.yaml` pulls in via
# `packages: common: !include includes/common.yaml`; without it the
# `parallel compile: garage-door-big pinned to local-worker` smoke
# fast-fails on file-not-found (#225).
FLEET_TARGETS="${FLEET_TARGETS:-cyd-world-clock.yaml garage-door-big.yaml .common.yaml secrets.yaml fonts images includes}"

# SSH multiplexing on both ends — see deploy.sh / haos/install-addon.sh
# for why. Two control sockets, one per remote.
SSH_CTRL_DST="$(mktemp -u -t standalone-ssh.XXXXXX)"
SSH_CTRL_SRC="$(mktemp -u -t fleet-src-ssh.XXXXXX)"
SSH_OPTS_DST=(-o ControlMaster=auto -o ControlPath="$SSH_CTRL_DST" -o ControlPersist=60s)
SSH_OPTS_SRC=(-o ControlMaster=auto -o ControlPath="$SSH_CTRL_SRC" -o ControlPersist=60s)
cleanup() {
  ssh "${SSH_OPTS_DST[@]}" -O exit "$STANDALONE_HOST" 2>/dev/null || true
  ssh "${SSH_OPTS_SRC[@]}" -O exit "$FLEET_SOURCE_HOST" 2>/dev/null || true
  rm -f "$SSH_CTRL_DST" "$SSH_CTRL_SRC"
}
trap cleanup EXIT

echo "==> Source:            $FLEET_SOURCE_HOST:$FLEET_SOURCE_DIR"
echo "==> Target:            $STANDALONE_HOST:$STANDALONE_COMPOSE_DIR/config-esphome"
echo "==> Files:             $FLEET_TARGETS"

# -----------------------------------------------------------------------
# 1. Sanity: figure out which FLEET_TARGETS exist on the source host,
#    warn (don't fail) on missing entries, and ensure the dest dir is
#    in place.
# -----------------------------------------------------------------------
MISSING=""
AVAILABLE=""
for f in $FLEET_TARGETS; do
  if ssh "${SSH_OPTS_SRC[@]}" "$FLEET_SOURCE_HOST" "test -e '$FLEET_SOURCE_DIR/$f'" 2>/dev/null; then
    AVAILABLE="$AVAILABLE $f"
  else
    MISSING="$MISSING $f"
  fi
done
if [[ -n "$MISSING" ]]; then
  echo "    warning: skipping missing on source host:$MISSING" >&2
fi
if [[ -z "${AVAILABLE// /}" ]]; then
  echo "    ERROR: no FLEET_TARGETS exist on $FLEET_SOURCE_HOST:$FLEET_SOURCE_DIR" >&2
  exit 1
fi

if ! ssh "${SSH_OPTS_DST[@]}" "$STANDALONE_HOST" "test -d '$STANDALONE_COMPOSE_DIR/config-esphome'" 2>/dev/null; then
  echo "    ERROR: $STANDALONE_COMPOSE_DIR/config-esphome missing on $STANDALONE_HOST — run deploy.sh first" >&2
  exit 1
fi

# -----------------------------------------------------------------------
# 2. Stream a tarball from source to dest, extract in place.
# -----------------------------------------------------------------------
echo ""
echo "==> Streaming fleet tarball ..."
# shellcheck disable=SC2029  # $AVAILABLE must expand locally
ssh "${SSH_OPTS_SRC[@]}" "$FLEET_SOURCE_HOST" \
    "cd '$FLEET_SOURCE_DIR' && tar cz $AVAILABLE" \
  | ssh "${SSH_OPTS_DST[@]}" "$STANDALONE_HOST" \
    "cd '$STANDALONE_COMPOSE_DIR/config-esphome' && tar xzf -"

# The server container runs as root and the bind-mount inherits the
# remote host's ownership; no chown needed. Empirical check:
echo ""
echo "==> Fleet installed:"
ssh "${SSH_OPTS_DST[@]}" "$STANDALONE_HOST" \
  "cd '$STANDALONE_COMPOSE_DIR/config-esphome' && ls -la"

# -----------------------------------------------------------------------
# 3. Prime the scanner — a GET on /ui/api/targets runs scan_configs()
#    in-handler so the next UI refresh / e2e spec sees the new files
#    without needing a server restart.
# -----------------------------------------------------------------------
TOKEN_FILE="${STANDALONE_TOKEN_FILE:-$HOME/.config/distributed-esphome/standalone-token}"
TOKEN=""
[[ -s "$TOKEN_FILE" ]] && TOKEN=$(cat "$TOKEN_FILE")
ssh "${SSH_OPTS_DST[@]}" "$STANDALONE_HOST" \
  "curl -sf --max-time 10 ${TOKEN:+-H 'Authorization: Bearer $TOKEN'} http://127.0.0.1:8765/ui/api/targets >/dev/null 2>&1 || true"

echo ""
echo "==> Seed complete."
