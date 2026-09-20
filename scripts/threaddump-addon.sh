#!/usr/bin/env bash
# Capture a Python thread dump from the running Fleet for ESPHome add-on.
#
# **Normally you don't need this script.** Since 1.6.2 (#189) the
# add-on UI has a first-class "Request diagnostics" action (Settings
# → Advanced → Diagnostics, and Workers tab → Actions → Request
# diagnostics) that produces the same thread-dump output via a
# pure-Python frame walk with no privileged capabilities required.
# Use it first.
#
# This script stays as the last-resort triage tool for the
# pathological case the UI can't serve: the Python interpreter is so
# deadlocked the HTTP endpoint never answers. py-spy walks frames
# externally via ptrace, so it can still dump a process whose event
# loop is frozen.
#
# Runs py-spy in a throwaway sidecar container that shares the add-on's
# PID namespace. Does not modify the add-on or require a restart, so it
# is safe to run while a "stuck at 100% CPU" reproduction is live.
#
# Requires Docker on the host (HAOS: SSH & Web Terminal add-on with
# Protection Mode OFF; Supervised: the host shell). No extra packages to
# install — the sidecar pulls python:3.13-slim and `pip install py-spy`
# on the fly (~15s first time, cached after). The add-on image itself
# no longer ships py-spy (removed in #189 when the UI path landed).
#
# Usage:
#   ./threaddump-addon.sh              # dump the server process
#   ./threaddump-addon.sh --worker     # dump the bundled local worker instead
#   ./threaddump-addon.sh --all        # dump every python3 process in the container

set -euo pipefail

TARGET="server"
case "${1:-}" in
  "") ;;
  --server) TARGET="server" ;;
  --worker) TARGET="worker" ;;
  --all)    TARGET="all" ;;
  -h|--help)
    sed -n '2,16p' "$0"
    exit 0
    ;;
  *)
    echo "Unknown argument: $1" >&2
    exit 2
    ;;
esac

CONTAINER="$(docker ps --format '{{.Names}}' | grep -E '^addon_.*_esphome_dist_server$' | head -1 || true)"
if [[ -z "$CONTAINER" ]]; then
  echo "No running Fleet for ESPHome add-on container found." >&2
  echo "Running add-on containers:" >&2
  docker ps --format '  {{.Names}}' | grep -E '^addon_' >&2 || echo "  (none)" >&2
  exit 1
fi

echo "Target container: $CONTAINER"
echo "Launching py-spy sidecar (target=$TARGET) ..."
echo

docker run --rm \
  --pid="container:$CONTAINER" \
  --cap-add SYS_PTRACE \
  --security-opt apparmor=unconfined \
  -e TARGET="$TARGET" \
  python:3.13-slim \
  bash -c '
set -eu
PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1 \
  pip install --quiet py-spy

# Find processes of interest by matching /proc/<pid>/cmdline.
# We can read /proc from the shared PID namespace.
find_pids() {
  # Match on /proc/<pid>/comm (executable basename) = python3 AND
  # /proc/<pid>/cmdline containing the given pattern. The comm check
  # excludes PID 1 — /sbin/docker-init is the HA add-on init wrapper
  # and reproduces its childs cmdline, so a naive cmdline substring
  # match would grab PID 1 ahead of the real Python process.
  local pattern="$1"
  for p in /proc/[0-9]*; do
    [[ -r "$p/cmdline" && -r "$p/comm" ]] || continue
    [[ "$(cat "$p/comm")" == "python3" ]] || continue
    local cmd
    cmd=$(tr "\0" " " < "$p/cmdline")
    if [[ "$cmd" == *"$pattern"* ]]; then
      echo "$(basename "$p") $cmd"
    fi
  done
}

dump_one() {
  local pid="$1" label="$2"
  echo "========================================================================"
  echo "== $label (PID $pid)"
  echo "========================================================================"
  py-spy dump --pid "$pid" || echo "(py-spy failed for PID $pid)"
  echo
}

case "$TARGET" in
  server)
    hit=$(find_pids "/app/main.py" | head -1)
    if [[ -z "$hit" ]]; then
      echo "Could not find the server process (python3 /app/main.py) in the container." >&2
      exit 3
    fi
    dump_one "${hit%% *}" "server (main.py)"
    ;;
  worker)
    hit=$(find_pids "/app/client/client.py" | head -1)
    if [[ -z "$hit" ]]; then
      echo "Could not find the local worker process (python3 /app/client/client.py)." >&2
      echo "The bundled worker may be disabled — check Max parallel jobs (local worker) in settings." >&2
      exit 3
    fi
    dump_one "${hit%% *}" "worker (client.py)"
    ;;
  all)
    found=0
    while read -r line; do
      [[ -z "$line" ]] && continue
      pid="${line%% *}"
      cmd="${line#* }"
      dump_one "$pid" "$cmd"
      found=1
    done < <(find_pids "python3" | sort -n)
    if [[ "$found" -eq 0 ]]; then
      echo "No python3 processes found in the container." >&2
      exit 3
    fi
    ;;
esac
'
