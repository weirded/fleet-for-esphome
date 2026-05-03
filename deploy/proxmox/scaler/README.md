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
2. Computes desired worker count: `ceil(queue_size / target_per_worker)`, clamped to `[min_workers, max_workers]`.
3. Reconciles the pool: starts stopped LXCs until enough are running; stops running LXCs (after `cooldown_seconds`) when fewer are needed.

LXCs themselves are **pre-provisioned** by the operator (clone a template, install the worker image, configure autostart). The scaler only manages start/stop of an existing pool. This keeps the scaler small and avoids the failure modes of cloning-on-demand (template versioning, storage races, network setup).

## Quick start

### 1. Pre-provision an LXC pool on Proxmox

Create one LXC manually with:
- A Linux distro that runs Docker (Debian/Ubuntu).
- Docker installed, `ghcr.io/weirded/esphome-dist-client:latest` configured to autostart with the Fleet server URL and token in its env. (Or run the worker via a Python venv; either works — the LXC just needs to register with the Fleet server on boot.)
- Network access to (a) the Fleet server's worker API (port 8765) and (b) your ESP devices' subnet.

Clone N copies (e.g., VMIDs 200–205) so you have a pool. **Each clone must have a unique hostname** so Fleet can tell them apart in the Workers tab.

### 2. Configure the scaler

Copy `config.example.env` and fill in:

```ini
PROXMOX_SCALER_FLEET_URL=http://homeassistant.local:8765
PROXMOX_SCALER_FLEET_TOKEN=<paste from Fleet Settings drawer>

PROXMOX_SCALER_PROXMOX_HOST=proxmox.example.com:8006
PROXMOX_SCALER_PROXMOX_TOKEN_ID=root@pam!scaler
PROXMOX_SCALER_PROXMOX_TOKEN_SECRET=<api token secret>
PROXMOX_SCALER_PROXMOX_NODE=pve

PROXMOX_SCALER_VMIDS=200,201,202,203,204,205
PROXMOX_SCALER_MIN_WORKERS=1
PROXMOX_SCALER_MAX_WORKERS=5
PROXMOX_SCALER_TARGET_PER_WORKER=2
PROXMOX_SCALER_POLL_INTERVAL=30
PROXMOX_SCALER_COOLDOWN_SECONDS=600
```

The Proxmox API token needs `VM.PowerMgmt` and `VM.Audit` permissions on the LXC pool.

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

## Status

This is a **draft** scaffold accompanying PR #<TBD>. The polling loop, Fleet client, Proxmox client, and unit tests are in place; Dockerfile, systemd unit, docker-compose, and a chart-CI-equivalent test workflow are follow-ups.
