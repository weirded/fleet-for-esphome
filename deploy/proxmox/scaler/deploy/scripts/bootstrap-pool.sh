#!/usr/bin/env bash
# bootstrap-pool.sh — clone an LXC template into a worker pool on a Proxmox node.
#
# Idempotent: re-running with the same VMIDs is a no-op. Existing LXCs are
# left alone; only missing ones are cloned. Stopped clones are left stopped
# (the scaler will manage start/stop after).
#
# Run on the Proxmox host (root, has `pct` available).
#
# Usage:
#   ./bootstrap-pool.sh
#
# Required env (or set inline / source a config file):
#   TEMPLATE_VMID     VMID of the source LXC template (snapshot or "is_template" CT).
#   POOL_VMIDS        Space-separated VMIDs to create, e.g. "200 201 202 203 204".
#   POOL_HOSTNAME_FMT printf format with one %d placeholder for the index, e.g. "esphome-worker-%d".
#   POOL_STORAGE      Proxmox storage where clones land, e.g. "local-zfs".
#   POOL_BRIDGE       Bridge for the LXC's vmbr (e.g. "vmbr0").
# Optional:
#   POOL_DESCRIPTION  Description set on each clone. Default: "esphome-fleet worker (managed by scaler)".
#   POOL_FULL_CLONE   "1" to use full clones (independent storage); "0" for linked clones (faster, shared base). Default 1.
#
# Pre-reqs you have to do once, by hand:
#   1. Create one LXC manually (Debian/Ubuntu + Docker + the worker container
#      configured with SERVER_URL/SERVER_TOKEN env, autostart on boot).
#   2. Convert to template: `pct stop <id>; pct template <id>` (or take a snapshot).
#   3. Create a Proxmox API token for the scaler with VM.PowerMgmt + VM.Audit
#      on the pool's VMIDs.

set -euo pipefail

: "${TEMPLATE_VMID:?TEMPLATE_VMID is required}"
: "${POOL_VMIDS:?POOL_VMIDS is required}"
: "${POOL_HOSTNAME_FMT:?POOL_HOSTNAME_FMT is required}"
: "${POOL_BRIDGE:?POOL_BRIDGE is required}"

# Auto-detect a rootdir-capable storage if not pinned. `local-lvm` is the
# fresh-install default but Ceph/ZFS-only clusters don't have it.
if [ -z "${POOL_STORAGE:-}" ]; then
  POOL_STORAGE=$(pvesm status --content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')
  if [ -z "$POOL_STORAGE" ]; then
    echo "error: no active rootdir-capable storage found. Set POOL_STORAGE=<name>." >&2
    exit 1
  fi
  echo "[bootstrap] auto-detected POOL_STORAGE=$POOL_STORAGE"
fi

POOL_DESCRIPTION="${POOL_DESCRIPTION:-esphome-fleet worker (managed by scaler)}"
POOL_FULL_CLONE="${POOL_FULL_CLONE:-1}"

if ! command -v pct >/dev/null 2>&1; then
  echo "error: pct CLI not found. Run this on a Proxmox VE host." >&2
  exit 1
fi

if ! pct status "$TEMPLATE_VMID" >/dev/null 2>&1; then
  echo "error: template VMID $TEMPLATE_VMID not found on this node" >&2
  exit 1
fi

idx=0
for vmid in $POOL_VMIDS; do
  idx=$((idx + 1))
  if pct status "$vmid" >/dev/null 2>&1; then
    echo "[$vmid] already exists — skipping"
    continue
  fi

  hostname=$(printf "$POOL_HOSTNAME_FMT" "$idx")
  echo "[$vmid] cloning $TEMPLATE_VMID → $vmid (hostname=$hostname, storage=$POOL_STORAGE, bridge=$POOL_BRIDGE)"

  args=(
    "$TEMPLATE_VMID" "$vmid"
    --hostname "$hostname"
    --description "$POOL_DESCRIPTION"
    --storage "$POOL_STORAGE"
  )
  if [ "$POOL_FULL_CLONE" = "1" ]; then
    args+=(--full 1)
  fi
  pct clone "${args[@]}"

  # Replace the cloned template's network config with our bridge.
  pct set "$vmid" --net0 "name=eth0,bridge=$POOL_BRIDGE,ip=dhcp"
done

echo
echo "Done. Pool VMIDs: $POOL_VMIDS"
echo "Next:"
echo "  - Verify each clone boots: pct start <vmid>; pct enter <vmid>"
echo "  - Check the worker autostart inside each LXC registers with the Fleet server"
echo "  - Stop the clones (pct shutdown <vmid>) so the scaler can manage start/stop"
echo "  - Configure the scaler with PROXMOX_SCALER_VMIDS=$(echo $POOL_VMIDS | tr ' ' ,)"
