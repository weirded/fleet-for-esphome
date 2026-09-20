#!/usr/bin/env bash
#
# install-addon.sh
# Deploy the local ha-addon/ source into a HAOS test VM, then install
# (or rebuild) the add-on via Supervisor.
#
# File transfer mechanism is identical to ha-outback-mate3's install-addon.sh:
#   tar -> scp to the Proxmox host -> chunked pvesh file-write into the
#   guest via qemu-guest-agent -> guest tar-extract into
#   /mnt/data/supervisor/addons/local/esphome_dist_server/
# Supervisor operations run via `docker exec hassio_cli ha ...`. No HA long-
# lived token needed for these calls (Supervisor trusts host-side commands).
#
# Two install modes, selected by INSTALL_MODE env var:
#
#   INSTALL_MODE=local  (default)
#     Strips the `image:` key from config.yaml on the guest so Supervisor
#     takes the local-build path. This is the analogue of push-to-hass-4.sh's
#     behaviour — always works, doesn't depend on GHCR carrying the -dev.N
#     tag, but does NOT test the #82 prebuilt-image path.
#
#   INSTALL_MODE=ghcr
#     Keeps the `image:` key intact so Supervisor pulls the prebuilt
#     ghcr.io/weirded/<arch>-addon-esphome-dist-server:<VERSION> image.
#     This is the direct regression guard for bug #82 (IM.1/IM.2 — we no
#     longer drive Supervisor into the local-build path that pulls
#     docker:<HOST_VER>-cli). Requires the GHCR publish workflow to have
#     already tagged the current VERSION; typically run after a release
#     tag or against `:develop`.
#
# Prerequisites:
#   - Test VM provisioned (scripts/haos/provision-vm.sh) with -agent 1
#   - SSH access to the Proxmox host, which can run `pvesh`
#
# Usage:
#   scripts/haos/install-addon.sh                       # local-build mode
#   INSTALL_MODE=ghcr scripts/haos/install-addon.sh     # GHCR-pull mode
#
# Env overrides:
#   PVE_HOST     (default pve)
#   VMID         (default 106)
#   INSTALL_MODE (default local, or ghcr)
#   SKIP_HA_RESTART (default 0 — set 1 to skip `ha core restart` after install;
#                    normally needed so HA reloads the custom integration)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

PVE_HOST="${PVE_HOST:-pve}"
VMID="${VMID:-106}"
INSTALL_MODE="${INSTALL_MODE:-local}"
SKIP_HA_RESTART="${SKIP_HA_RESTART:-0}"
ADDON_SLUG="esphome_dist_server"
ADDON_DIR="ha-addon"

# Proxmox cluster node name used in pvesh API paths (/nodes/<node>/...).
# Defaults match the single-host `pve` install; override PVE_NODE explicitly
# for clusters, or leave empty to auto-detect from `pvesh get /nodes`.
PVE_NODE="${PVE_NODE:-}"

if [[ "$INSTALL_MODE" != "local" && "$INSTALL_MODE" != "ghcr" ]]; then
  echo "INSTALL_MODE must be 'local' or 'ghcr' (got '$INSTALL_MODE')" >&2
  exit 1
fi

if [[ -z "$PVE_NODE" ]]; then
  # Use a plain ssh here — SSH_OPTS is set up below, after argument validation.
  PVE_NODE=$(ssh "$PVE_HOST" "pvesh get /nodes --output-format json" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['node'])" 2>/dev/null) \
    || { echo "Couldn't auto-detect PVE_NODE; set PVE_NODE explicitly" >&2; exit 3; }
fi

FULL_SLUG="local_${ADDON_SLUG}"
GUEST_TAR="/tmp/${ADDON_SLUG}.tar"
# Resolved lazily in step 3 — the probe needs guest_exec(), which is
# defined further down. See the detection block there for why this
# can't be a hardcoded path.
GUEST_TARGET=""

[[ -d "$ADDON_DIR" ]] || { echo "Add-on source dir $ADDON_DIR not found (REPO_ROOT=$REPO_ROOT)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 required locally (for parsing pvesh JSON)" >&2; exit 2; }

# SSH connection multiplexing. An install run fires 15+ ssh/scp invocations
# (one per ha_cli call + scp + chunk push + guest_exec pairs). When ssh-agent
# offers many identities, sshd's MaxAuthTries trips partway through and the
# rest of the run fails with "Too many authentication failures". The
# ControlMaster below opens one TCP+auth session up front; every subsequent
# ssh/scp reuses it with no reauth. ControlPersist keeps it warm across
# helper functions; the EXIT trap tears it down cleanly so we don't leak
# sockets in $TMPDIR.
SSH_CTRL="$(mktemp -u -t pve-ssh.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$SSH_CTRL" -o ControlPersist=60s)
cleanup_ssh() {
  ssh "${SSH_OPTS[@]}" -O exit "$PVE_HOST" 2>/dev/null || true
  rm -f "$SSH_CTRL"
}
trap cleanup_ssh EXIT

