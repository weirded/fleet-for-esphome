# Fleet for ESPHome

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-orange?logo=buy-me-a-coffee&logoColor=white)](https://buymeacoffee.com/weirded)

**A modern Home Assistant UI for ESPHome — works just as well for three devices as it does for a hundred.** Compile, flash, edit, and track every ESPHome device with config history you can roll back, a live job queue, and a UI that actually shows you what's happening.

> Previously known as **ESPHome Fleet** (1.5.0–1.7.0). <!-- br1-allow: rebrand-history hint, removed for 1.8 -->

> **Not an official ESPHome project.** Fleet for ESPHome is an independent, community-built tool that depends on [ESPHome](https://esphome.io) but is not part of the ESPHome project and is not maintained by the ESPHome team. "ESPHome" is a trademark of its respective owners; this project uses the name only to describe what it works with.

![Fleet for ESPHome — Devices tab](docs/screenshot.png)

![Fleet for ESPHome — fleet tags and tag filter pills](docs/screenshot-tags.png)

## Why use this instead of the ESPHome Device Builder?

The ESPHome Device Builder — the stock dashboard since ESPHome 2026.6 — is good and getting better: it has a job queue, remote build servers, labels, and bulk updates. If you mostly write YAML and flash one device at a time, it may be all you need.

Fleet for ESPHome is for when you care about **which version, when, and where** each device gets built:

- **Pin an ESPHome version to one device.** Beta-test a new ESPHome release on your garage sensor without upgrading the rest of the house, or hold a picky device back on a known-good version indefinitely. Different devices can build against different ESPHome versions side by side — workers install and cache each version on demand.
- **Scheduled upgrades.** Upgrade the office lights every Sunday at 3am, or once — *"tomorrow at 8pm when nobody's home"*. The schedule lives in the device YAML, so it travels with your config and respects the pin.
- **A firmware archive, automatically.** Every successful compile keeps its binary, and every compile — even ones from months ago — stays in a searchable history with its log. Re-flash an older build by hand, work out when a regression started, or rescue a device with a broken OTA path.
- **Build workers anywhere, with no pairing.** Paste a one-line snippet (Bash, PowerShell, or Docker Compose) on any machine — gaming PC, NAS, a server in another building — and it shows up in the Workers tab. Workers reach out to the add-on over HTTP, so they need no open inbound ports. They flash devices directly, and if a worker can't reach a device (another VLAN, a firewall, a Thread/Matter device on the HA host's mesh) it hands the upload to the add-on instead of failing the job. Worker code updates itself when the add-on upgrades.
- **Tags + routing rules.** Tag devices and workers however suits your fleet (`kitchen`, `production`, `ratgdo`, `os:windows`, …) and write rules like *"compile any `ratgdo`-tagged device on a `windows`-tagged worker"*. If no eligible worker is online, the queue shows the job as **BLOCKED** and names the rule.
- **One-click restore for config edits.** Every save is a `git commit`, so each file has a history, a side-by-side diff, and a **Restore** button on any previous version — right next to Edit. Archived configs come back the same way.
- **Your fleet in Home Assistant.** The companion integration turns every device, every worker, and the add-on itself into HA devices with firmware update entities, status sensors, and services — so you can automate upgrades from HA ("rebuild the porch sensor every Sunday", "tell me when a new ESPHome release is out").
- **Bulk operations that go beyond "update".** Upgrade everything that's outdated tonight, rebuild the whole fleet against a new ESPHome release, pin half the fleet while the rest moves forward, rerun every failed job, bulk tag and archive.
- **A device table with the details up front.** Online status, firmware version, IP, Wi-Fi/Ethernet, matched HA entity, last-compiled time, and project name — sortable, filterable, and linked to each device's page in HA.

## How it works

Three moving parts:

```
                              ┌──────────────┐
                        ┌─────┤   Worker 1   ├───► ESP devices
  Home Assistant        │     └──────────────┘
┌──────────────────┐    │     ┌──────────────┐
│ Fleet for ESPHome│◄───┼─────┤   Worker 2   ├───► ESP devices
│    (add-on)      │    │     └──────────────┘
└──────────────────┘    │     ┌──────────────┐
                        └─────┤   Worker N   ├───► ESP devices
                              └──────────────┘

         workers poll the add-on over HTTP for jobs (bearer token auth)
         and push firmware OTA directly to ESP devices
```

1. **The add-on** runs inside Home Assistant. It owns the device list, the job queue, the web UI, and coordinates everything. Workers never talk to each other; everything flows through here.
2. **Build workers** are small Docker containers that do the actual compiling. Each worker *polls* the add-on over HTTP asking for work — it's authenticated with a bearer token, so the add-on doesn't need open inbound ports on the worker. Workers decide which ESPHome version a job needs (from the global default + any per-device pin), install that version into a local venv on first use, and keep a small LRU cache of the most recent versions so subsequent jobs start instantly. The add-on ships with a **built-in local worker** so you don't need any remote hardware to get started — just increase its slot count in the Workers tab.
3. **ESP devices on your network** receive firmware OTA-style from the worker that built it — same mechanism as the stock ESPHome dashboard, just triggered from this UI. The worker needs network reach to the device. **Exception: Thread/Matter devices** use an IPv6 mesh only reachable from the HA host, so for those the worker compiles and hands the binary to the add-on, which performs the OTA push itself. Workers also detect this automatically for ordinary WiFi/Ethernet devices — if a worker can't reach a device (different VLAN, firewall, etc.), it falls back to the same compile-and-handoff behavior instead of failing the job.

## Installation

### As a Home Assistant add-on (recommended)

> **Add-ons only work on Home Assistant OS (HAOS) and Home Assistant Supervised.** If you're on **HA Container** (HA running in Docker) or **HA Core** (HA in a Python venv), there's no Add-on Store and the badge below won't do anything — skip to [As a standalone Docker container](#as-a-standalone-docker-container) instead. Not sure which you have? Open **Settings → About** in Home Assistant; *Installation Type* tells you.

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fweirded%2Fdistributed-esphome)

