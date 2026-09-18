# Fleet for ESPHome

> Previously known as **ESPHome Fleet** (1.5.0–1.7.0). <!-- br1-allow: rebrand-history hint, removed for 1.8 -->

> **Not an official ESPHome project.** Fleet for ESPHome is an independent, community-built tool that depends on [ESPHome](https://esphome.io) but is not part of the ESPHome project and is not maintained by the ESPHome team. "ESPHome" is a trademark of its respective owners; this project uses the name only to describe what it works with.

> This file covers the **Home Assistant add-on** — available on HAOS and HA Supervised installs only. Running HA Container or HA Core? See [Standalone Docker installation](../README.md#as-a-standalone-docker-container) instead.

A modern Home Assistant UI for ESPHome — works just as well for three devices as for a hundred. Git-backed config history with one-click rollback, a real device table, a live compile queue with searchable history, an inline YAML editor with autocomplete, per-device ESPHome version pinning, scheduled OTA upgrades, and optional distributed compilation when a slow HA host becomes the bottleneck.

## Getting Started

Start the add-on, then open the web UI via the **Fleet for ESPHome** entry in the HA sidebar.

From 1.6.2, installing this add-on pulls a prebuilt image from GitHub Container Registry instead of building locally on your Home Assistant host. Installs now finish in a few seconds instead of a few minutes, and no longer fail when Docker Hub is rate-limiting or briefly unreachable.

**ESPHome 2026.4 or newer is required.** The add-on uses ESPHome's built-in bundle format to ship only each device's own YAML + referenced secrets to the compiling worker — older ESPHome versions don't have that API. Pinning a device to an ESPHome version older than 2026.4 is refused; the UI will surface a clear "version too old" error rather than hang.

### First steps

1. Your existing ESPHome configs in `/config/esphome/` are picked up automatically — you should see them on the **Devices** tab.
2. The add-on includes a **built-in local worker** that runs inside the HA host. It starts with one build slot — enough to compile any target sequentially. On a fast host you can raise it to 2–4+ via the `+`/`-` buttons next to `local-worker` on the **Workers** tab. Setting slots to 0 pauses the local worker entirely (useful if you've connected remote workers and want the HA host out of the build loop).
3. To offload compilation to a faster machine, click **+ Connect Worker** in the Workers tab. Pick **Bash**, **PowerShell**, or **Docker Compose**, copy the generated snippet, and run it on whatever machine you want to compile on. The snippet includes your actual server URL and token, so there's nothing to edit. Workers poll the add-on over HTTP for jobs (bearer token auth) and push firmware directly to ESP devices; no inbound ports need to be open on the worker machine, but it does need network reach to the ESP devices it'll flash. **Thread/Matter devices are an exception** — their IPv6 mesh is only reachable from the HA host, so the add-on performs the OTA push for those targets automatically; any worker can still do the compile. If a worker can't reach a device at all — a different VLAN, a firewall in the way — it now detects that automatically and falls back to the same behavior: it compiles the firmware and hands the OTA push to the add-on instead of failing the job.
4. **Restart Home Assistant** once after the first install. The add-on ships a custom HA integration (`esphome_fleet`) that it auto-installs to `/config/custom_components/` on startup — but Home Assistant only loads integrations at Core startup, so the integration stays dormant until you restart HA. Go to **Settings → System → Restart** and pick *Restart Home Assistant*.
5. After the restart, Home Assistant will pop a "Fleet for ESPHome discovered" notification within a few seconds. Accept it to get all the devices, workers, and the add-on itself as real HA devices with entities.

> **Upgrading the add-on later?** If a Fleet release changes the integration (check the changelog — look for the `Integration` heading), you'll need to restart Home Assistant again after the add-on finishes updating. Restarting *the add-on* alone doesn't pick up integration changes, because HA Core only loads Python integrations at boot.

### Add-on configuration

Everything is configured from the **Settings drawer** inside the web UI — click the gear icon in the top-right of the header. The Supervisor **Configuration** tab is intentionally empty; every option moved into the drawer so edits apply immediately without restarting the add-on. Settings are split into **Basic** (what you touch most — versioning, authentication, display) and **Advanced** (retention, cache, timeouts, polling) tabs. Each field carries inline help text describing what it does and the valid range, so there's no need to mirror the settings reference here.

Settings persist across add-on updates. When upgrading from a pre-1.6 release, your existing Supervisor options (token, timeouts, thresholds, `require_ha_auth`) are one-shot imported on first boot so nothing you set before is lost.

## What's on the Web UI

**Devices.** Every ESPHome config in one place. Columns for online status, **tags**, current firmware version, HA entity link, IP address, WiFi vs Ethernet, last-compiled time, schedule, and ESPHome version. Optional columns for chip platform + PlatformIO board, BLE-proxy state, and a few more — opt in via the column picker. Click Upgrade on any row to compile + OTA that device. The row menu (⋮) exposes live logs, restart, ping, install-to-address, rendered config view, config history, rename, duplicate, pin, archive, delete, and copy-api-key (for devices with a native-API encryption key). Toggle **Show archived devices** in the column-picker dropdown to inline archived rows at 50 % opacity.

**Queue.** Every compile job — pending, running, succeeded, failed, or **blocked** (no eligible worker satisfies the active routing rules). Live build logs. Inline Rerun · Clear · Log on every row; everything else (cancel, download firmware, edit YAML, plus the device-section actions) lives behind the per-row hamburger. The worker-selection cell stacks **what the user asked for** (any worker / specific worker / tag expression) on top, **why this worker won** below.

**Workers.** Every connected worker — local and remote — with platform info, **tags**, slot count, **disk quota usage** (e.g. `Quota: 2.1 / 10 GiB`), current job, and uptime. **Routing rules…** opens a builder for fleet-wide rules backed by device + worker tags. **Set disk quota…** in the per-row hamburger overrides the fleet-default disk quota for one worker. Workers self-pause when their disk crosses 95 % and un-pause below 90 %, surfacing a **disk full** badge in the Status column. The worker's Python source refreshes itself automatically when the add-on upgrades; its underlying Docker image only refreshes when you run `docker pull && docker restart`, and workers running an outdated image are flagged with an **Image stale** badge telling you when that's time. Workers stay in the list until you delete them — stopping a worker or restarting the add-on just shows it as offline with how long it's been gone. **Delete** in the per-row Actions menu removes a worker for good; it's only available once the worker is offline, because a running worker would simply re-register on its next heartbeat.

**Schedules.** Every scheduled upgrade in one view. Recurring (daily/weekly/monthly or full cron) and one-time future schedules. Schedules live in the device YAML itself so they travel with your config and respect each device's pinned ESPHome version.

**Header** has a dark/light theme toggle, a "streamer mode" that blurs tokens and secrets (for screen-sharing demos), the currently-selected ESPHome version (changes for all new compiles unless overridden per-device via pinning), a shortcut to edit `secrets.yaml`, and a link to [ESPHome Web](https://web.esphome.io/) for browser-based initial flashing.

### Running different ESPHome versions across your fleet

The header dropdown sets the **global** ESPHome version — every new compile uses it unless a device is pinned. To pin a device, open the row menu (⋮) on the **Devices** tab and choose **Pin ESPHome version**. Pinned devices stick to their version regardless of what the global selector says; scheduled upgrades on a pinned device respect its pin.

Typical uses:

- **Beta-test a release** on one low-stakes device (a garage sensor, an outdoor thermometer) while leaving the rest of the fleet on the stable version.
- **Hold a picky device back** on a known-good version indefinitely when a newer ESPHome release breaks a component you depend on.
- **Stage an upgrade** — flip the global version, compile one device, verify, then bulk-upgrade everything outdated.

Workers install whatever ESPHome version each job asks for, on demand, into a local per-version cache so subsequent jobs using that version start instantly. The cache is bounded by the per-worker disk quota (default 10 GiB, change in **Settings → Disk management**) and evicts the oldest things first when the budget fills up — see the **Workers tab** for live disk usage.

### Adding a build worker

**Start here: you may not need one.** The add-on already runs a *built-in local
worker* on your Home Assistant machine, and it compiles everything on its own.
Add a second worker when builds are slow because HA runs on modest hardware (a
Pi), when you want several devices compiling at once, or when you'd rather your
HA box wasn't doing the heavy lifting. If compiles finish in a time you're happy
with, you're done — no worker needed.

**What a worker actually is.** A Docker container that you start on another
computer. When it starts, it *calls out* to Fleet over HTTP on port **8765**,
proves who it is with a shared token, and then asks for jobs. Fleet never
connects *to* it, so:

- **SSH is not involved anywhere.** Not port 22, not port 2222. If you changed an
  SSH port hoping to connect a worker, change it back — it has no effect on Fleet.
- **There is no `remote_builders:` setting**, and no YAML anywhere that lists your
  workers. A worker appears in the **Workers** tab by connecting; that's the whole
  registration mechanism.
- **No port forwarding is needed** for the worker machine. It has to be able to
  *reach* your HA machine on 8765, and to reach your ESP devices on the network in
  order to flash them.

**What the worker machine needs.** A normal desktop operating system with Docker
installed: Linux (Debian, Ubuntu, anything), macOS, or Windows.

> ⚠️ **Home Assistant OS is not a suitable worker machine.** HAOS is a locked-down
> appliance that only runs Home Assistant add-ons — you cannot start your own
> containers on it. If you have a spare laptop or mini-PC you want to use as a
> build machine, install ordinary **Debian or Ubuntu** on it plus Docker, not
> HAOS. (Installing HAOS on a second machine gives you a second Home Assistant,
> which is not what you want here.)

**Steps.**

1. On the machine that will do the building, install Docker. On Debian/Ubuntu:
   `curl -fsSL https://get.docker.com | sh`
2. In Home Assistant, open Fleet → **Workers** tab → **+ Connect Worker**.
3. Pick your format (Linux/macOS shell, PowerShell, or Docker Compose). The
   dialog writes out a complete command with your server URL and token already
   filled in — you don't have to work any of it out.
4. Copy it, paste it into a terminal on the build machine, run it.
5. Back in the **Workers** tab, the machine shows up within a few seconds. If it
   doesn't, see below.

That's the whole process. The command it gives you looks roughly like this, but
**use the one the dialog generates** — yours carries the right token:

```bash
docker run -d \
  --name esphome-worker \
  --restart unless-stopped \
  --network host \
  -e SERVER_URL=http://homeassistant.local:8765 \
  -e SERVER_TOKEN=<your token> \
  -v esphome-versions:/esphome-versions \
  ghcr.io/weirded/esphome-dist-client:latest
```

`--network host` matters: without it the container sits on Docker's internal
network and can't reach your ESP devices, so builds succeed and flashing fails.

**If the worker doesn't appear.** Check, in order:

- Is it running? `docker ps` on the build machine should list it. If not,
  `docker logs esphome-worker` says why.
- Can it reach HA? From the build machine, `curl http://homeassistant.local:8765`
  should answer something rather than hang. If it hangs, use HA's IP address
  instead of `homeassistant.local` — mDNS names don't resolve on every network.
- Wrong token? The worker log says so plainly. Re-copy the command from
  **+ Connect Worker**.

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

### When a worker can't reach a device

Before starting an upgrade, a worker makes a quick connection test against the device it's about to flash. If the device doesn't answer, the worker compiles the firmware anyway and hands the upload to the add-on, which is usually on a network with a clearer path to your devices. The same handoff happens if the connection test passes but the upload itself fails partway through — a weak Wi-Fi link, for example. Either way you get a finished upgrade instead of a failed job, and the Queue tab shows **Server OTA** while the add-on takes over.

This is on by default and there's nothing to configure. If it gets in the way — a network where the test is unreliable, or you'd rather a worker always did its own uploads — set `OTA_REACHABILITY_CHECK=0` on the worker container:

```bash
docker run -e OTA_REACHABILITY_CHECK=0 … ghcr.io/weirded/esphome-dist-client:latest
```

Turning it off means a worker that genuinely can't reach a device will fail the job rather than hand it over. Thread/Matter devices are unaffected either way — those are always uploaded by the add-on.

## Verifying what you're running

Every server and client image on GHCR is signed with [cosign](https://docs.sigstore.dev/) using GitHub's keyless OIDC flow (no long-lived keys anywhere). You can verify that the image you pulled is the one this repo built:

```bash
# Server image
cosign verify \
  --certificate-identity-regexp 'https://github.com/weirded/fleet-for-esphome/.github/workflows/publish-server\.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/weirded/esphome-dist-server:latest

# Worker image
cosign verify \
  --certificate-identity-regexp 'https://github.com/weirded/fleet-for-esphome/.github/workflows/publish-client\.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/weirded/esphome-dist-client:latest
```

A successful verification prints the OIDC claims (workflow ref, run ID, commit SHA). Run this once before you trust an image in production, or wire it into your container-pull automation.

### Checking the software bill of materials

Every 1.5.0+ image also carries a CycloneDX SBOM as a cosign attestation — the full list of Python packages, OS libraries, and their pinned versions that went into the image. Handy for CVE audits.

```bash
# Server image — download + print the SBOM
cosign verify-attestation \
  --certificate-identity-regexp 'https://github.com/weirded/fleet-for-esphome/.github/workflows/publish-server\.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --type cyclonedx \
  ghcr.io/weirded/esphome-dist-server:latest \
  | jq -r '.payload | @base64d | fromjson | .predicate' \
  > esphome-dist-server.sbom.json

# Worker image
cosign verify-attestation \
  --certificate-identity-regexp 'https://github.com/weirded/fleet-for-esphome/.github/workflows/publish-client\.yml@.*' \
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

## The Home Assistant integration

The add-on installs a companion integration (`esphome_fleet`) into
`/config/custom_components/` and registers itself for discovery, so Home
Assistant offers to set it up on its own. It gives you entities for the fleet
as a whole, for each managed device, and for each build worker.

### How data updates

The integration **polls** the add-on every **30 seconds**. There is no push
channel — the add-on and Home Assistant are separate processes, and a poll on
a local socket is cheap enough that a persistent connection would add failure
modes without buying much.

That means:

- Entity values can be **up to 30 seconds behind** what the Fleet Web UI shows.
  The Web UI refreshes once a second, so the two disagreeing briefly during a
  compile is expected, not a fault.
- **Compile events are derived from the poll, not streamed.** Each poll
  compares the job queue against the previous one and fires an
  `esphome_fleet_compile_complete` event for any job that has just reached a
  terminal state. A compile that starts *and* finishes inside one 30-second
  window still fires exactly one event when the poll catches it, so automations
  will not miss it — but the event arrives when the poll runs, not the instant
  the build ends.
- **To force a refresh**, use the *Reload* action on the integration card
  (**Settings → Devices & services → Fleet for ESPHome → ⋮ → Reload**). This
  re-polls immediately rather than waiting out the interval.

If every entity goes `unavailable`, the add-on is the thing to check first —
the integration reports unavailable when it cannot reach it.

### Examples

Three automations to copy and adapt. Replace the entity IDs with your own —
device entities are named after the device, so a device called `porch-sensor`
gets `update.porch_sensor_firmware`.

**1. Notify when any device has a firmware update pending**

```yaml
automation:
  - alias: "ESPHome firmware update available"
    trigger:
      - trigger: state
        entity_id: update.porch_sensor_firmware
        attribute: latest_version
    condition:
      - condition: state
        entity_id: update.porch_sensor_firmware
        state: "on"
    action:
      - action: notify.persistent_notification
        data:
          title: "ESPHome update available"
          message: >-
            {{ state_attr('update.porch_sensor_firmware', 'title') }} can move to
            {{ state_attr('update.porch_sensor_firmware', 'latest_version') }}.
```

**2. Compile a device on a schedule**

Useful for rebuilding against a newer ESPHome without doing it by hand. The
`target` is the YAML filename as it appears in `/config/esphome/`.

```yaml
automation:
  - alias: "Weekly rebuild of the porch sensor"
    trigger:
      - trigger: time
        at: "03:30:00"
    condition:
      - condition: time
        weekday:
          - sun
    action:
      - action: esphome_fleet.compile
        data:
          target: porch-sensor.yaml
```

**3. Warn when a build worker drops off**

```yaml
automation:
  - alias: "Fleet worker went offline"
    trigger:
      - trigger: state
        entity_id: binary_sensor.workshop_pc_online
        from: "on"
        to: "off"
        for: "00:02:00"
    action:
      - action: notify.persistent_notification
        data:
          title: "Fleet worker offline"
          message: "workshop-pc has been offline for two minutes."
```

The `for: "00:02:00"` matters — see the heartbeat note under Known limitations.

The other services are `esphome_fleet.cancel` (stop a queued or running job)
and `esphome_fleet.validate` (check a config without building it). Blueprint
contributions are welcome; there are none published yet.

### Known limitations

- **Home Assistant must be restarted after an upgrade that changes the
  integration.** HA Core only loads integration Python at boot, so restarting
  the *add-on* alone is not enough. The changelog flags releases where this
  applies.
- **Entity data can lag the Web UI by up to 30 seconds** — see How data updates.
- **Worker-offline detection has a 30-second heartbeat window.** A worker that
  blips for less than that is never marked offline; one that pauses for around
  45 seconds registers as offline and then online again. Put a `for:` on
  automations that react to it rather than firing on the first transition.
- **The Update entity does not distinguish factory from OTA images.** It
  performs the ordinary OTA upgrade. If you need a factory image — a first
  flash over serial, or recovering a device that will not accept OTA — use the
  Web UI, which lets you pick the variant.
- **Requires Home Assistant Core 2024.11 or newer.** This is not declared in
  `manifest.json` because custom-integration manifests reject the
  `homeassistant` key; it is enforced at runtime instead. On an older Core you
  will get a traceback rather than a clean "unsupported version" message.
- **The add-on image is not pinned by digest.** Supervisor's schema does not
  currently allow `@sha256:` pinning for add-ons, so the image is referenced by
  tag.
- **The AppArmor profile is first-pass confinement.** It denies the obvious
  things — reading other add-ons' secrets, `/proc/*/mem`, writes to
  `/sys/kernel` — but file and network access is otherwise unrestricted. See
  `SECURITY.md` for the threat model this does and does not cover.


## Troubleshooting

### The integration card says "Reconfigure"

The add-on's token was rotated, or its URL changed. Run the Reconfigure flow on
the integration card (**Settings → Devices & services → Fleet for ESPHome → ⋮ →
Reconfigure**) and enter the current URL; the token is picked up automatically
when Fleet is running as an add-on on the same machine.

### Entities are stuck at "unavailable"

The integration cannot reach the add-on. Check, in order: the add-on is
actually running (**Settings → Add-ons → Fleet for ESPHome**); its log has no
startup error; and the URL on the integration card matches where the add-on is
listening. After a Home Assistant restart, give it one 30-second poll before
concluding anything.

### Home Assistant never discovers the add-on

Discovery uses mDNS. If Home Assistant and the add-on are on different subnets,
or the router does not reflect mDNS between VLANs, the discovery card never
appears. Add the integration manually instead — **Add integration → Fleet for
ESPHome** — and type the add-on's URL.

### The Reauth prompt keeps coming back

Rare, and usually an entry whose stored credentials no longer match a rotated
token. Delete the integration entry and add it again; nothing is lost, because
device and worker entities are recreated from the add-on on the next poll.

### A compile failed with "Fix the YAML error above" right after I changed the ESPHome version

This should no longer happen. Fleet used to blame the config when a build was
dispatched while it was still installing a newly-selected ESPHome version; it
now waits and retries the job by itself. If you still see it, the message is
about a genuine problem in that config.

### The add-on won't start: "address already in use"

Fleet needs port **8765** free on the host. It runs on the host network and
Home Assistant Ingress pins the port it listens on, so the port field in the
Configuration tab can't move it — the port has to be free.

The usual cause is the **OpenThread Border Router** add-on, which is also
host-networked and binds the same port. Stopping it releases the port:

```
ha addons stop core_openthread_border_router
```

If the log ends with what looks like a clean shutdown (scheduler stopped, mDNS
unregistered) followed by a traceback, that's Fleet cleaning up after the
failed bind — not something asking it to stop. Look for the line beginning
*"Cannot bind to port 8765"* just below it.

## Support

If this add-on has saved you time or frustration, you can support continued development:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-orange?logo=buy-me-a-coffee&logoColor=white&style=for-the-badge)](https://buymeacoffee.com/weirded)
