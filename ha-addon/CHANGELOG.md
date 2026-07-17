# Changelog

## 1.7.2

**Newer ESPHome releases now compile.** ESPHome 2026.7.0 requires Python 3.12 or newer, but the add-on and its build workers still shipped on Python 3.11 — so every compile pinned to 2026.7.0 failed at the install step with "No matching distribution found," before any firmware was built. Even once that was cleared, ESP32 builds hit a second wall: 2026.7 replaced its build toolchain with a native ESP-IDF installer that needs a system library the image didn't carry, so ESP32 firmware still wouldn't compile. Both are fixed — the add-on now runs on Python 3.13 with the required libraries, and current and upcoming ESPHome versions install and compile normally across ESP32, ESP8266, RP2040, and LibreTiny targets. **Remote build workers must rebuild their Docker image** to pick up the new runtime — a worker still on the old image shows as needing an upgrade in the Workers list and won't take jobs until it's rebuilt; the built-in worker updates automatically with the add-on.

**Settings → Display → Font size.** New three-way picker (Small / Normal / Large) that scales the whole UI proportionally — tables, buttons, dialogs, and modals shrink or grow together rather than just body copy. Useful if you run Home Assistant at a non-100 % browser zoom and find Fleet's secondary text too small. Default stays at Normal so existing installs render byte-identical to 1.7.1.

**Per-worker "Working" sensor.** Every build worker now exposes a `binary_sensor.fleet_<worker>_working` entity in Home Assistant alongside the existing `_online` sensor. The entity flips on while the worker has a compile in flight, so you can write automations like "stop the VSCode add-on while any worker is busy" — exposed as the building block rather than hard-coding any specific stop/start action. Generalises beyond VSCode to Frigate, NodeRED, AdGuard, or a dashboard warning card; mirrors HA's `BinarySensorDeviceClass.RUNNING` for the right icon and label.

**Version dropdown — "Installable only" filter.** Both the header version dropdown and the Upgrade modal's version picker now offer a third filter alongside Show betas: **Installable only** (default ON) hides ESPHome versions older than 2023.7.0 — the floor below which `pip install esphome==X` is likely to fail on the current Python runtime. Untick to see the full PyPI catalogue. Pairs with 1.7.1's compile-with-old-versions fix.

**Bug fixes.**

- **Compiles no longer get stuck failing after the built-in worker frees disk space.** When the add-on's built-in worker trimmed its ESPHome version cache to stay within its disk budget, it could delete the very ESPHome version the server needs to prepare each build — after which every compile failed with a "Bundle creation failed" error and the queue stalled until the add-on was restarted. The server now reserves its active ESPHome version so the worker's cache cleanup leaves it alone, and transparently reinstalls it if it ever goes missing, so the build queue keeps running on its own.
- **`wifi.use_address` set to a non-`.local` hostname is now honoured everywhere.** For a device reached by a corporate-DNS FQDN (e.g. a VPN'd device where mDNS doesn't reach Home Assistant), the address was respected by the Devices tab and the OTA network-diagnostics dump, but Live Logs and the OTA upload itself silently fell back to `<name>.local` and failed to connect. All four paths now resolve the address the same way.
- **Standalone Docker workers no longer flap Offline during large ESPHome installs.** On the standalone (non-add-on) deployment with slower storage, the built-in worker could stall its own heartbeat while measuring disk usage after a big ESPHome install — long enough for the server to mark it offline and abandon the in-flight compile, then recover, in a loop. Disk measurement now runs on a background sampler so heartbeats keep flowing.

**For integrators.**