Or manually:

1. **Settings → Add-ons → Add-on Store → ⋮ (top right) → Repositories** and add `https://github.com/weirded/fleet-for-esphome`.
2. Find **Fleet for ESPHome** in the store and click **Install**.
3. **Start** the add-on. Open the web UI from the HA sidebar.

Home Assistant also auto-discovers the add-on and offers to add the companion integration — accept that notification once to get the device, sensor, and update entities into HA.

### Adding a remote build worker

You don't need one to start — the add-on ships with a built-in local worker that runs inside the HA host with one build slot out of the box. If local compiles feel slow, raise the slot count for `local-worker` on the **Workers** tab or add a remote worker on a faster machine:

1. Open the **Workers** tab and click **+ Connect Worker**.
2. Pick **Bash**, **PowerShell**, or **Docker Compose** depending on the target machine.
3. Copy the generated snippet (the snippet bakes in your server URL + auth token, nothing to edit) and run it on the target machine. That's it — the worker registers and shows up in the Workers tab within a few seconds.

The worker container is `ghcr.io/weirded/esphome-dist-client:latest`. All it needs on the host is Docker and network reach to (a) the add-on's HTTP API and (b) the ESP devices it'll flash. No inbound ports.

The worker's Python source updates itself from the server whenever the add-on upgrades (so bug fixes to client code reach remote machines automatically). Its **Docker image** doesn't — when the image needs refreshing (system packages, Python version, pinned dependencies), the Workers tab flags it with an **Image stale** badge and you refresh it on the worker host with `docker pull ghcr.io/weirded/esphome-dist-client:latest && docker restart <name>`. [DOCS.md](ha-addon/DOCS.md) has the longer explanation.

### As a standalone Docker container

