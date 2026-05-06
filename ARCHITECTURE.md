# Fleet for ESPHome — Architecture

A single-page map of how the pieces fit together. Aimed at outside readers opening the repo for the first time; for the deep internal conventions (invariants, the end-of-turn loop, design-judgment rules) read `CLAUDE.md`.

## What this is

Fleet for ESPHome is a Home Assistant add-on that manages a fleet of ESPHome devices. It discovers the ESPHome YAMLs already in `/config/esphome/`, runs a web UI that talks to them, and offloads firmware compilation to one or more build workers. Workers are Docker containers — the first one runs inside the add-on itself; any number of additional ones run on other machines (a gaming PC, a NAS, a mini-PC) for faster compiles.

## Three tiers

```
  Browser        │  Home Assistant host            │  Other machines
─────────────────┼─────────────────────────────────┼───────────────────────
                 │                                 │
  Web UI  ──────►│  Server (ha-addon/server/)  ◄───│──  Worker(s)
                 │    + built-in local worker      │     (ha-addon/client/)
                 │         │                       │        │
                 │         │ OTA (Thread/Matter    │        │ OTA (WiFi/Ethernet
                 │         │  — IPv6 mesh only     │        │  — worker needs
                 │         │  reachable from HA)   │        │  LAN reach to ESP)
                 │         ▼                       │        ▼
                 │    ESP Thread devices           │     ESP devices
```

### Server (`ha-addon/server/`)

An `aiohttp` async app. It's the only thing users interact with directly; everything else is downstream.

- `main.py` wires up the app, middleware chain, and background loops (queue timeout checker, HA entity poller, device poller, firmware-budget enforcer, job-history retention). Also spawns the built-in local worker as a subprocess on startup.
- `api.py` — the **worker API** at `/api/v1/*`. Workers register, heartbeat, claim jobs, post results, and stream build logs here. Bearer-token authenticated (shared secret).
- `ui_api.py` — the **browser API** at `/ui/api/*`. Every surface in the web UI (devices, queue, history, settings, versioning) reads from here. Authenticated via Home Assistant Ingress trust — if the request arrived through the Ingress tunnel, it's already HA-authenticated.
- `job_queue.py` — in-memory job queue persisted to `/data/queue.json`. Drives the `PENDING → WORKING → SUCCESS | FAILED | TIMED_OUT` state machine, retries, coalescing, and cancellation.
- `scanner.py` — discovers `.yaml` targets in `/config/esphome/`, bundles the config directory for a worker, and lazy-installs the ESPHome version the add-on reports.
- `registry.py` — tracks connected workers (live heartbeats, active job count, image version, requested slot count). Pure in-memory; no persistence.
- `device_poller.py` — discovers ESPHome devices via mDNS and polls them over `aioesphomeapi` for their running firmware version.
- `git_versioning.py` — the config-versioning engine that landed in 1.6: debounced auto-commit on every user-initiated write, `git mv` on archive/restore, path-traversal-safe wrappers around every write.
- `job_history.py` — persistent SQLite-backed compile history (JH.* in WORKITEMS-1.6). Survives queue coalescing and Clear.
- `settings.py` — `/data/settings.json` store for every user-editable setting, validated per-field and live-read by downstream consumers on each tick.
- `protocol.py` — the single source of truth for server↔worker wire messages (pydantic v2). A byte-identical copy ships in `ha-addon/client/protocol.py`; a test asserts they match.
- `static/` — the Vite-built React app. Source in `ha-addon/ui/`.

### Worker (`ha-addon/client/`)

A small synchronous polling loop. Registers with the server, polls for jobs, ensures the correct ESPHome version is installed into its own `/esphome-versions/<version>/` venv cache, extracts the bundle, runs `esphome run`, and OTA-uploads the firmware directly to the target ESP. Heartbeats on a background thread. Because the worker does the OTA upload, it needs network reach to the ESP devices on its own LAN segment.

**Exception — Thread/Matter devices:** Thread devices use an IPv6 mesh that is only reachable from the HA host, not from remote workers on a different subnet. For these targets the worker compiles and uploads the binary to the server (same as the `download_only` path), and then the server performs the OTA push via `esphome upload`. Auto-detected via the `openthread:` block in the device YAML; any worker can claim the compile job.

Workers auto-update the Python source at every heartbeat (the server pushes any source changes that match the worker's `IMAGE_VERSION`). The Docker image itself only refreshes when the operator runs `docker pull`; a minimum-image-version gate on the server refuses source-code pushes to an image that's too old to apply them safely.

### Frontend (`ha-addon/ui/`)

React 19 + TypeScript + Vite + Tailwind + shadcn/ui (Base-UI primitives). SWR polls the server at 1 Hz for the live-feel surfaces (workers, devices, queue). TanStack Table drives every tabular view (Devices, Queue, Queue History, Schedules, Workers).

All HTTP calls live in `src/api/client.ts` — components never call `fetch()` directly, enforced by an invariant that fails CI if they do. Shared types in `src/types/index.ts`; Playwright fixtures import those types so a field rename breaks the tests.

The add-on's built-in integration (`ha-addon/custom_integration/esphome_fleet/`) gets installed into `/config/custom_components/` on first boot; it discovers the add-on via mDNS, creates HA devices for every managed ESPHome target, and surfaces update entities, sensors, and service actions (`esphome_fleet.compile`, `.cancel`, `.validate`) on the HA side.

## Authentication model

Two distinct tiers:

- **Browser UI** (`/ui/api/*` + the SPA shell) → authenticated by Home Assistant Ingress. The add-on trusts anything that arrives on the Ingress tunnel because HA has already authenticated it.
- **Worker API** (`/api/v1/*`) → Bearer token. Every worker ships with a shared server token (the one visible in the Settings drawer). Requests from the Supervisor IP are also accepted so the HA integration can call without a token.

The direct-port API on `:8765` (outside the Ingress tunnel — used by scripts and `curl`) also requires the Bearer token, gated by a `require_ha_auth` setting that's on by default.

## Configuration

Post-1.6, all user-editable settings live in `/data/settings.json` and are edited via the Settings drawer in the web UI. The Supervisor Configuration tab is intentionally empty. A one-shot import on first boot carries pre-1.6 values across. Deployment-level fields (listen port, config dir) stay in `AppConfig` and come from env vars.

## Further reading

- **`CLAUDE.md`** — the long-form conventions document: enforced invariants, end-of-turn loop, PR review loop, design-judgment rules that aren't mechanically checkable.
- **`dev-plans/README.md`** — the release index. Each `WORKITEMS-X.Y.md` mixes feature work items and bug fixes for that release; closed releases are archived.
- **`dev-plans/RELEASE_CHECKLIST.md`** — step-by-step for cutting a stable release.
- **`dev-plans/SECURITY_AUDIT.md`** — the threat model + every F-N finding with current status.
- **`CONTRIBUTING.md`** — shorter "how do I run tests and open a PR" walkthrough.
