# Work Items — Future / Advanced Features

Items with less certainty on scope or priority. Will be shaped into a release when the time comes. Releases are scope-driven, not time-boxed (see `CLAUDE.md` → Project Tracking) — items graduate from here into a specific `WORKITEMS-X.Y.md` file only when they're selected for a release, not on a calendar.

Section order here is not a priority signal.

## GitHub Sync (optional remote)

Connect the local git repo to a GitHub (or any git remote) for backup and team collaboration. Private repos work identically to public. Pairs naturally with 1.6's auto-versioning (AV.*) — once every save is a git commit, pushing upstream is the obvious next step.

**Auth options:** GitHub Personal Access Token (HTTPS) or SSH deploy key. Stored in add-on options (encrypted at rest by HA Supervisor). No OAuth flow needed — PATs are simpler and work for private repos.

- [ ] **GS.1 Remote configuration** — add-on options: `git_remote_url` (string, e.g. `https://github.com/user/esphome-configs.git`), `git_remote_token` (secret string, PAT for HTTPS auth), `git_remote_ssh_key` (secret string, for SSH auth). On startup, if configured, run `git remote add origin <url>` (or update if remote exists). Validate connectivity with `git ls-remote`.
- [ ] **GS.2 Push** — `POST /ui/api/git/push`. Runs `git push origin main`. Called automatically after each auto-commit batch (configurable: after every commit, every N minutes, or manual-only). Auth via credential helper for HTTPS or SSH key file for SSH. Surfaces errors (auth failure, force-push rejected, network) via the `/ui/api/info` response so the UI can show a banner.
- [ ] **GS.3 Pull** — `POST /ui/api/git/pull`. Runs `git pull --rebase origin main`. Called on startup (if remote configured) and on-demand via UI button. On conflict: abort the rebase, keep local, log the conflict at WARNING, surface it in the UI as "Remote has changes that conflict with local edits — resolve manually or force-push".
- [ ] **GS.4 Sync status UI** — indicator in the header or settings page showing: last push time, last pull time, sync errors. "Push now" / "Pull now" buttons.
- [ ] **GS.5 `.gitignore` management** — ensure `secrets.yaml` is always in `.gitignore` (auto-add on init and on remote config). Warn in the UI if `secrets.yaml` has been committed (it contains WiFi passwords and API keys).

## HA Native Updates

Make the per-device update flow behave like HA's stock [`esphome` integration](https://www.home-assistant.io/integrations/esphome/) — surfaced in **Settings → Updates**, clickable from the HA frontend update card, alongside HA Core / add-on / HACS updates. Extends HI.3 from 1.4.1 (which delivered the basic `UpdateEntity` per managed device) with the polish that makes HA-native update UX actually feel native: release notes, progress reporting, skip-version persistence, and "update all" coalescing.

**Trigger path:** user clicks Install in HA's update card → HA calls `update.install` service on `update.esphome_fleet_<device>` → our `EsphomeFleetUpdate.async_install()` → `POST /ui/api/compile` → job lands in Fleet's queue → progress reports back via coordinator polling → `in_progress` reflects on the HA entity. No separate Fleet UI for updates — HA's update card is the one surface.

Scope: pure `custom_integration/esphome_fleet/` work. No add-on UI changes, no worker changes.

