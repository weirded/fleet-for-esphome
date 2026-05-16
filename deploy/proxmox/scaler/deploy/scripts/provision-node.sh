#!/usr/bin/env bash
# provision-node.sh — automate LXC worker creation on a Proxmox node.
#
# Creates N unprivileged Debian-based LXCs from scratch, installs Docker,
# configures the distributed-esphome worker container to autostart, applies
# the scaler's discovery tag, and stops the LXC so the scaler can manage
# its lifecycle.
#
# Idempotent: re-running with the same VMIDs is a no-op for LXCs that
# already exist with the worker tag. Non-tagged collisions abort.
#
# Inspired by the community-scripts.org pattern (single-shot Debian + Docker
# bootstrap) but tailored to our worker container so we don't have to depend
# on an external script's URL stability.
#
# Run on the Proxmox host (root, has `pct` available).
#
# Required env (or set inline / source a config file):
#   FLEET_URL         e.g. http://homeassistant.local:8765
#   FLEET_TOKEN       Bearer token from the Fleet Settings drawer
#   POOL_VMIDS        Space-separated VMIDs to ensure exist on THIS node, e.g. "200 201"
#
# Optional:
#   POOL_HOSTNAME_FMT printf format with one %d for the index. Default: "esphome-worker-%d"
#   POOL_STORAGE      e.g. "local-lvm" (default), "local-zfs"
#   POOL_BRIDGE       e.g. "vmbr0" (default)
#   POOL_DISK_GB      LXC root disk size, default 8
#   POOL_MEM_MB       LXC memory, default 2048
#   POOL_SWAP_MB      LXC swap, default 512
#   POOL_CORES        LXC vCPUs, default 2
#   POOL_OS_TEMPLATE  Path to LXC template (default: pveam-downloaded debian-12-standard)
#   WORKER_TAG        Discovery tag for the scaler. Default "esphome-fleet-worker".
#   WORKER_IMAGE      Worker container image. Default ghcr.io/weirded/esphome-dist-client:latest
#   WORKER_TAGS_EXTRA Comma-separated tags appended to WORKER_TAGS env in the worker. Default: "proxmox,lxc"

set -euo pipefail

: "${FLEET_URL:?FLEET_URL is required}"
: "${FLEET_TOKEN:?FLEET_TOKEN is required}"
: "${POOL_VMIDS:?POOL_VMIDS is required}"

POOL_HOSTNAME_FMT="${POOL_HOSTNAME_FMT:-esphome-worker-%d}"
POOL_BRIDGE="${POOL_BRIDGE:-vmbr0}"
POOL_DISK_GB="${POOL_DISK_GB:-8}"
POOL_MEM_MB="${POOL_MEM_MB:-2048}"
POOL_SWAP_MB="${POOL_SWAP_MB:-512}"
POOL_CORES="${POOL_CORES:-2}"
WORKER_TAG="${WORKER_TAG:-esphome-fleet-worker}"
WORKER_IMAGE="${WORKER_IMAGE:-ghcr.io/weirded/esphome-dist-client:latest}"
WORKER_TAGS_EXTRA="${WORKER_TAGS_EXTRA:-proxmox,lxc}"

if ! command -v pct >/dev/null 2>&1; then
  echo "error: pct CLI not found. Run this on a Proxmox VE host." >&2
  exit 1
fi

