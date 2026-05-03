# PR draft — Kubernetes deployment chart for build workers

Adds a Helm chart at `deploy/kubernetes/` that lets users run distributed-esphome **build workers** on a Kubernetes cluster, optionally autoscaling on the server's queue depth via KEDA. Targets users with existing clusters who want to offload compilation without running another always-on host.

The chart deploys workers only — the server still runs as the HA add-on (or a standalone Docker container; both shapes are supported). Workers reach the server's worker API at `<server_url>/api/v1/*` over HTTP, identical to a Docker-based worker today.

## Design choices

- **Pure replica scaling.** The chart has zero awareness of node-level state (sleep/wake/cordon). It exposes `minReplicaCount` and `maxReplicaCount` and lets the cluster handle the rest. Power-management features like Wake-on-LAN node autoscaling are explicitly out of scope and live in the user's own platform layer.
- **Replicas cap at worker count, not job count.** `maxReplicaCount` should be set to the number of physical worker slots the user wants utilized (typically equal to the number of nodes in the target pool, or 2× if `MAX_PARALLEL_JOBS=2`). distributed-esphome's queue plus per-worker `MAX_PARALLEL_JOBS` already handle oversubscription — N workers will drain a queue of 100 jobs sequentially, no need to spawn 100 pods. README will hammer this point.
- **KEDA optional.** With `autoscaling.enabled=false` the chart is just a fixed-replica `Deployment` with `replicas: {{ .Values.replicas }}`. With `autoscaling.enabled=true` we deploy a `ScaledObject` against a `metrics-api` scaler hitting `/api/v1/status` (Bearer auth) and reading `queue_size`.
- **No KEDA scale-up timeout.** When KEDA scales the Deployment up, pods sit `Pending` until the scheduler can place them. There's no per-pod "wake the node" timeout in Kubernetes — the pod stays Pending forever (unless a deadline is explicitly set, which the chart does NOT set). This means the chart works fine on clusters where node provisioning is slow (cluster autoscaler, WoL, Cluster API, Karpenter). Documented in README.
- **`cooldownPeriod` (default 600s)** governs how long after `queue_size` returns to zero before KEDA scales replicas back down. Tunable via `autoscaling.cooldownPeriod`. Chart README points users at their own node-sleep idle threshold for tuning.
- **Server token** lives in a `Secret` the chart creates from `serverToken` in values, OR references an existing Secret via `existingSecret`. README discourages putting the token in the values file directly; recommends sealed-secrets / SOPS / external-secrets.
- **PVC for ESPHome version cache.** `/esphome-versions/` per pod, default 10Gi RWO. Configurable; can be set to `emptyDir` for stateless workers (re-downloads the venv on cold start, ~30s for ESPHome's pip install).
- **`terminationGracePeriodSeconds: 600`** so a pod mid-compile has time to finish before SIGKILL on scale-down.
- **`topologySpreadConstraints`** is exposed in values as an empty list; users supply their own. The chart does NOT inject a default spread constraint — clusters with one node would always violate `maxSkew: 1`. (Earlier drafts suggested a built-in default; that's not implemented and the design favors letting users opt in based on their topology.)
- **Bring-your-own networking.** Chart does NOT set `hostNetwork: true` by default; assumes pod networking can route to the ESP devices' IPs. Users with pods on a different L3 segment from devices can either set `hostNetwork: true` via values or use a CNI feature like `Multus` to attach a second interface. README has a troubleshooting section.

## Chart layout

```
deploy/kubernetes/
├── Chart.yaml
├── values.yaml
├── values.schema.json
├── README.md
├── ci/
│   ├── values-minimal.yaml          # fixed replicas, no KEDA
│   ├── values-autoscale.yaml        # KEDA enabled
│   └── values-existing-secret.yaml  # reference an external Secret
└── templates/
    ├── _helpers.tpl
    ├── NOTES.txt
    ├── serviceaccount.yaml
    ├── secret.yaml                   # only if .Values.serverToken set (else expects existingSecret)
    ├── deployment.yaml
    ├── pvc.yaml                      # only if .Values.persistence.enabled
    ├── scaledobject.yaml             # only if .Values.autoscaling.enabled
    └── triggerauthentication.yaml    # KEDA TriggerAuthentication for the bearer token
```

## values.yaml shape (preview)

```yaml
image:
  repository: ghcr.io/weirded/esphome-dist-client
  tag: ""    # default: appVersion from Chart.yaml
  pullPolicy: IfNotPresent

server:
  url: ""                          # required, e.g. http://homeassistant.local:8765
  token: ""                        # set this OR existingSecret
  existingSecret: ""               # name of a Secret containing the token
  existingSecretTokenKey: SERVER_TOKEN

worker:
  tags: ["k8s", "os:linux"]
  tagsOverwrite: false
  maxParallelJobs: 2
  diskQuotaGb: 0
  minFreeDiskPct: 10
  pollInterval: 1
  heartbeatInterval: 10
  jobTimeout: 600
  otaTimeout: 120
  extraEnv: []

replicas: 1              # used when autoscaling.enabled is false

autoscaling:
  enabled: false
  minReplicaCount: 0
  maxReplicaCount: 3
  cooldownPeriod: 600
  pollingInterval: 30
  targetQueueSize: 2
  metricsApi:
    insecureTLSSkipVerify: false   # only meaningful when server.url is https

persistence:
  enabled: true
  storageClass: ""
  size: 10Gi
  accessModes: [ReadWriteOnce]
  annotations: {}                   # use helm.sh/resource-policy: keep to preserve on uninstall

resources:
  requests:
    cpu: 200m
    memory: 512Mi
  limits:
    memory: 2Gi

nodeSelector: {}
tolerations: []
affinity: {}
topologySpreadConstraints: []
hostNetwork: false
dnsPolicy: ""
priorityClassName: ""

podLabels: {}                       # at root, NOT under worker:
podAnnotations: {}                  # at root, NOT under worker:
podSecurityContext: {}
securityContext: {}

serviceAccount:
  create: true
  name: ""
  annotations: {}

terminationGracePeriodSeconds: 600
```

## Why a Helm chart and not raw manifests / kustomize

- KEDA `ScaledObject` (and `TriggerAuthentication`) need conditional rendering — kustomize patches get unwieldy when the resource itself is optional.
- Helm chart is the lingua franca for "I have a cluster and want to install your thing." ArgoCD, Flux, and `helm install` all consume it directly.
- Schema-validated values via `values.schema.json` catch typos at install time.

We can ship raw manifests too (just `helm template ... > kubernetes-manifests.yaml`) for users who explicitly don't want Helm; documented in README.

## Tests

- **`helm lint`** in CI on every PR.
- **`helm template`** unit tests via `helm-unittest` for:
  - Default values produce a valid Deployment.
  - `autoscaling.enabled=true` produces ScaledObject + TriggerAuthentication.
  - `existingSecret` skips the Secret template.
  - `hostNetwork=true` propagates correctly.
- No live cluster integration test in this PR; that needs a kind/k3d harness that doesn't yet exist in CI. Could follow up with a separate `chart-testing` GitHub Action.

## Open questions for the maintainer

1. Path: `deploy/kubernetes/` vs `charts/esphome-fleet-worker/` vs separate repo. I picked `deploy/kubernetes/` because it sits next to `docker-compose.yml` at the repo root — same "deployment artifacts" idea. Push back if you'd rather a `charts/` dir or even a separate repo for chart releases.
2. Chart publishing: do you want the chart pushed to GHCR as an OCI artifact on releases (`ghcr.io/weirded/charts/esphome-fleet-worker`)? Or keep it as a repo-only chart users install via `helm install ... ./deploy/kubernetes`?
3. The "always-on" use case: the chart already supports it via `autoscaling.minReplicaCount: 1`. Is that good, or would you prefer a separate `replicas` knob that's mutually exclusive with autoscaling? My take: one knob is simpler.
4. Should the chart also offer to deploy a **standalone server** (the `ghcr.io/weirded/esphome-dist-server` image) for users without HA? Out of scope here, but worth asking — it'd be a sibling chart `esphome-fleet-server`.

## Out of scope here

- Standalone-server chart (see #4).
- Cluster autoscaler / Karpenter / WoL integration.
- Per-tenant or multi-server worker setups.
- ARM64 worker images (the upstream `esphome-dist-client` image is multi-arch; the chart just inherits whatever the user's nodes can pull).
- A CRD-based ESPHomeWorker resource — too much abstraction for this iteration.