| Area | HI.3 in 1.4.1 | UE.* later |
|---|---|---|
| `UpdateEntity` exists | ✅ | — |
| `installed_version` + `latest_version` (global) | ✅ | — |
| Install button → compile API | ✅ | — |
| `release_url` (points at ESPHome's changelog) | ❌ | ✅ |
| `release_summary` (breaking changes snippet) | ❌ | ✅ |
| `entity_picture` (device-specific icon) | ❌ | ✅ |
| `in_progress` / `update_percentage` wired to queue state | ❌ | ✅ |
| Pinned-version awareness in `latest_version` | ❌ | ✅ |
| "Skip this version" persists across polls | ❌ | ✅ |
| "Update all" coalesces into one batch compile run | ❌ | ✅ |

- [ ] **UE.1 ESPHome release notes fetcher** — coordinator gains a 24h-cached fetch of ESPHome's GitHub releases metadata (`api.github.com/repos/esphome/esphome/releases/tags/<version>`). No auth needed (public API; unauth rate limit is 60/hr, this is <1 req/day). Returns `release_url` and a parsed `release_summary` (first paragraph or the "breaking changes" section if present). Cache keyed by `<version>`; eviction on coordinator reload.
- [ ] **UE.2 Wire release metadata on entities** — `EsphomeFleetUpdate` entity sets `release_url` and `release_summary` from UE.1's cache for the current `latest_version`. HA's update card renders these under the "Install" button as expected.
- [ ] **UE.3 `entity_picture`** — pick a device-specific image. Start simple: one icon per platform family (esp32 / esp8266 / rp2040 / …) derived from the target's `platform` / `board`. Future extension: let the user set an explicit `entity_picture_url` in the `# distributed-esphome:` comment block (same pattern as `tags`, `pin_version`, `schedule`), and have the entity prefer that if set.
- [ ] **UE.4 Progress reporting** — wire `in_progress: True` on the entity when the user-initiated compile job is PENDING or WORKING; derive `update_percentage` from either the worker's status_text (if it carries a percent) or from elapsed-vs-expected compile time. Clear `in_progress` when the job reaches any terminal state. Enables HA's update card progress bar.
- [ ] **UE.5 Pinned-version awareness** — if the target has `pinned_version` set in its YAML metadata, the entity's `latest_version` is the pinned version (not the global default). HI.3 already handles the compile-time pin resolution; UE.5 is the display-side counterpart so HA doesn't show "Update available → 2026.4.0" to a user who deliberately pinned a device to 2026.3.3.
- [ ] **UE.6 Skip-version persistence** — HA's `UpdateEntity` natively supports a "Skip" action that writes to HA's own state store (`skipped_version`). The coordinator must not clobber that when it refreshes `latest_version`. Verify the entity reports the HA-stored skipped version correctly and the "Update available" badge stays suppressed until a newer version ships. Mostly a *don't-break-what-HA-gives-you-for-free* task, plus a test.
- [ ] **UE.7 "Update all" coalescing** — when HA fires `update.install` on every `update.esphome_fleet_*` entity concurrently (the Updates card's "install all" button does this), the coordinator should batch them into a single compile run with a shared `run_id`, rather than N independent compile calls. Use Fleet's existing `POST /ui/api/compile` which already accepts a target list + returns a `run_id`. Batch window: ~2 seconds after the first incoming install call.
- [ ] **UE.8 Tests** — new `tests/test_integration_update_entities.py` covering: `release_url` format + cache behavior, `release_summary` extraction from typical ESPHome release notes markdown, `in_progress` transition on job state changes, pinned-version display, skip-version persistence, update-all batching. Extends the coverage that HI.12 already laid down for the integration scaffold.

## PlatformIO Package Cache Proxy

Optional caching proxy on the server that intercepts workers' PlatformIO downloads. First worker to compile a given platform/toolchain fetches from the internet; every subsequent worker gets it from the server over LAN in seconds. Eliminates the biggest time sink in cold compiles (~200-400MB of toolchains downloaded per platform per worker).

```
Worker --HTTP--> Server :8766 --HTTPS--> registry.platformio.org / github.com
                    |
              /data/pio-cache/
              (LRU, disk-limited)
```

Enabled via add-on option `pio_cache_enabled: true` (default off). Workers detect the proxy URL from the server info response and set `HTTPS_PROXY` on the ESPHome subprocess. Workers that can't reach the proxy (e.g., running outside the LAN) fall back to direct downloads transparently.

- [ ] **PC.1 Caching proxy listener** — new `aiohttp` app on a second port (e.g., 8766), started conditionally when `pio_cache_enabled` is set. Handles standard HTTP proxy `CONNECT` requests: opens upstream HTTPS connection, streams response to the worker, writes the response body to `/data/pio-cache/<sha256(url)>` on first request. Subsequent requests for the same URL serve from disk. Only caches responses from `*.platformio.org` and `github.com/platformio/*` — all other traffic is passed through uncached.
- [ ] **PC.2 Cache storage + LRU eviction** — `/data/pio-cache/` directory. Each cached file has an access-time timestamp updated on every hit. Background task runs eviction when total size exceeds `pio_cache_max_gb` (add-on option, default 5GB). Evicts least-recently-accessed files first until under the limit. Cache stats exposed via `/ui/api/server-info` (`pio_cache_size_mb`, `pio_cache_entries`).
- [ ] **PC.3 Worker integration** — worker reads `pio_proxy_url` from the `/api/v1/workers/register` or heartbeat response (server advertises it when enabled). Sets `HTTPS_PROXY=<url>` on the `subprocess_env` passed to ESPHome. Falls back to no proxy if the field is absent or the proxy is unreachable (connectivity check with a 2s timeout on job start).
- [ ] **PC.4 Add-on options** — `pio_cache_enabled` (bool, default false), `pio_cache_max_gb` (float, default 5.0), `pio_cache_port` (int, default 8766). Documented in `DOCS.md` and `config.yaml` schema.
- [ ] **PC.5 Cache management UI** — server info panel or settings section showing: cache enabled/disabled, current size / limit, entry count. "Clear cache" button. Workers tab shows per-worker "Proxy: yes/no" indicator.
- [ ] **PC.6 Tests** — unit tests: proxy caches a GET response, second request serves from disk, LRU eviction removes oldest when over limit, non-platformio URLs pass through uncached. Integration test: two sequential `pip install platformio && pio pkg install` calls, second is faster.

## Import

- [ ] **2.1c Create device: import from URL** — fetch config from GitHub/project URL

## Config versioning refinements

- [ ] **Move archive under git tracking (from 1.6 #62).** Today `delete` renames a YAML into `.archive/` (which the fresh-init gitignore excludes) and commits the delete as a plain "file removed" entry; restore renames back and commits as a plain "file added". That's easy to reason about but drops the file's history on a delete-then-restore. Alternative: un-ignore `.archive/`, swap the raw `Path.rename` for `git mv`, and commit the archive as a move — `git log --follow` then threads the file's history across the archive/restore boundary. Cost: `.archive/` contents start showing up in the repo (YAML-only, small, but visible in `git status`). Evaluate whether history continuity is worth the extra tracked surface — decision point when the file-tree editor lands (which makes `.archive/` directly browsable anyway).

## Perf hardening

- [ ] **PH.1 Cache firmware-variant presence per `/ui/api/history` page load.** Today `JobHistoryDAO.query()` calls `firmware_storage.list_variants(job_id)` once per row and each call does its own `os.listdir` of `/data/firmware/`. At `limit=50` + infinite-scroll that's O(rows × directory-size) stat per scroll session. Fine at home-lab scale (dozens of download-only binaries). Revisit when someone actually accumulates hundreds and the Queue-History dialog feels laggy. Two viable shapes: (a) single `os.scandir` snapshot per `query()` call, build per-id lookup from the cache; (b) denormalize variant-presence into the DB at firmware-write time and invalidate on eviction (avoids the probe entirely). Surfaced from the PR #64 code review. Referenced by the `TODO(PH.1)` comment at `ha-addon/server/job_history.py:~435`.

## LLM-Augmented UX

Introduce an optional LLM backend to generate content that currently falls back to hand-curated templates. First use case (from 1.6 bug #34): replace the static `"Automatically saved after editing in UI"` auto-commit subject with an LLM-generated one-line summary of the actual diff ("Tuned PWM duty cycle", "Added wifi fallback AP", etc). Opt-in — no LLM calls unless the user configures a provider + key. Candidate providers: Anthropic, OpenAI, local Ollama. Scope a dedicated release (naming TBD) when we're ready to commit to the operational cost / privacy tradeoffs.

## Device Organization

Key/value tags (like AWS resource tags), stored in the per-device `# distributed-esphome:` comment block as a `tags:` map. Users can group the Devices table by any tag key (Notion-style table groups) and filter by `key=value`.

Format in the YAML comment block:
```yaml
# distributed-esphome:
#   tags:
#     location: kitchen
#     floor: "1"
#     env: prod
#     owner: stefan
```

The existing `tags` field landed in 1.4.0-dev.2 as a simple list of strings — that needs to migrate to a key/value map. `read_device_meta()` should accept both shapes during the transition (list → coerce to `{tag: ""}` or warn-and-ignore) and `write_device_meta()` always writes the map shape going forward.

- [ ] **DO.1 Tag schema migration** — `read_device_meta()` accepts either list-of-strings (legacy) or string-keyed map; normalizes to map on read. `write_device_meta()` always writes the map. Add a unit test that round-trips both shapes.
- [ ] **DO.2 Tag CRUD endpoints** — `POST /ui/api/targets/{f}/tags` (set, body `{key, value}`), `DELETE /ui/api/targets/{f}/tags/{key}` (clear). Reuses `read_device_meta()` / `write_device_meta()`. Validates key is non-empty, max 64 chars, no leading/trailing whitespace; value is string, max 256 chars (allow empty for "key present, no value").
- [ ] **DO.3 Tag editor UI** — modal opened from the device hamburger menu ("Edit tags…"). Shows current tags as editable rows: `[key] [value] [×]` plus an "+ Add tag" button. Save persists via `POST /ui/api/targets/{f}/tags` for each changed entry. Datalist autocomplete on `key` from the union of all keys currently in use across the fleet.
- [ ] **DO.4 Tag column** — toggleable "Tags" column on the Devices tab showing each device's tags as compact `key=value` chips (truncated, full set in tooltip). Sortable by string representation.
- [ ] **DO.5 Group-by-tag selector** — top-of-table dropdown: "Group by: [None / location / floor / env / …]". When set, rows are grouped under sticky group headers showing the value (e.g., "location: kitchen — 4 devices"). Devices without that tag key fall into an "— unset —" group at the bottom. Group state persists in localStorage. Like Notion table groups: collapsible group headers, group-level select-all checkbox.
- [ ] **DO.6 Filter by tag** — top-of-table filter chips: click a tag chip in any row to add it as a filter (`location=kitchen`). Multiple chips AND together. Clear-all button. Filter state in URL query string so it survives reloads and is shareable.
- [ ] **DO.7 Bulk tag operations** — extend multi-select on the Devices tab: "Set tag…" (prompts for key+value, applies to all selected via `Promise.all`), "Remove tag…" (prompts for key, removes from all selected). Single summary toast per bulk action.
- [ ] **DO.8 Bulk delete + bulk validate** *(formerly 6.6)* — extend multi-select: bulk delete and bulk validate alongside the existing bulk upgrade.

## Disk Management

LRU-based disk usage controls for both the server and workers. Currently nothing caps the growth of ESPHome version caches, PlatformIO toolchains, compiled firmware, build directories, or job logs. On a worker with limited disk (e.g., a Raspberry Pi), these can silently fill the volume.

### Worker-side

- [ ] **DM.1 Worker disk budget** — new env var `MAX_DISK_USAGE_GB` (default: unlimited). On each job completion, the worker checks total usage of `/esphome-versions/` (versions + builds + PlatformIO). If over budget, evicts in LRU order: oldest unused ESPHome version venvs first (already has `MAX_ESPHOME_VERSIONS` for version count — this adds a size-based cap), then oldest build cache directories, then oldest PlatformIO packages. Logs what was evicted at INFO.
- [ ] **DM.2 Worker disk stats in heartbeat** — add `disk_total_mb`, `disk_used_mb`, `disk_free_mb` (for the `/esphome-versions` mount point) to the worker's `system_info` heartbeat. Server surfaces these on the Workers tab so operators can see when a worker is running low before it fails a compile.
- [ ] **DM.3 Build cache LRU** — the per-target build cache (`/esphome-versions/cache/<target>/`) currently grows unboundedly. Add LRU eviction: track last-access time per target cache dir, evict oldest when total build cache exceeds `MAX_BUILD_CACHE_GB` (env var, default 10GB). The existing `MAX_ESPHOME_VERSIONS` (count-based) stays for version venvs; this adds size-based eviction for the build artifacts.

### Server-side

- [ ] **DM.4 Server disk budget for caches** — `firmware_cache_max_gb` (default 2.0) and `job_log_retention_days` (default 30) live in the Settings store (see 1.6 SP.*). Background task prunes `/data/firmware/` and old job logs on a daily schedule, reading current values from `get_settings()` each run (live-effect). Exposed in the Settings drawer under `Disk management`.
- [ ] **DM.5 Disk usage dashboard** — section on the Workers tab or a new Settings page showing: per-worker disk breakdown (versions, builds, PlatformIO, total), server-side cache sizes (PIO proxy cache, firmware, job logs), and the configured limits. Visual bar showing used/limit per category.

## Worker Constraints

Let users declare which workers can run which jobs. Originated in [issue #59](https://github.com/weirded/fleet-for-esphome/issues/59) ("Thread devices can't be reached from Windows desktop due to IPv6 limitation — can you auto-detect?"). **Reframed:** instead of trying to probe network reachability (fragile, slow, guesses wrong), let the user express their knowledge of the topology as declarative rules. This also generalizes to every other worker-selection need a user might have — "encrypted devices only go to the on-prem worker", "big configs only go to the beefy worker", "dev YAMLs only go to my laptop".

### Foundation: durable worker identity

The current `client_id` is an auto-generated UUID persisted to `/esphome-versions/.client_id` inside the worker's volume (`client.py:119,213-227`). If the volume wipes, the container gets rebuilt on a different host, or the user blows away their worker setup, they get a new UUID — breaking any saved config that referenced the old one. Worker constraints need a more durable identifier. The answer is: let the user name their workers.

- [ ] **WC.1 `WORKER_NAME` env var** — new optional env on the client (`client.py`). When set, it becomes the worker's primary identifier instead of the auto-UUID. When unset, fall back to the current auto-UUID behavior for backwards compatibility. `WORKER_NAME` values must match `^[a-z0-9][a-z0-9-]{0,63}$` (same slug rules as device names) so they're safe in URLs and UI chips.
- [ ] **WC.2 `WORKER_TAGS` env var** — comma-separated list of free-form tags, e.g. `WORKER_TAGS=ipv6,beefy,on-prem`. Sent at registration and on every heartbeat. Server surfaces them on `/ui/api/workers` for display and for constraint evaluation.
- [ ] **WC.3 Server: name-keyed registry** — `registry.py` accepts a `name` field on `RegisterRequest` (via `protocol.py` — **PROTOCOL_VERSION bump**, see note below). Registry key preference: `name` if provided, else `client_id`. If two workers register with the same `name`, the later one wins (logs a warning about the collision). Existing UUID-keyed workers continue to work unchanged.
- [ ] **WC.4 Protocol extension** — add `name: Optional[str]` and `tags: List[str]` to `RegisterRequest` and `HeartbeatRequest` in both `ha-addon/server/protocol.py` and `ha-addon/client/protocol.py` (byte-identical per PY-6). This is an **additive protocol change** — old workers sending neither field still register fine — so `PROTOCOL_VERSION` stays at its current value per the protocol.py docstring rule ("additive + optional unless PROTOCOL_VERSION is bumped").
- [ ] **WC.5 UI: show worker name + tags** — Workers tab's Hostname column becomes "Name / Hostname": shows `WORKER_NAME` prominently if set, falls back to hostname. New toggleable "Tags" column rendering tags as chips. Connect Worker modal's generated `docker run` command includes `-e WORKER_NAME=<slug>` and an optional `-e WORKER_TAGS=<tags>` pre-filled with a hint.

### Constraint expression

Declarative matching between targets and workers, stored in the per-device `# distributed-esphome:` YAML comment block (same pattern as tags, pin_version, schedules). Constraints are **additive to the existing `pinned_client_id`** — pinning a specific worker on a job still wins over general constraints.

- [ ] **WC.6 Target-side constraint fields** — extend the per-device metadata comment:
  ```yaml
  # distributed-esphome:
  #   worker_requires:       # worker must have ALL of these tags
  #     - ipv6
  #   worker_forbids:        # worker must have NONE of these tags
  #     - cloud
  #   worker_only:           # whitelist by worker name (overrides tags if set)
  #     - home-beefy
  ```
  `read_device_meta` / `write_device_meta` extended to parse + emit these three fields. Surfaced on `/ui/api/targets` as `worker_requires`, `worker_forbids`, `worker_only`.

### Evaluation

- [ ] **WC.7 `claim_next` constraint filter** — in `job_queue.claim_next()`, before a worker can claim a job, check whether its name/tags satisfy the job's target constraints. If not, the job is skipped for that worker and remains PENDING for the next eligible worker to claim. The existing `pinned_client_id` check runs first (explicit pin wins); constraints run second.
- [ ] **WC.8 "No eligible worker" detection** — when a job has been PENDING for > N minutes AND no currently-online worker satisfies its constraints, mark it FAILED with a clear message: *"No eligible worker: target requires tags [ipv6]; online workers: home-pi (tags: []), office-desktop (tags: [beefy])"*. This is the "stuck forever" failure mode — better to fail loudly than hang.

### UI

- [ ] **WC.9 Constraint editor modal** — per-device hamburger menu item "Worker constraints…" opens a modal with three fields (required tags, forbidden tags, whitelisted worker names) plus an "Available tags / workers" hint that lists tags currently in use across the fleet. Saves via the existing `POST /ui/api/targets/{f}/meta` generic endpoint.
- [ ] **WC.10 Queue tab: surface constraint misses** — when a job is PENDING longer than expected and the reason is constraints, the Queue tab's status text shows it (e.g. *"Waiting — no worker tagged `ipv6` is online"*) so the user doesn't stare at a pending job wondering what's wrong.
- [ ] **WC.11 E2E coverage** — mocked Playwright test for the constraint editor modal. Prod hass-4 test: tag one worker with `ipv6`, set `worker_requires: [ipv6]` on a target, trigger compile, verify it lands on the tagged worker (and fails fast with the clear message if no tagged worker is online).

### Notes & non-goals

- **Not auto-detection.** This is deliberately user-declarative. We don't probe reachability, we don't ping from each worker, we don't parse YAML for `manual_ip` to infer things. Users know their network better than we do, and probe-based logic is a maintenance tax.
- **Backwards compatibility.** Workers without `WORKER_NAME` keep UUIDs. Targets without `worker_requires`/`worker_forbids`/`worker_only` accept any worker. Zero breaking changes.
- **Relationship to `pinned_client_id`.** Explicit pin in the UpgradeModal still overrides everything — that's a one-shot manual choice and shouldn't be filtered out by general constraints. Constraints are the persistent default; pins are the override.
- **Related, out of scope:** per-worker `WORKER_CAN_RUN` whitelists (the inverse: worker declares which targets it accepts). Makes sense for shared workers in mixed-trust environments but not needed for the home-lab use case. Revisit in a future release if requested.

## File Tree Editor

Browse and edit any file in the ESPHome config directory, including subdirectories. VS Code-style file tree sidebar in the editor modal.

- [ ] **FT.1 `GET /ui/api/files`** — recursive directory listing, returns flat `[{path, size, binary}]`
- [ ] **FT.2 `GET /ui/api/files/{path:.+}`** — read file by relative path (path traversal prevention)
- [ ] **FT.3 `POST /ui/api/files/{path:.+}`** — write file (invalidates config cache for .yaml)
- [ ] **FT.4 Install `@headless-tree/core` + `@headless-tree/react`** — headless tree library
- [ ] **FT.5 `FileTree.tsx` component** — flat list → tree, expand/collapse, active highlight, binary grayed out
- [ ] **FT.6 Sidebar layout** — editor body flex row: `[file tree 240px] | [monaco flex-1]`, sidebar toggle
- [ ] **FT.7 File switching** — dirty check → load/save, language detection by extension
- [ ] **FT.8 Conditional buttons** — Save & Upgrade/Validate/Rename only for entry-point YAML; includes get Save only
- [ ] **FT.9 API functions** — `listFiles()`, `readFile()`, `writeFile()` in client.ts
- [ ] **FT.10 Git status badges** — show modified/untracked badges on files in the tree using `git status --porcelain`. Pairs with 1.6's auto-versioning (AV.*) — the file tree becomes the place Pat sees at a glance which files have uncommitted edits. Fetched once per tree load + re-fetched on save. Small dot or letter glyph next to the filename; hover reveals the status ("modified" / "untracked" / "staged").

## Integration Quality Scale

Carried forward from 1.6.1 (QS.8 was deferred there; the bronze → silver flip landed in 1.6.1 as QS.9). Graduates to a `WORKITEMS-X.Y.md` when we're ready to commit the cycle budget.

- [ ] **QS.8 Test coverage to ≥95% (real-hass lifecycle)** *(carried forward from 1.6.1)* — blocked on the follow-up to IT.2 (1.6.0-dev.27): the `test_integration_setup.py` real-hass scaffolding is in place but the first lifecycle tests are `@pytest.mark.skip`-ed pending `async_mock_integration` wiring for the integration's package layout under `ha-addon/custom_integration/`. The existing 17-case suite across `test_integration_*_logic.py` covers setup, services, coordinator auth + events, device_info, installer, config flow, diagnostics, system health, stale devices, and reconfigure using mock `hass` fixtures — honest bronze-plus / silver-tier coverage. Gold-grade coverage requires real-hass fixture coverage of the lifecycle (entry load/unload/reload, listener leak detection, platform registration). Unblocks the `quality_scale: silver` → `gold` flip.

## Bugs blocked on upstream HA Supervisor changes

Tracked deferrals for community-reported bugs whose real fix needs a Supervisor capability that doesn't exist today. The GH issue is the system of record; this section preserves the analysis so a future release doesn't have to re-derive it. Graduate when the upstream blocker resolves.

- [ ] **Port 8765 conflicts with the HA OpenThread Border Router add-on (gh [#114](https://github.com/weirded/fleet-for-esphome/issues/114)).** Reported by chilled35: every fresh start crashes with `OSError: [Errno 98] error while attempting to bind on address ('0.0.0.0', 8765): address already in use` because OTBR (`core_openthread_border_router`) also listens on 8765. Reporter's investigation shows the real fix — parameterising the bind port via `config.yaml` options — is blocked on HA Supervisor not supporting dynamic ingress ports; the user-visible port option is cosmetic today. Workaround for affected users (in the GH thread): run OTBR on a different host, or run Fleet standalone outside HA on a host where 8765 is free. Re-scope into a release when Supervisor exposes dynamic ingress ports. One reporter so far; deferred from 1.7.1 because the only actionable fix at our level is documentation, and the population of users hitting the conflict is too small for a Known-Limitations entry to pull weight.

## ESPHome delegation (carryovers from 1.7.0)

Audit on 2026-04-22 of `ha-addon/server/` + `ha-addon/client/` looking for places we re-implement functionality ESPHome ships as a library (`esphome.*`) or a CLI (`esphome <subcommand>`). EH.2 landed in 1.6.2-dev.11 along the #84 fix (ESPHome's full validator became the primary resolver). Three remaining items, deferred from 1.7.0 when tags + routing consumed the bandwidth, then bumped out of 1.7.1 when scope tightened to "rebrand + low-hanging community bugs". Not user-visible; pure internal cleanup. Graduate when a release has the bandwidth.

- [ ] **EH.1 Use `esphome idedata` for firmware-artifact discovery.** `ha-addon/client/client.py:1225-1275` (`_collect_firmware_variants`) walks `.esphome/build/<name>/.pioenvs/<name>/firmware.{factory,}.bin` via hardcoded path templates. Stable for ESP32/ESP8266 today; breaks the moment ESPHome ships a target platform with a different PlatformIO layout (RP2040, nRF52, Zephyr, the new `host` platform). ESPHome's `esphome idedata <yaml>` (and `esphome.platformio_api.get_idedata()` Python API) emits JSON including `firmware_elf_path`, `firmware_bin_path`, and `extra_flash_images` (list of `{offset, path}` entries for bootloader/partition-table blobs). Fix: after a successful `esphome run`, invoke `<venv>/bin/esphome idedata <yaml>` and parse the JSON as the authoritative artifact manifest; retain today's path walk as a legacy fallback (probe once per job, cache the decision). Bonus: `extra_flash_images` unblocks a future USB-first-flash flow without further reinvention (currently we only archive `firmware.factory.bin` + `firmware.bin`; bootloader + partition table are silently dropped). Test: `tests/test_client_firmware_collection.py` across ESP32 (factory+ota), ESP8266 (ota only), and a synthetic RP2040 fixture; mocked `idedata` JSON drives the parse path and a real compile against `cyd-office-info` proves the wire.

- [ ] **EH.3 Replace magic-string config keys with `esphome.const` imports.** `ha-addon/server/scanner.py` and `ha-addon/client/client.py` reference `"esphome"`, `"name"`, `"wifi"`, `"ethernet"`, `"openthread"`, `"api"`, `"substitutions"`, `"packages"`, `"platform"`, `"framework"`, `"board"`, `"use_address"`, `"manual_ip"`, `"static_ip"`, `"domain"` as string literals scattered across many call sites. ESPHome's own code uses `CONF_ESPHOME`, `CONF_NAME`, `CONF_WIFI`, `CONF_ETHERNET`, `CONF_OPENTHREAD`, `CONF_API`, `CONF_SUBSTITUTIONS`, `CONF_PACKAGES`, `CONF_PLATFORM`, `CONF_FRAMEWORK`, `CONF_BOARD`, `CONF_USE_ADDRESS`, `CONF_MANUAL_IP`, `CONF_STATIC_IP`, `CONF_DOMAIN` from `esphome.const` — any upstream rename becomes an `ImportError` in our layer instead of a silent dict-miss that drops `friendly_name` / `use_address` / etc. for every user. Mechanical sweep; `ruff` + `mypy` + existing test suite catches typos. Scope the change to files where ESPHome is already on `sys.path` (i.e. anywhere touched after `_esphome_ready` fires) — the cold-start fallback paths keep the literal strings so they work before the venv is installed. UD.4's pairing note (use `CONF_BLUETOOTH_PROXY` once the magic-string sweep lands) folds in here — UD.4 itself shipped in 1.7.0-dev.18 via bug #23.

- [ ] **EH.4 Simplify ESPHome version detection.** `ha-addon/server/scanner.py:207-228` (`_get_installed_esphome_version`) shells to `<venv>/bin/esphome version` and string-parses `"Version: X.Y.Z"` on stdout as the primary path; `importlib.metadata.version("esphome")` at lines 232-234 is a fallback. Once the venv's `site-packages` is on `sys.path` (which happens before `_esphome_ready` fires) the two paths return the same answer — the subprocess is redundant and ~50ms of fork+exec on a hot path. Fix: either (a) reorder so `importlib.metadata` is primary and subprocess is the disambiguator when we have reason to believe server-process Python is pointing at a different ESPHome than the venv, or (b) import `esphome.const.__version__` directly (single attr read, no subprocess, no parsing). Keep the current memoization. Lowest priority of the three; bundle with any nearby scanner.py work rather than as a standalone PR. Test: `tests/test_scanner_version.py` with and without the venv activated, asserting the two paths agree and the subprocess is skipped on the warm path.
