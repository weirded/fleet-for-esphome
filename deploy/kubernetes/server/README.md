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

## Sidecars, init containers and extra volumes

Four values add pod-level wiring without forking the chart. Each is rendered verbatim (`toYaml`), so write plain Kubernetes syntax; all default to `[]`.

| Path | Goes into |
| --- | --- |
| `initContainers` | `spec.template.spec.initContainers` — run to completion before the server starts. |
| `extraContainers` | appended to `spec.template.spec.containers` — sidecars. |
| `extraVolumes` | appended to `spec.template.spec.volumes`. |
| `extraVolumeMounts` | appended to the **server** container's `volumeMounts`. |

Sidecars and init containers can mount `extraVolumes` and also the chart's own `config` (`/config/esphome`) and `data` (`/data`) volumes by name.

### Example: ESPHome YAML from a Git repo (git-sync)

Keep your device YAML in GitHub and let [git-sync](https://github.com/kubernetes/git-sync) pull it into the pod. A one-shot init container makes sure the checkout exists before the server starts; a sidecar keeps it fresh. The server reads the checkout via `ESPHOME_CONFIG_DIR`.

```yaml
server:
  extraEnv:
    - name: ESPHOME_CONFIG_DIR
      value: /git/esphome     # git-sync's symlink to the current checkout

extraVolumes:
  - name: esphome-git
    emptyDir: {}

extraVolumeMounts:
  - name: esphome-git
    mountPath: /git

initContainers:
  - name: git-sync-init
    image: registry.k8s.io/git-sync/git-sync:v4.4.0
    env:
      - { name: GITSYNC_REPO, value: https://github.com/you/esphome-configs }
      - { name: GITSYNC_REF, value: main }
      - { name: GITSYNC_ROOT, value: /git }
      - { name: GITSYNC_LINK, value: esphome }
      - { name: GITSYNC_ONE_TIME, value: "true" }
    volumeMounts:
      - { name: esphome-git, mountPath: /git }

extraContainers:
  - name: git-sync
    image: registry.k8s.io/git-sync/git-sync:v4.4.0
    env:
      - { name: GITSYNC_REPO, value: https://github.com/you/esphome-configs }
      - { name: GITSYNC_REF, value: main }
      - { name: GITSYNC_ROOT, value: /git }
      - { name: GITSYNC_LINK, value: esphome }
      - { name: GITSYNC_PERIOD, value: 60s }
    volumeMounts:
      - { name: esphome-git, mountPath: /git }
```

Notes:

- For a private repo, add credentials per the git-sync docs (e.g. `GITSYNC_USERNAME` + `GITSYNC_PASSWORD` from a Secret, or an SSH key volume).
- The repo is the source of truth: edits made in the Fleet UI are overwritten on the next sync. Make changes in Git, and turn config versioning off in the Fleet Settings so the server doesn't keep its own history on top of the checkout.
- ESPHome's `!secret` needs `secrets.yaml` next to the device YAML. If you keep it out of Git, plan for that (e.g. a private repo that includes it).
- With the checkout in place, `persistence.config` is unused; set `persistence.config.enabled: false` to skip the PVC.

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
| `initContainers`, `extraContainers`, `extraVolumes`, `extraVolumeMounts` | `[]` | See Sidecars section. |

## Uninstall

```bash
helm uninstall fleet-server -n esphome-fleet
```

This deletes the PVCs too unless you set the `helm.sh/resource-policy: keep` annotation above.

## Compatibility

- Kubernetes ≥ 1.25.
- Server image is multi-arch (`amd64`, `arm64`).
