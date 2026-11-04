# Work Items — Future / Advanced Features

Items with less certainty on scope or priority. Will be shaped into a release when the time comes. Releases are scope-driven, not time-boxed (see `CLAUDE.md` → Project Tracking) — items graduate from here into a specific `WORKITEMS-X.Y.md` file only when they're selected for a release, not on a calendar.

Section order here is not a priority signal.

## GitHub Sync (optional remote)

Connect the local git repo to a GitHub (or any git remote) for backup and team collaboration. Private repos work identically to public. Pairs naturally with 1.6's auto-versioning (AV.*) — once every save is a git commit, pushing upstream is the obvious next step.

**Auth options:** GitHub Personal Access Token (HTTPS) or SSH deploy key. Stored in add-on options (encrypted at rest by HA Supervisor). No OAuth flow needed — PATs are simpler and work for private repos.

- [ ] **GS.1 Remote configuration** — **settings-drawer fields** (not add-on options — SP.8 in 1.6 moved every user-facing setting into `/data/settings.json`, and `config.yaml` deliberately ships empty `options:`/`schema:` blocks; note `settings.json` is plain JSON, *not* Supervisor-encrypted, so a stored PAT rides along in every HA backup and must be added to the diagnostics redaction set): `git_remote_url` (string, e.g. `https://github.com/user/esphome-configs.git`), `git_remote_token` (secret string, PAT for HTTPS auth), `git_remote_ssh_key` (secret string, for SSH auth — **no runtime image currently installs an SSH client**, so this half needs an apt-layer change plus a PY-4 `IMAGE_VERSION`/`MIN_IMAGE_VERSION` bump; PAT-over-HTTPS needs neither and should ship first). On startup, if configured, run `git remote add origin <url>` (or update if remote exists). Validate connectivity with `git ls-remote`.
- [ ] **GS.2 Push** — `POST /ui/api/git/push`. Runs `git push origin main`. Called automatically after each auto-commit batch (configurable: after every commit, every N minutes, or manual-only). Auth via credential helper for HTTPS or SSH key file for SSH. Surfaces errors (auth failure, force-push rejected, network) via the `/ui/api/info` response so the UI can show a banner.
- [ ] **GS.3 Pull** — `POST /ui/api/git/pull`. Runs `git pull --rebase origin main`. Called on startup (if remote configured) and on-demand via UI button. On conflict: abort the rebase, keep local, log the conflict at WARNING, surface it in the UI as "Remote has changes that conflict with local edits — resolve manually or force-push".
- [ ] **GS.4 Sync status UI** — indicator in the header or settings page showing: last push time, last pull time, sync errors. "Push now" / "Pull now" buttons.
- [ ] **GS.5 `.gitignore` management** — ensure `secrets.yaml` is always in `.gitignore` (auto-add on init and on remote config). Warn in the UI if `secrets.yaml` has been committed (it contains WiFi passwords and API keys).
  **Ordering constraint — GS.5 must land with GS.2 (push), not after it.** `git_versioning._ensure_gitignore()` runs *only* on a fresh `git init`; on a pre-existing repo it deliberately leaves `.gitignore` alone (the hass-4 incident — don't clobber a curated file). But the GS user *is* the pre-existing-repo user: someone already keeping `/config/esphome` in git who now wants it on GitHub. That is exactly the path where `secrets.yaml` was never auto-ignored and may already be tracked, so shipping push first means the first `git push` can publish WiFi passwords and API keys — irreversibly, since a later force-push doesn't reach forks, caches or GitHub's own views. Minimum bar before any push ships: refuse to configure a remote (or refuse the first push) while `secrets.yaml` is tracked, with a `git rm --cached` pointer. Never auto-untrack.

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

## Carried forward from 1.8

Unfinished 1.8 items, moved verbatim at the 1.8.0 release (2026-11-04). 1.8.0 shipped without its planned LLM-assistance theme; full context and design notes for these items are in [`archive/WORKITEMS-1.8.md`](archive/WORKITEMS-1.8.md).

### Foundation: LLM provider layer

- [ ] **LL.1 LiteLLM call surface** — single `llm_call(messages, *, structured_model=None, tools=None, stream=False)` wrapper around `litellm.completion`. Reads provider/model/api_key/api_base/temperature/max_tokens from the live settings object on every call (no client init at startup). Models named in `<provider>/<model>` form (`anthropic/claude-opus-4-5`, `openai/gpt-4o`, `ollama/llama3.2`, `openai/local-model` for LM Studio with `api_base`). New dep: `litellm` in `requirements.txt` + lockfile (PY-8).

- [ ] **LL.2 Settings drawer "AI" tab** — extends the SP.8 settings store. Fields: `enabled` (bool), `provider` (dropdown: anthropic / openai / google / ollama / lmstudio), `model` (free-text — model lists rot, let users type what they actually have), `api_key` (secret, hidden for local providers), `api_base` (only shown for ollama/lmstudio), `temperature` (slider, default 0.2 — YAML edits want determinism), `max_tokens` (default 4096), `permission_mode` (`READ_ONLY` / `SUGGEST` / `AUTO_APPROVE`, default `SUGGEST`). Conditional show/hide on `provider`. Persists to `/data/settings.json`. Live-reload via the existing SP.8 broadcast.

- [ ] **LL.3 "Test connection" button** — beside the AI fields. Sends a 10-token "reply with OK" prompt through the configured provider and surfaces success / latency / error inline. Catches the most common config mistakes (wrong key, wrong api_base, model name typo) before the user discovers them mid-edit.

- [ ] **LL.4 Permission tier enforcement** — every write tool checks `settings.ai.permission_mode` before acting. `READ_ONLY` rejects writes with a clear "permission denied" tool result. `SUGGEST` stages the change to an in-memory pending-changes map keyed by `(conversation_id, device_name)` and returns `{"status": "staged"}`. `AUTO_APPROVE` writes directly. Tier shown as a badge in the chat UI so users can see what's active.

- [ ] **LL.5 Structured streaming events** — internal pydantic event model `AgentEvent { type: "thinking" | "tool_call" | "tool_result" | "text" | "diff" | "error", content: str | dict, ts: float }`. The agent loop yields these; `/ui/api/ai/stream` surfaces them over WebSocket so the chat UI can render thinking indicators, tool-call badges, and inline diffs progressively rather than waiting for the full turn.

- [ ] **LL.6 Conversation persistence** — full message history (text + tool_calls + tool_results) persisted to a small SQLite table alongside JH.* (1.6's job history DB), keyed by `conversation_id`. Survives add-on restart, lets the model say "as we discussed earlier when I read your porch sensor config…" Retention bound: keep last N=20 conversations, prune older.

- [ ] **LL.7 Hardcoded "weak local model" warning** — when `provider in {ollama, lmstudio}` and the model string doesn't match a known-good list (Llama 3.1+, Qwen 2.5+, Mistral Large), show a one-line warning under the model field: "Tool calling may be unreliable on this model — recommended: …". Doesn't block, just sets expectations. Worth doing because tool-calling failures on small local models are confusing — the model "answers" without ever calling the tool that would have given the right answer.

- [ ] **LL.8 No telemetry / no cloud egress when disabled** — when `ai.enabled = false` we make zero outbound LLM calls and the AI tab in the editor is hidden. Spot-check with `tcpdump`/integration test. Pat-friendly: a user who never turns AI on never sees a cent of cloud cost or a packet leaving their network.

### Per-device YAML assistant (in-editor)

The editor (Monaco-based, already shipped) gains an AI sidebar for the device the user is currently editing. Per-device, single-file scope — the global fleet chat (FC.*) handles cross-device prompts.

- [ ] **AI.1 `POST /ui/api/ai/complete`** — completion endpoint scoped to one file. Body: `{device_name, yaml_content, cursor_offset, prompt}`. Returns the completion stream. Used by AI.3's inline ghost text and AI.4's chat panel. Uses `instructor` + the `ESPHomeEdit` Pydantic model below for the diff path; raw streamed text for free-form completions.

- [ ] **AI.2 `ESPHomeEdit` Pydantic model** — `class ESPHomeEdit(BaseModel): modified_yaml: str; explanation: str; warnings: list[str]`. Wraps every "modify YAML" call so the LLM has to return parseable structured output rather than freeform text we'd have to fence-strip. Fallback: if `instructor` parse fails (older / smaller models), regex-extract the first ` ```yaml ` block and surface a warning event.

- [ ] **AI.3 Inline ghost-text completions** — Monaco inline-completion provider that calls AI.1 on idle (debounced ~500 ms after typing stops). Shows the suggestion as ghost text; Tab accepts, Esc rejects. `permission_mode` does not gate this — it's user-initiated and user-applied keystrokes, no filesystem write happens until they save the file the normal way.

- [ ] **AI.4 Chat side panel in editor** — collapsible panel beside the Monaco view. Free-form prompt → response is either an `ESPHomeEdit` (rendered as a Monaco diff with Accept / Reject buttons) or freeform text (rendered as markdown). On Accept under `SUGGEST` mode, applies the change to the editor buffer (still requires the user to hit Save — symmetric with manual edits). On Accept under `AUTO_APPROVE`, saves automatically.

- [ ] **AI.5 ESPHome-aware system prompt** — module-level constant containing: "You modify ESPHome YAML configs. Output must be valid YAML. Preserve `# distributed-esphome:` comment blocks verbatim. Prefer existing substitutions / packages / secrets over inlining values. Never embed real WiFi passwords or API keys — use `!secret` references." Versioned (`ESPHOME_PROMPT_V = 1`) so we can iterate without surprising users with regressions.

- [ ] **AI.6 Diff-only reject path** — when the user rejects a staged AI.4 diff, we don't just discard — we feed the rejection back into the conversation as `"User rejected this change"` so a follow-up "try again" prompt has the context. Cheap to implement, big UX win.

### Fleet chat (global)

A new top-level **AI Assistant** tab. Free-form prompts against the whole fleet, powered by tool calling so we don't blow the context window stuffing every YAML upfront. Where the per-device AI panel knows about one file, this knows about everything.

- [ ] **FC.1 Agentic tool loop** — the canonical "while tool_calls is not None" loop on top of LL.1. Yields LL.5's `AgentEvent`s as it goes (thinking → tool_call → tool_result → text). Loop bound: max 25 tool calls per turn (defensive — a runaway agent can easily eat thousands of tokens; surface "I hit the tool-call limit" to the user rather than silently truncating).

- [ ] **FC.2 `list_devices` tool** — returns `[{name, board, platform, components: [...], tags: {...}}]` for every device in the fleet. Reuses `scanner.py`'s parsed config — no re-parsing per call. Cheap (~one row per device), so it's the LLM's first move on most prompts.

- [ ] **FC.3 `get_device_config` tool** — reads one device's full YAML. Bounded by `safe_resolve` (`ha-addon/server/helpers.py`) so the LLM cannot escape `/config/esphome/`. Returns `{yaml: str, sha: str}` so a later `stage_device_change` can detect drift between read and write.

- [ ] **FC.4 `search_configs` tool** — substring + regex search across all YAMLs. Returns `[{device, line, snippet}]`. Cheap way for the model to answer "which devices use the deprecated `xyz` component?" without reading each config in full.

- [ ] **FC.5 `stage_device_change` tool** — write-equivalent. Takes `{device_name, modified_yaml, reason, expected_sha?}`. Under `SUGGEST` mode (default), inserts into the pending-changes panel and returns `{"status": "staged", "id": "..."}`. Under `AUTO_APPROVE`, writes via the existing AV.* commit path so changes flow through git history. Refuses if `expected_sha` doesn't match the current file (concurrent-edit guard — important if the user is editing the same device in the editor at the same time).

- [ ] **FC.6 `read_release_notes` tool** — wraps BC.1's release-notes fetcher so the chat can answer "what changed in 2026.5.0?" without us pre-emptively passing it. Pairs with the breaking-change analyzer — users will naturally ask about a release in chat after seeing the BC.* impact report.

- [ ] **FC.7 Chat UI** — new tab with: conversation list (left), active conversation (center, streaming `AgentEvent`s), pending-changes panel (right, shows staged diffs with Accept-all / Reject-all / per-row controls). Permission badge in the header. "New conversation" button. The pending-changes panel is the single place users review every staged write before it lands — same pattern as the per-device AI.4 diff but aggregated across the conversation.

- [ ] **FC.8 Bulk-apply staged changes** — Accept-all on the pending-changes panel commits all staged writes in one batch via `Promise.all`, surfaces a single summary toast (per the "Batch operations get one toast" design judgment in CLAUDE.md). Each write goes through AV.*'s git path so the auto-commit subject reflects "AI: <reason from FC.5>" rather than a generic message.

- [ ] **FC.9 Conversation history view** — left rail lists prior conversations from LL.6's persistence. Click to resume — the model picks up where it left off, including tool-call results. Pruning: oldest dropped when over the 20-conversation cap.

- [ ] **FC.10 Cost / token surface** — small footer in the chat tab showing tokens used this turn + cumulative for this conversation, plus a per-provider $/1K-tok hint where known. Local providers show "$0.00 (local)". Optional but high-value for users on metered plans — the conversation can otherwise hide its real cost.

### ESPHome release breaking-change analyzer

Largely as scoped before — adapted to ride on LL.* (LiteLLM, structured outputs). Naturally paired with FC.6 so the chat can drill in after the upfront report.

- [ ] **BC.1 Release notes fetcher** — pull ESPHome release notes from the GitHub releases API (fallback: `esphome.io/changelog/`); cache under `/data/esphome_releases/<version>.json`. No GitHub auth required for public repos; rate-limit is generous and we cache.

- [ ] **BC.2 Device component inventory** — for each managed device, extract the set of components / platforms in use from its parsed YAML. Reuse `scanner.py` parsing — do not hand-roll regex per PY-1.

- [ ] **BC.3 `POST /ui/api/ai/analyze-release`** — input: `{target_version, device_filter?}`. Calls LL.1 with an `instructor` model `BreakingChangeReport { items: list[DeviceImpact] }` where `DeviceImpact = { device, risk: "none" | "low" | "high", affected_components: list[str], summary: str, links: list[str] }`. The "links" are anchors into the cached release notes so the UI can deep-link.

- [ ] **BC.4 UI entry points** — "Check breaking changes" action on the ESPHome version picker and on the Upgrade Outdated flow. Results modal grouped by device (high → low → none), with expandable per-component detail and a "Discuss in chat" button that opens a fresh FC.7 conversation seeded with this device's report.

- [ ] **BC.5 Result caching** — key by `(release_version, device_yaml_sha256)`. Re-opening the modal is instant; LLM calls only happen when something actually changed (release version bumps, or the device's YAML edits). Stored alongside BC.1's release notes cache.

- [ ] **BC.6 Surface in HA Updates** — paired with the future `UE.*` work in `WORKITEMS-future.md`, expose the `risk` value on the per-device update entity's `release_summary` so HA's native update card shows "⚠ 2 high-risk components affected" without the user having to open the Fleet UI. Stretch — drop if `UE.*` isn't on the table for this release.

### Open Bugs

- [ ] **LNT.1** — *Adopt a newer ruff deliberately, with an explicit rule selection.* The repo has **no ruff config file at all** (`pyproject.toml` / `ruff.toml` / `.ruff.toml` are all absent), so the rule set is whatever ruff's defaults happen to be that release — which is exactly what #245 got bitten by, and pinning only defers. Land a `ruff.toml` with an explicit `select`, then bump the pin as a standalone change so the diff is reviewable as "lint policy changed" rather than smuggled into a feature PR. The 754 findings are worth triaging on their own merits: `UP045` is a mechanical `--fix`, while `BLE001`/`S110` are worth reading case by case since several blind excepts in the worker are load-bearing (a compile must not die because a log flush raised).

- [ ] **DEP.4** — *Dependabot **security** PRs bypass `target-branch: develop` and pile up on `main`, so the fixes that matter most never reach the integration branch.* `.github/dependabot.yml` sets `target-branch: develop` on all six ecosystems, with a comment explaining that targeting main "would require merging then pulling back into develop — unnecessary churn." That works for *scheduled version* updates. It does **not** apply to Dependabot **security** updates, which ignore `target-branch` entirely and always open against the repository's default branch — here, `main`. The two PR classes are distinguishable by their labels: scheduled PRs carry the config's labels (`dependencies`, `frontend`), security PRs carry Dependabot's own (`dependencies`, `javascript`).
  **Observed impact (2026-08-24 open-PR sweep):** seven npm PRs were sitting against `main` — #237, #247, #248, #249, #253, #254, #255 — some open since 2026-08-04. Two of them (#254 js-yaml 4.3.1, #255 postcss 8.5.26) were the fix for the *high*-severity `CVE-2026-59870` and a postcss advisory that were **actively failing the `frontend` CI job on develop** the entire time. The fix existed, Dependabot had opened it, and it was aimed at a branch where no development happens. Releases flow develop → main, so nothing pulls them back the other way; they would have been silently clobbered by the next release merge. Cleared this round by applying `npm audit fix` directly on develop, but that is a manual step that happened only because someone was reading the PR list.
  **Why it will recur:** the gap is structural, not a one-off. Every future high/critical npm or pip advisory lands on `main` and is invisible to the develop CI that gates day-to-day work. `dev-plans/RELEASE_CHECKLIST.md` has a release-time check for open high/critical alerts, which is what caught this class before — but that is a gate at *tag* time, not a signal during the cycle, so develop can sit red for weeks in between (it did).
  **Options, roughly in order of preference:** (a) make `develop` the repository's default branch, so security PRs and the alerts dashboard both track the branch development actually happens on, and `main` becomes purely the tagged-release pointer — this fixes the alert-scanning half too, since Dependabot alerts also scan the default branch; (b) keep `main` as default but add a scheduled workflow that re-opens (or cherry-picks) any security PR targeting main onto develop; (c) accept it and add an explicit "sweep security PRs on main" step to the *turn* loop rather than only the release checklist. Option (a) is the only one that removes the manual step rather than relocating it.

- [x] **DEP.3** *(1.9.0-dev.4)* — done as **DEP.1** in `WORKITEMS-1.8.1.md` (46.x evaluated, PR #304 merged). Original entry: *Evaluate `aioesphomeapi` 46.x deliberately; the floor is pinned `<46` until then.* Dependabot PR #259 asked to move the floor `45.7.0 → 45.12.1`; merging it and re-running `scripts/refresh-deps.sh` resolved the lock straight to **46.1.0**, because `requirements.txt` carried no upper bound. 46.0.0's headline change is `ConnectionClosedEvent` for API consumers (esphome/aioesphomeapi#1853) — a disconnect-semantics change. **Why this can't ride along on a dependabot merge:** `tests/test_device_poller.py:16` does `sys.modules.setdefault("aioesphomeapi", MagicMock())`, so the entire library is stubbed out and a green `pytest` proves nothing about a 46.x behaviour change. Our surface is small and does *not* register a disconnect callback — `APIClient(...)`, `connect(login=True)`, `device_info()`, `subscribe_logs(...)`, `disconnect()` in `device_poller.py:515` and `ui_api.py:1325`/`:3080` — so 46.x is plausibly a no-op for us, but plausibly is not the same as verified. **Do:** read the 46.0.0 diff against those five call sites, then verify against a real device on hass-4 (device version polling still populates, and the live-log stream in the UI still opens and closes cleanly) before dropping the `<46` bound. Consider whether the mock in `test_device_poller.py` should be replaced with a narrower fake that would actually break on a signature change — the blind spot is the real finding here, and it will recur on the next major.

- [ ] **DEP.5** — *Two Dependabot majors are held back from the 1.8.0-dev.9 batch because they are migrations, not bumps.* **(a) `@tanstack/react-table` 8.21.3 → 9.2.3 (PR #265).** Merged cleanly and then failed `tsc -b` with ~10 errors in `useDeviceColumns.tsx` alone: v9 makes every feature opt-in (`getIsAllRowsSelected`, `getToggleSelectedHandler`, `enableHiding` are gone from the core types until the row-selection / column-visibility features are registered), changes the `createColumnHelper` generics (`Expected 2 type arguments, but got 1`), and types `row.original` as `RowData` unless the table is constructed with the new `tableFeatures` shape. Every TanStack table in the app (Devices, Workers, Queue, History) has to move together. Backed out with `npm install @tanstack/react-table@8.21.3`; the PR stays open. **(b) `typescript` 6.0.3 → 7.0.2 (PR #225).** Conflicting against develop and a compiler-line change (7.x is the Go-native `tsgo` port); wants its own evaluation of `tsc -b` project references and the e2e tsconfig before it lands. **Do:** take each as a standalone change with the mocked Playwright suite as the gate; for (a) start from the v9 migration guide's `tableFeatures` example and convert one table first.

### QS — Quality Scale: the rule walk

Every rule below either (i) still reads `todo` in `quality_scale.yaml`, (ii) reads `done` but the code tells a different story, or (iii) is missing from the file entirely. Lifting each to honest `done` or `exempt` is what makes TP.3's tier-flip safe. Rule slugs match `script/hassfest/quality_scale.py`'s `ALL_RULES`.

QS.G10 (declare HA-version floor in `manifest.json`) was attempted in 1.7.0-dev.27 and reverted in dev.28 — the `"homeassistant"` key is core-only, custom-integration manifests reject it. The "document the minimum in DOCS.md" half rolls into QS.G3 below.

#### QS.S — Silver

- [ ] **QS.S1 Silver `test-coverage` → Gold-grade coverage.** Silver's bar is ≥95% real line-coverage (not mocked). 1.6.2 landed HT.1 / HT.7 / HT.11 (real-hass lifecycle + reconfigure + reauth flow tests) which unblocks most of this; HT.12 (coverage measurement) still needs to land here. Sequence: HT.12 lands → re-run `pytest --cov=ha-addon.custom_integration.esphome_fleet` → confirm ≥95% → flip `test-coverage` to `done` in `quality_scale.yaml`. Until then it stays `todo` and Gold doesn't ship. **Chain:** QS.S1 ⇐ HT.12 ⇐ CI.6 (gate).

#### QS.G — Gold tier (the main lift)

- [ ] **QS.G6 `runtime-data` — migrate from `hass.data[DOMAIN][entry.entry_id]` to `entry.runtime_data`.** The `quality_scale.yaml:109–115` comment hedged "migration planned when HA minimum is bumped past 2024.11" — we're well past (today is 2026-04). 1.6.2's TP.3 restated the hedge honestly; 1.7.1 actually migrates. Concretely:
  1. Replace `hass.data[DOMAIN][entry.entry_id] = coordinator` in `__init__.py` with `entry.runtime_data = coordinator`.
  2. Update every platform read: `sensor.py:56`, `binary_sensor.py`, `button.py`, `number.py`, `update.py` — replace `hass.data[DOMAIN][entry.entry_id]` with `entry.runtime_data`.
  3. Introduce a typed `ConfigEntry` alias: `type EsphomeFleetConfigEntry = ConfigEntry[EsphomeFleetCoordinator]` in `const.py` or a new `types.py`; annotate `async_setup_entry` / `async_unload_entry` / platform setups / diagnostics / config_flow `async_get_options_flow` to use it. (This also pre-pays for Platinum's `strict-typing` rule, whose `runtime-data` validator adds typed-alias checks when `strict-typing` is `done`.)
  4. Update `diagnostics.py` to read via `entry.runtime_data`.
  5. Audit `hass.data` cleanup in `async_unload_entry`: since there's nothing there to clean up post-migration, remove the pop.
  6. Run full test + hass-4 smoke; flip `runtime-data` to `done` in `quality_scale.yaml`.

- [ ] **QS.G7 `stale-devices` — add `async_remove_config_entry_device` for user-initiated deletion.** Current state: stale-devices is *active removal* via `registry.async_remove_device` in `__init__.py:226` when the coordinator's target/worker snapshot drops an entry. That closes the *automatic* side of the rule, but HA's device page also offers a per-device **Delete** button whose enablement requires the integration to define `async def async_remove_config_entry_device(hass, config_entry, device_entry) -> bool` at the top level of `__init__.py`. Without it, the Delete button is greyed-out and users can't clear stale devices manually (e.g. a worker that's been physically decommissioned but the server still remembers). Implement it: return `True` when the device's identifier no longer appears in the coordinator snapshot; `False` otherwise (still active — refuse). Covered by a unit test in `tests/test_integration_remove_device_logic.py`. Update the `quality_scale.yaml:stale-devices` comment to name both the active-removal and user-removal paths.

- [ ] **QS.G8 `repair-issues` — audit actionable conditions, add custom issues where warranted.** `quality_scale.yaml:repair-issues` says `done` because `ConfigEntryAuthFailed` auto-creates a repair. That's one condition; Gold wants us to surface every user-actionable condition via `ir.async_create_issue`. Audit for these:
  - Worker offline >1h despite being configured → fixable by starting/restarting the worker or removing the config. Severity WARNING.
  - Firmware-storage budget full → fixable by clearing old binaries or raising the budget. Severity WARNING. Link to the Queue-History dialog's Download tab.
  - ESPHome lazy-install failed (PyPI unreachable, no disk space) → surface the install error as a repair issue with the stderr blob. Severity ERROR.
  - Scheduled upgrade failed three times in a row for the same target → Severity WARNING; fix hint: check device reachability or pinned-version mismatch.
  - Worker self-paused on disk pressure (`Worker.health_blocked_reason == "disk_full"`, landed in 1.7.0 #219). The server-side gate already keeps work off a full worker; what's missing on the HA side is a Repair card so a non-admin user notices that the fleet is partially degraded without having to open the Workers tab. Severity WARNING; description includes the disk-used-pct from the worker's last heartbeat; fix hint: open the Workers tab → Actions → Clean cache, or grow the worker's disk. Self-clears the moment the registry reports `health_blocked_reason = None` (i.e. usage dropped back below the 90 % exit threshold) — wire the `ir.async_delete_issue` to the same coordinator update tick that detects the recovery.

  For each, define the issue in `strings.json` → `issues.<issue_id>` with `title` + `description`, create on detection, clear with `ir.async_delete_issue` when the condition resolves. Non-actionable noise stays in logs — don't pollute Repairs with transient events. Update the `quality_scale.yaml:repair-issues` comment to enumerate the new issues.

- [ ] **QS.G9 `entity-disabled-by-default` — re-audit the "exempt — all useful" claim.** `quality_scale.yaml:309–313` currently reads `exempt`. That may or may not be right; the rule wants niche or high-cardinality entities disabled by default. Audit every entity and decide — `_attr_entity_registry_enabled_default = False` on anything whose value (i) changes more often than every ~5 minutes on a steady-state fleet, or (ii) is only useful for debugging. Candidates: per-worker active-jobs count (high-churn), the `scheduled_once` sensor (rarely consulted). If after the audit no entity qualifies, keep `exempt` but replace the comment with the audit rationale ("audited every entity at <date>; none qualify as niche/noisy"). If any qualify, mark `done` and list them.

#### QS.P — Platinum lookahead (not claimed this release)

- [ ] **QS.P1 Scope `strict-typing` for a future release.** Run `mypy --strict` against `ha-addon/custom_integration/esphome_fleet/` and count the diagnostics; triage into (a) genuinely fixable right here (add an annotation), (b) fixable after QS.G6 lands (typed ConfigEntry alias unlocks half of them), (c) bounded by `Any` on coordinator dict reads — needs a `TypedDict` for the server's response shape (which is pydantic-shaped on the server side; we could import and re-use the `protocol.py` models). **No code changes this release** — but produce a short `dev-plans/STRICT-TYPING-PLAN.md` that enumerates counts, categories, and a 1.8 or 1.9 landing plan. Platinum also needs every dep in `manifest.json.requirements` to ship `py.typed` or a `types-*` stub; since our requirements list is empty, that half is free. Update the `quality_scale.yaml:strict-typing` comment with the counts from the audit.

### HT — Honest testing (close TEST-AUDIT-1.6.1's remaining blind spots)

1.6.2 landed HT.1 / HT.7 / HT.11 (real-flow tests wired to the TR.* fixes), HT.14 (standalone-Docker install regression guard), and the full HT.13 family. 1.7.0 added HT.5 (mdns advertiser unit coverage) and HT.6 (Connect Worker modal e2e). The remaining TEST-AUDIT blind spots (and the CI wiring for HT.13/HT.14, which both need a GHCR-reachable runner with SSH reach to a test host) land here.

#### Static-IP regression coverage (the bug class behind #18 + #84)

HT.2/HT.3/HT.4 are one bug class — `_resolve_esphome_config` returning `None` during the ESPHome lazy-install window leaving address-resolution dicts unseeded — covered at three altitudes (invariant grep, fixture matrix, prod e2e). Land them together; partial coverage at any one altitude leaves the regression path open.

- [ ] **HT.2 Reseed-consumer invariant (`check-invariants.sh` new rule).** The class of bug behind **#11 (1.6.1)** (encryption-key race on fresh boot) and **#18 (1.6.1)** (static-IP OTA regression) is the same: `_resolve_esphome_config` returns `None` during the ESPHome lazy-install window, leaving `_encryption_keys` / `_address_overrides` / `_name_map` unseeded. Fix landed as `main.reseed_device_poller_from_config`. New invariant: grep for every module-level read of those three dicts; for each hit, require the same module references `reseed_device_poller_from_config` OR is `main.py` itself. Fails CI if a future consumer lands without the reseed wire-up. **This is the durable close on the bug class — don't skip it in favour of yet another narrow test.**

- [ ] **HT.3 Static-IP fixture suite (the deferred-in-#18 trap).** `tests/fixtures/esphome_configs/` gains: `wifi_use_address.yaml`, `wifi_static_ip.yaml`, `ethernet_static_ip.yaml`, `openthread_use_address.yaml`, `wifi_static_ip_via_substitution.yaml` (`static_ip: ${ip}` + substitutions block), `wifi_static_ip_via_secret.yaml` (`static_ip: !secret my_ip`), `packages_with_network.yaml` (address comes from an included package). New `tests/test_ota_address_resolution.py` parametrises over every fixture and asserts `(address, source)` matches what ESPHome's own `esphome.core.CORE.address` produces against the same YAML — **ESPHome as the oracle, not hand-coded expected values**, so the test tracks upstream behaviour automatically when ESPHome's resolver shifts. Also folds in `wifi_domain.yaml`, `ethernet_domain.yaml`, `wifi_domain_via_substitution.yaml`, `wifi_domain_via_secret.yaml` per #84's coverage plan.

- [ ] **HT.4 `e2e-hass-4/static-ip-ota.spec.ts` — prod regression guard.** Add a target with `wifi.manual_ip.static_ip: 192.0.2.1` (TEST-NET-1, unroutable by design). Trigger compile. Assert the resulting job record has `ota_address == "192.0.2.1"` (not `shopaccesscontrol.local` or similar). Compile fails at the OTA step because the IP is unroutable — intentional; the assertion is on job metadata, not successful upload. The static-IP bug has shipped twice (radiowave911 in 1.4.x and again in 1.6). A third ship is unacceptable; this guard forces the failure mode onto CI instead of into the next support thread. Sibling `e2e-hass-4/wifi-domain-ota.spec.ts` for #84 per the coverage plan: target with `wifi.domain: .invalid-tld.test` — compile succeeds, job record's `ota_address` ends in `.invalid-tld.test`, OTA fails at the resolve step.

#### Concurrency stress for thin modules

HT.8 + HT.9 share a frame: each module took a non-trivial complexity bump (git-versioning lock, firmware-storage eviction) without commensurate test growth. One stress test per module — passes today = baseline regression guard, fails today = real bug surfaced. Cheap to write, durable in value.

- [ ] **HT.8 One stress test for git-versioning concurrency.** `tests/test_git_versioning.py` gains: 50 concurrent `commit_file` calls via `asyncio.gather` against a single tmp repo. Assert 50 commits land in `git log --oneline | wc -l`, no `.git/index.lock` error, no file-content bleed across commits (e.g. commit N's content appears in commit N+1's tree). Module docstring explicitly flags the `.git/index.lock` race as a concern; there's currently no test that would detect if the module-level lock broke. If it passes today, baseline regression guard; if it fails, we have a real bug to fix.

- [ ] **HT.9 One stress test for firmware-storage concurrency.** `tests/test_firmware_storage.py` gains: 10 concurrent firmware uploads via `asyncio.gather` against a single DAO with a budget set lower than the aggregate size. Assert: none get evicted mid-write (no half-written .bin files survive), budget enforcer's "evict oldest" picks the correct victim under contention, `has_firmware` protection against coalesced-job eviction holds. Module took 81 new lines in 1.6.1 #9; current test file is 142 lines — thin.

#### Wire-format coverage

- [ ] **HT.10 Protocol cross-version mismatch test.** Pin the current `ha-addon/server/protocol.py` as `tests/fixtures/protocol_v{PROTOCOL_VERSION}.py` at the start of the release cycle. New test in `tests/test_protocol.py`: instantiate a worker-shaped request-builder from the pinned old copy; POST it through the current server; assert graceful `ProtocolError` with a version-mismatch field (no undefined-field crash, no silent parse-as-unrelated-endpoint). PY-6 invariant covers "server + client files byte-identical"; this covers "we didn't break wire compat without bumping `PROTOCOL_VERSION`."

#### Coverage measurement (closes QS.S1; gated by CI.6)

- [ ] **HT.12 Integration coverage measurement.** Add `--cov=ha-addon/custom_integration/esphome_fleet` to the `pytest` invocation in `pytest.ini` (or `pyproject.toml`, wherever coverage config currently lives). Pipe to `--cov-report=term-missing --cov-fail-under=95` guarded by an env var so local runs don't fail on intermediate states — CI sets the env var and gates on the threshold. Once this lands, the real number (post-HT.1 + HT.7 + HT.11) should be comfortably above 95%; confirm and flip `test-coverage` to `done` in `quality_scale.yaml` (closes QS.S1).

#### Test-infrastructure carryover

- [ ] **HT.15 Automate config-entry creation on the haos-pve test VM.** Discovered 1.6.2-pre-tag while broadening `@requires-ha` coverage to haos-pve in commit 382e2b8: the integration installer copies files to `/config/custom_components/esphome_fleet/` at add-on boot, but no automated step completes the config flow, so HA never registers `esphome_fleet.compile` / `cancel` / `validate` services. Result: the three `ha-services.spec.ts` specs (#64) returned 400 against haos-pve — not a 1.6.2 regression, just a test-infrastructure gap on the throwaway VM. Workaround landed: tagged the affected describe block with `@requires-integration-config` and `--grep-invert`-filtered it on haos-pve in `scripts/test-matrix.py`. The proper fix is to add a step to `scripts/haos/install-addon.sh` (or a new helper invoked from `push-to-haos.sh`) that POSTs `/api/config/config_entries/flow` with `domain=esphome_fleet`, walks `async_step_user` with `base_url` + the add-on token sourced from `~/.config/distributed-esphome/haos-addon-token`, and confirms the entry reaches `loaded` state before Playwright kicks off. Once that lands, drop `--grep-invert=@requires-integration-config` from the haos-pve target so all `@requires-ha` specs run there too.

### TP — Truth-in-claims (the tier flip itself)

TP.3 is the single delivery this release exists to make; CI.4 below is its gate. Land them as one push, in order: TP.3 (after every QS.* lands) → CI.4 (so future drift fails the build, not the next reviewer).

- [ ] **TP.3 (clauses c + d) — flip `manifest.quality_scale` to `gold` only when it's honest.** (c) Every Bronze+Silver+Gold rule from `script/hassfest/quality_scale.py`'s `ALL_RULES` must appear with `done`/`exempt` in our `quality_scale.yaml` — any rule still `todo` must be closed by a QS.* workitem above or re-scoped to a future release (and the manifest tier drops accordingly). (d) After every QS.* item lands, run `python3 -m script.hassfest --action quality_scale` locally; expect zero errors at tier `gold`. Only then edit `manifest.json` from `quality_scale: bronze` → `quality_scale: gold`. Ground rule: if even one Gold rule stays `todo` at ship-time, manifest stays at `silver` or `bronze` — we do not ship a claim hassfest doesn't back.

### CI — Gate the Gold claim

CI.4 protects the tier flip (TP.3); CI.6 protects the coverage claim (HT.12 / QS.S1). Without both gates, "honest" Gold reverts to "honest at tag time, anyone's guess by next release."

- [ ] **CI.4 Hassfest runs the quality-scale validator at our claimed tier.** `.github/workflows/hassfest.yml` today validates manifest shape only. Adjust the action inputs (or run `python3 -m script.hassfest --action quality_scale` directly against a checkout of `home-assistant/core`) so the committed `quality_scale.yaml` gets validated against `manifest.json.quality_scale`'s claimed tier on every PR. Validators that fire at Gold (from `script/hassfest/quality_scale_validation/`): `action_setup.py`, `config_entry_unloading.py`, `config_flow.py`, `diagnostics.py`, `discovery.py`, `parallel_updates.py`, `reauthentication_flow.py`, `reconfiguration_flow.py`, `runtime_data.py`, `test_before_setup.py`, `unique_config_entry.py`. Without this gate, TP.3's tier-flip is a file that could silently rot.

- [ ] **CI.6 Coverage ratchet for the integration.** Add a job step that runs `pytest --cov=ha-addon/custom_integration/esphome_fleet --cov-report=term --cov-fail-under=95` (HT.12) and fails if the number drops below the committed threshold. Store the threshold in a single place (env var or a pytest config key) so bumping it post-HT.1 is one line. Keeps Gold's `test-coverage` claim honest between releases.

### SD — Scope discipline (pre-tag gates)

- [ ] **SD.2 Release-blocker gate pre-tag (Gold-grade).** Before tagging `v1.7.2`, every one of the following must be true:
  1. `dev-plans/RELEASE_CHECKLIST.md`'s security-docs cross-check passes (no stale claims).
  2. `python3 -m script.hassfest --action quality_scale` passes clean at the tier declared in `manifest.json.quality_scale`. If the manifest says `gold`, zero errors; if every Gold rule isn't `done`/`exempt`, the manifest tier drops to whatever is honest (silver or bronze) **before** the release tag — we do not ship a claim hassfest doesn't back.
  3. The TEST-AUDIT-1.6.1 Top-5 blind spots (HT.2–HT.4 here, plus HT.1 in 1.6.2 and HT.5 in 1.7.0) have landed. Not `in progress`, not `partially`. Landed + merged + CI-green.
  4. `brands` PR at `home-assistant/brands` (opened in 1.7.1 by BR.2) is either merged (so `brands` can be `done`) or the `quality_scale.yaml:brands` comment carries the open PR URL and tier drops if it was gating Gold.
  5. `scripts/check-invariants.sh` — all rules (PY-1..12, HT.2's reseed-consumer rule, UI-1..7, E2E-1) green.
  6. HT.12's coverage number ≥95% for `ha-addon/custom_integration/esphome_fleet/**`.
  7. `ha-addon/CHANGELOG.md` accurately describes what users see changing from 1.7.1 → 1.7.2 (the tier flip, translated entity names, runtime-data migration, the new docs sections, repair-issues surfacing). Per CLAUDE.md: only changes relative to 1.7.1.

- [ ] **SD.3 Produce `TEST-AUDIT-1.7.2.md` as the last workitem before tag.** Prove each TEST-AUDIT-1.6.1 top blind spot has durable closure (a test exists AND would fail without the fix AND the underlying bug class is structurally prevented, not just patched). For each of items 1–13 in TEST-AUDIT-1.6.1, write one line: "closed via HT.X (1.6.2 / 1.7.0 / 1.7.2)" or "re-deferred — here's why and here's the owning workitem in 1.8." If even one entry reads "we ran out of time," treat that as a signal to cut non-blocking scope and land the test. Audit the audit. Also produce `TEST-AUDIT-1.7.0.md` covering tags + routing + DM + RC if 1.7.0 ships without one — defer to whichever turn closes the 1.7.0 release.

### I18N — string extraction + German (carried from 1.7.2; foundation I18N.1/I18N.2 shipped in 1.7.2)

- [ ] **I18N.3 Server error responses carry `error_code`.** Audit every `json_error(...)` / `{"error": ...}` site in `ha-addon/server/ui_api.py` (~20 places). Add `error_code` (snake_case, defined in new `ha-addon/server/error_codes.py`). UI error helpers in `src/api/client.ts` map known codes via `t('error.<code>')` with English `error` field as fallback for unknown codes. Server logs stay English.

- [ ] **I18N.4 Extract React UI strings to `en.json`.** Replace inline English JSX literals with `t('key')` / `<Trans>` calls, component by component. Order: `App.tsx` → `SettingsDrawer.tsx` → tabs (`DevicesTab`, `QueueTab`, `WorkersTab`, `SchedulesTab`) → modals (`NewDeviceModal`, `LogModal`, `EditorModal`, `UpgradeModal`, etc.) → column factories (`useDeviceColumns.tsx` and queue/worker columns — ensure `i18n.language` is in the `useMemo` deps so a locale switch re-renders headers; mirrors the existing `time_format` dep at `useDeviceColumns.tsx:611`) → misc (`EsphomeInstallBanner`, `EsphomeVersionDropdown`, `TagFilterBar`, `StatusDot`, …). Keys grouped by surface in `en.json`: `toast.*`, `dialog.*`, `column.*`, `tooltip.*`, `button.*`. Use i18next plural keys (`worker_one` / `worker_other`) to retire inline `${n > 1 ? 's' : ''}` ternaries (~8 sites). Long pole — bulk of the work; expect creep from ~200 to 250–300 keys once placeholders, error toasts, empty states, aria-labels surface.

- [ ] **I18N.5 Translate `utils/format.ts` relative-time + duration suffixes.** `timeAgo()`, `fmtEpochRelative()`, `fmtDuration()` currently hardcode `"s ago"` / `"m ago"` / `"h ago"` / `"d ago"` / `"just now"` / `"5m 34s"` (`format.ts:9–14, 41–52, 20–28`). Replace with `t('relative.*', { count })` calls leveraging i18next's plural rules so German renders `"vor 5 Min."`-style phrasing. Also: `_formatDate()` for the `'long'` date-format pref uses an English-locale `month: 'short'`; switch to `i18n.language` so German reads `"9. Mai 2026"` (`format.ts:115`).

- [ ] **I18N.6 i18n key-coverage check.** New `npm run i18n:check` script: diffs key sets between `en.json` and `de.json`; warns on every English key missing from German (silent fallback otherwise). Wired into `npm run build` so a forgotten German value is visible in CI rather than only on inspection.

- [ ] **I18N.7 HA custom integration `translations/de.json`.** AI-translate `ha-addon/custom_integration/esphome_fleet/translations/en.json` (113 lines, ~50 keys: config flow, services, system_health, exceptions, issues) and the QS.G5 additions to `entity.<platform>.<key>.name`. Stefan reviews. HA loads automatically when frontend locale = German. Coordinate landing order with QS.G5 — extract entity translation keys first, then translate.

- [ ] **I18N.8 Add-on options `translations/de.yaml`.** AI-translate `ha-addon/translations/en.yaml` (5 option keys for the Supervisor config UI). Stefan reviews. Five strings — minutes of work given AI assistance.

- [ ] **I18N.9 React UI `de.json` translation pass.** AI-translate the populated `en.json` into `de.json`. Translation prompt preserves brand names verbatim ("Fleet for ESPHome", "ESPHome", "Home Assistant"), keeps i18next placeholders (`{{name}}`, `{{count}}`), keeps technical terms untranslated (OTA, firmware, YAML), matches register per surface (terse imperative buttons, neutral tooltips, friendly toasts). Stefan reviews in a single pass; corrections committed before merge. Land last in the I18N sequence — after I18N.4 + I18N.5 are done so the source catalog is stable.

- [ ] **I18N.10 Locale-switch e2e spec.** New `ha-addon/ui/e2e/i18n-locale-switch.spec.ts`: set `language` to `de` via the settings API mock, assert key strings on each tab localize (header title, "Workers" tab, "Compile" button label, a representative toast). Reset to `en` and re-verify. Runs in `npm run test:e2e`.

- [ ] **I18N.11 Translation contributor guide.** New `dev-plans/CONTRIBUTING-TRANSLATIONS.md` (~30 lines): the AI-draft + native-review workflow, the three catalog locations, the i18next placeholder rules, the brand-name verbatim rule (PY-12 invariant). Linked from README's "Contributing" section.

- [ ] **I18N.12 Verification.** Manual smoke through every tab and modal in German on hass-4. HA Settings → Devices & Services → "Fleet for ESPHome" config flow with HA frontend locale = German. Supervisor add-on Configuration tab with HA in German. All 37+ existing e2e specs still green at default (English) locale. New `i18n-locale-switch.spec.ts` green. `npm run i18n:check` reports zero missing German keys.

#### Carried forward from 1.7.2 — Open bug

- [ ] **[#127](https://github.com/weirded/fleet-for-esphome/issues/127)** — *Python dependencies fail to install in penv on HAOS 2026.4.4* (DerekSeaman). 1.7.1-dev.10 shipped a scoped mitigation: bumped `pip install esphome==X` timeout 180 s → 300 s with one retry in `ha-addon/client/version_manager.py::_install` (lines 137–219), and set `UV_HTTP_TIMEOUT=300` / `UV_REQUEST_TIMEOUT=300` / `UV_HTTP_CONNECT_TIMEOUT=30` / `PIP_DEFAULT_TIMEOUT=300` on the compile subprocess env at `ha-addon/client/client.py:1498-1501` so PlatformIO's penv-bootstrap pip+uv inherit generous timeouts. The 1.7.1 entry explicitly punted deeper investigation here; the GitHub issue is still **open** with 0 follow-up comments and the reporter still listed on 1.7.0, so we don't yet know whether the mitigation unblocks them. Plan for 1.7.2: (a) post a comment on #127 asking DerekSeaman to upgrade past 1.7.0 and confirm — close if confirmed. (b) If they confirm still-failing, dig deeper. The two known gaps if (b) hits: (i) `_BROKEN_PIO_SIGNATURES` at `client.py:461-479` does not yet match the `Failed to install Python dependencies into penv` / `uv installation via pip timed out` strings, so a clean penv-install timeout fails the job outright instead of triggering the existing wipe-and-retry self-heal — adding those two regex patterns gives the same one-shot retry that #214/#220 use for other PIO corruption modes (the existing `_wipe_broken_toolchain` already removes `penv/`, so no other code change is needed); (ii) we have no log-line proof at the top of compile that the env-var setdefaults actually reached PlatformIO's pip subprocess — a single `logger.info("PIO env: UV_HTTP_TIMEOUT=%s PIP_DEFAULT_TIMEOUT=%s", …)` line near `client.py:1501` would make the next bug report self-debuggable. Don't pre-emptively land (i) + (ii) without a confirmed second report — the 1.7.1 mitigation may already be sufficient and adding speculative self-heal patterns risks false positives swallowing real failures.

