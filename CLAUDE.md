# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Concision

Be extremely concise. Sacrifice grammar for the sake of concision.

## Project Overview

Fleet for ESPHome (internally: `distributed-esphome`; previously branded "ESPHome Fleet" 1.5.0–1.7.0, "ESPHome Distributed Build Server" before that) manages fleets of ESPHome devices — offloads compilation to remote workers, schedules upgrades, pins versions per device, and organizes devices via tags. Runs as a Home Assistant add-on with a built-in local worker. Additional build workers run in Docker on remote machines, poll the server for jobs, compile firmware using ESPHome, and push firmware via OTA directly to ESP devices. <!-- br1-allow: brand-history -->

**Naming convention:** user-facing docs/UI/log lines say **"Fleet for ESPHome"** (1.7.1 BR.1 rebrand). Code identifiers, Docker image names (`esphome-dist-server`, `esphome-dist-client`), the add-on slug (`esphome_dist_server`), the custom-integration domain (`esphome_fleet`), the mDNS service type (`_esphome-fleet._tcp.local.`), Python module names, and the YAML comment marker (`# distributed-esphome:`) all keep their existing form — changing those would force a migration on every existing install for no user benefit. The GitHub repo was renamed to `weirded/fleet-for-esphome` in 1.7.2; GitHub auto-redirects the old URL.

## Architecture

### Server (`ha-addon/server/`)

`aiohttp` async application with two authentication tiers:

- `/api/v1/*` — Bearer token auth for build workers (also accepts requests from the HA Supervisor IP).
- `/ui/api/*` — HA Ingress trust (no worker auth) for the browser UI.

Component responsibilities:

