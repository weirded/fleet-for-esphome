# PR draft — Proxmox LXC autoscaler at `deploy/proxmox/scaler/`

Adds a small Python service that **autoscales distributed-esphome workers running as Proxmox LXC containers**, driven by the Fleet server's queue depth. Mirrors the architectural shape of `deploy/kubernetes/` (PR #107) but for Proxmox VE instead of Kubernetes — same "external observer of the public queue API" pattern.

## Why a scaler instead of a worker pool always-on

Same answer as the chart's autoscaling case: workers register with the Fleet server and stay registered until they shut down. Always-on workers consume a slice of the LXC host's CPU and memory continuously, even between compile bursts. Operators with a Proxmox host doing other work want to scale down to 1 (or 0) when idle.

If a user prefers always-on, setting `MIN_WORKERS = MAX_WORKERS` makes the scaler a no-op steady-state.

## Why "manage a fixed pool of pre-provisioned LXCs" instead of "clone-on-demand"

Cloning an LXC template on demand is brittle:
- Template versioning + drift between operator and scaler.
- Storage races if multiple clones run concurrently.
- Cloud-init / first-boot setup adds wall-clock latency that users don't expect from a scaler ("why does scale-up take 90s?").
- Destroy-on-scale-down means the LXC's local state (build cache, ESPHome venv) is gone every cycle.

Instead, the operator pre-creates the pool (e.g., 6 LXCs at VMIDs 200–205), each one with the worker container/process configured to autostart. The scaler is then **just a start/stop loop**. Cold-start latency is bounded by LXC boot time + worker registration heartbeat (both fast). Build cache + venv survives across stop/start.

## Scope of this PR

Draft scaffolding. Includes:

- `esphome_fleet_proxmox_scaler/` package — config, Fleet client, Proxmox client, reconciliation loop, entry point. ~350 LOC of source.
- `tests/` — unit tests for config parsing, Fleet client, and the reconciliation loop. Proxmox API mocked. 24 tests.
- `requirements.txt` + `pyproject.toml` for `pip install` / `python -m esphome_fleet_proxmox_scaler`.
- `config.example.env` showing the expected env-var surface.
- `README.md` walking through pre-provisioning + configuration + run.

Deferred to a follow-up PR (intentionally — keeps this draft reviewable):

- `Dockerfile` + `docker-compose.yml` so the scaler can run as a container.
- `systemd/` unit for running the scaler natively on a Proxmox host.
- A CI workflow (`.github/workflows/proxmox-scaler-ci.yml`) running `pytest` on PRs touching `deploy/proxmox/scaler/**`.
- Integration test using a real Proxmox sandbox (out of scope for repo CI).

## Design choices worth review

1. **Pool of fixed VMIDs vs cloning.** Discussed above. Strong preference for pool. Push back if you'd rather a clone-on-demand variant — the surface area gets significantly bigger.
2. **`shutdown` (graceful) instead of `stop` (hard kill).** When scaling down, we POST to `/lxc/<vmid>/status/shutdown` so the worker has a chance to drain its current job within the LXC's grace period. Hard `stop` would mid-kill compiles. Default LXC shutdown timeout is plenty for an esphome compile.
3. **Cooldown gates scale-down only.** Scale-up always fires immediately so a queue burst gets compute as fast as the LXCs can boot. Scale-down waits `cooldown_seconds` (default 600s, matching the k8s chart). First scale-down on a fresh process is NOT cooldown-gated — otherwise the scaler would never scale down on first startup.
4. **VMIDs are started low-to-high, stopped high-to-low.** Symmetric and deterministic. Operator's mental model: "the first slot in the pool is the one that wakes first."
5. **Fleet failure → tick is a noop.** If the Fleet server is unreachable, we log and skip — no Proxmox writes happen. Avoids the failure mode where a network blip causes the scaler to nuke the pool because it thinks the queue is 0.
6. **Token IDs.** Proxmox API tokens are `<user>@<realm>!<token-name>` + a separate secret. We accept the full token-id string and split it client-side. This is the format the Proxmox UI shows when you create a token.

## Open questions for you

1. **Path:** `deploy/proxmox/scaler/`. Sibling to `deploy/kubernetes/`. OK?
2. **Should we ship a Helm chart for the scaler itself** (so a user with both Proxmox + a small k8s cluster could run the scaler in the cluster talking to Proxmox)? My take: leave that to systemd/Docker — running k8s in front of Proxmox just to run a scaler is overkill. But the Dockerfile follow-up makes either workable.
3. **Server-side change to expose pool capacity?** Not strictly needed — the scaler reads `queue_size` and reconciles its own pool. But a future enhancement could expose `total_slots_online` so a fleet-wide UI could show "you have 6 worker slots online but a queue of 20".
4. **Graceful drain endpoint.** `POST /api/v1/workers/{id}/control` already exists for worker control. Would you accept adding a `disabled=true` toggle path the scaler could call before LXC shutdown, so the worker stops claiming new jobs cleanly while still finishing the current one? Out of scope here, but worth flagging — the LXC shutdown grace + the worker's heartbeat-driven graceful exit on disable are belt-and-suspenders.

## Test plan

- [x] `pytest deploy/proxmox/scaler/tests/` — all unit tests pass against mocked clients
- [ ] Live test against a Proxmox sandbox (homelab, not CI)
- [ ] Dockerfile + docker-compose
- [ ] systemd unit
- [ ] CI workflow

## Out of scope here

- Provisioning the LXC pool itself (operator does this once).
- Cross-Proxmox-node scaling (this scaler manages one node's pool; a multi-node variant is conceptually a follow-up).
- Cluster-mode (Proxmox VE cluster) failover.
- Backups, monitoring, metrics.
