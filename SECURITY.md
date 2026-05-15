# Security Policy

## Supported Versions

| Version  | Supported          |
|----------|--------------------|
| 1.7.1    | ✅ Current release  |
| 1.7.0    | ✅ Previous stable — security fixes only if trivially backportable |
| < 1.7.0  | ❌ No patches       |

*(Note: the 1.5 release was developed as `1.4.1-dev.N` through dev.72 and renumbered late cycle as scope grew beyond a patch release. Docker tags with the `1.4.1-dev.N` stamp remain pullable from GHCR but are superseded by the 1.5.x stable tags.)*

## Reporting a Vulnerability

If you discover a security vulnerability, please [open a GitHub issue](https://github.com/weirded/fleet-for-esphome/issues/new) with:

- A description of the vulnerability
- Steps to reproduce
- The affected version(s)
- Any suggested fix (optional but appreciated)

For vulnerabilities you'd prefer not to disclose publicly, open a minimal placeholder issue asking for a private contact channel and the maintainer will follow up.

## Threat Model

This project's security posture is documented in [`dev-plans/SECURITY_AUDIT.md`](dev-plans/SECURITY_AUDIT.md), including:

- An explicit **Threat Model** section spelling out the six trust assumptions (browser, LAN, workers, Supervisor, operator, build-log provenance) and the items explicitly NOT accepted
- A supply chain threat model with current mitigation state
- An OWASP Top 10 (2021) assessment
- 21 individual findings (F-01 through F-21) with severity ratings and current status
- A "Post-audit mitigations" summary of everything shipped since the original 2026-03-29 audit

The stated threat model is a **trusted home network** behind Home Assistant's Ingress authentication. The server add-on relies on HA Ingress for UI authentication and a shared Bearer token for worker authentication. No open findings remain; **F-18 (worker pip install hash-pinning)** is marked WONTFIX as of 1.6.1 — see §F-18 in the audit for the rationale (single-version hash-pinning is defense-useless against lazy-install drift, since the committed version rarely matches the version actually requested at job time).

## Security Measures

### Supply chain

- **Hash-pinned Python dependencies** (`--require-hashes`) in both server and client Docker images. Lockfiles regenerated via `scripts/refresh-deps.sh`.
- **`pip-audit` + `npm audit`** gating CI on every push — hard failures block merge.
- **Dependabot** configured for pip × 2 (server + client), npm, docker × 2, and github-actions (weekly). Zero open alerts at release time.
- **Base-image digest pinning** — both the worker Dockerfile and the server's `ARG BUILD_FROM` default pin the Python base image by `@sha256:…`. In production under Supervisor the server base is overridden via `build.yaml` (which Supervisor's current `build_from` regex doesn't accept digest pins for — tracked); the worker pin is authoritative for every standalone deployment.
- **Cosign-signed GHCR images** (keyless / GitHub OIDC) — verify with:
  ```bash
  cosign verify \
    --certificate-identity-regexp 'https://github.com/weirded/fleet-for-esphome/.github/workflows/publish-.*\.yml@.*' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    ghcr.io/weirded/esphome-dist-client:latest
  ```
- **CycloneDX SBOM attestations** — every published image has a CycloneDX SBOM bound to its digest via `cosign attest --type cyclonedx`. Inspect the component inventory with `cosign verify-attestation --type cyclonedx ... | jq`.
- **SHA-pinned GitHub Actions** — every non-local `uses:` in the workflow files is pinned to a 40-char commit SHA with a trailing `# vN.M.P` version comment. New invariant in `scripts/check-invariants.sh` fails CI on any floating-tag reference. Dependabot bumps both the SHA and the version comment together.
- **PY-7 invariant** — every `--ignore-vuln` in `pip-audit` must carry an inline applicability assessment (why the fix can't be pulled in, whether our code exercises the vulnerable path, dated). Prevents silent CVE dismissals.
- **PY-8 invariant** — every direct dep in `requirements.txt` must also appear in `requirements.lock`. Enforced by `scripts/check-invariants.sh` so a forgotten `refresh-deps.sh` fails CI instead of shipping a broken image.
- **PY-9 invariant** — no macOS-only transitive packages (`pyobjc*`, `appnope`) in `requirements.lock`. Forces lockfile regeneration through the linux/amd64 Docker wrapper in `scripts/refresh-deps.sh`.

### Web surface

- **Security response headers** (CSP, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `X-Frame-Options: SAMEORIGIN`) on every UI response via a dedicated aiohttp middleware. Deliberately not applied to the `/api/v1/*` worker tier.
- **Path traversal prevention** — all file-endpoint handlers route through `helpers.safe_resolve()`.
- **`X-Ingress-Path` sanitization** — the Supervisor-supplied header is regex-stripped to `[/A-Za-z0-9._-]` before being interpolated into the HTML `<base href="…">`. Defence-in-depth against a misconfigured reverse proxy.
- **Monaco editor bundled via Vite** — no external CDN, eliminates a supply-chain vector and enables offline/air-gapped HA installations.
- **dompurify pinned to a patched version** via `package.json` overrides. Monaco's transitive dep tree kept the vulnerable 3.2.7 well past when 3.4.0 was released; we force the patched version directly rather than waiting on upstream. `shadcn` CLI is a devDependency so its `hono` transitive never ships to users.

### UI-API authentication (mandatory since 1.5.0)

- **`require_ha_auth` add-on option** — **default `true` in 1.5.0** (AU.7). Direct-port (`:8765`) `/ui/api/*` requests must carry a valid Bearer token or get `401 Bearer realm="Fleet for ESPHome"`. Ingress-tunneled access is unaffected (Supervisor injects `X-Ingress-Path`).
- **Two Bearer shapes accepted:** (a) the add-on's shared worker token — used automatically by the native HA integration's coordinator, which receives it via the Supervisor-discovery payload; (b) a Home Assistant long-lived access token, validated against the Supervisor's `/auth` endpoint.
- **Mutation attribution** — when the request was authenticated, compile / pin / schedule / rename / delete log lines suffix the resolved user's name (`…enqueued by stefan`), giving per-user audit trails in the add-on log. System-Bearer callers (the integration) attribute to `esphome_fleet_integration` so you can distinguish system from user actions.

### Protocol & validation

- **Typed protocol** (pydantic v2) with structured `ProtocolError` responses on malformed payloads. `PROTOCOL_VERSION` gate rejects mismatched peers with a clear error.
- **Byte-identical `protocol.py`** between server and client, enforced by `tests/test_protocol.py::test_server_and_client_protocol_files_are_identical` — prevents wire-contract drift.
- **Log payload DoS guard** — `/api/v1/jobs/{id}/log` rejects bodies larger than ~2MB (`log_payload_too_large` → HTTP 413) before aiohttp buffers the full input.

### Supervisor hardening

- **AppArmor profile (attached, permissive-plus-targeted-denies)** — `ha-addon/apparmor.txt` ships alongside `config.yaml` with `apparmor: true`; Supervisor loads the named profile on install/upgrade and the security-star card lights up. **Be honest about what this buys.** The broad allow rules (`file,`, `capability,`, `network,`, `signal,`, `dbus,`, `unix,`, `mount,`, `pivot_root,`) are functionally close to `unconfined` at the kernel boundary — the Supervisor badge is cosmetic at that layer. What the profile *does* add is a set of **targeted deny rules** (1.6.1 PR #80 review) that block a compromised compile-time Python (ESPHome's `external_components` / `includes:` / `libraries:`, the highest-leverage attack surface the add-on exposes) from reading host shadow files (`/etc/shadow*`, `/etc/gshadow*`), Supervisor secrets (`/run/secrets/**`), arbitrary process memory (`/proc/*/mem`), writing kernel sysctls, or ptracing across the container namespace. Tightening the *permissive* side (replacing unqualified `file,` + `capability,` with specific path allowlists) is tracked against observed denial telemetry — the working allowlist varies by Python minor version + PlatformIO release + ESPHome toolchain, so pinning it eagerly would churn every upstream bump.
- **`stage: experimental` flag removed** from `config.yaml` — the add-on defaults to stable per Supervisor convention now that cosign signing, SBOM attestations, the AppArmor profile, and hash-pinned deps are all in place.
- **Privileged-flag rationale documented** — `DOCS.md` has a "Why this add-on requests these permissions" section with a concise reason for each non-default flag (`host_network`, `hassio_api`, `homeassistant_api`, `auth_api`, `privileged: [NET_RAW]`). The lower security star count has a written rationale store-page readers can check before install.
- **`NET_RAW` capability scoped to ICMP ping (1.7.0).** The 1.7.0 ping diagnostic (`POST /ui/api/targets/{filename}/ping`) tries unprivileged datagram ICMP first and falls back to a raw-socket path for installs where the host's `net.ipv4.ping_group_range` disables the unprivileged route (HAOS default). The capability is requested via `privileged: [NET_RAW]` in `config.yaml` and is the only non-default Linux capability the add-on holds. The endpoint is under `/ui/api/*` (HA Ingress / `require_ha_auth` Bearer-gated, same authorisation tier as every other UI mutation) and is rate-bounded by ICMP's own `count=10, timeout=2 s` shape.

### Auth / observability

- **Structured 401 reasons** (`missing_authorization_header`, `authorization_not_bearer_scheme`, `bearer_token_mismatch`) logged at WARNING with the peer IP for every worker-tier auth refusal.
- **IPv6-aware peer IP normalization** — IPv6 zone IDs stripped, IPv4-mapped IPv6 unwrapped, `peername=None` handled without crashing.
- **Token file least-privilege** — `/data/auth_token` is written with `0600` so even a world-readable `/data` volume mount on the host can't leak the worker-tier bearer.

### What is *not* in scope

These are accepted risks within the home-network threat model; see the full audit for rationale:

- **HTTP between workers and server** (not HTTPS). Users with remote workers across network segments should front the server with their own reverse proxy.
- **Bearer token visible to the browser** (required for the Connect Worker modal's `docker run` command UX).
- **Direct-port `/ui/api/*` Bearer required by default** (AU.7). If a user flips `require_ha_auth: false` deliberately — for an isolated test harness, say — they're opting out of this default and the old "relies only on HA Ingress" trust model applies.
- **`secrets.yaml` delivered to every build worker — filtered when the server is on ESPHome 2026.4+** (workers receive only the `!secret` keys the bundled target actually references, courtesy of ESPHome 2026.4's built-in bundle format; the full `secrets.yaml` is no longer shipped on that path). When the server's *active* ESPHome is pinned below 2026.4 (lifted in 1.7.1, see #131), the bundle falls back to a full-config-dir tar that includes the entire `secrets.yaml` plus every other device's YAML. Workers are trusted per the threat model; the modern, scoped path is the default and narrows the blast radius when a worker is on a less-trusted host. Operators sharing workers with untrusted parties should keep the server pinned to 2026.4+ regardless of per-target version pins.
- **Build workers can execute `external_components:` / `includes:` / `libraries:` Python** during compile — core ESPHome feature, accepted because workers are trusted.
- **Worker-to-worker job-result authorization isn't checked** on `submit_result` / `update_status` — any authenticated worker can submit results for any job. Accepted because workers are trusted.

### Residual posture

All 21 audit findings are now FIXED, WONTFIX-by-threat-model, or marked INFO. Cycle deltas for 1.7.1 (no F-* status flips):

- **Brand rebrand — metadata-only at the network/auth layer.** *"ESPHome Fleet"* (1.5.0–1.7.0) renamed to **Fleet for ESPHome**. Verified pre-flip in `dev-plans/archive/WORKITEMS-1.7.1.md` BR.1 sub-bullet 12: code identifiers (add-on slug, integration domain, GHCR image names, mDNS service type, Bearer-realm consumers, all `esphome_fleet.*` HA service names) keep their existing forms. No migration on existing installs; no trust-boundary change. <!-- br1-allow: rebrand chronology -->
- **Legacy full-config-dir bundle path (#131).** Lifted the install-time refusal of ESPHome <2026.4. The server's `create_bundle` now branches on the *server*'s installed ESPHome version: ≥2026.4 keeps the validated, target-scoped `ConfigBundleCreator` subprocess; <2026.4 falls back to a deterministic full-config-dir tar (mirrors the pre-1.6.2 layout). Trade-off: the legacy path ships every device's `secrets.yaml` and every other device's YAML to the worker — see the bullet under "What is *not* in scope" above. Per-job ESPHome version selection is independent of the server's bundling version (the server's *active* venv decides bundle shape; the *job*'s pinned version decides what the worker compiles with), so an operator who keeps the server on 2026.4+ retains the scoped bundle even when individual targets compile against older releases.
- **Quieter device polling (#238).** Pre-1.7.1 the add-on opened a fresh `aioesphomeapi` connection to every known device every 60 s. The new default reads steady-state liveness from mDNS announcements (which already carry `version` in the TXT record) and only opens an API connection on first sight of a new device or right after a Fleet-driven OTA. Reduces the add-on's egress footprint inside the LAN by ~60×. New `device_native_api_poll` Setting (default OFF) restores the old behaviour for users diagnosing a flaky device. Defensive only.
- **Worker eligibility-check error handling (#234).** `GET /api/v1/jobs/next` now wraps the per-worker eligibility check in a try/except that logs the traceback at WARNING with `client_id` and falls through to HTTP 204 instead of bubbling a HTTP 500. Closes a DoS-by-stupidity loop where a single misformatted worker would lock-loop the server with 500s while never claiming a job. Same trust tier; defensive only.
- **Server-side firmware-upload grace window (#236).** `POST /api/v1/jobs/{id}/firmware/{variant}` accepts a 60-second grace past `finished_at` for the still-assigned worker only (other workers' uploads on a finished job continue to be rejected via `client_id` lookup). Logged at INFO. Closes a worker-side race where slow variant uploads after the OTA succeeded would land just after the timeout-checker flipped the job to FAILED. Not a trust-boundary change.
- **Dependabot alerts at release time:** two open HIGH alerts on `fast-uri` (#10 / #14) — transitive of the *dev-only* `shadcn` CLI → `@modelcontextprotocol/sdk` → `ajv`. Never reaches production bundles (Vite-built UI does not import `shadcn` at runtime). Upstream advisories list `first_patched_version: null` — nothing to upgrade to. Tracked as `fast-uri-DEV` WONTFIX in `dev-plans/archive/WORKITEMS-1.7.1.md`; re-evaluate next release.

Cycle deltas for 1.7.0 (no F-* status flips):

- **`NET_RAW` capability added (DM.2 / #206).** The new ICMP ping diagnostic needs raw ICMP sockets on installs where `net.ipv4.ping_group_range` is empty (HAOS default `1 0`). `config.yaml` declares `privileged: [NET_RAW]`; the helper tries the unprivileged datagram path first and only falls back to raw sockets when the kernel refuses the safer path. Scoped to ICMP — the endpoint accepts no arbitrary host, only resolves the configured device's OTA address through `device_poller.resolve_ota_address`. The capability is the only non-default Linux capability the add-on holds.
- **Bundle-creation race eliminated (#111).** Before 1.7.0, parallel job claims could race ESPHome's `git.clone_or_update` (which is not safe under concurrent invocation) and surface partial-state trees as misleading validation errors in build logs. 1.7.0 wraps every bundle subprocess behind a server-wide `asyncio.Lock`, eliminating both the sporadic build-log error confusion and the (lower-probability) race-window in which a concurrently-extracting `external_components` checkout could leak intermediate state into another job's bundle. Filed upstream against ESPHome.
- **Bundle-failure log scrubbed of ESPHome logger chatter (#112).** Job-failure messages used to be decorated with whatever ESPHome's own `_LOGGER` printed during the validation subprocess (deprecation warnings, INFO chatter, the upstream `2026.4.3` false-positive `Including a single package under \`packages:\` is deprecated` for the dict-of-strings `packages:` form). Now only our explicit error writes — and uncaught Python tracebacks — reach the captured stream. Defensive: the chatter wasn't sensitive but the noise around it concealed real diagnostics.
- **Rendered-config endpoint never logs the rendered body (RC.1).** The new `GET /ui/api/targets/{filename}/rendered-config` returns a fully-resolved YAML with plaintext `!secret` substitutions; the handler logs only the byte count (`rendered config bytes=NN`), never the body. A regression test (`tests/test_rendered_config.py::test_rendered_config_logs_do_not_leak_output`) pins this contract — log scrubbing is not optional. Output stripping ANSI conceal sequences is byte-stripped server-side so a downstream Monaco render never displays raw escape codes that would otherwise leak `secret:`/`key:`/`psk:` values into the visible YAML.
- **Git working tree clean after every UI file mutation (#197 / PY-11).** New invariant: every `/config/esphome/` mutation endpoint must leave `git status --porcelain` empty after `drain_pending_commits()` returns. Before 1.7.0, archive (`os.unlink`) and rename (`git mv`) endpoints could leave dangling deletion entries staged but not committed, which the next user save would sweep into someone else's `git log` and produce an inaccurate rollback diff. 12-scenario regression test (`tests/test_git_clean_after_ops.py`) drives every file-mutating endpoint and asserts a clean tree.
- **Firmware retention + backup_exclude (#198 / #199).** New `firmware_retention_days` Settings field (default 2) caps how long compile binaries linger on disk; `firmware/` added to the add-on's `backup_exclude` so HA snapshots no longer carry 200+ MB of regenerable binaries. Reduces the data exposure on a stolen / leaked HA backup tarball.
- **Tags + routing-rule endpoints (TG.*).** Five new mutation endpoints (`POST /ui/api/workers/{id}/tags`, `GET / POST /ui/api/routing-rules`, `PUT / DELETE /ui/api/routing-rules/{id}`). All under `/ui/api/*` so they inherit the existing `require_ha_auth` + Ingress auth model — no new tokens, no new privileges, no widening of the trust boundary. Bodies are validated through pydantic models / typed dict shapes. Per-device `routing_extra` round-trips through the existing `POST /ui/api/targets/{filename}/meta` (no new endpoint shape).
- **Disk-quota endpoint (DQ.5).** New `POST /ui/api/workers/{id}/disk-quota` accepts `{disk_quota_bytes: int | null}` with `_validate_int_range(1 GiB, 1 TiB)` server-side bound. Same auth tier; no new wire-protocol fields except additive optional `RegisterRequest.disk_quota_bytes` / `HeartbeatResponse.set_disk_quota_bytes` / `SystemInfo.{disk_usage_bytes,disk_quota_bytes,last_eviction_freed_bytes}` (`PROTOCOL_VERSION` unchanged — backward compatible).

Cycle deltas for 1.6.2:

- **Job-bundle scope narrowed (BD).** Before 1.6.2, every job claim shipped the full `/config/esphome/` directory to the claiming worker — `.git/` (history + remote URLs + any wired-up push credentials), the complete `secrets.yaml` with every device's Wi-Fi and API keys, and any in-place PlatformIO build caches. A worker operator on a less-trusted host effectively held read access to the entire fleet's secrets and git history. 1.6.2 switches to ESPHome's built-in bundle format (`esphome.bundle.ConfigBundleCreator`, ESPHome 2026.4+), which ships only the files the target's validated config references, with `secrets.yaml` filtered down to the keys the target actually uses. `.git/` and cross-device secrets no longer leave the server by construction.

Cycle deltas for 1.6.1:

- **F-13 (Docker base image digest pinning)** moved OPEN → **FIXED (partial)** via SS.4. Worker Dockerfile pins `python:3.11-slim@sha256:…`; server Dockerfile pins the `ARG BUILD_FROM` default digest. Supervisor's production build path still can't carry a digest (upstream `build_from` regex rejects `@sha256:…`); partial until that's relaxed.
- **F-18 (worker pip install hash-pinning)** was marked FIXED (partial) in 1.5.0 via SC.3, then re-assessed and marked **WONTFIX** in 1.6.1: the single-version constraints file we committed rarely matched the version actually requested at job time (users routinely pin older ESPHome versions or track newer releases than we'd had time to generate constraints for), so the hardened `--require-hashes` path's hit rate in practice was ~0% and the fallback-to-unpinned-install behavior was the load-bearing case. See §F-18 in the audit for the full re-assessment.
- **F-21 (add-on ran unconfined)** added and immediately **FIXED** in the same cycle via SS.1 — AppArmor profile attached, Supervisor runs the container under confinement.

If your deployment doesn't match the trusted-home-network model, read the audit carefully before exposing the add-on.
