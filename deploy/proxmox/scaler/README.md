# esphome-fleet-proxmox-scaler

A small Python service that **autoscales distributed-esphome build workers** running as Proxmox LXC containers, driven by the Fleet server's queue depth.

Same architectural shape as `deploy/kubernetes/` (PR #107), but for a Proxmox VE host instead of a Kubernetes cluster. The Fleet server stays untouched — this is a deployment-tier component that consumes the public `/api/v1/status` endpoint and reconciles a pool of pre-provisioned Proxmox LXC containers against the queue.

## How it works

```
                    Fleet server (HA add-on or standalone)
                              │
                              │  GET /api/v1/status (Bearer auth)
                              │  → {"queue_size": N, "online_workers": M}
                              ▼
                    ┌───────────────────────┐
                    │  proxmox-scaler       │
                    │  (this service)       │
                    └─────────┬─────────────┘
                              │  Proxmox VE API
                              │  (start/stop LXC)
                              ▼
       ┌────────────┬─────────┴──────────┬────────────┐
       ▼            ▼                    ▼            ▼
   LXC #200     LXC #201            LXC #202      LXC #203
   (worker)     (stopped)           (stopped)     (stopped)
       │
       │ each LXC autostarts the distributed-esphome worker container
       │ which registers with the Fleet server
       ▼
   Fleet server sees online_workers tick up
```

Every `poll_interval` seconds the scaler:

1. Reads `queue_size` from the Fleet server.
2. Discovers all LXCs in the cluster carrying the worker tag (default `esphome-fleet-worker`), grouped by Proxmox node.
3. Computes desired total worker count: `ceil(queue_size / target_per_worker)`, clamped to `[min_total_workers, sum(per_node_max)]`.
4. Reconciles the pool: starts stopped LXCs (preferring nodes with the most free capacity) until enough are running; after `cooldown_seconds`, stops the highest-VMID running LXC on the busiest node when fewer are needed.

**Per-node sizing.** Default is **one LXC per Proxmox node**. Override on beefy nodes (`PROXMOX_SCALER_PER_NODE_OVERRIDES=pve-beast:4`) or skip lightweight ones with `:0`. Most homelabs run with the default and call it good; the override is for clusters where one host is much faster than the others and you want it to do more of the work.

**LXCs themselves are pre-provisioned** by the operator (or by `provision-node.sh` / Terraform — see `deploy/scripts/` and `deploy/terraform/`). The scaler only manages start/stop of an already-discovered pool. This keeps the scaler small and avoids the failure modes of clone-on-demand (template versioning, storage races, network setup).

## Quick start

### 1. Provision the LXC pool on Proxmox

Pick one of:

- **`deploy/scripts/provision-node.sh`** (recommended) — creates LXCs from scratch on a Proxmox node. Downloads the Debian 12 LXC template, installs Docker, configures the worker container as a systemd unit, applies the discovery tag, stops the LXC. Run once per node.

- **`deploy/terraform/`** — multi-node Terraform module that clones a pre-built template across all your nodes with per-node counts. Better if you already use IaC.

- **`deploy/scripts/bootstrap-pool.sh`** — clones a pre-built template into a single-node pool. Faster than `provision-node.sh` (no first-boot install) but assumes you've baked a template by hand.

Whichever path you pick, the result is a pool of LXC workers carrying the `esphome-fleet-worker` tag, configured to autostart the worker container on boot, currently stopped.

### 2. Configure the scaler

Copy `config.example.env` and fill in the required fields. Default behavior is **1 worker per Proxmox node** with no always-on baseline:

```ini
PROXMOX_SCALER_FLEET_URL=http://homeassistant.local:8765
PROXMOX_SCALER_FLEET_TOKEN=<paste from Fleet Settings drawer>

PROXMOX_SCALER_PROXMOX_HOST=proxmox.example.com:8006
PROXMOX_SCALER_PROXMOX_TOKEN_ID=root@pam!scaler
PROXMOX_SCALER_PROXMOX_TOKEN_SECRET=<api token secret>

# Default: 1 worker per node. Bump for beefier nodes.
PROXMOX_SCALER_WORKERS_PER_NODE=1
# PROXMOX_SCALER_PER_NODE_OVERRIDES=pve-beast:4,pve-tiny:0
```

The Proxmox API token needs `VM.PowerMgmt` and `VM.Audit` permissions on the pool LXCs (and `VM.Allocate` + `VM.Config.*` if you'll also use the Terraform/provision scripts with the same token).

### 3. Run it

```bash
pip install -r requirements.txt
python -m esphome_fleet_proxmox_scaler
```

Or as a systemd unit / Docker container — see `deploy/` (forthcoming).

## Why a scaler instead of just running N LXCs always-on

Same answer as the k8s chart: workers register with the Fleet server and stay registered until they shut down. With always-on workers your build host is constantly running them, even when there's nothing to compile. For a homelab where the LXC host might be doing other work, scaling down to 1 (or 0) when idle saves CPU + RAM + power.

If you prefer always-on, set `MIN_WORKERS = MAX_WORKERS` and the scaler becomes a no-op steady-state: it'll start that many LXCs at boot and never touch them.

## Comparison with the k8s chart (deploy/kubernetes/)

| | k8s chart | proxmox-scaler |
| --- | --- | --- |
| Scaling driver | KEDA (cluster-side) | This Python service |
| Worker provisioning | Helm-managed Deployment + ReplicaSet | Pre-provisioned LXC pool |
| Server changes | None | None |
| Deployment artifacts | Helm chart | Python package + systemd / Docker |
| Networking | Pod CIDR (or hostNetwork) | LXC bridge (typically vmbr0) |

## Automated deployment

You don't have to do most of the install by hand. Three layers of automation, pick whichever fits:

- **`deploy/terraform/`** — Terraform module that provisions the entire LXC pool from a template. Idempotent; resize the pool by editing `pool_size` and re-applying. Recommended if you already use IaC.
- **`deploy/scripts/bootstrap-pool.sh`** — shell-script equivalent for users who don't want Terraform. `pct clone` loop with idempotency. Run on the Proxmox host.
- **`deploy/systemd/esphome-fleet-proxmox-scaler.service`** — runs the scaler natively under systemd with hardened sandboxing.
- **`Dockerfile` + `docker-compose.yml`** — runs the scaler as a container (anywhere with Proxmox API reachability).

The one thing you still do by hand is creating the **LXC template** (one container with worker autostart configured, then `pct template`). That's a five-minute setup once; the scaler + automation handles everything after.

## Status

This is a **draft** scaffold accompanying PR #113. Includes the polling loop, Fleet client, Proxmox client, unit tests (25/25 pass), Dockerfile, docker-compose, systemd unit, bootstrap shell script, and Terraform module. Live cluster integration test + a CI workflow at `.github/workflows/proxmox-scaler-ci.yml` are remaining follow-ups.
