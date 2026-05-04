# Proxmox provisioning scripts

Two scripts. Pick the one that matches your starting point.

| Script | Use when |
| --- | --- |
| `provision-node.sh` | You don't have a worker template yet. The script creates LXCs from scratch (Debian 12 + Docker + worker autostart). One run per Proxmox node. |
| `bootstrap-pool.sh` | You already pre-built an LXC template manually. The script clones the template into a pool. Faster but assumes the template exists. |

Both are run on the Proxmox host (root) and are idempotent — re-running with the same VMIDs leaves existing tagged LXCs alone.

## Recommended: `provision-node.sh` (no manual template needed)

Run once per node, with that node's chunk of VMIDs:

```bash
# On pve1 (the typical case — script auto-picks an active rootdir-capable storage)
FLEET_URL=http://homeassistant.local:8765 \
FLEET_TOKEN=<paste from Fleet Settings drawer> \
POOL_VMIDS="200 201" \
./provision-node.sh

# On pve-beast (4-worker node, also pin storage explicitly for reproducibility)
FLEET_URL=http://homeassistant.local:8765 \
FLEET_TOKEN=<paste from Fleet Settings drawer> \
POOL_VMIDS="300 301 302 303" \
POOL_STORAGE=local-zfs \
./provision-node.sh
```

If `POOL_STORAGE` isn't set, the script picks the first active storage that supports `rootdir` (via `pvesm status --content rootdir`). On a fresh Proxmox install that's usually `local-lvm`; on Ceph-only clusters it might be `Pool0` or similar. The script logs the choice so you can see what it picked.

What it does:

1. Downloads the Debian 12 LXC template via `pveam` if not already cached.
2. Creates each LXC with `pct create` (sane defaults for an unprivileged worker container).
3. Boots the LXC, installs Docker (official APT repo), configures the worker container as a systemd unit (`esphome-fleet-worker.service`), enables it on boot.
4. Tags each LXC with `esphome-fleet-worker` so the scaler discovers them.
5. Stops the LXC (the scaler manages start/stop after).

Re-running is safe: existing LXCs with the `esphome-fleet-worker` tag are skipped. LXCs with the same VMID but **without** the tag abort the run (so we don't clobber unrelated containers).

## Alternative: `bootstrap-pool.sh` (clone an existing template)

If you've already built a template LXC by hand, this script clones from it:

```bash
TEMPLATE_VMID=900 \
POOL_VMIDS="200 201 202 203 204 205" \
POOL_HOSTNAME_FMT="esphome-worker-%d" \
POOL_STORAGE=local-zfs \
POOL_BRIDGE=vmbr0 \
./bootstrap-pool.sh
```

Faster than `provision-node.sh` (linked/full clones avoid the apt + Docker install step) but you have to maintain the template. If the worker image gets updated, you re-bake the template + re-clone. With `provision-node.sh`, the worker container's `--pull always` plus systemd takes care of image updates per-LXC.

## For an IaC-managed alternative, see `deploy/proxmox/scaler/deploy/terraform/`.