pve_ssh() { ssh "${SSH_OPTS[@]}" "$@"; }
pve_scp() { scp "${SSH_OPTS[@]}" "$@"; }

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# Prime the multiplexed connection so later calls don't race on master setup.
pve_ssh "$PVE_HOST" true

# Run a shell command inside the HAOS guest via qemu-guest-agent and wait
# for completion. Prints guest stdout on 1, guest stderr on 2; returns the
# guest exit code. The script is uploaded to pve as a file (avoids escaping
# issues), then pve drives guest-exec and polls exec-status until done.
guest_exec() {
  local script="$1"
  local timeout_s="${2:-600}"

  # Upload the script text to a temp file on pve.
  local remote_script
  remote_script=$(pve_ssh "$PVE_HOST" mktemp -t guest_exec.XXXXXX)
  printf '%s' "$script" | pve_ssh "$PVE_HOST" "cat > $remote_script"

  # Orchestrator runs on pve: reads the script, fires guest-exec, polls.
  pve_ssh "$PVE_HOST" "PVE_NODE=$PVE_NODE VMID=$VMID TIMEOUT_S=$timeout_s SCRIPT_FILE=$remote_script bash -s" <<'REMOTE'
set -euo pipefail
TMPJSON=$(mktemp)
trap 'rm -f "$TMPJSON" "$SCRIPT_FILE"' EXIT

SCRIPT=$(cat "$SCRIPT_FILE")
pvesh create "/nodes/$PVE_NODE/qemu/$VMID/agent/exec" \
  --command /bin/sh --command -c --command "$SCRIPT" \
  --output-format json > "$TMPJSON"
PID=$(python3 -c "import sys,json; print(json.load(open(sys.argv[1]))['pid'])" "$TMPJSON")

for _ in $(seq 1 "$TIMEOUT_S"); do
  pvesh get "/nodes/$PVE_NODE/qemu/$VMID/agent/exec-status" \
    --pid "$PID" --output-format json > "$TMPJSON"
  if python3 -c "import sys,json; sys.exit(0 if json.load(open(sys.argv[1])).get('exited') else 1)" "$TMPJSON"; then
    python3 - "$TMPJSON" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("out-data"):
    sys.stdout.write(d["out-data"])
if d.get("err-data"):
    sys.stderr.write(d["err-data"])
sys.exit(d.get("exitcode", 0))
PY
    exit $?
  fi
  sleep 1
done
echo "guest exec timed out after ${TIMEOUT_S}s" >&2
exit 124
REMOTE
}

# Convenience: run ha CLI inside the VM. Default 60s timeout; installs and
# rebuilds can take several minutes on first build, so callers override.
ha_cli() {
  local timeout="${HA_CLI_TIMEOUT:-60}"
  guest_exec "docker exec hassio_cli ha $*" "$timeout"
}

# --- 1. Build tarball ------------------------------------------------------

log "Packaging $ADDON_DIR (mode=$INSTALL_MODE)"
TARBALL=$(mktemp -t distributed_esphome_addon.XXXXXX).tar
trap 'rm -f "$TARBALL"' EXIT
# macOS tar writes ._* AppleDouble entries for files with extended attrs;
# COPYFILE_DISABLE=1 is the documented way to suppress that on bsdtar / macOS tar.
# The ui/ subdir is the frontend source — not shipped in the built add-on;
# the compiled output lives under server/static/ and is included.
#
# INSTALL_MODE=ghcr: also skip everything that lives inside the runtime
# container image (server/client source, custom_integration, Dockerfiles,
# requirements). Supervisor only reads config.yaml + apparmor.txt +
# translations + icons/docs from the on-disk add-on directory; the image
# is pulled from GHCR. Default (local) mode keeps the full tarball so
# Supervisor can local-build.
GHCR_EXCLUDES=()
if [[ "$INSTALL_MODE" == "ghcr" ]]; then
  GHCR_EXCLUDES=(
    --exclude="server"
    --exclude="client"
    --exclude="custom_integration"
    --exclude="Dockerfile"
    --exclude="Dockerfile.*"
    --exclude="requirements.txt"
    --exclude="requirements.lock"
  )