- **KEDA-compatible queue-depth metric.** New `GET /api/v1/metrics/queue` returns `{pending, working, active, online_workers, max_parallel_capacity, schema_version}` for external autoscalers (KEDA's `metrics-api` scaler, k8s HPA, Sablier, the in-tree Proxmox scaler). Bearer-auth via the existing `/api/v1/*` middleware — same worker token, read-only metric. Lets you spin workers up only when there's pending compile work and shut them down when idle.

**Under the hood.** The server and build-worker images move to Python 3.13. Groundwork for UI translations landed (an i18next foundation and a Settings language selector); actual translated locales will follow in a later release.

## 1.7.1

A brand-refresh release.

**Renamed to "Fleet for ESPHome"** (the add-on shipped as **ESPHome Fleet** in 1.5.0–1.7.0; before that **ESPHome Distributed Build Server**). The add-on store entry, sidebar tile, browser tab title, in-app wordmark, HA-integration display name, and the device-registry rows under Settings → Devices all flip to the new name on next deploy. The add-on slug, the integration domain, every `esphome_fleet.*` HA service, the GitHub repo, and the worker's Docker image names stay on their existing identifiers — nothing to migrate.

**YAML editor — Save vs. Save & Close.** The editor footer now offers two save buttons: **Save** writes the file to disk and stays open without creating a git commit (use it as a working checkpoint while you keep editing); **Save & Close** writes the file, commits, and closes the modal. With auto-commit on the close path commits silently with a default message; with auto-commit off it prompts for a message. The old "Save & Commit" button is gone — Save & Close covers both flows. The browser's "Save As HTML" no longer hijacks Ctrl+S (Cmd+S on Mac) inside the editor either; Ctrl+S maps to the same plain Save. The footer also has an explicit Close button (or just press Esc).

**Scheduler — remote workers no longer starve behind a fast local one.** With the local worker on 1 slot and remote workers idle, the scheduler used to defer every remote claim back to the local worker (because the local's "active" count momentarily dipped to 0 between jobs). The queue now claims onto remote workers as soon as it exceeds what the higher-priority pool can drain alone, so a 6-job queue across 1 local + 3 remote workers actually parallelises.

**Worker registration — fresh workers no longer get stuck in 500 loops.** A freshly registered worker that hadn't reported a `perf_score` yet could trip a server-side `TypeError` on every job poll, returning HTTP 500 forever; the worker would never claim a job, never benchmark, and never recover. The scheduler is now defensive on missing/None perf data and falls through to "no job for you right now" on any unexpected eligibility-check error so a single bad worker can't lock itself out of the fleet.

**Worker firmware archive — no more spurious "Failed" jobs after a successful flash.** A slow worker uploading both factory + ota variants of the compiled binary could race the server's job-timeout checker mid-upload; the server then refused both uploads with HTTP 409 and the UI flagged the job as failed even though the device was already flashed. The server now tolerates a brief grace window after a job finishes so the still-assigned worker's late variant uploads land cleanly.

**Slow networks — pip install for ESPHome venvs is more patient.** The `pip install esphome==X.Y.Z` step that runs on first use of a new ESPHome version was timing out for some HAOS users on slower or remote networks. Bumped the timeout, added a single retry, and a failure now names the network/PyPI cause so it's distinguishable from a real compile error.

**Install docs — HA Container / HA Core users now have a clearer path.** The README and add-on docs both spell out that the Add-on Store install path is HAOS / HA Supervised only, and point HA Container / HA Core users straight at the standalone Docker section.

**Older ESPHome versions are installable again.** The version dropdown showed every release back to 2024 but rejected any pin below 2026.4 — pre-1.7.1 the server's bundle path required `esphome.bundle` (which only landed in 2026.4), so anyone pinning an older release got "Fleet for ESPHome 1.6.2+ requires 2026.4.0 or newer" and a stuck install. 1.7.1 keeps the modern, scoped bundle for ESPHome 2026.4+ and falls back to a full-config-dir tar for older versions, so pinning 2026.3.x to dodge a 2026.4 YAML-parser regression (or to keep an older toolchain) just works. The legacy bundle ships the full config directory to the worker — same shape ESPHome's own dashboard uses — so users sharing workers with untrusted parties should still pin to 2026.4+; single-user fleets are unaffected.

**Quieter device logs — Fleet no longer reconnects every 60 seconds.** The add-on used to open a fresh native-API connection to every known ESPHome device once a minute just to read the running firmware version. On a device with `on_connect:` automations or a chatty publish-on-connect path, those automations re-fired on every Fleet poll; on devices whose ESPHome firmware allowed only one API client, the constant churn could compete with Home Assistant's persistent connection and pressure the device's `reboot_timeout`. Fleet now leans on mDNS announcements (which already carry the running version) for steady-state liveness and only opens an API connection on first sight of a new device or right after a Fleet-driven OTA. Power users diagnosing a flaky device can re-enable the old behaviour by flipping `device_native_api_poll` in Settings.

**Bug fixes (carried from earlier in the cycle).**

- **#231** — The "Authentication Expired" repair flow, the initial config-flow copy, and the underlying error message used to point users to *Settings → Add-ons → ESPHome Fleet → Configuration* (the Supervisor add-on Configuration tab). The Server token actually lives in the add-on's own UI at **Settings → Authentication → Server token**. The four affected strings now match the visible UI labels.

## 1.7.0

A major release built around **fleet tags + rule-based job routing**, with bounded worker disk, device-management polish, and a unified Upgrade modal on top.

**Tags and routing rules.** Devices and workers can now carry user-managed tags. Click any tag chip in the Devices or Workers tab to edit; autocomplete pulls from the fleet's existing tag pool. A new tag-filter pill bar above each table narrows the visible set as you click successive tags.

- **Routing rules…** in the Workers toolbar opens a builder with `all_of` / `any_of` / `none_of` clauses against device and worker tags. A rule constrains which workers may claim matching jobs, with a live preview of how many devices and online workers it covers.
- A new **BLOCKED** queue state surfaces when a rule eliminates every eligible worker; click the badge to jump straight into the offending rule.
- Workers can be tagged at startup; the Connect Worker dialog has a Tags field that bakes the value into the generated docker snippet.
- `esphome_fleet.compile` and `esphome_fleet.validate` HA services accept a `tags` list (with optional `any_of` / `all_of` / `none_of` matcher) so an automation can target a tag-defined subset of the fleet without naming each device.

**Unified Upgrade modal.** Per-row Upgrade, the bulk-upgrade items, and per-row Rerun all open the same modal — pick action, worker, and ESPHome version up-front, then confirm.

- **Three actions:** Upgrade Now, Download Now, or Schedule for later.
- **Three worker modes:** Any worker, a Specific worker, or a **Tag expression** that constrains a one-off compile (e.g. "any worker tagged `windows`") without authoring a full routing rule.
- When your selection conflicts with an active routing rule, the modal lists the offending rules and offers an **Upgrade & override** path.

**Bounded worker disk.** Build workers used to grow their cache without an upper bound. 1.7.0 replaces the loose `MAX_ESPHOME_VERSIONS=3` count cap with a single byte budget across the whole tree (default **10 GiB**), configurable per-worker or fleet-wide in **Settings → Disk management**. The Workers tab shows `Quota: 2.1 / 10 GiB` per worker and a worker that fills its disk pauses itself with a **disk full** badge instead of repeatedly failing jobs with `No space left on device`.

**Device-management polish.**

- **Archived devices live in the same table** at 50 % opacity below active ones — toggle **Show archived devices** in the column picker. The separate Archived page is gone, and **Archive** moves into the row menu (no confirm modal, just a toast with the restore path).
- **Ping device…** fires 10 ICMP packets at the device's OTA address and reports reachability + RTT. Works on every install path, including HAOS where unprivileged ICMP is disabled.
- **Install to address…** sends the OTA bundle to a hand-typed address — useful when mDNS resolves the wrong IP after a router reboot or a device shows up on a recovery network.
- **View rendered config** opens a read-only, syntax-highlighted view of the fully-resolved YAML (`!secret` substituted, `packages:` flattened, `external_components:` realized). Header carries a "contains plaintext secrets — copy with care" notice.

**Workers self-heal corrupted toolchains.** When a build fails because the cached PlatformIO toolchain is in a bad state, the worker now wipes the affected directories and retries the same job in-process. The first hit pays a 5–10 min re-download tax; previously an operator had to ssh in and clean up by hand.

**Other fleet-management wins.**

- New **Upgrade Changed** bulk action targets every device whose YAML has drifted since its last successful compile (distinct from **Upgrade Outdated**, which targets firmware-version mismatch).
- New **Commit all uncommitted** entry commits every dirty YAML in one shot, with an optional shared commit message and a live count.
- Bulk **Archive Selected** / **Unarchive Selected** in the Devices action menu, plus per-row **Rerun** that opens the Upgrade modal pre-seeded with the original job's parameters.
- New `firmware_retention_days` Settings field (default 2) evicts old compile binaries; combined with excluding `firmware/` from add-on backups, a typical partial-addon snapshot drops from ~237 MB to ~2 MB.
- The Queue's worker-selection cell now stacks **what the user asked for** above **why this worker won** (Pinned / Only eligible / Least busy / Fastest / First to poll), so the routing story is readable from one cell.
- New optional Devices-tab columns: **Platform** (chip family + PlatformIO board) and **BLE proxy** (off / passive / active). Both default off.
- The address-source label under each IP carries a plain-language tooltip ("Detected via ARP scan…", "From wifi.use_address in the device YAML…").

**Bug fixes.**

- The **Last compiled** column is now populated for every device running ESPHome firmware, even when the server has no compile history — falls back to the device-reported build time (marked `~`).
- Devices that compose their `esphome:` block via `packages:` / `<<: !include` keep their friendly-name and area when archived (used to render the bare filename).
- Archived devices show **Archived** in the Status column instead of "Checking…" forever, and tag editing is disabled on them on every surface.
- The Reconfigure form's submit button reads **Save changes** instead of HA's stock "Submit".
- Tag chip palette redesigned for distinctness — a row of 4 tags now reads as 4 visibly different colors instead of "two reds, two greens".
- Cleaning a worker's cache no longer wipes ESPHome venvs or PlatformIO toolchains; it clears only the volatile build outputs, so the next compile doesn't pay the toolchain re-download tax.
- Concurrent compiles no longer race on a shared git checkout — a batch sharing the same `external_components` repo used to fail with multiple phantom errors.

**Under the hood.** Hardened the file-mutation paths so every UI edit leaves your config's git history clean, and CI now builds against multiple ESPHome versions to catch upstream breakage earlier.

## 1.6.2

A hardening release on top of 1.6.1.

**Corrections to 1.6.1.** A handful of things 1.6.1 claimed or shipped weren't quite right; 1.6.2 is where they get honest.

- **AppArmor confinement is real now.** 1.6.1 added an AppArmor profile and lit up the Supervisor security-star badge, but the profile was permissive enough that it didn't meaningfully confine the running container. 1.6.2's profile keeps the badge and adds a handful of explicit `deny` rules that actually close concrete paths (`/etc/shadow*`, `/run/secrets/**`, `/proc/<pid>/mem`, kernel sysctl writes, cross-container ptrace).
- **Integration quality-scale claim corrected.** 1.6.1's `manifest.json` declared `silver`, but the audit file (`quality_scale.yaml`) hadn't been reconciled against the Silver rules. 1.6.2 honestly retreats to `bronze` — which is hassfest-validated and accurately reflects what's in the repo today. The `gold` tier-flip moves to 1.6.3 where every rule gets walked to `done`/`exempt` with a reason.
- **Install paths that were documented to work but didn't.** Fresh HAOS installs could get stuck on "Installing ESPHome…" forever, standalone Docker installs required an HA Bearer token the user had no way to obtain, and OTA failed outright on any device with a non-`.local` mDNS domain. Each of those is fixed in this release — see Bug fixes below for the specifics.
- **Remote workers stop shipping the whole config directory.** 1.6.1's worker bundle tar+gzipped every file under `/config/esphome/`, including `.git/` (with push credentials baked into remote URLs), every unrelated device's YAML, and in-place PlatformIO caches. The bundle now contains only what the target being compiled actually needs. Upgrading in place is enough — no action required.

**Bug fixes.**

- Add-on install no longer fails with `Image docker.io/library/docker:<version>-cli does not exist` when Docker Hub is rate-limiting or briefly unreachable. The add-on now ships as a prebuilt multi-architecture image on GitHub Container Registry, so fresh installs pull in seconds instead of building locally through the Docker-in-Docker builder image that was the source of the failure.
- Fresh installs without the Home Assistant ESPHome builder add-on no longer get stuck on "Installing ESPHome…" forever. The first boot now auto-bootstraps the latest stable ESPHome from PyPI when no other version is pinned; if the ESPHome builder add-on is installed later, its version takes over on the next refresh. The Version picker in the header is also a working recovery path now — picking a version there actually triggers the install instead of just recording the selection.
- Direct-port access to the web UI works out of the box on standalone Docker Compose installs. 1.6.1 required a Home Assistant Bearer token on direct port 8765 regardless of install path, which left standalone users at a bare-JSON 401 with no way forward. Fresh standalone installs now default "Require Home Assistant auth on direct port" to off; turn it on in Settings → Authentication if the port is reachable from an untrusted network. Home Assistant add-on installs (where the tunnel is always available) continue to default the flag to on, so direct port 8765 still returns 401 there. Home Assistant tunnel access is unaffected on either install path.
- When direct-port auth is on and a browser lands on `:8765` without a token, you now see a styled Authentication required page that explains both recovery paths (provide a token, or disable the flag via the Home Assistant tunnel), instead of a raw JSON error body.
- OTA targets honour `wifi.domain` / `ethernet.domain` / `openthread.domain` again. If your devices live on a non-`.local` mDNS domain, the worker now uses the correct `<name><domain>` address ESPHome itself would use — previously the compile succeeded but the OTA step hung at "Error resolving IP address" because the server was handing the worker `<name>.local` regardless of the `domain:` field.
- Queue page: the **Clear** dropdown has a new **Clear Selected** option, so you can check a few rows and remove just those. The existing "Clear Succeeded", "Clear All Finished", and "Clear Entire Queue" entries are unchanged.
- Startup logs no longer flood with a `WARNING` line every scan cycle when `/config/esphome/` doesn't exist. A single informational line now explains what the state means (install the Home Assistant ESPHome builder add-on, or create the directory yourself).
- **Add Device** now works on a truly-empty first install, even when `/config/esphome/` doesn't exist yet (i.e. you installed Fleet without ever installing the Home Assistant ESPHome builder add-on). The directory is created the moment you create your first device instead of failing with a `No such file or directory` error.
- The **Connect Worker** modal's **Bash** and **PowerShell** snippets now include `--network host`, matching the **Docker Compose** snippet that already had `network_mode: host`. Without it, a worker started from the copy-pasted command landed on Docker's default bridge network and could not reach ESP devices on the host's LAN, so every OTA failed.
- The Devices tab's "config changed" badge no longer lights up on devices you haven't edited. It now tracks the same `git status` the rest of the UI uses, so a device whose YAML is unchanged locally stays unflagged even if the file's timestamp got nudged (an editor autosave, a `git checkout`, or a failed OTA that left the firmware's own "compiled at" time behind). The Upgrade button's "config has changed" highlight also keys off this same signal now, so the two can't disagree.
- Fixes a hang where the add-on pegged at 100 % CPU and the UI stopped responding on fleets with large `/config/esphome/` trees (many top-level YAMLs plus a `common/` package tree and/or external-components directory). Root cause was the per-job config bundling synchronously tar+gzipping the whole directory on the event loop for every claimed job — now bundles are built off-thread and scoped to just the files the target being compiled references (see **Remote workers stop shipping the whole config directory** above).
- Reconfigure and Reauth flows no longer crash with `TypeError: object dict can't be used in 'await' expression` on Home Assistant 2024.11 and newer — the handler was incorrectly awaiting a synchronous helper. Was a shipped regression in 1.6.1; every Reconfigure click logged the traceback.

**Smaller changes.**

- New **Request diagnostics** action. Click it in the Workers tab's per-worker Actions menu (online workers only) to pull a live Python thread dump from any remote worker, or in Settings → Advanced → Diagnostics to capture one from the add-on's own server process. The dump downloads as a timestamped `.txt` file you can attach to a bug report. Useful when a compile hangs or a worker pegs at 100 % CPU — the dump shows exactly which line each thread is sitting on. Works on every install variant out of the box — Home Assistant add-on, HAOS, Supervised, and standalone Docker — with no special container capabilities or shell access required on your part.

**Under the hood.**

- Integration config-flow hardening: the Reconfigure and Reauth flows have gained end-to-end tests against a real Home Assistant fixture. The same test pass is what caught the Reconfigure `TypeError` in 1.6.1 covered under Bug fixes above.

## 1.6.1

A bug-fix + polish release on top of 1.6.

**Bug fixes.**

- Static-IP devices OTA correctly again — workers were shipping the `.local` mDNS hostname to `esphome upload` when the YAML declared a static IP.
- Live logs and OTA to encrypted devices work on fresh first-boot — the scanner used to race the ESPHome install and miss the `api.encryption.key`.
- Turning config versioning on from the Settings drawer now initialises the git repo immediately (no add-on restart needed). If `.git/` goes missing on a later boot (restored backup, container rebuild), it's recreated automatically.
- The noisy `aioesphomeapi.connection: disconnect request failed` traceback that fired after every OTA is gone — the expected disconnect-during-reboot case now logs at DEBUG instead of ERROR.
- OTA failure diagnostics now report real ping RTT / packet-loss instead of `Ping: [Errno 2] No such file or directory: 'ping'`.
- When config versioning is off, every UI surface that led to an empty History drawer (hamburger *Config history…*, editor **History** button, *Diff since compile*, commit-hash columns) is now hidden or disabled with a tooltip.
- Devices tab IP column is visible again for users upgrading from before it existed.

**Small additions.**

- Every successful compile's firmware is archived on the server, not just "Download Now" jobs. A **Download** button shows up on every row in Queue, per-device Compile history, and fleet-wide Queue history.
- Optional **MAC** column on the Devices tab (toggle via the column picker). The IP column gets an ARP-table fallback when mDNS doesn't know the address.
- New **Worker selection** column in Queue + Compile history explaining why a given worker picked up a compile (*Pinned to worker*, *Only worker online*, *Least busy*, *Fastest available*, *First to poll*).
- The integration now supports HA **Download diagnostics** (redacted), shows up on the **System Health** panel, and the **Configure** button lets you edit the add-on URL + token in place. Deleted devices are removed from HA's device registry automatically.

**Housekeeping.**

- AppArmor profile added; the add-on runs under confinement now and the Supervisor security-star card reflects it. `stage: experimental` dropped.
- All open Dependabot alerts resolved (dompurify + hono).
- Integration now ships a hassfest-validated quality-scale declaration (stays at `bronze`) and runs hassfest in CI.
- Add-on icon swapped for the one the official ESPHome add-on ships, so the store card renders at the same visual size as ESPHome Device Builder.
- YAML config comment marker renamed from `# distributed-esphome:` to `# esphome-fleet:` (both are still read; the new one is written on save).

**Note for standalone worker users.** The worker Docker image bumped from version 6 to 7 (adds `iputils-ping`). Old-image workers are flagged in the Workers tab — run `docker pull ghcr.io/weirded/esphome-dist-client:latest && docker restart <name>` to refresh.

## 1.6.0

**An in-app Settings drawer.** Fleet now has a proper Settings surface — click the gear icon in the header. Every knob that used to live on the Supervisor Configuration tab (server token, timeouts, worker-offline threshold, require-HA-auth) has moved here, and several new fields sit alongside them (history retention, firmware cache size, job-log retention, time-of-day format, config-versioning toggle + author). Edits apply immediately without restarting the add-on. The drawer is split into **Basic** and **Advanced** tabs — Basic holds what you'll touch most (versioning, authentication, display), Advanced holds the plumbing knobs (retention, cache, timeouts, polling). Values from your previous Supervisor configuration are imported on first boot so nothing you set before is lost.

**Per-file config history with diff + rollback.** Every save to `/config/esphome/` becomes a local git commit. Click **Config history…** in any device's hamburger menu to browse that file's full history — Monaco side-by-side diff viewer on the right, commit list on the left — and roll back to any earlier version in one click. The Queue tab grows a **Diff since compile** button that opens the history panel pre-set to "what's changed since this compile started." Works both for users without git experience (fresh installs get a Fleet-owned repo) and users who already run git in `/config/esphome/` (your existing repo is left completely untouched — no `.gitignore` mutations, no identity override, no auto-commits unless you opt in). Commit messages are human-readable (`"Automatically saved after editing in UI"`, `"Pinned ESPHome version"`, etc.) so `git log` reads like an activity feed.

**Archive is tracked in git.** Deleted devices land in `.archive/` as before, but the move now uses `git mv` so blame + `log --follow` thread cleanly through the delete → restore cycle. Permanent-delete runs through `git rm` + commit, so even that's recoverable by a git operator.

**Compile history that sticks around.** The Queue tab only ever shows the latest compile per device (by design — one row per device, not an ever-growing list). 1.6 adds a persistent history that survives queue coalescing *and* explicit Clear, reachable three ways:

- **History** button on the Queue toolbar — fleet-wide modal with filters for device, state, and time window, plus a universal search.
- **Compile history…** in each Device row's hamburger — scoped to that one device, with a stats strip (total / ok / failed / avg) at the top.
- An optional **Last compiled** column on the Devices tab (off by default, enable via the column picker).

Every row expands to show the last bit of the build log inline.

**Local worker starts active.** Fresh installs boot the built-in `local-worker` with 1 slot instead of 0 (paused). The first compile just works; raise the slot count in Workers if you want parallelism, set to 0 to pause.

**Smaller fleet-management wins.**

- Devices tab: **+ New Device** and **Archive…** buttons collapsed into one **Add device ▾** dropdown — New device, Duplicate existing (clone any target), or Restore from archive.
- Devices tab: hamburger items that would no-op on a given device (Restart with no `restart_button` in YAML, Copy API Key with no encryption) are disabled with a tooltip explaining what YAML to add to enable them.
- Queue + History "Triggered" column is now consistent — same icons, same labels across both surfaces, with compiles initiated via the direct API rendering distinctly from HA-action triggers.
- Queue History is sortable on every column (Device, State, ESPHome, Duration, Started, Finished, Trigger, Worker) with infinite-scroll pagination.
- `secrets.yaml` no longer trips the Validate button — the `esphome config` schema doesn't apply to a `!secret` dictionary, so Fleet now mirrors the ESPHome Dashboard and skips validation on that file.

**UX polish pass.**

- The History drawer's Monaco diff editor follows the app's light/dark theme.
- Drawers (History, Compile history) darken the underlying tab so still-updating rows don't distract.
- Restore button on the HEAD row is disabled with "Already at this version" when the working tree is clean.
- Every greyed-out dropdown item (`Retry All Failed`, `Clear Succeeded`, bulk actions, etc.) explains *why* it's disabled in a hover tooltip.
- Every numeric Settings field shows its `(default N, min M, max K)` bounds inline.
- Authentication field's Show / Copy icon buttons are properly labeled for screen readers.

**Bug fixes users will notice.**

- Scheduled compiles cancelled before a worker picks them up now render `Scheduled (once) — cancelled before start` in history instead of a row full of `—` dashes.
- Restore-confirmation dialog no longer hard-codes "No new commit will be created" — it now reads the live auto-commit setting and tells the truth.
- Manual-commit message field's placeholder is the curated default (`"Manually committed from UI"`) instead of the old raw `save: foo.yaml (manual)` form.
- Queue tab's Commit column is a clickable short-hash button that opens the History panel preset to "what's changed since this compile."
- Queue / History / Schedules tabs show absolute timestamps stacked below relative ones so you don't have to hover to see when something happened.
- Config-history diff editor is the full visible drawer height instead of a cramped 330 px panel.
- Toggling auto-commit on with uncommitted changes offers to commit them first instead of silently leaving them dirty.

**Under the hood.** Settings writes are atomic and validated per-field; downstream consumers re-read them on every tick so a drawer edit takes effect within one cycle with no restart. The auto-commit write path is debounced + async so the editor's Save never blocks on `git`. The job-history DAO uses SQLite WAL mode for read-while-write concurrency; writes are sub-millisecond.

## 1.5.0

**Rebrand: now called ESPHome Fleet.** Same add-on, same Docker images, no migration needed — just a new name that better describes what the tool does. The HA sidebar entry, add-on store name, browser tab title, and add-on logs all read "ESPHome Fleet" instead of "ESPHome Distributed Build Server". The GitHub repo, Docker image names, and add-on slug are unchanged so existing installs upgrade in place with no action required.

**Native Home Assistant integration.** ESPHome Fleet is now a first-class HA citizen. The add-on drops a custom integration into your HA config on first boot and advertises itself via mDNS; HA pops a one-click "ESPHome Fleet discovered" notification. Once you confirm, every managed device, every build worker, and the add-on itself become real HA devices with entities you can dashboard on:

- **Update entities** per device — use HA's built-in Update card to upgrade firmware, with the current/latest version both visible.
- **Sensors** for queue depth, worker online state, per-worker active-job count, fleet-wide online/outdated/total-slot counts, and per-device running version.
- **Buttons** to kick off compiles directly from an automation.
- **Numbers** for each worker's max-parallel-jobs slot.
- **`esphome_fleet.compile` / `.cancel` / `.validate` actions** — invoke from the HA action editor with a real device picker (choose the device, not a filename) and an optional worker pin. Supports bulk targets, `"all"`, and `"outdated"`.
- **`esphome_fleet_compile_complete` events** on the HA bus for automation triggers — fires once per terminal state with target, state, duration, worker, and schedule context.
- Devices managed by this add-on auto-merge with the same device from the stock ESPHome integration, so the Update card sits next to your existing ESPHome sensors and switches.

**Downloadable firmware binaries.** Pick "Download Now" in the Upgrade dialog to get a compile without OTA — the Queue tab then offers a Download menu with every variant the compile produced. ESP32 gets both **Factory** (full flash image for first-time USB/serial flash) and **OTA** (smaller OTA-safe image); ESP8266 gets OTA. Each is available raw or **gzip-compressed** for smaller wire transfer (~30–40% smaller on typical ESP firmware).

**Mandatory authentication on the direct-port API.** The `/ui/api/*` endpoints now require an `Authorization: Bearer <token>` on port 8765. Two token shapes are accepted: the add-on's shared worker token (used automatically by the HA integration's coordinator, so no user action needed) and a Home Assistant long-lived access token (for scripts and `curl` — gives real per-user audit attribution). Ingress access from the HA sidebar is unaffected. The add-on's Bearer token flows through Supervisor discovery into the integration transparently — just accept the discovery notification and it works.