# --- pick a rootdir-capable storage ---
# `local-lvm` is the most common default after a fresh Proxmox install but is
# absent on Ceph-only or ZFS-only clusters (we hit this in homelab testing —
# this cluster only had `Pool0` (rbd) and `local` (dir, no rootdir support)).
# Auto-detect the first active rootdir-capable storage if the operator didn't
# pin one explicitly.
if [ -z "${POOL_STORAGE:-}" ]; then
  POOL_STORAGE=$(pvesm status --content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')
  if [ -z "$POOL_STORAGE" ]; then
    echo "error: no active rootdir-capable storage found on this node. Set POOL_STORAGE=<name>." >&2
    pvesm status --content rootdir >&2 || true
    exit 1
  fi
  echo "[provision] auto-detected POOL_STORAGE=$POOL_STORAGE (override with POOL_STORAGE=...)"
fi

# --- ensure Debian 12 standard template is downloaded ---
if [ -z "${POOL_OS_TEMPLATE:-}" ]; then
  pveam update >/dev/null
  TEMPLATE_NAME=$(pveam available --section system | awk '/debian-12-standard.*amd64/ {print $2}' | sort -V | tail -1)
  [ -z "$TEMPLATE_NAME" ] && {
    echo "error: could not find debian-12-standard template via pveam available" >&2
    exit 1
  }
  if ! pveam list local 2>/dev/null | grep -q "$TEMPLATE_NAME"; then
    echo "[provision] downloading $TEMPLATE_NAME"
    pveam download local "$TEMPLATE_NAME"
  fi
  POOL_OS_TEMPLATE="local:vztmpl/$TEMPLATE_NAME"
fi
echo "[provision] using OS template: $POOL_OS_TEMPLATE"

# --- create LXCs ---
idx=0
for vmid in $POOL_VMIDS; do
  idx=$((idx + 1))
  hostname=$(printf "$POOL_HOSTNAME_FMT" "$idx")

  if pct status "$vmid" >/dev/null 2>&1; then
    # Already exists. If it carries our tag, treat as idempotent. Otherwise
    # bail loudly so we don't clobber unrelated LXCs.
    existing_tags=$(pct config "$vmid" | awk -F': ' '/^tags:/{print $2}')
    if echo "$existing_tags" | tr ';' '\n' | grep -Fxq "$WORKER_TAG"; then
      echo "[$vmid] exists with tag $WORKER_TAG — leaving alone (idempotent)"
      continue
    fi
    echo "error: LXC $vmid already exists but is NOT tagged with $WORKER_TAG. Refusing to clobber." >&2
    exit 1
  fi

  echo "[$vmid] creating from $POOL_OS_TEMPLATE (hostname=$hostname)"
  pct create "$vmid" "$POOL_OS_TEMPLATE" \
    --hostname "$hostname" \
    --cores "$POOL_CORES" \
    --memory "$POOL_MEM_MB" \
    --swap "$POOL_SWAP_MB" \
    --rootfs "${POOL_STORAGE}:${POOL_DISK_GB}" \
    --net0 "name=eth0,bridge=$POOL_BRIDGE,ip=dhcp" \
    --features "nesting=1,keyctl=1" \
    --unprivileged 1 \
    --onboot 0 \
    --tags "$WORKER_TAG" \
    --description "esphome-fleet build worker (managed by proxmox-scaler)"

  echo "[$vmid] starting for first-boot install"
  pct start "$vmid"
  # Wait for network in the LXC.
  for _ in $(seq 1 30); do
    pct exec "$vmid" -- ping -c1 -W1 1.1.1.1 >/dev/null 2>&1 && break
    sleep 1
  done

  echo "[$vmid] installing Docker"
  pct exec "$vmid" -- bash -lc '
    set -e
    apt-get update -y -q
    apt-get install -y -q ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release; echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -y -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io
    systemctl enable --now docker
  '

  echo "[$vmid] configuring worker container as systemd unit"
  # Use a systemd unit instead of `docker run --restart always` so we don't
  # depend on Docker's restart policy starting things in the right order
  # after the LXC's own boot. Also cleaner for `journalctl -u`.
  pct exec "$vmid" -- bash -lc "
    set -e
    cat >/etc/systemd/system/esphome-fleet-worker.service <<UNIT
[Unit]
Description=Fleet for ESPHome build worker
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker rm -f esphome-fleet-worker
ExecStart=/usr/bin/docker run --rm --name esphome-fleet-worker \\
  -e SERVER_URL='${FLEET_URL}' \\
  -e SERVER_TOKEN='${FLEET_TOKEN}' \\
  -e WORKER_TAGS='${WORKER_TAGS_EXTRA}' \\
  -e MAX_PARALLEL_JOBS=2 \\
  --network host \\
  --pull always \\
  ${WORKER_IMAGE}
ExecStop=/usr/bin/docker stop esphome-fleet-worker

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable esphome-fleet-worker
  "

  echo "[$vmid] stopping (scaler will manage start/stop from here)"
  pct shutdown "$vmid"
done

echo
echo "Done."
echo "LXCs created/verified on this node:"
for vmid in $POOL_VMIDS; do
  echo "  - $vmid"
done
echo
echo "All LXCs are tagged '$WORKER_TAG' so the scaler will discover them automatically."
echo "Configure the scaler with PROXMOX_SCALER_WORKER_TAG=$WORKER_TAG (default)."
echo
echo "Run this script on each Proxmox node in your cluster (with that node's"
echo "VMID range) to populate the multi-node pool."