fi
COPYFILE_DISABLE=1 tar cf "$TARBALL" -C "$ADDON_DIR" \
  --exclude="__pycache__" --exclude=".pytest_cache" --exclude=".mypy_cache" \
  --exclude=".ruff_cache" --exclude="._*" --exclude=".DS_Store" \
  --exclude="ui" \
  ${GHCR_EXCLUDES[@]+"${GHCR_EXCLUDES[@]}"} \
  .
log "Tarball size: $(du -h "$TARBALL" | awk '{print $1}')"

# --- 2. Push to guest in chunks (pvesh --content capped at ~60 KB) --------

log "Copying tarball to $PVE_HOST"
REMOTE_STAGE="/tmp/distributed_esphome_addon-$$.tar"
pve_scp -q "$TARBALL" "$PVE_HOST:$REMOTE_STAGE"

CHUNK_PREFIX="deaddon"
log "Clearing any stale chunks on the guest from a previous run"
# If the previous tar had more chunks than the new one, stale tail chunks
# would concatenate onto the new tar and corrupt it. Wipe first.
guest_exec "rm -f /tmp/${CHUNK_PREFIX}.*" 30 >/dev/null

log "Pushing tarball to VM $VMID via qga file-write (chunked)"
pve_ssh "$PVE_HOST" bash -s "$PVE_NODE" "$VMID" "$REMOTE_STAGE" "$CHUNK_PREFIX" <<'REMOTE'
set -euo pipefail
PVE_NODE="$1"
VMID="$2"
SRC="$3"
PREFIX="$4"
CHUNK_DIR=$(mktemp -d)
trap 'rm -rf "$CHUNK_DIR" "$SRC"' EXIT
split -b 40000 -a 3 -d "$SRC" "$CHUNK_DIR/$PREFIX."
for f in "$CHUNK_DIR/$PREFIX".*; do
  name=$(basename "$f")
  B64=$(base64 -w0 < "$f")
  pvesh create "/nodes/$PVE_NODE/qemu/$VMID/agent/file-write" \
    --encode 0 --file "/tmp/$name" --content "$B64" >/dev/null
done
REMOTE

# --- 3. Reassemble + extract on the guest, honor INSTALL_MODE --------------