- `main.py` — app setup, auth middleware, background loops (timeout checker, HA entity poller, PyPI version refresher), HA Ingress `X-Ingress-Path` injection.
- `job_queue.py` — in-memory job queue persisted to `/data/queue.json`. State machine: `PENDING → WORKING → SUCCESS | FAILED | TIMED_OUT`. Jobs retry up to 3 times before permanent failure. On server restart, `WORKING` jobs reset to `PENDING`. Loader recovers gracefully from malformed/truncated queue files.
- `scanner.py` — discovers `.yaml` targets in `/config/esphome/`. `create_bundle()` produces a tar.gz of the full config directory (including `secrets.yaml`, needed for ESPHome's `!secret` resolution). **ESPHome is NOT bundled in the server Docker image (SE.1–SE.10).** At first boot, `ensure_esphome_installed()` lazy-installs the version reported by the HA ESPHome add-on into `/data/esphome-versions/<ver>/` via the shared `VersionManager`. The venv's `site-packages` is prepended to `sys.path` so `from esphome.* import ...` works; the binary at `<venv>/bin/esphome` is used by `/ui/api/validate`. Downstream callers (`_resolve_esphome_config`, `/ui/api/components`, validate) degrade gracefully while the install is in flight — 1–3 min on first boot; subsequent restarts are instant.
- `registry.py` — in-memory build worker registry (no persistence); workers are "online" if last heartbeat was within `worker_offline_threshold` seconds.
- `device_poller.py` — discovers ESPHome devices via `_esphomelib._tcp` mDNS, polls them via `aioesphomeapi` for running version.
- `api.py` — worker REST API (register, heartbeat, claim job, submit result, stream log). Parses every request body through the typed pydantic models in `protocol.py`.
- `ui_api.py` — browser JSON API (targets, devices, workers, queue, compile, cancel).
- `protocol.py` — **single source of truth** for server↔worker wire messages (pydantic v2). Byte-identical copy lives in `ha-addon/client/protocol.py`; a test enforces they match.
- `static/` — Vite-built React app output (source in `ha-addon/ui/`).

### Worker (`ha-addon/client/`)

`client.py` is a synchronous polling loop with a background heartbeat thread. Registers with the server, polls for jobs, ensures the correct ESPHome version is installed (`version_manager.py` — LRU cache of virtualenvs under `/esphome-versions/<version>/`), extracts the config bundle, runs `esphome run`, and submits results. Because the worker performs the OTA upload itself, **it must have network access to the ESP devices**.

`IMAGE_VERSION` (baked into the Docker image) and `MIN_IMAGE_VERSION` (in `ha-addon/server/constants.py`) gate the in-place source-code auto-update: the server refuses to push `.py` updates to workers whose Docker image is below `MIN_IMAGE_VERSION`, because a stale image can't be fixed by rewriting files in place.

### Job Bundle Flow

When a worker claims a job, the server calls `scanner.create_bundle()` which tarballs the ESPHome config directory into a base64 payload. The worker extracts this, compiles the target YAML, and OTA-flashes the firmware directly to the ESP device.

### Configuration

Server config is loaded from `/data/options.json` with environment variable fallbacks. Worker config is all via environment.

Key worker env vars:

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_URL` | required | e.g. `http://homeassistant.local:8765` |
| `SERVER_TOKEN` | required | Shared auth token |
| `POLL_INTERVAL` | `5` | Seconds between job polls when idle |
| `HEARTBEAT_INTERVAL` | `10` | Seconds between heartbeats |
| `JOB_TIMEOUT` | `600` | Compile timeout in seconds |
| `OTA_TIMEOUT` | `120` | OTA upload timeout in seconds |
| `MAX_ESPHOME_VERSIONS` | `3` | Max cached ESPHome versions on disk |
| `MAX_PARALLEL_JOBS` | `2` | Concurrent build jobs per worker (0 = paused). Server-spawned local worker defaults to `1` on fresh install unless the user has configured it via the UI (persisted in `/data/local_worker_slots`) — #99. |
| `HOSTNAME` | system hostname | Worker name shown in UI |
| `ESPHOME_SEED_VERSION` | — | Pre-install this ESPHome version at startup |
| `ESPHOME_BIN` | — | Use this binary instead of the version-manager venvs |
| `HOST_PLATFORM` | — | Override detected OS in UI (e.g. `macOS 15.3 (Apple M1 Pro)`) |

## Commands

Scripts live in `scripts/`:

| Script | Purpose |
|--------|---------|
| `scripts/bump-dev.sh` | Increment `-dev.N` — **run at the end of every turn.** |
| `scripts/bump-version.sh X.Y.Z` | Set stable version for a release. |
| `scripts/check-invariants.sh` | Run the enforced-invariant grep linter (also runs in CI). |
| `./push-to-hass-4.sh` | Deploy to hass-4 and run the full prod Playwright smoke suite. |

Common dev commands:

- `pytest tests/` — full test suite.
- `ruff check ha-addon/server/ ha-addon/client/` — Python lint.
- `mypy ha-addon/server/ --ignore-missing-imports` / `mypy ha-addon/client/ ...` — type check.
- `cd ha-addon/ui && npm run build` — frontend build (`tsc -b && vite build`).
- `cd ha-addon/ui && npx playwright test` — 37-test mocked e2e suite.

See `dev-plans/RELEASE_CHECKLIST.md` for the full stable-release process.

## Test Setup

`tests/conftest.py` adds `ha-addon/server` and `ha-addon/client` to `sys.path`. `pytest.ini` sets `asyncio_mode = auto`. Sample ESPHome YAML fixtures live in `tests/fixtures/esphome_configs/`.

## Frontend (`ha-addon/ui/`)

React 19 + TypeScript 5.9 + Vite 8 + Tailwind v4 + shadcn/ui (Base UI primitives). Build output lands in `ha-addon/server/static/`. Path alias `@/*` → `src/*`.

- SWR polls workers/devices/queue at 1 Hz (state is cheap in-memory on the server side).
- All server calls live in `src/api/client.ts` (or siblings under `src/api/`). Components never call `fetch()` directly — enforced by `scripts/check-invariants.sh` rule UI-1.
- Shared types in `src/types/index.ts`. Playwright fixtures are typed against these so a field rename breaks tests.

## Enforced Invariants

Checked mechanically by `scripts/check-invariants.sh` (wired into the CI `test` job) or by pytest / mypy / ruff / the TS build. **Violating these fails CI.**

**UI-1 — No `fetch()` outside `src/api/`.** All HTTP calls go through the `api/` layer. Components never call `fetch()` directly.

**UI-2 — No Tailwind `@apply`.** Use utility classes in JSX. CSS files only for things Tailwind can't express (animations, complex selectors).

**UI-3 — No `any` type in new TS code.** Use `unknown` or a real type. Existing sanctioned uses (Monaco/xterm internals) are allow-listed with `// ALLOW_ANY: <reason>` inline.

**UI-4 — No `flex`/`inline-flex` on `<td>`.** Table cells must not be flex containers — it breaks table layout.

**UI-5 — Typed fixtures.** E2E Playwright fixtures in `ha-addon/ui/e2e/fixtures.ts` must import the runtime types from `src/types` so a field rename breaks the e2e build. (Enforced by `tsc -b` on the e2e project.)

**UI-7 — Icon-only buttons need both `aria-label` and `title`.** Icon controls carry no visible text, so screen readers need `aria-label` and sighted hover needs `title`. If you reach for one, add both — they're almost always the same string. Landed from UX.12 after the UX review (bug class: icons that hover-reveal no context, or lack accessible names).

**E2E-1 — No `page.waitForTimeout()` in Playwright specs.** Fixed sleeps are flake factories — CI is slower than your laptop, or the page state settles faster. Wait on an observable condition instead (`expect.poll`, `toBeVisible`, `toHaveCount(0)`, a route-interceptor counter, etc.). Landed from CR.6 after a 200ms sleep in `e2e-hass-4/cyd-office-info.spec.ts` was found flaking the prod-smoke suite on slow HA restarts.

**PY-1 — YAML goes through `yaml.safe_load`.** Never hand-rolled regex parsers for YAML content (regression source: #160, ESPHome device-name detection). The `_ota_network_diagnostics` regex fallback is allow-listed because it tries `safe_load` first.

**PY-2 — Every file that calls `subprocess.run`/`subprocess.Popen` must have a module-level `logger`.** The actual command line must also be logged before the subprocess runs (reviewed in PR; file-level logger presence is the grep-able floor). Bug sources: #176, #177, #180 — untriageable reports when the command line wasn't in the log.

**PY-3 — `esphome upload` invocations must not pass `--no-logs`.** That flag is `esphome run`-only. Direct regression guard for #177.

**PY-4 — Bump `IMAGE_VERSION` + `MIN_IMAGE_VERSION` when the worker Docker image changes.** System packages, Python version, `requirements.txt`, Dockerfile — any change to what `COPY`'d into the image (other than the auto-updatable `.py` source). A file-mtime check in `check-invariants.sh` warns if `requirements.txt` / `Dockerfile` is newer than `IMAGE_VERSION`. See the `1.3.1-dev.2` incident for why: the pydantic add-on broke every deployed worker because this wasn't bumped.

**PY-5 — No `# noqa`, `# type: ignore`, `eslint-disable`, or `@ts-ignore` without a comment explaining why.** Enforced by code review; if you're silencing a tool, fix the root cause instead.

**PY-6 — Pydantic models in `protocol.py` are the wire contract.** `ha-addon/server/protocol.py` and `ha-addon/client/protocol.py` must stay byte-identical (enforced by `tests/test_protocol.py::test_server_and_client_protocol_files_are_identical`). Every server-facing `/api/v1/*` handler parses its body through the typed model; workers build their requests from the typed model. New fields are additive + optional unless `PROTOCOL_VERSION` is bumped.

**PY-7 — Every `--ignore-vuln` must have an applicability assessment.** When adding a CVE ignore to `pip-audit` (or any audit tool), the inline comment must include: (1) why the fix version can't be pulled in (transitive bound, breaking change, etc.), (2) whether our code actually exercises the vulnerable code path, and (3) a date so staleness is visible. Don't just say "can't upgrade" — say whether the vulnerability matters for this codebase. If it does matter, track a follow-up in WORKITEMS rather than silently ignoring it.

**PY-8 — Every direct dep in `requirements.txt` must also appear in `requirements.lock`.** Dockerfiles install from the lockfile with `--require-hashes`, so anything present only in `requirements.txt` is silently missing from the image. Root cause of bug #39: `croniter` was added to `ha-addon/server/requirements.txt` but `scripts/refresh-deps.sh` was never rerun — the production image had no croniter, `schedule_checker` caught the `ImportError` and returned, and no scheduled upgrade ever fired in prod. `scripts/check-invariants.sh` now verifies the lockfile covers every entry in the .txt file.

**PY-9 — No macOS-only packages in `requirements.lock`.** `pyobjc-core`, `pyobjc-framework-*`, and `appnope` leak in as platform-conditional transitives when `pip-compile` is run on a Mac host (they should carry `sys_platform == "darwin"` markers but `pip-compile --generate-hashes` strips markers). The Linux Docker build then errors with `PyObjC requires macOS to build`. Happened twice (1.3.1-dev.9, 1.4.1-dev.55). Always regenerate lockfiles via `scripts/refresh-deps.sh`, which runs `pip-compile` inside a `python:3.13-slim` container on `linux/amd64`. `scripts/check-invariants.sh` greps the lock for the known macOS-package names and fails CI on any hit.

**PY-10 — `tests/test_integration_*.py` (without a `_logic` suffix) must import `pytest_homeassistant_custom_component`.** The plain `test_integration_*` name reads as "real test against a running HA" — if that's not what the file does, rename it to `test_integration_*_logic.py` (which the invariant exempts) so the filename doesn't mislead. Origin: IT.1 from 1.6 — mock-based helper tests were file-named as integration tests and reviewers kept assuming coverage that wasn't there, letting CR.12-class bugs ship (`async_setup_entry` misuse, `unique_id` collisions, config-flow regressions). `scripts/check-invariants.sh` greps each non-`_logic` integration test file for the `pytest_homeassistant_custom_component` import and fails CI if it's absent.

**PY-10b — Skipped-integration-test ratio across non-`_logic` `test_integration_*.py` files must stay ≤ 50 %.** PY-10 above guarantees those files *import* the HA custom-integration plugin, but a future regression where every real test gets `@pytest.mark.skip`-decorated would leave the import as the only honest part — same coverage-mirage failure mode IT.1 was filed for. `scripts/check-invariants.sh` counts `@pytest.mark.skip` decorators vs `def test_` / `async def test_` declarations across every non-`_logic` integration test file; if the ratio crosses 50 %, CI fails. Origin: CI.5 from 1.7.0.

**PY-11 — Every UI-driven file mutation under `/config/esphome/` must leave the git working tree clean.** When versioning is on, an endpoint that writes / renames / archives / restores / deletes a file MUST also commit the change (via `commit_file`, `archive_and_commit`, `restore_and_commit`, or `delete_archived_and_commit`) so `git status --porcelain` is empty once `drain_pending_commits()` finishes. A "dangling" working-tree entry (file modified-but-not-committed, or — worse — a half-staged rename like `D src` left over from a partial commit) means the next user save sweeps the leftover into someone else's git log, the rollback diff lies, and `has_uncommitted_changes` reports the wrong thing. Origin: #94 (`os.unlink` on archived file left `deleted: .archive/<f>` in working tree) and #197 (rename pathspecs filtered out the source side of `git mv` so commits left the deletion staged — fixed by widening `_staged_paths` to include both halves of renames). Regression net: `tests/test_git_clean_after_ops.py` drives every file-mutating endpoint and asserts `dirty_paths(config_dir) == set()` afterward. Every new file-mutating endpoint MUST add a scenario there.

**PY-12 — The literal string `ESPHome Fleet` (the pre-1.7.1 brand) must not appear outside the rebrand allowlist.** The 1.7.1 BR.1 sweep renamed every customer-visible mention to **Fleet for ESPHome**. The grep guard in `scripts/check-invariants.sh` walks every tracked file and fails CI if the old wording resurfaces in a forgotten string, a copy-pasted log line, or a refactor that lifts a stale comment into a live label. Allowlist (where the old literal is intentional): (a) `dev-plans/archive/` — frozen historical plans, including the 1.7.1 rebrand plan; (b) `ha-addon/CHANGELOG.md` — past-release entries describe what shipped under the old brand and stay accurate; (c) `scripts/check-invariants.sh` — the rule text + grep pattern have to mention the literal in order to enforce it; (d) any line carrying the marker `br1-allow: <reason>` — the per-line opt-out for legitimate cases like the back-compat header set in `scanner.py`, the README/DOCS "Previously known as" hint (drop the hint + marker for 1.8), and the brand-history sentence in CLAUDE.md / AGENTS.md. Aim for fewer than ten markers across the repo; beyond that, prefer rephrasing.

## Design Judgment (aspirational — reviewed, not enforced)

These aren't grep-checkable but matter just as much. They're how the codebase stays coherent.

- **Disable, don't fail.** When a feature isn't available for a target/worker/job (no restart button in YAML, no API key, worker offline, etc.), render the button or menu item **disabled with an explanatory tooltip** rather than letting the user click it and watch it fail. The tooltip should tell them what's missing and ideally how to fix it. Detect availability up-front from data we already have (YAML metadata, registry state) — don't probe by trying. **Exception: the Upgrade button is always enabled** regardless of device state, because compiling for a target is meaningful even if the device is offline (the firmware is still produced and OTA-pending). Origin: bug #14 — Restart was always clickable but silently no-op'd for devices whose YAML had no restart button.
- **Default to shadcn/ui.** All new interactive UI (buttons, dialogs, dropdowns, inputs, selects) uses the shadcn wrappers in `components/ui/`. Don't hand-roll components that already exist there. If shadcn doesn't have it yet, add a thin wrapper (see `components/ui/input.tsx`, `components/ui/select.tsx`).
- **No native JavaScript modals — ever.** No `window.alert`, no `window.confirm`, no `window.prompt`. They ignore the app's theme, look like Web 1.0 browser chrome, can't be styled, and pop outside the iframe in HA Ingress (ugly). Use a shadcn `Dialog` from `components/ui/dialog.tsx` for confirmations, a `Sheet` for side panels, and the `sonner` toast helpers for transient notifications. Origin: bug #15 — the AV.6 Restore button used `window.confirm()` for its "are you sure" prompt and it looked terrible against the dark app.
- **Use library components as intended.** Prefer composition over override. Adjust layout to accommodate library behavior rather than stripping features.
- **Server state in SWR, UI state in React.** SWR is the cache — read from it, don't copy it into `useState`.
- **Lift DropdownMenu `open` state out of any row cell.** When a `<DropdownMenu>` lives inside a TanStack Table cell (Devices hamburger, Queue Download, etc.), the 1 Hz SWR poll re-instantiates the row's cell components and tears down any state kept *inside* the menu — the dropdown slams shut mid-click. Always control the menu with an `open` + `onOpenChange` prop where the state lives in the parent tab component (`useState<string | null>(null)` keyed by row id, so only one dropdown is open at a time), and add that state to the columns `useMemo` deps so cells re-render when it flips. Origin: bug #2 (devices hamburger, 1.4.1-dev.3) and bug #71 (queue Download dropdown, 1.5.0-dev.75). If the same symptom shows up on a third menu, fix it the same way — do not try to stop SWR from re-rendering.
- **One component per file, colocate related code.** Types/helpers/constants used by a single component live near that component, not in a global utils grab-bag.
- **Semantic HTML.** `<button>` not `<div onClick>`, `<table>` for tabular data.
- **Icons: Lucide only.** All UI icons come from `lucide-react`. No emoji glyphs (🕐 📅 📌), no HTML entities (`&#8942;`, `&#9881;`), no custom SVGs inline. Sized with Tailwind (`size-3`, `size-3.5`, `size-4`) to match the shadcn convention. Wrap icon-only buttons with `aria-label` (see QS.2); when the icon carries meaning beyond decoration (status indicator, stateful toggle), wrap in a `<span title="…">` so hover reveals the semantic.
- **Batch operations get one toast.** Bulk actions use `Promise.all` and a single summary toast — never one toast per item. Bulk actions live in `App.tsx`, not in child component loops.
- **Think about the UX before shipping.** Walk through the change mentally: does the layout make sense on real data? Would it look sloppy to a user?
- **Update `.gitignore` whenever a new tool is introduced.** Most tools generate cache/lock/build/report directories — add them in the same commit that introduces the tool.

## Docker Image

The runtime image must keep `gcc`, `libffi-dev`, `libssl-dev`, and `git` installed — they are **not** build-only. ESPHome compiles C/C++ firmware at runtime via PlatformIO, and the server/worker lazy-install arbitrary ESPHome versions via `pip install esphome==X.Y.Z` at runtime (`scanner.ensure_esphome_installed`, `VersionManager._install`), whose transitive deps occasionally ship sdist-only on `linux/arm64` and need a compiler. The ~280 MB apt layer on `ha-addon/Dockerfile:19` is therefore intentional. `git` accounts for ~70–100 MB of it and is required because some PlatformIO/ESPHome dependencies pull from git URLs. Do **not** attempt to "optimize" this with a multi-stage build that drops build tooling from the final stage — that breaks on-demand ESPHome install on ARM hosts (the majority of HA users). Keep the chained `RUN apt-get update && install && rm -rf /var/lib/apt/lists/*` pattern exactly as-is: splitting it either bloats the layer (cleanup becomes a no-op) or creates a stale-index race.

## Performance Expectations

This is a home-lab tool used by one or two people intermittently, not a high-traffic web service. Optimize for **idle efficiency**, not peak throughput.

- **Idle is the default state.** When no user has the UI open and no compile is running, the server should be close to zero CPU. Background tasks (scheduler, device poller, entity poller, PyPI refresher) sleep on long intervals — don't add tight loops or frequent timers without justification. Log noise is a proxy for wasted work.
- **Active use can be expensive.** When a user is interacting with the UI or a compile is running, it's fine to do real work — scan configs, query devices, resolve YAML. Don't pre-compute or cache aggressively for a user who might not show up for days.
- **Be mindful of payload size.** Users access the UI over home networks that may be slow (VPN, remote access, mobile tethering). Enable gzip/deflate on the web server for JSON and static assets. Don't send large blobs (full job logs, firmware binaries) in polling responses — stream them on demand via WebSocket or separate endpoints. The 1Hz SWR polls should be small JSON; strip heavy fields (like `log`) from list endpoints and let the UI fetch them individually when a modal opens.
- **Don't over-optimize.** Shaving milliseconds off a response that runs once a second for one user is not worth the code complexity. Prefer simple, correct implementations over clever ones. If something is slow, measure before optimizing.

## Quality Standards (QG.1)

The bar for landing new code on `develop`. Most are automated; the rest are developer discipline.

### Automated gates (CI must be green)

1. **`pytest tests/`** with `pytest-cov` — full test suite.
2. **`ruff check ha-addon/server/ ha-addon/client/`** — Python lint, zero warnings.
3. **`mypy ha-addon/server/` and `mypy ha-addon/client/`** — type check, zero errors.
4. **`cd ha-addon/ui && npm run build`** — TypeScript + Vite production build.
5. **`cd ha-addon/ui && npm run test:e2e`** — 37-test mocked Playwright suite.
6. **`bash scripts/check-invariants.sh`** — the enforced invariants above.
7. **`.github/workflows/compile-test.yml`** — real `esphome compile` against 16 fixture YAMLs across platforms/frameworks.

### Manual gates (developer discipline)

1. **Test coverage for new code.** New module or significant function gets unit tests in the same commit. Bug fixes get a regression test that fails before the fix and passes after.
2. **E2E coverage for user-visible features.** New UI features get a mocked Playwright test in `e2e/` at minimum. Features touching the real compile path also get a test in `e2e-hass-4/`.
3. **Constants over magic strings.** When a string/header/path/threshold appears 2+ times, extract it. `ha-addon/server/constants.py` is the canonical home.
4. **Error handling at boundaries.** Use the helpers in `ha-addon/server/helpers.py` (`safe_resolve`, `json_error`, `clamp`, `constant_time_compare`). Every endpoint is a boundary.
5. **Update `dev-plans/WORKITEMS-X.Y.md` immediately after completing work.** Don't batch updates.
6. **Production smoke test after every turn.** `./push-to-hass-4.sh` is part of the dev loop, not a release-only step.

### What this is NOT

- No code style enforcement beyond ruff.
- No coverage target. Aim for tests that prove non-obvious behavior, not cosmetic coverage of trivial getters.
- No "comprehensive" PR templates. This is a single-developer project with an AI pair — keep the bar high, the process light.

## Documentation

When adding features or changing user-visible behavior, keep in sync:

- `README.md` — public project overview.
- `ha-addon/DOCS.md` — docs shown in the HA add-on panel.
- `ha-addon/CHANGELOG.md` — **written for users, not developers.** ~90% of the entry should cover things users see and experience (new UI features, UX improvements, bug fixes with user-visible symptoms, configuration changes). ~10% at most for internal/behind-the-scenes work (tests, CI, protocol types, code cleanup) — collapse into a brief "Under the hood" section, not detailed workstream breakdowns. Group by what the user experiences, not by internal workstream labels. Never say "no new features" when there are user-visible features — scan the WORKITEMS bug list for UI/UX work. **Only mention changes relative to the last public release** — if a bug was introduced during the dev cycle and fixed before release, it never existed from the user's perspective and doesn't belong in the changelog. Same for regressions, intermediate refactors, or test-only fixes that shipped and un-shipped within the same cycle. The changelog describes what changed *for the user upgrading from the previous stable*, not the full internal git history.

**User-facing docs must not reference internal development docs.** `README.md`, `ha-addon/DOCS.md`, and `ha-addon/CHANGELOG.md` are read by end users who don't have access to (or interest in) the repo's `dev-plans/` directory, work-item IDs (SP.8, AV.7, JH.5, UX_REVIEW §N.M), numbered bug IDs, or internal file paths (`settings.py`, `check-invariants.sh`). Describe behavior in plain terms. If a changelog entry needs to point somewhere for detail, point at the add-on UI ("see Settings → Authentication") or an externally-reachable GitHub file, never at `dev-plans/*` or internal source paths. This also means no "See WORKITEMS-X.Y.md for the full list" at the bottom of a changelog — the entry itself IS the full list as far as users are concerned.

## Project Tracking

Everything lives in `dev-plans/`:

- `dev-plans/README.md` — index of active + archived release plans. Current-release pointer lives here.
- `dev-plans/WORKITEMS-X.Y.md` — one file per release. Feature work items (checkboxes) + bug fixes (numbered). **Bug numbers are global and monotonic across releases** — never reset. Each file's first paragraph is the authoritative theme.
- `dev-plans/WORKITEMS-future.md` — unscheduled backlog.
- `dev-plans/archive/` — released WORKITEMS files from prior versions. Don't edit.
- `dev-plans/SECURITY_AUDIT.md` — security audit findings.
- `dev-plans/RELEASE_CHECKLIST.md` — step-by-step release process.
- `dev-plans/USER_PERSONA.md` — the target user "Pat." Scope / UX / copywriting tiebreaker.

**Release cadence is scope-driven, not time-boxed.** Ship a release when it delivers a meaningful chunk of functionality — never pad scope to fit a calendar and never compress it to meet one. Pull items forward from `WORKITEMS-future.md` or push them back into it based on whether they move the needle for Pat, not on how close to "done" they happen to be. The current `WORKITEMS-X.Y.md` is a commitment to a *coherent* release, not a deadline.

**Never reshuffle workitems between releases without an explicit ask.** Don't move action items in or out of the current `WORKITEMS-X.Y.md`, between release files, to/from `WORKITEMS-future.md`, or to/from `dev-plans/archive/` unless the user explicitly asks for that move. This includes "helpful" consolidation like deferring an unchecked item to the next release, promoting a future item because it seems timely, or rescoping a release that looks too big. Scope decisions are the user's call — surface a concern if you have one, but wait for an explicit instruction before touching the file structure. The only edits to WORKITEMS files you make unprompted are: checking off an item you just completed (with the `(X.Y.Z-dev.N)` tag), adding a newly-discovered bug under the appropriate Open Bugs section of the current release, and updating bug status in place.

**Turn** = one user prompt → one assistant response cycle. At the end of every turn:
1. Run `bash scripts/bump-dev.sh` — auto-increments `-dev.N`. Never skip.
2. Run `python scripts/test-matrix.py` — builds+pushes the three dev images to GHCR, then deploys + Playwright-smokes all three install paths (hass-4, haos-pve, standalone-pve) in parallel and prints a collated summary with clickable URLs. Targets budget is ≲5 min warm-cache. When iterating on a narrow UI change where the full matrix is overkill, `./push-to-hass-4.sh` remains as a faster single-target loop (source-rebuild, no GHCR round-trip).
3. **Check add-on logs for errors/warnings** on hass-4 after deploy: `ssh root@hass-4.local "ha addons logs local_esphome_dist_server" | grep -iE "ERROR|WARNING|Traceback|DeprecationWarning" | tail -20`. Fix any new issues before moving on. hass-4 is the primary target for log-watching; `build/test-matrix/<target>/deploy.log` has the per-target capture for the other two if a failure points there. Warnings that existed before this turn can be noted but don't block.
4. Update `dev-plans/WORKITEMS-X.Y.md` immediately — check the box, add the specific dev.N tag. Don't batch.

**Work item / bug checkbox format:** `- [x] **#NNN** *(X.Y.Z-dev.N)* — description` (the `#NNN` only applies to bugs). Use the exact dev.N, not a generic `dev`. For wontfix/duplicate/stale entries, use `~~**#NNN**~~ WONTFIX —` (strike-through bold ID + label).

**Next release file:** Create `dev-plans/WORKITEMS-X.Y+1.md` immediately after tagging `vX.Y.Z` (part of the post-release checklist). The current file moves to `dev-plans/archive/` at the same time, and this file's "Project Tracking" section is updated to point at the new current release.

**In-code TODOs must reference a workitem.** Every `TODO` / `FIXME` / `HACK` / `XXX` comment in source (`ha-addon/`, `scripts/`, `tests/`, `.github/`) is shaped as `TODO(<ID>): <body>` where `<ID>` is an identifier greppable in `dev-plans/` — a bug number (`#NNN`), a workstream code (`IT.2`, `SS.1`, `QS.1`, `PH.1`, …), or equivalent. A TODO with no pointer silently rots: the next reader has no way to tell whether it's still relevant, already fixed, deliberately parked, or never going to happen. Concrete rules:
- If the underlying work is worth doing someday → file it in the current `WORKITEMS-X.Y.md`, a successor file, or `WORKITEMS-future.md`, and use that ID in the TODO.
- If the work isn't worth filing → the TODO isn't worth keeping. Either fix the code or delete the comment.
- PR numbers, reviewer names, and commit SHAs are NOT valid pointers — PRs close, context evaporates. Always point at a dev-plans entry.

`dev-plans/RELEASE_CHECKLIST.md` has a grep step that fails the release if any in-source TODO points at an ID not found under `dev-plans/` — so stale pointers get caught before tag.

## Branching Strategy

Two long-lived branches: `develop` and `main`.

- **`develop` is the integration branch.** All dev work lands here — every turn ends with a commit + push to `develop`. The `-dev.N` versions live on this branch and are what `./push-to-hass-4.sh` deploys. Default branch for day-to-day work.
- **`main` is the release branch.** Tagged stable versions (`vX.Y.Z`) live here. Never commit directly.
- **Releases happen via pull request.** When `develop` is ready to cut a stable release, open a PR from `develop` → `main`, merge it, then tag. One PR per release; no long-running release branches for now. See `dev-plans/RELEASE_CHECKLIST.md` for the full flow.

This is deliberately simple for a single-developer project. If parallel lines of work ever need isolation, introduce short-lived feature branches off `develop` — don't complicate the trunk model preemptively.

## PR Review Loop

When a PR has review comments (Copilot bot, human reviewer, or both), the working pattern is:

1. **Address every comment in the same push.** Fix the code, update tests, land a workitem entry if the reviewer is pointing at future work. Don't leave comments hanging across pushes — a later reader can't tell which comment drove which commit.
2. **Resolve the review thread on GitHub in the same turn the fix lands.** Automatic — don't wait for a reminder. After `git push` succeeds:
    - Re-query the PR's unresolved threads (snippet below).
    - For every thread that the push just addressed, **post a reply** citing the commit SHA (e.g. `"Fixed in <SHA> — <one-line what-changed>"`) so a later reader can cross-reference thread ↔ commit without diffing the push.
    - **Then** call `resolveReviewThread` with that thread's id.
    - Re-query to confirm the thread flipped to `isResolved: true`.
   An unresolved thread looks like an open concern even when the underlying code is already fixed, and it clutters the PR sidebar until merge. This is as important as the fix itself.
3. **Exception — intentionally-deferred items.** If the comment points at work that's legitimately out of scope for this PR, file it in `dev-plans/WORKITEMS-*.md` first, reply to the comment with a pointer to the new workitem ID, *then* resolve the thread. The workitem is the record of the deferral; the thread should close because the next step (fix in a future PR) is now tracked elsewhere.
4. **Exception — disagreed with / false-alarm comments.** Reply with the reasoning (link to the `dev-plans/` entry or design doc that makes the decision, e.g. a WONTFIX finding in `SECURITY_AUDIT.md`), then resolve. A resolved thread with a reply is readable later; an ignored thread is a perpetual "maybe there's a bug here" sidebar row.

Resolve threads with:

```bash
# List unresolved threads on a PR:
gh api graphql -f query='
  query($owner:String!,$repo:String!,$pr:Int!) {
    repository(owner:$owner, name:$repo) {
      pullRequest(number:$pr) {
        reviewThreads(first:50) {
          nodes { id isResolved comments(first:1) { nodes { body } } }
        }
      }
    }
  }' -F owner=weirded -F repo=distributed-esphome -F pr=64

# Post a reply to a thread (cite the commit SHA that addressed it so
# the cross-reference is readable later). Needs the *first* comment's
# databaseId, which you fetch from the thread node — API lets you POST
# "replies" off the first comment:
CID=$(gh api graphql -f query='query($id:ID!){node(id:$id){... on PullRequestReviewThread{comments(first:1){nodes{databaseId}}}}}' \
      -F id=PRRT_kwDO... --jq .data.node.comments.nodes[0].databaseId)
gh api "repos/weirded/fleet-for-esphome/pulls/<PR>/comments/$CID/replies" \
  -X POST -f body='Fixed in <SHA> — <one-line what-changed>.'

# Resolve one by its thread id (from the query above):
gh api graphql -f query='
  mutation($id:ID!) {
    resolveReviewThread(input:{threadId:$id}) { thread { isResolved } }
  }' -F id=PRRT_kwDO...
```

End-of-turn rule when a PR has review feedback: **before marking the turn done, re-query unresolved threads and close every one the push addressed.** "Fix and leave the thread open" is not finished work — the fix is half-shipped until the thread is closed. Same rule for automated reviewers (Copilot) as for human reviewers.

## Deployment

`hass-4` is one of three machines in the integration-testing home lab that every turn smokes against. See `dev-plans/HOME-LAB.md` for the full host list, the `192.168.224.0/22` flat-network assumption, and the SSH setup all hosts share.

**`python scripts/test-matrix.py --web`** is the canonical end-of-turn command. **Always pass `--web` and `open http://127.0.0.1:8099` in the default browser** so Stefan can watch progress live — the dashboard streams per-target state, logs, and the final matrix, and it stays up after the run until Ctrl-C. The script builds+pushes three dev-tagged images to GHCR (addon for hass-4 + haos-pve, standalone server + client for standalone-pve), deploys in parallel via `push-to-hass-4.sh --from-ghcr`, `push-to-haos.sh --from-ghcr`, and `scripts/standalone/deploy.sh`, runs the `e2e-hass-4` Playwright suite against each (standalone filters out `@requires-ha`-tagged specs), and collates results into a pass/fail matrix + URL list. Per-target logs land under `build/test-matrix/<target>/`. `--targets` selects a subset; `--no-build` reuses the last pushed tag for fast iteration on the orchestrator itself. `--web-port` overrides the default 8099 if something else has the port.

**`./push-to-hass-4.sh`** is the fast-path single-target loop: source-tarball to hass-4, Supervisor local-build, `e2e-hass-4` Playwright run. Preferred when iterating on a UI-only change and the full matrix is overkill. Flags: `--from-ghcr` (pull instead of rebuild; what the matrix uses) and `--skip-smoke` (deploy only).

**`./push-to-haos.sh`** drives the HAOS VM at `192.168.226.135`. Same two flags.

**HA Core restart when the custom integration changes.** Changes under `ha-addon/custom_integration/` require a full `ha core restart` to take effect — the integration_installer copies new files to `/config/custom_components/` on add-on boot, but HA Core loads Python modules once at startup and doesn't hot-reload them. The add-on restart during deploy does NOT restart HA Core (Supervisor only restarts the add-on container). `push-to-hass-4.sh` hashes the integration directory and compares to a remote stamp file (`/tmp/esphome_fleet_integration.hash`); on a mismatch it runs `ha core restart` before the smoke suite. Skipped when the integration is byte-identical to the last push so non-integration turns don't pay the 30-60s restart cost.

