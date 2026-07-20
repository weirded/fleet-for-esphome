# esphome-fleet-server Helm chart

Run the **Fleet for ESPHome server** (distributed-esphome) on a Kubernetes cluster — the web UI, job queue, and ESPHome device discovery — for users who don't run it as a Home Assistant add-on.

Pair it with the sibling [`esphome-fleet-worker`](../README.md) chart for build workers, or run workers as Docker containers — both shapes register with this server identically.

## Quick start

```bash
helm install fleet-server ./deploy/kubernetes/server \
  --namespace esphome-fleet --create-namespace \
  --set server.token="$(openssl rand -hex 32)"
```

Then reach the UI:

```bash
kubectl -n esphome-fleet port-forward svc/fleet-server 8765:8765
# open http://127.0.0.1:8765
```

Use the **same `server.token`** when you install `esphome-fleet-worker`, and point the workers at `http://fleet-server.esphome-fleet.svc:8765`.

## What it deploys

A single-replica `Deployment` (the server owns local state, so it is a singleton) running `ghcr.io/weirded/esphome-dist-server`, plus:

- a `Service` on port `8765`,
- two `PersistentVolumeClaim`s — `/config/esphome` (ESPHome YAML + git edit history) and `/data` (job queue, settings, cached ESPHome version venvs),
- an optional `Ingress`.

The pod runs **non-root** by default (`runAsUser: 1000`, `fsGroup: 1000`). Two environment knobs make that work with the mounted volumes:

- `UV_CACHE_DIR=/tmp/.uv` — keeps uv's package cache on a writable path.
- `GIT_CONFIG_PARAMETERS='safe.directory=/config/esphome'` — the server keeps a git history of config edits; this stops git's "dubious ownership" refusal when the PVC uid differs from the process uid.

The `Deployment` uses the `Recreate` strategy: the RWO PVCs cannot multi-attach, so the old pod must terminate before the new one starts on upgrade.

## Persistence

Both volumes default to `ReadWriteOnce` PVCs and **must** persist — losing `/config/esphome` loses your device YAML and its edit history; losing `/data` loses the job queue and settings. Set `persistence.<config|data>.enabled: false` only for throwaway test installs (falls back to `emptyDir`).

To keep a PVC across `helm uninstall`, add the resource-policy annotation:

```yaml
persistence:
  config:
    annotations:
      helm.sh/resource-policy: keep
```

## Ingress

Disabled by default. Enable it to expose the UI:

```yaml
ingress:
  enabled: true
  className: nginx
  hosts:
    - host: fleet.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: fleet-tls
      hosts:
        - fleet.example.com
```

## Values reference

See `values.yaml` for the full list. The most-touched fields:

| Path | Default | Notes |
| --- | --- | --- |
| `server.token` | `""` | **Required** (or `existingSecret`). Shared bearer token; workers must match it. |
| `server.existingSecret` | `""` | Name of a Secret with key `SERVER_TOKEN`. Skip `server.token` when set. |
| `image.repository` | `ghcr.io/weirded/esphome-dist-server` | |
| `service.type` | `ClusterIP` | `NodePort` / `LoadBalancer` also valid. |
| `service.port` | `8765` | |
| `ingress.enabled` | `false` | See Ingress section. |
| `persistence.config.size` | `2Gi` | `/config/esphome`. |
| `persistence.data.size` | `10Gi` | `/data` — includes cached ESPHome venvs. |
| `terminationGracePeriodSeconds` | `60` | |

## Uninstall

```bash
helm uninstall fleet-server -n esphome-fleet
```

This deletes the PVCs too unless you set the `helm.sh/resource-policy: keep` annotation above.

## Compatibility

- Kubernetes ≥ 1.25.
- Server image is multi-arch (`amd64`, `arm64`).
