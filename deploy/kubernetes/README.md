# esphome-fleet-worker Helm chart

Run distributed-esphome **build workers** on a Kubernetes cluster, optionally autoscaling on the server's queue depth via KEDA.

The chart deploys workers only — the server still runs as the Home Assistant add-on (or a standalone Docker container; both shapes work). Workers reach the server's worker API at `<server.url>/api/v1/*` over HTTP, identical to a Docker-based worker.

## Quick start

```bash
helm install fleet-worker ./deploy/kubernetes \
  --namespace esphome-fleet --create-namespace \
  --set server.url=http://homeassistant.local:8765 \
  --set server.token=<paste-from-fleet-settings-drawer>
```

That gives you a single-replica Deployment registered with the server. Open the Fleet UI's **Workers** tab to confirm the pod shows up.

## Autoscaling on queue depth

Enable [KEDA](https://keda.sh/) (install separately) and set:

```yaml
autoscaling:
  enabled: true
  minReplicaCount: 0
  maxReplicaCount: 3
  cooldownPeriod: 600
  targetQueueSize: 2
```

KEDA polls `GET /api/v1/status` on the server every `pollingInterval` seconds, reads the `queue_size` field (= count of `PENDING` + `WORKING` jobs), and sets `desiredReplicas = ceil(queue_size / targetQueueSize)` capped at `maxReplicaCount`.

When `queue_size` returns to zero, KEDA waits `cooldownPeriod` seconds before scaling back down to `minReplicaCount`.

### Important: replicas cap at *worker* count, not *job* count

distributed-esphome's queue + each worker's `MAX_PARALLEL_JOBS` already handle oversubscription. With `maxReplicaCount: 3` and `worker.maxParallelJobs: 2`, a queue of 100 jobs is drained by 3 pods running 2 jobs each in parallel — *not* 100 pods. Set `maxReplicaCount` to the number of physical worker slots you want to utilize (typically the size of the target node pool).

### Important: KEDA has no scale-up timeout

When KEDA scales up, pods sit `Pending` until the Kubernetes scheduler can place them. There is no per-pod "wake the node" timeout (and the chart deliberately does not set `activeDeadlineSeconds`). This means the chart works fine on clusters where node provisioning is slow — Cluster Autoscaler, Karpenter, Cluster API, Wake-on-LAN, even Talos nodes that need to be booted by hand. The pod just waits.

`cooldownPeriod` only governs scale-*down*. Tune it to match your underlying node-idle threshold (e.g., 600s if your nodes sleep after 10 minutes idle).

### Always-on workers

Set `autoscaling.minReplicaCount: 1` (or higher). The chart treats this as a baseline — workers above the baseline scale on demand, the baseline ones stay running.

## Networking

Workers initiate two outbound flows:

1. **Server worker API** — `<server.url>/api/v1/*` over HTTP/HTTPS. Bearer-token auth.
2. **OTA upload to ESP devices** — direct to the device's IP on whatever port ESPHome's native API uses (typically TCP 6053). Workers run the actual `esphome run` invocation.

By default the chart uses pod networking. If your pods cannot reach the ESP devices' L3 segment (different VLAN, different subnet, no L3 route), you have a few options:

- **`hostNetwork: true`** — workers run on the host network of their node. Simplest fix when nodes themselves can reach the device segment but pods can't (e.g., cluster CNI without proper egress to that subnet).
- **Multus + secondary interface** — attach a second NIC to worker pods on the device VLAN. More complex but cleaner separation.
- **Egress route** — add a static route in your CNI / network policy that lets pod CIDR talk to device CIDR. This is the most "kubernetes-native" answer.

The chart does not pick for you.

## Cache disk

`/esphome-versions/` holds ESPHome version venvs (one per active version). First compile per-version costs ~30 seconds for `pip install esphome==<v>`; subsequent compiles in the same version are instant.

- `persistence.enabled: true` (default) — uses a PVC. Cache survives pod restarts. **Only works in non-autoscaling mode** (a Deployment can't template per-replica PVCs without becoming a StatefulSet, which complicates things for too small a win).
- `persistence.enabled: false`, OR `autoscaling.enabled: true` — uses `emptyDir`. Fresh cache per pod restart / scale-up. Acceptable; first compile of a burst is ~30s slower.

If you really want persistent per-replica caches with autoscaling, switch to a StatefulSet workload manually for now. A StatefulSet variant of this chart may land later.

## Values reference

See `values.yaml` for the full list. The most-touched fields:

| Path | Default | Notes |
| --- | --- | --- |
| `server.url` | `""` | **Required.** e.g. `http://homeassistant.local:8765` |
| `server.token` | `""` | Bearer token from Fleet Settings drawer. Use `existingSecret` instead in production. |
| `server.existingSecret` | `""` | Name of a Secret with key `SERVER_TOKEN`. Skip `server.token` when set. |
| `replicas` | `1` | Used when `autoscaling.enabled=false`. |
| `autoscaling.enabled` | `false` | Requires KEDA installed in cluster. |
| `autoscaling.minReplicaCount` | `0` | Set to `1` for an always-on baseline. |
| `autoscaling.maxReplicaCount` | `3` | Cap to your worker-slot count. |
| `autoscaling.cooldownPeriod` | `600` | Seconds before scale-down once queue is empty. |
| `autoscaling.targetQueueSize` | `2` | Desired jobs per worker. KEDA: `ceil(queue_size / targetQueueSize)`. |
| `worker.tags` | `["k8s","os:linux"]` | Show up in the Fleet UI; usable in routing rules. |
| `worker.maxParallelJobs` | `2` | Per-pod parallelism. |
| `persistence.enabled` | `true` | Static PVC; only honored in non-autoscaling mode. |
| `persistence.size` | `10Gi` | |
| `nodeSelector`, `tolerations`, `affinity` | `{}` / `[]` | Standard. |
| `hostNetwork` | `false` | See Networking section. |
| `terminationGracePeriodSeconds` | `600` | Mid-compile pods get 10 min before SIGKILL. |

## Uninstall

```bash
helm uninstall fleet-worker -n esphome-fleet
```

The PVC is **not** deleted automatically (Helm doesn't delete PVCs created from a `volumeClaimTemplate`-style flow). Delete manually if you want the disk freed:

```bash
kubectl -n esphome-fleet delete pvc -l app.kubernetes.io/instance=fleet-worker
```

## Compatibility

- Kubernetes ≥ 1.25 (tested on 1.30, 1.32).
- Optional: KEDA ≥ 2.13 for autoscaling.
- Worker image is multi-arch (`amd64`, `arm64`); pods schedule on whichever architecture your nodes provide.