**ESPHome unbundled from the server image.** The add-on no longer ships with a particular ESPHome version baked in — it installs whatever your HA ESPHome add-on reports at first boot. This means your builds always match the version HA expects. Heads up: **first boot takes 1–3 minutes** while ESPHome installs into `/data/esphome-versions/`. The UI shows an "Installing ESPHome…" banner during that window and features that depend on the binary (validate, autocomplete, compiles) stay disabled until ready. Subsequent restarts are instant because the install is cached.

**Supply-chain signals you can verify.** Every GHCR image carries a cosign signature (keyless, GitHub OIDC) and a CycloneDX-format SBOM attestation. See `DOCS.md` for the `cosign verify` + `cosign verify-attestation` commands. Every GitHub Actions step is SHA-pinned, and CI fails on any unpinned `uses:` line. `pip-audit` and `npm audit` gate every push.

**UI quality sprint.**

- **Accessibility** — every icon-only button now has both an `aria-label` (for screen readers) and a `title` (for hover), enforced by a new invariant. Hamburger/sort-header clicks are real `<button>`s now; row-menu keyboard nav comes from Radix.
- **Lucide icons everywhere** — emoji glyphs (🕐 📅 📌 👁 🔒 ☀ ☾) and HTML entities (⋮ ⚙ ↻ ↓ ↗ ▲ ▼) replaced with consistent Lucide icons across the app.
- **One Action selector in the Upgrade dialog** — Upgrade Now / Download Now / Schedule Upgrade, replacing the former nested Now/Scheduled + Compile+OTA/Compile+Download toggles.
- **Queue dropdown stability** — the Download and Retry dropdown menus no longer slam shut on each 1 Hz SWR poll (same class of bug as the devices-tab hamburger #2 fix). Applied to every row-cell DropdownMenu in the app.
- **Table polish** — column headers now consistently sentence-case across all four tabs; state badges (Compiling, Failed, Timed Out, Cancelled, etc.) are title-case everywhere, same badge component used on both the Queue and Workers tabs. The Worker cell renders the slot on a muted second line (`local-worker / slot 2`) instead of gluing it to the hostname.
- **Queue tab triggered-by column** — shows "Manual", "Recurring · Daily 03:00", "Once @ 2026-04-17 08:30" with full cron/tz in the tooltip.
- **Connect Worker modal** — new Docker Compose format tab alongside Bash and PowerShell, with the shared token baked into the snippet. Default container name is now `esphome-fleet-worker` (rebranded). The old `docker-compose.worker.yml` in the repo root is retired — the modal generates it live.
- **Download buttons disambiguated** — per-row "Download .bin" vs log-file "Download log" are no longer ambiguously named.
- **Schedules tab empty-state copy** fixed to point at the Upgrade dialog's Scheduled option (the previous text referenced a hamburger menu item that had been removed).
- **Error Boundary** around the root `<App />` so a transient render error shows a recoverable dialog instead of a blank page.
- **QS.27 polish** — persisted sort state per tab, URL `?tab=` deep-linking, and a small pile of micro-cleanups.

**Under the hood.**

- Frontend API calls all funnel through a typed `api/client.ts` layer with named response interfaces; no more `fetch()` in components (enforced invariant). Silent `return [] on !r.ok` fallbacks are now throws that SWR's error path surfaces.
- `DevicesTab.tsx` split from 1,173 lines + 24 hooks into six focused files. Monaco glue split into `yamlValidation.ts` / `completionProvider.ts` / `monacoSetup.ts`.
- HA integration tests (37 new): config flow, device-info builders, coordinator events, installer atomicity, services schema + lifecycle.
- Protocol (`protocol.py`) pydantic contracts enforced byte-identical between server and client; every `/api/v1/*` handler parses through a typed model.
- `integration_installer` now writes atomically (`os.replace` over a staging dir) so a crash mid-install can't strand a missing custom integration.
- 16 enforced invariants checked mechanically on every push (up from 10): `fetch()` placement, shadcn wrappers, `any` ban in new code, `<td>` flex ban, typed e2e fixtures, YAML `safe_load` vs regex, subprocess logging, worker image version bumps, hash-pinned lockfiles, macOS-package lock guard, protocol parity, `--ignore-vuln` applicability, typed request bodies, icon-button labels, e2e waits, API-layer error propagation.

## 1.4.0

The fleet management release. Schedule upgrades, pin device versions, and create new devices — all from the UI.

**New features**

- **Scheduled upgrades** — set any device to upgrade on a recurring schedule (daily, weekly, monthly, custom cron) or at a one-time future date. Times are entered in your local timezone. A new **Schedules tab** lists every scheduled device with its next/last run, status, and recent run history.
- **Per-device version pinning** — pin individual devices to a specific ESPHome version. Bulk upgrades and scheduled runs respect the pin. The upgrade modal warns you when a one-off upgrade differs from a device's pin.
- **Create devices from the UI** — "+ New Device" makes a new YAML from a stub; "Duplicate…" clones an existing device (preserving `!include`, `!secret`, and substitutions). Both open the editor on the new file. Cancelling without saving cleans up after itself.
- **Unified Upgrade dialog** — one dialog handles "upgrade now" and "schedule for later" with a mode toggle.
- **Searchable ESPHome version picker** ([#44](https://github.com/weirded/fleet-for-esphome/issues/44)) — both the header version dropdown and the Upgrade dialog now have a search box and list every historic ESPHome release from PyPI (no more 50-version cap). Beta versions are hidden by default with a "Show betas" toggle.
- **Home Assistant device deep-links** — the "Yes" indicator in the HA column is now a clickable link to the device's page in Home Assistant.
- **Cancel and clear queue jobs** — cancelled jobs are now distinct from failed (gray "Cancelled" badge) and can be retried. New "Clear Entire Queue" option cancels everything in flight and clears all terminal jobs in one click.

**Improvements**

- The editor and log viewers now scale to fill the available screen space.
- Schedule fields and last-run timestamps in the Devices and Schedules tabs are clickable shortcuts that open the upgrade dialog.
- Concurrent compiles on the same worker no longer share build directories — full parallelism, with cache reuse across worker slots.
- Home Assistant backup sizes shrunk significantly — the local worker's ESPHome cache (1-2 GB after a fleet upgrade) is no longer included in backups.

**Bug fixes**

- Web server links now appear for devices that enable `web_server:` with default settings.
- Devices that have been offline for more than 4 hours are now cleaned up automatically, so the Devices tab doesn't fill up with ghost entries from retired or unplugged hardware.

## 1.3.1

**New features**

- **Upgrade modal** — clicking Upgrade on a device opens a dialog where you can pick which worker should run the build and which ESPHome version to use. The version override is per-job only — it won't change your global default. Replaces the old "Upgrade on..." submenu.
- **Queued follow-up compiles** — clicking Upgrade while a compile is already running for the same device queues exactly one follow-up that starts automatically when the current build finishes. It picks up the latest YAML at the time it starts, so you can edit → save → click Upgrade again without waiting. Re-clicking a third time updates the queued follow-up (worker, version) instead of piling up entries. The Queue tab shows a "Queued" badge on these follow-up jobs.
- **Network columns on the Devices tab** — new toggleable columns show each device's network type (WiFi / Ethernet / Thread), IP mode (Static / DHCP), IPv6 status, Matter support, and whether a fallback access point is configured. The "Net" column is visible by default; the others can be toggled from the column picker.
- **Upgrading indicator** ([#32](https://github.com/weirded/fleet-for-esphome/issues/32)) — an orange pulsing dot appears in the Status column while a device has a compile in flight, with live status text ("Compiling…", "OTA Retry", etc.) from the queue. No more wondering whether your Upgrade click registered.
- **Save & Upgrade goes through the modal** — the editor's "Save & Upgrade" button now opens the same Upgrade dialog so you can pick a worker and version before triggering the build.
- **Queue tab improvements** — new "Version" column shows the ESPHome version each job will compile against. A 📌 pushpin icon appears next to workers that were explicitly chosen in the Upgrade modal. Successful jobs show a green "Rerun" button instead of the amber "Retry".
- **HA-confirmed unmanaged devices** — devices discovered via mDNS that don't have a YAML config but ARE known to Home Assistant now show "in HA" under the IP and "Yes" in the HA column, so you can tell real ESPHome devices from stray mDNS broadcasts.
- **Connect Worker modal remembers context** — clicking the "Image Stale" badge on a worker pre-populates the hostname, max parallel jobs, and host platform from the existing worker.

**Improvements**

- Devices, Workers, and Queue tabs now poll at 1 Hz (was 3–15 seconds) for much snappier updates.
- After a successful OTA, the device's running version updates within ~1 second instead of waiting up to 60 seconds for the next poll cycle.
- Compile and clean-cache actions instantly refresh the relevant UI data instead of lagging by one poll interval.
- Unavailable actions (like Restart on devices without a restart button in their YAML) are now grayed out with an explanatory tooltip instead of silently failing when clicked.
- ESPHome add-on version detection works with any slug format (including hashed community-repo slugs) without needing elevated Supervisor permissions.
- Repeated identical warnings from the HA entity poller are demoted to DEBUG after the second occurrence, so a persistent HA outage doesn't drown the logs.
- Every 401 rejection now logs a structured reason (missing header, wrong scheme, token mismatch) with the peer IP, making auth issues much easier to diagnose.

**Bug fixes**

- [#25](https://github.com/weirded/fleet-for-esphome/issues/25) — UI didn't load on HAOS with 1.3.0 (startup blocked on Supervisor API + poller tight-retry loop).
- [#27](https://github.com/weirded/fleet-for-esphome/issues/27) — Divider line between managed and unmanaged devices disappeared on toggle.
- [#31](https://github.com/weirded/fleet-for-esphome/issues/31) — "Upgrade on..." submenu overflowed with long hostnames and closed when moving the mouse to it.
- [#6](https://github.com/weirded/fleet-for-esphome/issues/6) — Intermittent `Failed to install Python dependencies into penv` on ARM Mac workers (increased network timeouts for uv/pip).
- Restart endpoint no longer silently reports success when the device has no restart button — it returns a clear error with the candidates it tried.
- A corrupted `queue.json` entry no longer crashes the server at startup — the bad entry is skipped and logged.
- Matter/Thread devices with both `wifi:` and `openthread:` blocks are now correctly detected as Thread (was incorrectly picking WiFi due to block-order precedence).
- Editor no longer gets stuck on a loading screen (CSP was blocking Monaco's CDN; now allowed).

**Under the hood**

- Server↔worker payloads are now typed via pydantic v2 models with protocol versioning and forward-compatible field handling. Malformed requests return structured errors instead of being half-processed.
- Python dependencies are hash-pinned in lockfiles and installed with `--require-hashes`. pip-audit + npm audit gate CI. Dependabot configured for all ecosystems. GHCR images are cosign-signed.
- Security response headers (CSP, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, X-Frame-Options) on every UI response.
- 338 Python tests (was 264), 37 mocked Playwright tests, 6 prod Playwright tests against a real HA instance with real device compilation + OTA.
- New `scripts/check-invariants.sh` enforces 8 codebase rules in CI (no `fetch()` outside `api/`, no `any` in TS, YAML via `safe_load` only, etc.).

## 1.3.0

Theme: **Quality + Testing.** Mostly internal hardening to prevent regressions and increase confidence in future releases. A handful of user-visible bug fixes and small UX improvements ride along.

**Reliability infrastructure**
- 264 Python tests (up from 117) covering UI API, worker REST API, auth middleware, scanner metadata, queue pinning/retries, device poller IPv6 + name normalization, and more. ~55% server+client coverage baseline.
- 37 mocked Playwright browser tests covering devices, queue, workers tabs, editor modal, theme + responsive behavior.
- 16-target ESPHome compile matrix in CI (every push) — actual `esphome compile` runs against fixture YAMLs covering ESP8266, ESP32 (Arduino + IDF), ESP32-S2/S3/C3/C6 (IDF), RP2040, BK72xx, RTL87xx, plus complex configs (external components, packages, Bluetooth Proxy, Thread).
- CI also runs ruff lint, mypy on server + client, pytest with coverage reporting, frontend build, and Playwright tests on every push.
- Docker images now also published from `develop` (`ghcr.io/weirded/esphome-dist-{client,server}:develop`) so users can test unreleased changes without rebuilding locally. `:latest` stays pinned to `main`.

**Worker image versioning** ([#16](https://github.com/weirded/fleet-for-esphome/issues/16))
- Workers now report a Docker `IMAGE_VERSION` separate from the source code version. The server enforces a `MIN_IMAGE_VERSION` and refuses source-code auto-updates to workers running a stale image (since they'd just exec into a broken state).
- Workers tab shows a red "image stale" badge next to outdated workers, clickable to open the Connect Worker modal with the latest `docker run` command.

**Build worker uses psutil**
- Worker system info (memory, CPU usage, disk) now comes from psutil instead of hand-rolled `/proc` parsing. Cross-platform (Windows works as a bonus) and more accurate CPU utilization.

**Bug fixes**
- Fixed duplicate device rows for Thread-only and statically-IP'd devices ([#2](https://github.com/weirded/fleet-for-esphome/issues/2)). The scanner now resolves addresses through ESPHome's full `wifi → ethernet → openthread` precedence chain (each honoring `use_address` → `manual_ip.static_ip` → `{name}.local`), and the device poller correctly handles IPv6 mDNS records and merges discovery into existing YAML-derived rows by normalized name.
- Fixed `esphome run` prompting interactively when the worker host has multiple upload targets ([#22](https://github.com/weirded/fleet-for-esphome/issues/22)). The `--device` flag is now always passed (using the literal `"OTA"` when no specific address is known) so ESPHome never blocks waiting for stdin.
- Fixed OTA-only retries crashing with `unrecognized arguments: --no-logs` ([#21](https://github.com/weirded/fleet-for-esphome/issues/21)). `esphome upload` doesn't accept that flag — only `esphome run` does. The retry path now invokes `upload` without it.
- Fixed streamer mode not blurring IPs on unmanaged devices ([#19](https://github.com/weirded/fleet-for-esphome/issues/19)).
- Fixed Workers tab showing "up 5m" for offline workers based on stale process uptime. Now shows "offline for Xm" using the last heartbeat timestamp.
- Fixed queue duration showing the worker's compile time instead of wall-clock time. Now `Took 2m 14s` from enqueue to finish, and `Elapsed 45s` for in-progress jobs.
- Fixed queue sort defaulting to time instead of state — running jobs are now back at the top by default.
- Fixed validation request results showing the duplicate enqueue time twice in the queue.

**UI improvements**
- New "IP source" label under each device IP showing how the address was resolved: `via mDNS`, `wifi.use_address`, `wifi static_ip`, `ethernet static_ip`, `openthread.use_address`, or `{name}.local`. mDNS only "wins" over the default — explicit user choices stay authoritative because that mismatch is itself useful information.
- Queue tab: separate "Start Time" and "Finish Time" columns with absolute HH:MM:SS plus relative duration.
- "Clean Cache" button on online workers (per-worker) and "Clean All Caches" in the Workers tab header to clear stale ESPHome version caches without restarting workers.
- "Show unmanaged devices" toggle in the Devices column picker to hide mDNS discoveries with no matching config.
- Retry button now also available on successful jobs (not just failed) for the "I want to re-run this exactly" case.
- Worker compile commands now logged in the user-visible job log (cyan text) so bug reports include the exact command that ran.
- Image-stale badges turn the version cell red and link directly to the Connect Worker modal.

**Security hardening**
- Timing-safe Bearer token comparison (`secrets.compare_digest`) instead of `==`.
- Bounded log storage: worker-streamed logs capped at 512 KB per job, truncated with a marker (prevents OOM from runaway build output).
- `max_parallel_jobs` validation on worker registration (0–32, was unbounded).

**Codebase cleanup**
- New `helpers.py` (server) consolidates `safe_resolve`, `json_error`, `clamp`, `constant_time_compare` — replaces ~80 lines of inline path-traversal/error-response/auth code.
- Worker system info code extracted from `client.py` into `sysinfo.py`.
- Server constants (header names, supervisor IP, `secrets.yaml`) moved to `constants.py`.
- Test anti-patterns cleaned up: removed redundant `sys.path` from 7 test files, replaced hardcoded `/tmp` with `tmp_path`, converted queue tests to native async.

**Reorganized dev plans**
- Moved roadmap, release process, security audit, and per-release work-item files into a new `dev-plans/` directory.
- Released versions live under `dev-plans/archive/`.

## 1.2.0

**Built-in Local Worker** ([#4](https://github.com/weirded/fleet-for-esphome/issues/4))
- The add-on now includes a built-in build worker — no external Docker container required to get started
- Starts paused (0 slots); increase via the Workers tab to activate
- Great for HaOS setups where adding Docker containers is difficult

**Choose Which Worker Compiles** ([#5](https://github.com/weirded/fleet-for-esphome/issues/5))
- New "Upgrade on..." submenu in the device menu lets you pin a compile job to a specific worker
- Useful for debugging or when certain configs only work on specific hardware

**Docker Compose Support** ([#8](https://github.com/weirded/fleet-for-esphome/issues/8))
- Added `docker-compose.worker.yml` for easy worker deployment

**Configurable Device Columns**
- New columns: Area, Comment, Project (extracted from your YAML configs)
- Gear icon column picker to show/hide columns; preferences saved across sessions

**Redesigned UI**
- Modern design system with consistent buttons, modals, dropdowns, and badges
- Upgrade options consolidated into a single dropdown (All, All Online, Outdated, Selected)
- Device menu restructured into Device actions, Config actions, and worker submenu
- Search boxes on all three tabs (Devices, Queue, Workers)
- Queue actions grouped into Retry and Clear dropdowns
- Close button on all modals
- Copy to Clipboard button on compile and live log modals

**Worker Improvements**
- Simplified worker management: set slots to 0 to pause (removed separate Disable button)
- Disk space reporting with color warnings when running low
- Automatic cleanup of unused ESPHome versions when disk space is low
- Built-in worker highlighted and pinned to top of list

**Streamer Mode**
- New toggle in header blurs IPs, tokens, and sensitive data — useful for streams and screenshots

**Device Config Improvements**
- Better metadata extraction for configs using git packages (area, comment, project)
- Configs with substitution variables now resolve correctly in the device list

**Other Improvements**
- Validation output opens directly without cluttering the job queue
- "Version" column (renamed from "Running") shows firmware version more clearly
- Archived configs can be restored via new API endpoints
- Stale queue entries auto-cleaned after 1 hour
- Pinned worker preserved when retrying failed jobs

**Bug Fixes**
- Fixed OTA always using known device IP address
- Fixed timezone mismatch causing unnecessary recompiles
- Fixed editor content sometimes being wiped during poll cycles
- Fixed duplicate devices appearing after rename
- Fixed HA status not matching devices with non-standard entity names
- Fixed ESPHome install errors not showing in job log

## 1.1.0
Major update: React UI rewrite, ESPHome dashboard-grade features, Home Assistant integration.

**New React UI**
- Complete rewrite from vanilla JS to React + Vite + TypeScript
- Monaco YAML editor with ESPHome schema-aware autocomplete (697 components from installed package)
- Per-component config var suggestions fetched from schema.esphome.io
- !secret autocomplete from secrets.yaml, inline YAML syntax validation
- Save & Upgrade button (save + compile + OTA in one click)
- Unsaved change highlighting with line-level diff indicators
- Dark/light theme toggle with localStorage persistence
- Device search/filter bar across all columns

**Device Lifecycle**
- Rename device: updates config file, esphome.name, triggers compile+OTA to flash new name
- Delete device: archive to .archive/ or permanent delete with confirmation dialog
- Restart device via native ESPHome API (aioesphomeapi button_command) with HA REST fallback

**Live Device Logs**
- WebSocket streaming via aioesphomeapi with full ANSI color support in xterm.js
- Boot log included (dump_config=True)
- Timestamps on each log line [HH:MM:SS]
- Works with encrypted API connections (noise_psk)

**Compile Improvements**
- Switched to `esphome run --no-logs` (single process compile+OTA, matches native ESPHome UI)
- Colorized compile logs: INFO=green, WARNING=yellow, ERROR=red
- OTA retry with 5s delay on failure (keeps job in WORKING state for proper re-queuing)
- Server timezone passed to workers (prevents config_hash mismatch and unnecessary clean rebuilds)
- OTA always uses explicit --device with known IP address
- ESPHome install errors now visible in streaming job log

**Home Assistant Integration**
- Background poller detects ESPHome devices registered in HA via template API + /api/states
- MAC-based device matching (queries HA device connections) — most reliable method
- Name-based fallback: friendly_name, esphome.name, filename stem, MAC fragment matching
- HA column in Devices tab shows configured status (Yes/—)
- HA connectivity (_status binary_sensor) feeds into online/offline column
- Device restart via HA REST API as fallback when native API unavailable

**Config Validation**
- Validate button saves editor content first, then runs esphome config
- Validation opens streaming log modal directly (no toast intermediary)
- Badge shows Validating/Valid/Failed status in queue

**Performance**
- Concurrent device polling via asyncio.gather (all devices checked in parallel)
- HA entity poller runs immediately on startup (no 30s delay)
- Config resolution caches git clones (skip_update=True after first resolution)
- PyPI version list increased from 10 to 50

**UI Polish**
- Per-row Clear button in queue tab
- Edit buttons in queue rows and log modal header
- Hamburger menu redesigned: vertical ellipsis icon, plain text styling
- Live Logs and Restart moved to hamburger menu (never grayed out)
- Light mode: dark header for ESPHome logo readability, themed form inputs
- "Checking..." state with pulsing dot on startup (instead of showing offline)
- Copy API Key, Rename, Delete in device hamburger menu

**Operations**
- Suppressed aioesphomeapi.connection warnings (expected when devices offline)
- ESPHome add-on version detection at DEBUG level (no log spam)
- Debug endpoint GET /ui/api/debug/ha-status for HA matching troubleshooting
- Queue remove-by-ID endpoint for per-job clearing

**Bug Fixes**
- 89 bugs tracked and fixed during development (see BUGS.md)
- Fixed polling interval explosion (React useEffect dependency bug)
- Fixed editor content wiped on parent re-render (useRef pattern)
- Fixed disabled button CSS specificity (!important on all disabled properties)
- Fixed duplicate devices after rename (old entry removed from poller)
- Fixed modal closing on drag-select (mousedown target tracking)
- Fixed DeprecationWarning on app state mutation (clear+update pattern)

## 1.0.0
First stable release. Distributed ESPHome compilation with a full web UI.

**Distributed Compilation**
- Job queue with PENDING → WORKING → SUCCESS/FAILED state machine
- Performance-based job scheduling (fastest idle worker first, spread evenly)
- Workers report CPU benchmark, real-time utilization, system info
- Effective score = perf_score × (1 - cpu_usage/100) for load-aware scheduling

**Web UI**
- Three tabs: Devices, Queue, Workers
- xterm.js live log viewer with WebSocket streaming and ANSI support
- Monaco YAML editor with basic keyword completion
- ESPHome version dropdown (detect from HA add-on, select from PyPI)
- Connect Worker modal with configurable docker run command generator
- Auto-reload UI on server version change (X-Server-Version header)

**Device Management**
- mDNS device discovery + ping fallback + wifi.use_address support
- Device-to-config matching using ESPHome's full config resolution pipeline
- Encrypted API connections (extracts api.encryption.key from configs)
- Config change detection (file mtime vs device compilation time)
- Proactive device entries for use_address configs (no mDNS required)
- HA ESPHome add-on version detection via Supervisor API

**Build Workers**
- Docker-based remote workers with auto-update
- System info reporting (CPU, memory, OS, architecture, uptime)
- Persistent worker identity across restarts
- Clean deregistration on shutdown (SIGTERM handler)
- OTA firmware upload with retry and network diagnostics on failure
- OTA retry jobs pinned to original worker (PlatformIO cache reuse)

**Operations**
- Resolved config caching (mtime-based, eliminates repeated git clones)
- Suppressed noisy HTTP access logs
- hassio_api integration for ESPHome version detection
- host_network for mDNS device discovery
- Multi-arch Docker images (amd64 + arm64) published to GHCR

## Pre-1.0 Development History

See git history for detailed changes during the 0.0.1–0.0.73 development period.
