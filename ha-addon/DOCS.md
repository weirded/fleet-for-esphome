# Fleet for ESPHome

> Previously known as **ESPHome Fleet** (1.5.0–1.7.0). <!-- br1-allow: rebrand-history hint, removed for 1.8 -->

> **Not an official ESPHome project.** Fleet for ESPHome is an independent, community-built tool that depends on [ESPHome](https://esphome.io) but is not part of the ESPHome project and is not maintained by the ESPHome team. "ESPHome" is a trademark of its respective owners; this project uses the name only to describe what it works with.

A modern Home Assistant UI for ESPHome — works just as well for three devices as for a hundred. Git-backed config history with one-click rollback, a real device table, a live compile queue with searchable history, an inline YAML editor with autocomplete, per-device ESPHome version pinning, scheduled OTA upgrades, and optional distributed compilation when a slow HA host becomes the bottleneck.

## Getting Started

Start the add-on, then open the web UI via the **Fleet for ESPHome** entry in the HA sidebar.

From 1.6.2, installing this add-on pulls a prebuilt image from GitHub Container Registry instead of building locally on your Home Assistant host. Installs now finish in a few seconds instead of a few minutes, and no longer fail when Docker Hub is rate-limiting or briefly unreachable.

**ESPHome 2026.4 or newer is required.** The add-on uses ESPHome's built-in bundle format to ship only each device's own YAML + referenced secrets to the compiling worker — older ESPHome versions don't have that API. Pinning a device to an ESPHome version older than 2026.4 is refused; the UI will surface a clear "version too old" error rather than hang.

### First steps

1. Your existing ESPHome configs in `/config/esphome/` are picked up automatically — you should see them on the **Devices** tab.
2. The add-on includes a **built-in local worker** that runs inside the HA host. It starts with one build slot — enough to compile any target sequentially. On a fast host you can raise it to 2–4+ via the `+`/`-` buttons next to `local-worker` on the **Workers** tab. Setting slots to 0 pauses the local worker entirely (useful if you've connected remote workers and want the HA host out of the build loop).
3. To offload compilation to a faster machine, click **+ Connect Worker** in the Workers tab. Pick **Bash**, **PowerShell**, or **Docker Compose**, copy the generated snippet, and run it on whatever machine you want to compile on. The snippet includes your actual server URL and token, so there's nothing to edit. Workers poll the add-on over HTTP for jobs (bearer token auth) and push firmware directly to ESP devices; no inbound ports need to be open on the worker machine, but it does need network reach to the ESP devices it'll flash. **Thread/Matter devices are an exception** — their IPv6 mesh is only reachable from the HA host, so the add-on performs the OTA push for those targets automatically; any worker can still do the compile.
4. **Restart Home Assistant** once after the first install. The add-on ships a custom HA integration (`esphome_fleet`) that it auto-installs to `/config/custom_components/` on startup — but Home Assistant only loads integrations at Core startup, so the integration stays dormant until you restart HA. Go to **Settings → System → Restart** and pick *Restart Home Assistant*.
5. After the restart, Home Assistant will pop a "Fleet for ESPHome discovered" notification within a few seconds. Accept it to get all the devices, workers, and the add-on itself as real HA devices with entities.

> **Upgrading the add-on later?** If a Fleet release changes the integration (check the changelog — look for the `Integration` heading), you'll need to restart Home Assistant again after the add-on finishes updating. Restarting *the add-on* alone doesn't pick up integration changes, because HA Core only loads Python integrations at boot.

### Add-on configuration

Everything is configured from the **Settings drawer** inside the web UI — click the gear icon in the top-right of the header. The Supervisor **Configuration** tab is intentionally empty; every option moved into the drawer so edits apply immediately without restarting the add-on. Settings are split into **Basic** (what you touch most — versioning, authentication, display) and **Advanced** (retention, cache, timeouts, polling) tabs. Each field carries inline help text describing what it does and the valid range, so there's no need to mirror the settings reference here.

Settings persist across add-on updates. When upgrading from a pre-1.6 release, your existing Supervisor options (token, timeouts, thresholds, `require_ha_auth`) are one-shot imported on first boot so nothing you set before is lost.

## What's on the Web UI

**Devices.** Every ESPHome config in one place. Columns for online status, **tags**, current firmware version, HA entity link, IP address, WiFi vs Ethernet, last-compiled time, schedule, and ESPHome version. Optional columns for chip platform + PlatformIO board, BLE-proxy state, and a few more — opt in via the column picker. Click Upgrade on any row to compile + OTA that device. The row menu (⋮) exposes live logs, restart, ping, install-to-address, rendered config view, config history, rename, duplicate, pin, archive, delete, and copy-api-key (for devices with a native-API encryption key). Toggle **Show archived devices** in the column-picker dropdown to inline archived rows at 50 % opacity.

**Queue.** Every compile job — pending, running, succeeded, failed, or **blocked** (no eligible worker satisfies the active routing rules). Live build logs. Inline Rerun · Clear · Log on every row; everything else (cancel, download firmware, edit YAML, plus the device-section actions) lives behind the per-row hamburger. The worker-selection cell stacks **what the user asked for** (any worker / specific worker / tag expression) on top, **why this worker won** below.

**Workers.** Every connected worker — local and remote — with platform info, **tags**, slot count, **disk quota usage** (e.g. `Quota: 2.1 / 10 GiB`), current job, and uptime. **Routing rules…** opens a builder for fleet-wide rules backed by device + worker tags. **Set disk quota…** in the per-row hamburger overrides the fleet-default disk quota for one worker. Workers self-pause when their disk crosses 95 % and un-pause below 90 %, surfacing a **disk full** badge in the Status column. The worker's Python source refreshes itself automatically when the add-on upgrades; its underlying Docker image only refreshes when you run `docker pull && docker restart`, and workers running an outdated image are flagged with an **Image stale** badge telling you when that's time.

**Schedules.** Every scheduled upgrade in one view. Recurring (daily/weekly/monthly or full cron) and one-time future schedules. Schedules live in the device YAML itself so they travel with your config and respect each device's pinned ESPHome version.

**Header** has a dark/light theme toggle, a "streamer mode" that blurs tokens and secrets (for screen-sharing demos), the currently-selected ESPHome version (changes for all new compiles unless overridden per-device via pinning), a shortcut to edit `secrets.yaml`, and a link to [ESPHome Web](https://web.esphome.io/) for browser-based initial flashing.

### Running different ESPHome versions across your fleet

The header dropdown sets the **global** ESPHome version — every new compile uses it unless a device is pinned. To pin a device, open the row menu (⋮) on the **Devices** tab and choose **Pin ESPHome version**. Pinned devices stick to their version regardless of what the global selector says; scheduled upgrades on a pinned device respect its pin.

Typical uses:

- **Beta-test a release** on one low-stakes device (a garage sensor, an outdoor thermometer) while leaving the rest of the fleet on the stable version.
- **Hold a picky device back** on a known-good version indefinitely when a newer ESPHome release breaks a component you depend on.
- **Stage an upgrade** — flip the global version, compile one device, verify, then bulk-upgrade everything outdated.

Workers install whatever ESPHome version each job asks for, on demand, into a local per-version cache so subsequent jobs using that version start instantly. The cache is bounded by the per-worker disk quota (default 10 GiB, change in **Settings → Disk management**) and evicts the oldest things first when the budget fills up — see the **Workers tab** for live disk usage.

### Keeping workers up to date

The worker's Python source auto-updates from the server — every heartbeat negotiates the current client source revision and the worker rewrites its `.py` files in place when the server's copy is newer. Bug fixes and protocol-compatible additions to client code reach every worker (local and remote) without you touching the container. The **Docker image** is the part that doesn't auto-update: system packages (gcc, git, libffi), the Python interpreter itself, and hash-pinned dependencies only refresh when you pull a new image. The built-in local worker picks up a fresh image whenever the add-on upgrades; a remote worker you started with `docker run` stays on whatever image tag you pulled until you refresh it yourself.

**When you need to refresh.** Only when the Workers tab flags the worker with an **Image stale** badge. Below that threshold a fresh image is the only fix; the add-on's automatic source-code updates won't bring the worker back into compatibility on their own.

**How to tell.** Open the **Workers** tab. Stale workers carry an **Image stale** badge next to the hostname; hover to see the exact refresh command. The per-row version cell shows the client's baked-in image version.

**Refresh command (Linux / Mac / Windows with Docker CLI):**

```bash
docker pull ghcr.io/weirded/esphome-dist-client:latest
docker restart <your-worker-container-name>
```

The `<your-worker-container-name>` is whatever you passed via `--name` when you first ran the container — `docker ps --format '{{.Names}}'` lists them. The restart reuses the old container's volumes and env vars, so the worker reconnects with the same token and hostname.

**Docker Compose variant:**

```bash
docker compose pull
docker compose up -d
```

(Run it in the directory that has your `docker-compose.yaml`. Compose detects that the image changed and rebuilds the container in-place.)

**Full re-install** (needed when upgrading across a `MIN_IMAGE_VERSION` bump, or when changing host platform, max-parallel-jobs, or token): remove the old container and re-run the snippet from **+ Connect Worker**. The built-in Connect Worker modal always emits a snippet that matches the currently-deployed server.

**Automating refreshes.** We deliberately don't ship an auto-update mechanism — every option adds a dependency (Watchtower, What's Up Docker, Compose + cron, Kubernetes controllers…) we'd then have to support. Pick whatever scheduler you already use on the host and have it run the refresh command on a cadence you're comfortable with. We don't endorse a specific tool.

## Verifying what you're running

Every server and client image on GHCR is signed with [cosign](https://docs.sigstore.dev/) using GitHub's keyless OIDC flow (no long-lived keys anywhere). You can verify that the image you pulled is the one this repo built:

```bash
# Server image
cosign verify \
  --certificate-identity-regexp 'https://github.com/weirded/distributed-esphome/.github/workflows/publish-server\.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/weirded/esphome-dist-server:latest

# Worker image
cosign verify \
  --certificate-identity-regexp 'https://github.com/weirded/distributed-esphome/.github/workflows/publish-client\.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/weirded/esphome-dist-client:latest
```

A successful verification prints the OIDC claims (workflow ref, run ID, commit SHA). Run this once before you trust an image in production, or wire it into your container-pull automation.

### Checking the software bill of materials

Every 1.5.0+ image also carries a CycloneDX SBOM as a cosign attestation — the full list of Python packages, OS libraries, and their pinned versions that went into the image. Handy for CVE audits.

```bash
# Server image — download + print the SBOM
cosign verify-attestation \
  --certificate-identity-regexp 'https://github.com/weirded/distributed-esphome/.github/workflows/publish-server\.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --type cyclonedx \
  ghcr.io/weirded/esphome-dist-server:latest \
  | jq -r '.payload | @base64d | fromjson | .predicate' \
  > esphome-dist-server.sbom.json

# Worker image
cosign verify-attestation \
  --certificate-identity-regexp 'https://github.com/weirded/distributed-esphome/.github/workflows/publish-client\.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --type cyclonedx \
  ghcr.io/weirded/esphome-dist-client:latest \
  | jq -r '.payload | @base64d | fromjson | .predicate' \
  > esphome-dist-client.sbom.json
```

## Why this add-on requests these permissions

Home Assistant's add-on store shows each add-on's "stars" — a score based on how little Supervisor privilege it asks for. Fleet for ESPHome will never be a five-star add-on because managing a fleet of ESPHome devices genuinely requires a handful of elevated permissions. This section documents each one so the lower score is understood rather than mysterious.

- **`host_network: true`** — mDNS device discovery needs to see the LAN's `_esphomelib._tcp` broadcasts, which the default bridge network on a Supervisor add-on doesn't expose. Without host networking, Fleet wouldn't automatically discover ESPHome devices on your network; you'd have to hand-enter each IP. This is the single biggest "why not five stars" item, and it's load-bearing.
- **`hassio_api: true`** — used to query the Supervisor for the currently-installed ESPHome add-on version (so Fleet's default compile version tracks whatever you have installed) and to post discovery entries so the HA integration auto-pairs. Read + narrow-scoped writes only.
- **`homeassistant_api: true`** — reads entity states from HA Core to wire each managed ESPHome device to its matching HA device page (the Devices tab's "HA" column is the result). Read-only — Fleet never writes back into HA.
- **`auth_api: true`** — used to validate Home Assistant long-lived access tokens on the direct-port API (`:8765`) so scripts and `curl` can authenticate with per-user credentials instead of the shared server token. The browser UI doesn't need this — it comes in through Ingress, which is already HA-authenticated.
- **`privileged: [NET_RAW]`** — needed by the per-device **Ping** diagnostic on Home Assistant OS, where unprivileged ICMP is disabled by default. The ping helper tries the safer unprivileged path first and only falls back when the kernel refuses it. Scoped to ICMP — the endpoint accepts only the configured device's address, never an arbitrary host. This is the only non-default Linux capability the add-on holds.
- **`map: [config:rw]`** — Fleet's whole premise is editing `/config/esphome/`. Read access finds the YAML targets; write access is needed by the inline editor, the automatic git history, rename/delete operations, and the archive/restore flow. Scoped to `/config` — Fleet has no way to reach other parts of your HA configuration.

Everything else stays at Supervisor's defaults. The `options` / `schema` blocks are intentionally empty because every user-facing setting lives in the in-app Settings drawer (editable without an add-on restart); see the Add-on configuration section above.

## Support

If this add-on has saved you time or frustration, you can support continued development:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-orange?logo=buy-me-a-coffee&logoColor=white&style=for-the-badge)](https://buymeacoffee.com/weirded)