# Supervisor's local add-on directory moved between HA OS releases:
# `addons/local` → `apps/local`, mirroring the CLI rename this script
# already follows (`ha apps info` / `ha apps update` above). Writing to
# the wrong one is silent and total: the tar lands, the extract succeeds,
# every log line reads healthy — and Supervisor never sees it, because it
# scans the other directory. The add-on stays pinned at whatever version
# was last written to the live path, so the version-wait later times out
# with a message about the *image* being stale, pointing the reader at
# GHCR instead of at the deploy that never arrived. This burned the
# 1.8.0-dev.3 haos-pve run (writes to addons/local at 1.8.0-dev.3, live
# tree in apps/local still 1.7.2-dev.8 from June 1) and is the infra half
# of #243. push-to-hass-4.sh carries the same probe for the same reason.
#
# The base that ALREADY holds the add-on IS the one Supervisor reads —
# that beats guessing by HA OS version. Only on a fresh install do we
# fall back to the first existing base, newest layout first.
log "Locating Supervisor's local add-on dir on the guest"
GUEST_LOCAL_BASE=$(guest_exec '
bases="/mnt/data/supervisor/apps/local /mnt/data/supervisor/addons/local /usr/share/hassio/apps/local /usr/share/hassio/addons/local"
for b in $bases; do
  [ -d "$b/'"${ADDON_SLUG}"'" ] && { echo "$b"; exit 0; }
done
for b in $bases; do
  [ -d "$b" ] && { echo "$b"; exit 0; }
done
' | tr -d '\r' | head -1)
if [[ -z "$GUEST_LOCAL_BASE" ]]; then
  echo "ERROR: could not locate Supervisor's local add-on dir on VM $VMID." >&2
  echo "       Add the current path to the probe list in scripts/haos/install-addon.sh." >&2
  exit 1
fi
GUEST_TARGET="${GUEST_LOCAL_BASE}/${ADDON_SLUG}"
log "Supervisor local add-on dir: $GUEST_LOCAL_BASE"

log "Extracting into $GUEST_TARGET (mode=$INSTALL_MODE)"
# The local-build mode strips the `image:` key so Supervisor takes the
# local-build path for the current dev -dev.N tag (which doesn't exist on
# GHCR at push time). The ghcr mode keeps it so Supervisor pulls the
# prebuilt image — bug #82's actual fix path.
if [[ "$INSTALL_MODE" == "local" ]]; then
  STRIP_IMAGE_CMD="sed -i '/^image:/d' $GUEST_TARGET/config.yaml"
else
  STRIP_IMAGE_CMD=":"   # no-op
fi

guest_exec "
set -e
cat /tmp/${CHUNK_PREFIX}.* > $GUEST_TAR
rm -f /tmp/${CHUNK_PREFIX}.*
rm -rf $GUEST_TARGET
mkdir -p $GUEST_TARGET
cd $GUEST_TARGET
tar xf $GUEST_TAR
rm -f $GUEST_TAR
$STRIP_IMAGE_CMD
" >/dev/null

# --- 4. Install or rebuild via Supervisor CLI ----------------------------

log "Reloading Supervisor store"
ha_cli "store reload --no-progress" >/dev/null 2>&1 \
  || ha_cli "store reload" >/dev/null 2>&1 || true

# Check install state via `apps info --raw-json`. `version` is null for a
# store-only app and set to the installed version for an installed app;
# `state` is "unknown" for not-installed vs "started"/"stopped" when installed.
INFO_JSON=$(ha_cli "apps info $FULL_SLUG --raw-json" 2>/dev/null || echo '{"data":{}}')
INSTALLED_VERSION=$(python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('data',{}).get('version') or '')" <<<"$INFO_JSON")
LATEST_VERSION=$(python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('data',{}).get('version_latest') or '')" <<<"$INFO_JSON")
STATE=$(python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('data',{}).get('state') or '')" <<<"$INFO_JSON")

if [[ -z "$INSTALLED_VERSION" && "$STATE" == "unknown" ]]; then
  log "Installing $FULL_SLUG (first build can take several minutes)"
  HA_CLI_TIMEOUT=1200 ha_cli "apps install $FULL_SLUG" >/dev/null
elif [[ "$INSTALLED_VERSION" != "$LATEST_VERSION" ]]; then
  # Supervisor refuses `apps rebuild` when the version changed in config.yaml;
  # use `apps update` to pick up the new version + build.
  log "Updating $FULL_SLUG ($INSTALLED_VERSION → $LATEST_VERSION)"
  HA_CLI_TIMEOUT=1200 ha_cli "apps update $FULL_SLUG" >/dev/null
elif [[ "$INSTALL_MODE" == "ghcr" ]]; then
  # Supervisor refuses `apps rebuild` on image-based add-ons ("Can't
  # rebuild an image-based app"). In GHCR mode the runtime container
  # IS authoritative — if version already matches, the right image is
  # already installed. No-op here; the unconditional `apps restart`
  # below picks up any container-layer changes (re-tagged images).
  log "Already at $INSTALLED_VERSION (GHCR image-based) — skipping rebuild"
else
  log "Rebuilding $FULL_SLUG (was at $INSTALLED_VERSION, same version — code-only change)"
  HA_CLI_TIMEOUT=1200 ha_cli "apps rebuild $FULL_SLUG" >/dev/null
fi

log "Ensuring $FULL_SLUG is running"
# `apps restart` starts a stopped add-on AND restarts a running one — safe either way.
ha_cli "apps restart $FULL_SLUG" >/dev/null

# --- 5. HA Core restart so the bundled custom integration reloads ----------
# HA Core caches custom_components Python modules; only a full `ha core restart`
# makes integration changes visible. The add-on's integration_installer has
# already copied the files into /config/custom_components/esphome_fleet/ by
# the time the add-on restarted (step above), but HA still holds the old
# module objects.

if [[ "$SKIP_HA_RESTART" == "1" ]]; then
  log "SKIP_HA_RESTART=1 — add-on running, but HA Core NOT restarted"
else
  log "Restarting HA Core (~30s before it's responsive again)"
  # ha_cli takes a single positional and reads timeout from HA_CLI_TIMEOUT.
  # Passing `120` as $2 would concat onto the command line; use the env var.
  HA_CLI_TIMEOUT=120 ha_cli "core restart" >/dev/null
fi

# --- 6. Report --------------------------------------------------------------

log "Verifying add-on state"
STATE_JSON=$(ha_cli "apps info $FULL_SLUG --raw-json" 2>/dev/null || echo '{}')
VERSION=$(python3 -c "import sys,json; d=json.loads(sys.stdin.read()).get('data',{}); print(d.get('version','?'))" <<<"$STATE_JSON")
STATE=$(python3 -c "import sys,json; d=json.loads(sys.stdin.read()).get('data',{}); print(d.get('state','?'))" <<<"$STATE_JSON")

cat >&2 <<EOF

Add-on '$FULL_SLUG' is installed (version $VERSION, state $STATE, mode $INSTALL_MODE).

Tail logs:
  ssh $PVE_HOST 'pvesh create /nodes/$PVE_NODE/qemu/$VMID/agent/exec \\
    --command /bin/sh --command -c --command \\
    "docker exec hassio_cli ha apps logs $FULL_SLUG"'

EOF