Use this path if you're running **HA Container**, **HA Core**, or no Home Assistant at all — none of those have an Add-on Store, so the add-on path above doesn't apply. Everything still works; the few HA-Supervisor-only features (auto-discovery, the HA-connectivity column, the HA-service Restart fallback) fail-soft and the rest of the UI is unchanged. See [What works and what doesn't without Home Assistant](#what-works-and-what-doesnt-without-home-assistant) for the full list.

```bash
docker run -d \
  --name esphome-fleet-server \
  --network host \
  -v /path/to/esphome/configs:/config/esphome \
  -v esphome-dist-data:/data \
  -e SERVER_TOKEN=choose-a-random-string \
  ghcr.io/weirded/esphome-dist-server:latest
```

The UI is at `http://your-host:8765`. `--network host` is required so the server can discover ESP devices over mDNS.

For pre-release builds, use `:develop` instead of `:latest` — that tag updates whenever a new development build is published, so it isn't meant for production.

#### What works and what doesn't without Home Assistant

The server auto-detects its deployment shape via the `SUPERVISOR_TOKEN` env var (set by HA when running as an add-on, absent otherwise) and logs the result on startup (`Running in standalone mode (no HA Supervisor detected)` vs `Running as HA add-on (Supervisor detected)`). Override with `HA_MODE=standalone` or `HA_MODE=addon` if auto-detection is wrong for your environment.

**Full functionality in standalone**: compile queue, build workers, OTA upgrade, firmware archive + download, device poller + mDNS discovery, live device logs, git versioning of `/config/esphome/`, in-browser YAML editor with autocomplete, scheduled upgrades, settings drawer, entire Web UI.

**Unavailable without HA** (all fail-soft — server keeps running; the relevant UI affordances disable or return a 503 with a hint):
- HA auto-discovery of the companion custom integration (obvious — there's no HA to be discovered by).
- The Devices-tab "HA connectivity" column (reads HA's entity registry).
- The Restart button's HA-service fallback (native-API restart still works; the fallback is only reached when the device is offline).
- Supervisor-driven ESPHome version auto-detection. In standalone the server falls back to "latest stable from PyPI" on first boot; change via the version dropdown in the Web UI header.

**Configuration differences**:
- Settings live in the server's data volume instead of Supervisor options. Edit via the in-app Settings drawer (gear icon, top-right).
- `require_ha_auth` defaults to **off** for standalone Docker. If `:8765` is reachable from an untrusted network, turn it on in Settings → Authentication and hand out the bearer token; browsers without a token land on a styled 401 page explaining both recovery paths.

## A tour of the UI

- **Devices** — every ESPHome config you have. One-click Upgrade on any row; Upgrade dropdown for bulk actions (upgrade all outdated, upgrade changed, upgrade selected, upgrade everything). Edit YAML inline with autocomplete and validation. Pin a device to a specific ESPHome version. Tag devices for routing + filtering. Open a live device log. Ping a device or install to a specific address from the row menu. View the fully-rendered config (with `!secret` substituted, `packages:` flattened) before pushing. Deep-link to the HA device page. Toggle archived rows in-line via the column picker.
- **Queue** — what's compiling, what just finished, what failed, what's queued. Live build logs. Inline **Rerun · Clear · Log** on every row; Cancel, Download firmware, Edit YAML, and the full Devices-tab Device-section actions live behind the per-row hamburger.
- **Workers** — the workers you have connected, their platform, online status, build slots, current job, **disk usage vs. quota**, and tags. One-click **+ Connect Worker** generates the `docker run` / `docker compose` snippet for adding a new one. **Routing rules…** opens a builder for fleet-wide job-routing rules backed by device + worker tags.
- **Schedules** — every scheduled upgrade across your fleet in a single view. See what's due next, when it last ran, whether the run succeeded.

Dark/light theme toggle + a "streamer mode" that blurs tokens and secrets for screen-sharing are in the header.

More in [DOCS.md](ha-addon/DOCS.md) — configuration options, image signature verification, and the less-common operational workflows.

## License

[MIT](LICENSE).

## Support

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-orange?logo=buy-me-a-coffee&logoColor=white&style=for-the-badge)](https://buymeacoffee.com/weirded)
