# Design — Proxmox LXC autoscaler for build workers

Adds a small Python service at `deploy/proxmox/scaler/` that **autoscales distributed-esphome workers running as Proxmox LXC containers**, driven by the Fleet server's queue depth. Mirrors the architectural shape of the Kubernetes Helm chart at `deploy/kubernetes/` (same "external observer of the public queue API" pattern), but targets Proxmox VE instead.

## Why a scaler instead of a worker pool always-on

Same answer as the chart's autoscaling case: workers register with the Fleet server and stay registered until they shut down. Always-on workers consume a slice of the LXC host's CPU and memory continuously, even between compile bursts. Operators with a Proxmox host doing other work want to scale down to 1 (or 0) when idle.

If a user prefers always-on, setting `MIN_TOTAL_WORKERS` equal to the sum of `WORKERS_PER_NODE` overrides makes the scaler a no-op steady-state.

## Why "manage a fixed pool of pre-provisioned LXCs" instead of "clone-on-demand"

Cloning an LXC template on demand is brittle:
- Template versioning + drift between operator and scaler.
- Storage races if multiple clones run concurrently.
- Cloud-init / first-boot setup adds wall-clock latency that users don't expect from a scaler ("why does scale-up take 90s?").
- Destroy-on-scale-down means the LXC's local state (build cache, ESPHome venv) is gone every cycle.

Instead, the operator pre-creates the pool (one or more LXCs per Proxmox node, each tagged with the worker discovery tag and configured to autostart the worker container). The scaler is then **just a start/stop loop**. Cold-start latency is bounded by LXC boot time + worker registration heartbeat (both fast). Build cache + venv survives across stop/start.

## What's in `deploy/proxmox/scaler/`

- `esphome_fleet_proxmox_scaler/` package — config, Fleet client, Proxmox client, multi-node reconciliation loop, entry point.
- `tests/` — 28 unit tests covering config parsing, the Fleet client, and the multi-node reconciliation logic. Proxmox API mocked.
- `requirements.txt` + `pyproject.toml` for `pip install` / `python -m esphome_fleet_proxmox_scaler`.
- `config.example.env` showing the expected env-var surface.
- `Dockerfile` + `docker-compose.yml` so the scaler can run as a container.
- `deploy/systemd/esphome-fleet-proxmox-scaler.service` — hardened systemd unit (DynamicUser, ProtectSystem=strict, MemoryDenyWriteExecute, restricted syscalls).
- `deploy/scripts/provision-node.sh` — creates LXCs from scratch (Debian 12 via `pveam`, Docker via Docker's APT repo, worker container as a systemd unit with `--pull always`). Idempotent via the discovery tag.
- `deploy/scripts/bootstrap-pool.sh` — alternative for operators who already have a baked LXC template.
- `deploy/terraform/` — multi-node Terraform module (`bpg/proxmox` provider) with `node_targets = { node = { count, first_vmid } }`.
- `README.md` walking through pre-provisioning, configuration, and run.

A path-filtered CI workflow at `.github/workflows/proxmox-scaler-ci.yml` runs `pytest` + `shellcheck` on PRs touching `deploy/proxmox/scaler/**`.

## Design choices

1. **Multi-node, per-node count.** Default `workers_per_node = 1` (each Proxmox node hosts one ESPHome build worker). Beefy nodes opt in via `PROXMOX_SCALER_PER_NODE_OVERRIDES=pve-beast:4,pve-tiny:0`. The scaler discovers nodes via the Proxmox API and identifies its LXCs by tag (default `esphome-fleet-worker`), so the operator's contract is *counts*, not VMID lists. New nodes joining the cluster are picked up automatically once their LXCs are provisioned and tagged.
2. **Spread + load shed.** Scale-up picks the node with the most free capacity (so the pool spreads evenly across nodes); scale-down sheds load from the busiest node first. Empty pool returns `scale_up_blocked` rather than crashing.
3. **`shutdown` (graceful) instead of `stop` (hard kill).** When scaling down, we POST to `/lxc/<vmid>/status/shutdown` so the worker has a chance to drain its current job within the LXC's grace period. Hard `stop` would mid-kill compiles. Default LXC shutdown timeout is plenty for an esphome compile.
4. **Cooldown gates scale-down only.** Scale-up always fires immediately so a queue burst gets compute as fast as the LXCs can boot. Scale-down waits `cooldown_seconds` (default 600s, matching the k8s chart). First scale-down on a fresh process is NOT cooldown-gated — otherwise the scaler couldn't drain a fresh-startup pool down to its baseline.
5. **Fleet (or Proxmox) failure → tick is a noop.** If the Fleet server or Proxmox API are unreachable, we log and skip — no Proxmox writes happen. Avoids the failure mode where a network blip causes the scaler to nuke the pool because it thinks the queue is 0.
6. **Token IDs.** Proxmox API tokens are `<user>@<realm>!<token-name>` + a separate secret. We accept the full token-id string and split it client-side. This is the format the Proxmox UI shows when you create a token.
7. **Storage auto-detect.** Both `provision-node.sh` and `bootstrap-pool.sh` detect the first active rootdir-capable storage via `pvesm status --content rootdir` when `POOL_STORAGE` isn't pinned by the operator. Pinning `POOL_STORAGE` explicitly still wins — useful for reproducibility / multi-storage clusters.

## Out of scope

- Provisioning the LXC pool's *contents* (operator runs `provision-node.sh` once or supplies a template).
- Cluster-mode (Proxmox VE cluster) failover / replication for the scaler service itself.
- Backups, monitoring, metrics surface for the scaler.

## Possible follow-ups (not in this PR)

- **Graceful drain endpoint on the worker side.** `POST /api/v1/workers/{id}/control` already exists for worker control. A `disabled=true` toggle path the scaler could call before LXC shutdown would let the worker stop claiming new jobs cleanly while still finishing the current one. The LXC shutdown grace + the worker's heartbeat-driven graceful exit are already belt-and-suspenders without it.
- **Server-side `total_slots_online`.** Not strictly needed — the scaler reads `queue_size` and reconciles its own pool. But a future enhancement could expose `total_slots_online` so a fleet-wide UI could show "you have N worker slots online but a queue of M".
