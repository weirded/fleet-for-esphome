#!/usr/bin/env python3
"""Multi-target deploy + smoke orchestrator for Fleet for ESPHome.

Deploys the current dev build (ha-addon/VERSION) to three install paths in
parallel, runs the e2e-hass-4 Playwright suite against each, and prints a
collated pass/fail summary plus clickable URLs.

Replaces ``./push-to-hass-4.sh`` as the end-of-turn smoke command. Keeps
push-to-hass-4.sh around as a fast-path single-target loop for UI-only
iteration (no GHCR round-trip).

Targets (see dev-plans/HOME-LAB.md):
  hass-4               always-on HAOS box at 192.168.225.112
  haos-pve             throwaway HAOS VM on the `pve` Proxmox host
  standalone-pve       plain Docker host `docker-pve` running docker-compose

Image flow: GitHub Actions (publish-addon.yml / publish-server.yml /
publish-client.yml) already builds and pushes the three GHCR images on
every develop push — keyed off ha-addon/VERSION, which bump-dev.sh
changes every turn. This script waits for those tags to appear, then
deploys. No laptop-side GHCR write auth required.

End-of-turn sequence is therefore:
  bump-dev.sh → git commit + push → python scripts/test-matrix.py

Usage:
  scripts/test-matrix.py                    # all targets (default)
  scripts/test-matrix.py --targets hass-4   # single target
  scripts/test-matrix.py --no-wait          # skip the GHCR poll
  scripts/test-matrix.py --seq-tests        # serialize Playwright runs
  scripts/test-matrix.py --list             # show targets and exit
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import errno
import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "ha-addon" / "VERSION"
LOG_ROOT = REPO_ROOT / "build" / "test-matrix"
GHCR_OWNER = "weirded"

# Matches the upstream publish-addon.yml IMAGE_NAME pattern: one per arch
# with the arch prefixed. {arch} gets substituted to "amd64" here since
# all three targets are x86_64.
IMG_ADDON = f"ghcr.io/{GHCR_OWNER}/amd64-addon-esphome-dist-server"
# Standalone compose consumes these two unprefixed images
# (docker-compose.yml at repo root).
IMG_SERVER = f"ghcr.io/{GHCR_OWNER}/esphome-dist-server"
IMG_CLIENT = f"ghcr.io/{GHCR_OWNER}/esphome-dist-client"


# ANSI colors for per-target prefixes. Falls back to plain text when stdout
# isn't a TTY (pipes, CI logs). 38;5;N = 256-color.
COLORS = {
    "hass-4":              "\033[38;5;42m",   # green
    "haos-pve":            "\033[38;5;39m",   # blue
    "standalone-pve":      "\033[38;5;170m",  # magenta
    "build":               "\033[38;5;214m",  # orange
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _read_text(p: Path) -> str:
    """Read ``p`` stripped, or empty string if missing/unreadable. Used
    to pull per-target HA long-lived tokens out of
    ``~/.config/distributed-esphome/*-ha-token`` so ``@requires-ha``
    Playwright specs can run without needing the user to export
    ``HASS_TOKEN`` every shell."""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def color(name: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{COLORS.get(name, '')}{text}{RESET}"


def ts() -> str:
    """Local-time ``HH:MM:SS`` stamp used as a line prefix in every
    piece of streamed output. Minute-level granularity would lose us
    per-step timing on a typical 2-minute deploy; the matrix never
    outpaces one line per second, so subseconds would be noise.
    """
    return datetime.now().strftime("%H:%M:%S")


def tprint(text: str = "", **kwargs: Any) -> None:
    """``print`` with a leading wall-clock timestamp. Wrapping every
    top-level status line (``==> Deploying …``, ``==> All done …``)
    so they sort cleanly next to the subprocess output that already
    carries its own stamp via :func:`run_streaming`.
    """
    print(f"{ts()} {text}", **kwargs)


# ---------------------------------------------------------------------------
# Tee: every line the matrix prints to stdout also lands in an in-memory
# ring buffer so the --web HTTP server can serve it back to a browser.
# Wrapping sys.stdout once at startup (see main()) means there's no need
# to route every `print()` through a helper.
# ---------------------------------------------------------------------------

_output_lines: collections.deque[str] = collections.deque(maxlen=5000)
_output_lock = threading.Lock()


class _TeeStdout:
    def __init__(self, original: Any) -> None:
        self._orig = original
        # Python's print() makes two write() calls per line — text first,
        # then "\n" — so naive per-write splitlines yields a phantom "" for
        # every line printed. Buffer the unterminated tail across writes
        # and only emit complete lines.
        self._partial = ""

    def write(self, s: str) -> int:
        n = self._orig.write(s)
        if s:
            with _output_lock:
                self._partial += s
                if "\n" in self._partial:
                    parts = self._partial.split("\n")
                    self._partial = parts[-1]
                    for line in parts[:-1]:
                        _output_lines.append(line)
        return n

    def flush(self) -> None:
        self._orig.flush()

    def isatty(self) -> bool:
        return self._orig.isatty()

    def fileno(self) -> int:
        return self._orig.fileno()


# ---------------------------------------------------------------------------
# Per-target state files. Each target's state.json is the single source
# of truth the --web server reads to render the status table; the matrix
# overwrites it at each phase transition.
# ---------------------------------------------------------------------------

def _write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic enough for a reader that polls: write to tmp, rename.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _clear_state_dir() -> None:
    """Remove stale state files from a prior run so the web UI starts fresh."""
    if not LOG_ROOT.exists():
        return
    (LOG_ROOT / "build-state.json").unlink(missing_ok=True)
    for d in LOG_ROOT.iterdir():
        if d.is_dir():
            (d / "state.json").unlink(missing_ok=True)
            (d / "version.json").unlink(missing_ok=True)


# Images the matrix waits for. All three are published by the CI
# workflows under .github/workflows/publish-*.yml on every develop push
# whose diff touches ha-addon/VERSION (which bump-dev.sh always does).
EXPECTED_IMAGES = [IMG_ADDON, IMG_SERVER, IMG_CLIENT]

# Map image short-name (last path segment) → publish workflow filename.
# Used to render per-workflow GitHub Actions status in the --web UI.
GH_REPO = f"{GHCR_OWNER}/distributed-esphome"
WORKFLOW_BY_IMAGE = {
    "amd64-addon-esphome-dist-server": "publish-addon.yml",
    "esphome-dist-server": "publish-server.yml",
    "esphome-dist-client": "publish-client.yml",
}


@dataclass
class Target:
    name: str
    # Direct add-on port, with Bearer auth. Used by Playwright as
    # FLEET_URL and by the matrix for version-ready probes.
    base_url: str
    # Command to deploy (with --skip-smoke appended). Run from REPO_ROOT.
    deploy_cmd: list[str]
    # Browser-friendly URL for the user to click from the --web dashboard
    # and the terminal summary. For HA add-on targets this is the Ingress
    # URL (port 8123 + /<addon-slug>) so the HA session auto-authenticates
    # — the direct 8765 URL would hit a 401. Defaults to base_url if
    # unset (standalone-pve has no Ingress, so the direct port works).
    open_url: str = ""
    deploy_env: dict[str, str] = field(default_factory=dict)
    # Path the deploy script writes the add-on token to on success. Read
    # after deploy completes and passed to Playwright as FLEET_TOKEN.
    token_cache: Path = field(default_factory=Path)
    # Extra env to pass to Playwright (FLEET_TARGET, HASS_URL/HASS_TOKEN
    # for ha-services.spec.ts, etc.).
    playwright_env: dict[str, str] = field(default_factory=dict)
    # Extra args to `npm run test:e2e:hass-4 --`, e.g.
    # ["--grep-invert=@requires-ha"].
    playwright_args: list[str] = field(default_factory=list)

    def get_open_url(self) -> str:
        return self.open_url or self.base_url


def make_targets(version: str) -> dict[str, Target]:
    home = Path.home()
    tag = version  # 1:1 with VERSION; no separate --from-ghcr TAG argument

    # HA add-on slug — Ingress URL is http://<ha-host>:8123/<slug>
    addon_slug = "local_esphome_dist_server"

    return {
        "hass-4": Target(
            name="hass-4",
            base_url="http://192.168.225.112:8765",
            # Use the HA Ingress URL so the click auto-authenticates via
            # the HA session. The direct :8765 returns 401 under
            # require_ha_auth=true.
            open_url=f"http://hass-4.local:8123/{addon_slug}",
            deploy_cmd=["./push-to-hass-4.sh", "--from-ghcr", "--skip-smoke"],
            token_cache=home / ".config" / "distributed-esphome" / "hass4-token",
            playwright_env={
                "HASS_URL": "http://hass-4.local:8123",
                # HA long-lived token for the /api/services calls in
                # ha-services.spec.ts. Prefer an on-disk token (created
                # once in HA Profile → Security → Long-Lived Access
                # Tokens, saved to ~/.config/distributed-esphome/
                # hass4-ha-token) so the full @requires-ha set runs.
                # Falls back to $HASS_TOKEN from the shell, else empty
                # → ha-services.spec.ts self-skips with a clear
                # remediation message.
                "HASS_TOKEN": _read_text(
                    home / ".config" / "distributed-esphome" / "hass4-ha-token"
                ) or os.environ.get("HASS_TOKEN", ""),
                "FLEET_TARGET": "cyd-office-info.yaml",
            },
        ),
        "haos-pve": Target(
            name="haos-pve",
            base_url="http://192.168.226.135:8765",
            open_url=f"http://192.168.226.135:8123/{addon_slug}",
            deploy_cmd=["./push-to-haos.sh", "--from-ghcr", "--skip-smoke"],
            deploy_env={"HAOS_URL": "http://192.168.226.135:8123"},
            token_cache=home / ".config" / "distributed-esphome" / "haos-addon-token",
            playwright_env={
                # HA token + URL passed through so non-services
                # @requires-ha specs (e.g. direct-port-auth) can run.
                # ha-services.spec.ts itself filters out via
                # playwright_args below — see comment there.
                "HASS_URL": "http://192.168.226.135:8123",
                "HASS_TOKEN": _read_text(
                    home / ".config" / "distributed-esphome" / "haos-token"
                ) or os.environ.get("HASS_TOKEN", ""),
                "FLEET_TARGET": "cyd-world-clock.yaml",
            },
            # Skip the @requires-integration-config slice on the
            # throwaway VM: the integration installer copies files to
            # /config/custom_components/ at add-on boot, but no
            # automated step completes the config flow, so HA never
            # registers the esphome_fleet.* services. Tracked for
            # 1.6.3 — automating config-entry creation in
            # scripts/haos/install-addon.sh would let these run here.
            playwright_args=["--grep-invert=@requires-integration-config"],
        ),
        "standalone-pve": Target(
            name="standalone-pve",
            # IP, not the `docker-pve` SSH alias — the browser and
            # Playwright's Node client don't read ~/.ssh/config. The
            # SSH-side (STANDALONE_HOST below) still uses the alias.
            base_url="http://192.168.227.90:8765",
            # Standalone has no HA Ingress, so the direct port IS the
            # open URL. Also: standalone defaults to require_ha_auth=false,
            # so clicking it loads the UI without a Bearer prompt.
            deploy_cmd=["bash", "scripts/standalone/deploy.sh"],
            deploy_env={"TAG": tag, "STANDALONE_HOST": "docker-pve"},
            token_cache=home / ".config" / "distributed-esphome" / "standalone-token",
            playwright_env={
                "FLEET_TARGET": "cyd-world-clock.yaml",
            },
            playwright_args=["--grep-invert=@requires-ha"],
        ),
    }


@dataclass
class TargetResult:
    target: str
    deploy_ok: bool = False
    deploy_elapsed: float = 0.0
    tests_passed: int = 0
    tests_total: int = 0
    tests_ok: bool = False
    total_elapsed: float = 0.0
    report_dir: Path = Path()
    error: str = ""
    # Wall-clock epoch seconds when the target chain started. Used by the
    # terminal summary and the --web dashboard so readers can correlate
    # live console output (also timestamped) with the table.
    started_at_wall: float = 0.0


# ---------------------------------------------------------------------------
# Subprocess plumbing: run a command, tee stdout+stderr to prefixed terminal
# output AND a per-target log file, return the exit code.
# ---------------------------------------------------------------------------

async def run_streaming(
    cmd: list[str],
    *,
    prefix: str,
    log_path: Path,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    """Run ``cmd``, streaming output prefixed with ``[prefix]`` AND appending
    to ``log_path``. Returns the exit code.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Mark the start of this command in the log so post-mortems can tell
    # where each phase begins.
    with log_path.open("a") as log:
        log.write(f"\n===== {ts()} $ {' '.join(cmd)} =====\n")

    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    # Line-buffered stdout for Python children. Bash children generally flush
    # on newline when stdio is a pipe, but this makes the live view snappier
    # when any step is a Python script.
    full_env.setdefault("PYTHONUNBUFFERED", "1")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=full_env,
        cwd=str(cwd) if cwd else None,
    )

    tag = color(prefix, f"[{prefix:<14}]")
    assert proc.stdout is not None
    with log_path.open("a") as log:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            # Each line in the per-target log gets its own stamp (same
            # HH:MM:SS format as console) so the file reads like a
            # timestamped tail — post-mortem on which step stalled is
            # a grep+sort away.
            stamp = ts()
            log.write(f"{stamp} {line}\n")
            log.flush()
            print(f"{stamp} {tag} {line}", flush=True)

    return await proc.wait()


# ---------------------------------------------------------------------------
# Preflight checks — fail fast with actionable errors.
# ---------------------------------------------------------------------------

def preflight(skip_wait: bool) -> None:
    problems: list[str] = []

    if not VERSION_FILE.exists():
        problems.append(f"VERSION file missing at {VERSION_FILE}")

    if not skip_wait and shutil.which("docker") is None:
        problems.append(
            "`docker` not found on PATH (needed for `docker buildx imagetools inspect` "
            "against GHCR)"
        )

    for script in ("push-to-hass-4.sh", "push-to-haos.sh"):
        if not (REPO_ROOT / script).exists():
            problems.append(f"missing {script} at repo root")

    if problems:
        sys.stderr.write("Preflight failed:\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        sys.exit(2)


# ---------------------------------------------------------------------------
# Step 1: wait for CI-published images on GHCR.
# ---------------------------------------------------------------------------

async def _tag_exists(image: str, version: str) -> bool:
    """Return True iff ghcr.io/<image>:<version> currently resolves.

    Uses `docker buildx imagetools inspect`, which hits the registry API
    (no pull) and returns exit 0 iff the tag is present.
    """
    proc = await asyncio.create_subprocess_exec(
        "docker", "buildx", "imagetools", "inspect", f"{image}:{version}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


def _head_sha() -> str:
    """HEAD commit on the current branch, used to look up the GitHub
    Actions run that built this dev-tag. Empty string if git fails.
    """
    try:
        import subprocess as _sp
        r = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except OSError:
        return ""


async def _workflow_run(workflow_file: str, head_sha: str) -> dict[str, Any]:
    """Fetch the latest GitHub Actions run for ``workflow_file`` on
    commit ``head_sha``. Returns {status, conclusion, url}. Falls back
    to ``status='not_started'`` when no run matches, ``status='unknown'``
    when `gh api` fails (not authed, offline, etc.) — either way the
    matrix still proceeds on GHCR tag presence, so this is just for the
    UI.
    """
    if not head_sha:
        return {"status": "unknown", "conclusion": None, "url": None}
    # Query string goes in the URL — `gh api -f key=val` sends a form
    # body on GET, which the API ignores / rejects as a bad request.
    path = (
        f"/repos/{GH_REPO}/actions/workflows/{workflow_file}/runs"
        f"?head_sha={head_sha}&per_page=1"
    )
    cmd = ["gh", "api", path, "--jq", ".workflow_runs[0] // empty"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout_b, _ = await proc.communicate()
    if proc.returncode != 0:
        return {"status": "unknown", "conclusion": None, "url": None}
    out = stdout_b.decode("utf-8", errors="replace").strip()
    if not out:
        return {"status": "not_started", "conclusion": None, "url": None}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"status": "unknown", "conclusion": None, "url": None}
    return {
        "status": data.get("status") or "unknown",  # queued | in_progress | completed
        "conclusion": data.get("conclusion"),        # success | failure | cancelled | None
        "url": data.get("html_url"),
    }


async def wait_for_ghcr_tags(version: str, timeout_s: int = 600) -> bool:
    """Poll both the GitHub Actions publish workflows AND the GHCR
    registry for the three dev-tagged images. Returns True once every
    image is published on GHCR (the authoritative signal for downstream
    deploys). Workflow status is collected for the --web UI; a workflow
    failure short-circuits the wait so we don't sit out the full timeout.
    """
    head_sha = _head_sha()
    tprint(color(
        "build",
        f"==> Waiting for CI build (commit {head_sha[:7] or '?'}, tag {version}, "
        f"timeout {timeout_s}s) ...",
    ), flush=True)
    start = time.monotonic()
    published: set[str] = set()
    workflows: dict[str, dict[str, Any]] = {}
    last_progress = 0.0

    short_names = [img.rsplit("/", 1)[-1] for img in EXPECTED_IMAGES]

    def snapshot(phase: str) -> None:
        _write_state(LOG_ROOT / "build-state.json", {
            "version": version,
            "phase": phase,  # waiting | ready | failed
            "head_sha": head_sha[:7],
            "repo": GH_REPO,
            "started_at": start,
            "elapsed": time.monotonic() - start,
            "images_total": len(EXPECTED_IMAGES),
            "images_published": sorted(i.rsplit("/", 1)[-1] for i in published),
            "workflows": workflows,
        })

    snapshot("waiting")
    while True:
        # Poll workflows and GHCR tags together each round.
        wf_results = await asyncio.gather(*[
            _workflow_run(WORKFLOW_BY_IMAGE[n], head_sha) for n in short_names
        ])
        for name, result in zip(short_names, wf_results):
            workflows[name] = result

        # Tag-existence alone is racy: the ``1.6.2`` tag carries over
        # from the previous push and resolves True the moment the matrix
        # starts, even though the current commit's CI workflow is still
        # building. Deploy then pulls the stale digest. Require BOTH the
        # tag to exist AND the corresponding publish workflow for this
        # commit's head-sha to be `completed/success` before marking an
        # image "published." ``not_started`` for >NOT_STARTED_GRACE_S
        # means the workflow's path filter didn't match this commit
        # (e.g. a scripts/-only change) — fall back to tag-only there,
        # since no new image is coming. ``unknown`` (gh unauthed / offline)
        # also falls back so a misconfig doesn't block the run.
        NOT_STARTED_GRACE_S = 45
        elapsed_now = time.monotonic() - start
        tag_checks = await asyncio.gather(*[
            _tag_exists(img, version) for img in EXPECTED_IMAGES if img not in published
        ])
        for img, ok in zip([i for i in EXPECTED_IMAGES if i not in published], tag_checks):
            short = img.rsplit("/", 1)[-1]
            wf = workflows.get(short, {})
            wf_done = wf.get("status") == "completed" and wf.get("conclusion") == "success"
            wf_unavailable = wf.get("status") == "unknown"
            wf_skipped_by_paths = (
                wf.get("status") == "not_started" and elapsed_now >= NOT_STARTED_GRACE_S
            )
            if ok and (wf_done or wf_unavailable or wf_skipped_by_paths):
                published.add(img)
                if wf_done:
                    reason = "workflow+tag"
                elif wf_unavailable:
                    reason = "tag-only (workflow status unavailable)"
                else:
                    reason = "tag-only (workflow path filter didn't match this commit)"
                tprint(color(
                    "build",
                    f"[ghcr          ] ✔ {short}:{version} ({fmt_duration(elapsed_now)}) [{reason}]",
                ), flush=True)
        snapshot("waiting")

        if len(published) == len(EXPECTED_IMAGES):
            snapshot("ready")
            return True

        # Short-circuit on any workflow failure — image won't appear.
        failed = [
            n for n, w in workflows.items()
            if w.get("status") == "completed" and w.get("conclusion") not in (None, "success")
        ]
        if failed:
            tprint(color(
                "build",
                f"[ghcr          ] ✖ workflow(s) failed: {', '.join(failed)} — aborting wait",
            ), flush=True)
            snapshot("failed")
            return False

        elapsed = time.monotonic() - start
        if elapsed >= timeout_s:
            missing = [i.rsplit("/", 1)[-1] for i in EXPECTED_IMAGES if i not in published]
            tprint(color(
                "build",
                f"[ghcr          ] ✖ timed out after {fmt_duration(elapsed)}; "
                f"still missing: {', '.join(missing)}",
            ), flush=True)
            tprint(color(
                "build",
                "[ghcr          ]   hint: did you `git push` to develop? The publish-*.yml "
                "workflows only fire on develop pushes that change ha-addon/VERSION.",
            ), flush=True)
            snapshot("failed")
            return False

        # Status line every 30s so the terminal doesn't look frozen.
        # Include per-workflow status so the terminal mirrors what the
        # --web UI shows.
        if elapsed - last_progress >= 30:
            parts = []
            for n in short_names:
                if any(n in img for img in published):
                    parts.append(f"{n}:✔")
                else:
                    w = workflows.get(n, {})
                    status = w.get("status") or "pending"
                    concl = w.get("conclusion")
                    label = f"{status}" if not concl else f"{status}/{concl}"
                    parts.append(f"{n}:{label}")
            tprint(color(
                "build",
                f"[ghcr          ] ⧗ {'  '.join(parts)} ({fmt_duration(elapsed)})",
            ), flush=True)
            last_progress = elapsed

        await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Background: poll each target's /ui/api/server-info for the currently
# running addon_version. Written to <target>/version.json; the --web UI
# merges it into the target row so you can watch the version roll over
# mid-deploy. Uses the per-target token_cache (same file Playwright
# reads) — on hit-direct-port requests the ha_auth_middleware still
# enforces Bearer, so there's no "unauth" path here.
# ---------------------------------------------------------------------------

VERSION_POLL_INTERVAL_S = 5.0
VERSION_POLL_TIMEOUT_S = 3.0


def _fetch_addon_version(base_url: str, token: str) -> str | None:
    """Blocking /ui/api/server-info fetch. Returns addon_version or None."""
    req = urllib.request.Request(f"{base_url}/ui/api/server-info")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=VERSION_POLL_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
    v = data.get("addon_version") if isinstance(data, dict) else None
    return v if isinstance(v, str) and v else None


async def poll_target_version(target: Target) -> None:
    """Forever-loop background poller. Cancelled by the caller at teardown."""
    vf = LOG_ROOT / target.name / "version.json"
    vf.parent.mkdir(parents=True, exist_ok=True)
    while True:
        token = ""
        if target.token_cache.exists():
            try:
                token = target.token_cache.read_text().strip()
            except OSError:
                pass
        version = await asyncio.to_thread(
            _fetch_addon_version, target.base_url, token,
        )
        _write_state(vf, {
            "current_version": version,
            "checked_at": time.time(),
        })
        await asyncio.sleep(VERSION_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Step 2: per-target chain — deploy → read token → playwright.
# ---------------------------------------------------------------------------

async def run_target_chain(target: Target, sem: asyncio.Semaphore | None) -> TargetResult:
    result = TargetResult(target=target.name)
    target_dir = LOG_ROOT / target.name
    target_dir.mkdir(parents=True, exist_ok=True)
    result.report_dir = target_dir / "playwright-report"
    deploy_log = target_dir / "deploy.log"
    test_log = target_dir / "playwright.log"
    state_file = target_dir / "state.json"

    target_start = time.monotonic()
    result.started_at_wall = time.time()

    def snapshot(**overrides: Any) -> None:
        """Persist current target state for the --web UI."""
        _write_state(state_file, {
            "name": target.name,
            "url": target.get_open_url(),
            "started_at": target_start,
            # Wall-clock seconds-since-epoch so the --web UI can render
            # a local-time "Started HH:MM:SS" column.
            "started_at_wall": result.started_at_wall,
            "deploy_ok": result.deploy_ok,
            "deploy_elapsed": result.deploy_elapsed or None,
            "tests_passed": result.tests_passed,
            "tests_total": result.tests_total,
            "tests_ok": result.tests_ok,
            "total_elapsed": time.monotonic() - target_start,
            "error": result.error,
            **overrides,
        })

    # -- Deploy ------------------------------------------------------------
    snapshot(phase="deploying")
    deploy_start = time.monotonic()
    code = await run_streaming(
        target.deploy_cmd,
        prefix=target.name,
        log_path=deploy_log,
        env=target.deploy_env,
        cwd=REPO_ROOT,
    )
    result.deploy_elapsed = time.monotonic() - deploy_start
    if code != 0:
        result.error = f"deploy exited {code} (see {deploy_log})"
        result.total_elapsed = time.monotonic() - target_start
        snapshot(phase="deploy-failed")
        return result
    result.deploy_ok = True
    snapshot(phase="testing")

    # -- Resolve the add-on token the deploy script just cached ------------
    token = ""
    if target.token_cache.exists():
        token = target.token_cache.read_text().strip()
    if not token:
        # Warn but don't abort — the suite can still exercise unauthed
        # paths. Any Bearer-gated test will surface its own failure.
        tprint(color(
            target.name,
            f"[{target.name:<14}] ⚠ no token at {target.token_cache} — Bearer-gated tests will 401",
        ), flush=True)

    # -- Playwright --------------------------------------------------------
    npm_env = {
        "FLEET_URL": target.base_url,
        "FLEET_TOKEN": token,
        # Per-target report dir so parallel runs don't collide.
        "PLAYWRIGHT_HTML_REPORT": str(result.report_dir),
        # JSON reporter output path for post-run collation.
        "PLAYWRIGHT_JSON_OUTPUT_NAME": str(target_dir / "results.json"),
        # Per-target artifact dir too: the shared default ``test-results/``
        # let parallel runs delete each other's traces mid-write, failing
        # passing tests with ENOENT at context teardown.
        "PLAYWRIGHT_OUTPUT_DIR": str(target_dir / "test-results"),
    }
    npm_env.update(target.playwright_env)

    # npm run test:e2e:hass-4 -- <extra args>. The config auto-adds the
    # JSON reporter when PLAYWRIGHT_JSON_OUTPUT_NAME is set (which it is,
    # below), so we don't need a --reporter override.
    npm_cmd = ["npm", "run", "test:e2e:hass-4", "--"]
    npm_cmd += target.playwright_args

    if sem is None:
        code = await run_streaming(
            npm_cmd,
            prefix=target.name,
            log_path=test_log,
            env=npm_env,
            cwd=REPO_ROOT / "ha-addon" / "ui",
        )
    else:
        async with sem:
            code = await run_streaming(
                npm_cmd,
                prefix=target.name,
                log_path=test_log,
                env=npm_env,
                cwd=REPO_ROOT / "ha-addon" / "ui",
            )

    # -- Parse Playwright JSON --------------------------------------------
    results_json = target_dir / "results.json"
    if results_json.exists():
        try:
            data = json.loads(results_json.read_text())
            stats = data.get("stats", {})
            # Playwright's JSON reporter surfaces expected/unexpected counts.
            # tests_total = expected + unexpected + flaky + skipped.
            expected = stats.get("expected", 0)
            unexpected = stats.get("unexpected", 0)
            flaky = stats.get("flaky", 0)
            skipped = stats.get("skipped", 0)
            result.tests_passed = expected + flaky  # flaky still count as passed
            result.tests_total = expected + unexpected + flaky + skipped
            result.tests_ok = (code == 0)
        except (json.JSONDecodeError, OSError) as e:
            result.error = f"couldn't parse playwright results.json: {e}"
            result.tests_ok = False
    else:
        result.tests_ok = (code == 0)
        if not result.tests_ok:
            result.error = f"playwright exited {code} with no results.json (see {test_log})"

    result.total_elapsed = time.monotonic() - target_start
    snapshot(phase="done" if (result.deploy_ok and result.tests_ok) else "failed")
    return result


# ---------------------------------------------------------------------------
# Summary rendering.
# ---------------------------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def print_summary(results: list[TargetResult], targets: dict[str, Target]) -> None:
    print()
    print(f"{BOLD}===== Test matrix summary ====={RESET}" if sys.stdout.isatty() else "===== Test matrix summary =====")
    print()

    # Compute column widths.
    rows = []
    for r in results:
        if not r.deploy_ok:
            deploy_cell = "✖"
            tests_cell = "—"
        else:
            deploy_cell = f"✔ {fmt_duration(r.deploy_elapsed)}"
            if r.tests_ok and r.tests_total > 0:
                tests_cell = f"✔ {r.tests_passed}/{r.tests_total}"
            elif r.tests_total > 0:
                tests_cell = f"✖ {r.tests_passed}/{r.tests_total}"
            else:
                tests_cell = "✖"
        started_cell = (
            datetime.fromtimestamp(r.started_at_wall).strftime("%H:%M:%S")
            if r.started_at_wall else "—"
        )
        rows.append((
            r.target,
            started_cell,
            deploy_cell,
            tests_cell,
            fmt_duration(r.total_elapsed),
            str(r.report_dir.relative_to(REPO_ROOT)) if r.report_dir else "",
        ))

    headers = ("Target", "Started", "Deploy", "Tests", "Elapsed", "Report")
    widths = [max(len(str(row[i])) for row in (rows + [headers])) for i in range(len(headers))]

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    print(line(headers))
    print(line(tuple("─" * w for w in widths)))
    for row in rows:
        print(line(row))

    print()
    print("Open:")
    for r in results:
        url = targets[r.target].get_open_url()
        status = "✔" if (r.deploy_ok and r.tests_ok) else "✖"
        print(f"  {status} {r.target:<14}  {url}")

    # Per-target error lines for anything that failed. One line each so
    # the summary stays compact; the full log path is right there.
    errors = [r for r in results if r.error]
    if errors:
        print()
        print(f"{BOLD}Failures:{RESET}" if sys.stdout.isatty() else "Failures:")
        for r in errors:
            print(f"  {r.target}: {r.error}")

    print()


# ---------------------------------------------------------------------------
# --web HTTP server: serves a live status page + JSON feed the page polls.
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _read_target_states() -> list[dict[str, Any]]:
    if not LOG_ROOT.exists():
        return []
    states: list[dict[str, Any]] = []
    for d in sorted(LOG_ROOT.iterdir()):
        if not d.is_dir() or d.name == "build":
            continue
        sf = d / "state.json"
        if not sf.exists():
            continue
        try:
            state = json.loads(sf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        vf = d / "version.json"
        if vf.exists():
            try:
                vdata = json.loads(vf.read_text())
            except (OSError, json.JSONDecodeError):
                vdata = {}
            state["current_version"] = vdata.get("current_version")
            state["version_checked_at"] = vdata.get("checked_at")
        states.append(state)
    return states


def _read_build_state() -> dict[str, Any]:
    sf = LOG_ROOT / "build-state.json"
    if not sf.exists():
        return {}
    try:
        return json.loads(sf.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


_HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>test-matrix</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; background: #0f0f11; color: #e5e5e5; }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 24px; }
  h1 { font: 600 16px/1 inherit; margin: 0 0 6px; letter-spacing: 0.02em; }
  .sub { color: #888; font-size: 12px; margin-bottom: 20px; }
  .sub .v { color: #aaa; }
  .sub .sep { color: #444; margin: 0 8px; }
  .card { background: #18181b; border: 1px solid #27272a; border-radius: 6px;
          padding: 14px 16px; margin-bottom: 16px; }
  .card h2 { font: 600 11px/1 inherit; color: #888; text-transform: uppercase;
             letter-spacing: 0.08em; margin: 0 0 10px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid #27272a; }
  tr:last-child td { border-bottom: 0; }
  th { color: #888; font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
  td.name { font-weight: 500; color: #e5e5e5; }
  td.url a { color: #60a5fa; text-decoration: none; }
  td.url a:hover { text-decoration: underline; }
  .phase { display: inline-block; padding: 2px 8px; border-radius: 10px;
           font-size: 11px; letter-spacing: 0.02em; }
  .phase-pending       { color: #a1a1aa; background: #27272a; }
  .phase-deploying     { color: #fde68a; background: #3f2d00; }
  .phase-testing       { color: #93c5fd; background: #1e3a5f; }
  .phase-waiting       { color: #fde68a; background: #3f2d00; }
  .phase-ready         { color: #86efac; background: #14532d; }
  .phase-done          { color: #86efac; background: #14532d; }
  .phase-failed        { color: #fca5a5; background: #5c1a1a; }
  .phase-deploy-failed { color: #fca5a5; background: #5c1a1a; }
  .ok   { color: #86efac; }
  .fail { color: #fca5a5; }
  .dim  { color: #666; }
  .progress { height: 4px; background: #27272a; border-radius: 2px; overflow: hidden; }
  .progress .fill { height: 100%; background: #60a5fa; transition: width 0.3s; }
  pre#output { background: #050506; padding: 14px; border-radius: 6px; border: 1px solid #27272a;
               max-height: 50vh; overflow: auto; white-space: pre-wrap; word-break: break-all;
               font: 12px/1.45 "SF Mono", Menlo, Consolas, monospace; margin: 0; }
  .tabs { display: flex; gap: 2px; margin-bottom: 10px; border-bottom: 1px solid #27272a; }
  .tab { background: transparent; border: none; color: #888; padding: 7px 14px;
         font: 500 12px/1 inherit; letter-spacing: 0.02em; cursor: pointer;
         border-bottom: 2px solid transparent; margin-bottom: -1px; border-radius: 4px 4px 0 0; }
  .tab:hover { color: #e5e5e5; background: #1a1a1e; }
  .tab.active { color: #e5e5e5; border-bottom-color: #60a5fa; background: #1a1a1e; }
  .tab .badge { display: inline-block; margin-left: 6px; font-size: 10px;
                color: #666; font-weight: 400; }
  .tab.active .badge { color: #aaa; }
  .disconnected { color: #fca5a5; margin-left: 6px; }
  .disconnected.ok { color: #666; }
</style>
</head>
<body>
<div class="wrap">
  <h1>test-matrix</h1>
  <div class="sub" id="sub">loading&hellip;</div>

  <div class="card">
    <h2>Build</h2>
    <div id="build-card">—</div>
  </div>

  <div class="card">
    <h2>Targets</h2>
    <table>
      <thead><tr><th>Target</th><th>Started</th><th>Phase</th><th>Version</th><th>Deploy</th><th>Tests</th><th>Elapsed</th><th>URL</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <div class="card">
    <h2>Output <span id="conn" class="disconnected ok">live</span></h2>
    <div class="tabs" id="tabs"></div>
    <pre id="output"></pre>
  </div>
</div>
<script>
  const fmtDur = (s) => {
    if (s == null) return '';
    if (s < 60) return Math.round(s) + 's';
    const m = Math.floor(s / 60), r = Math.round(s - m * 60);
    return m + 'm ' + String(r).padStart(2, '0') + 's';
  };

  // Output pane tab filtering. Lines in the buffer are formatted
  // `HH:MM:SS [<prefix>...] content` by run_streaming / tprint — the
  // matrix pads prefixes to 14 chars. We match by prefix substring so
  // dup padding/unpadding still works.
  let currentTab = 'all';
  let lastTargets = [];
  const MATRIX_PREFIXES = { build: ['build', 'ghcr'] };
  // Strip "[prefix...] " (between timestamp and content) when rendering
  // a tab other than All — redundant once you've selected the target.
  const PREFIX_STRIP_RE = /^(\d{1,2}:\d{2}:\d{2})\s+\[[^\]]+\]\s?/;

  function renderTabs(targets) {
    const names = targets.map(t => t.name);
    // Only re-render when the set changed — avoids losing hover state.
    if (JSON.stringify(names) === JSON.stringify(lastTargets)) return;
    lastTargets = names;
    const tabs = [
      { id: 'all',   label: 'All' },
      { id: 'build', label: 'Build' },
      ...names.map(n => ({ id: n, label: n })),
    ];
    const el = document.getElementById('tabs');
    el.innerHTML = tabs.map(t =>
      '<button class="tab' + (currentTab === t.id ? ' active' : '') +
      '" data-tab="' + t.id + '">' + t.label + '</button>'
    ).join('');
    el.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        currentTab = btn.dataset.tab;
        el.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
        renderOutput(window.__lastLines || []);
      });
    });
  }

  function lineMatchesTab(line, tab) {
    if (tab === 'all') return true;
    // Skip the leading HH:MM:SS timestamp and anchor on "[prefix".
    const m = line.match(/^\d{1,2}:\d{2}:\d{2}\s+\[([^\s\]]+)/);
    if (!m) {
      // Lines without a [prefix] are top-level orchestrator status
      // ("==> Web UI:", "==> All targets done") emitted via tprint().
      // Surface them in the Build tab so it's a complete view of the
      // build/orchestration phase, not just the ghcr poll lines.
      return tab === 'build' && /^\d{1,2}:\d{2}:\d{2}\s+==>/.test(line);
    }
    const prefix = m[1];
    if (tab === 'build') return MATRIX_PREFIXES.build.includes(prefix);
    return prefix === tab;
  }

  function renderOutput(lines) {
    window.__lastLines = lines;
    let display = lines.filter(l => lineMatchesTab(l, currentTab));
    if (currentTab !== 'all') {
      display = display.map(l => l.replace(PREFIX_STRIP_RE, '$1 '));
    }
    const out = document.getElementById('output');
    const pinned = out.scrollHeight - out.scrollTop - out.clientHeight < 60;
    out.textContent = display.join('\\n');
    if (pinned) out.scrollTop = out.scrollHeight;
  }
  const phaseLabel = (p) => p || 'pending';
  const deployCell = (t) => {
    if (t.deploy_ok === true)  return '<span class="ok">&#x2714; ' + fmtDur(t.deploy_elapsed) + '</span>';
    if (t.deploy_ok === false) return '<span class="fail">&#x2716;</span>';
    return '<span class="dim">&mdash;</span>';
  };
  const testsCell = (t) => {
    if (t.tests_total != null && t.tests_total > 0) {
      const cls = t.tests_ok === true ? 'ok' : (t.tests_ok === false ? 'fail' : '');
      const mark = t.tests_ok === true ? '&#x2714;' : (t.tests_ok === false ? '&#x2716;' : '');
      return '<span class="' + cls + '">' + mark + ' ' + t.tests_passed + '/' + t.tests_total + '</span>';
    }
    return '<span class="dim">&mdash;</span>';
  };
  const versionCell = (t, deploying) => {
    const v = t.current_version;
    if (!v) return '<span class="dim">&mdash;</span>';
    // Green when the running addon_version matches the VERSION this
    // matrix run is deploying; dim otherwise (pre-deploy or lagging).
    if (deploying && v === deploying) return '<span class="ok">' + v + '</span>';
    return '<span class="dim">' + v + '</span>';
  };

  async function tick() {
    try {
      const r = await fetch('/status.json', { cache: 'no-store' });
      const s = await r.json();
      document.getElementById('conn').classList.add('ok');
      document.getElementById('conn').textContent = 'live';

      const b = s.build || {};
      const subParts = [];
      if (b.version) subParts.push('<span class="v">' + b.version + '</span>');
      subParts.push((s.targets || []).length + ' targets');
      if (b.phase) subParts.push(b.phase);
      document.getElementById('sub').innerHTML = subParts.join('<span class="sep">&middot;</span>');

      let buildHtml;
      if (!b.phase) {
        buildHtml = '<span class="dim">pending&hellip;</span>';
      } else {
        const pct = b.images_total ? Math.round(100 * (b.images_published || []).length / b.images_total) : 0;
        buildHtml =
          '<div style="display:flex;align-items:center;gap:12px;">' +
          '<span class="phase phase-' + b.phase + '">' + b.phase + '</span>' +
          '<div style="flex:1;"><div class="progress"><div class="fill" style="width:' + pct + '%"></div></div></div>' +
          '<span class="dim">' + ((b.images_published || []).length) + '/' + (b.images_total || '?') + '</span>' +
          '<span class="dim">' + fmtDur(b.elapsed) + '</span>' +
          '</div>';
        if (b.head_sha) {
          buildHtml += '<div style="margin-top:6px;font-size:11px;color:#666;">commit ' + b.head_sha + '</div>';
        }
        // Per-workflow status table. The matrix polls the GitHub Actions
        // API for each publish workflow keyed on HEAD SHA; displayed
        // alongside the GHCR tag check so it's obvious when CI is
        // running vs when it simply hasn't started.
        const wf = b.workflows || {};
        const wfRows = Object.keys(wf).map(name => {
          const w = wf[name] || {};
          const status = w.status || 'pending';
          const concl = w.conclusion;
          const pubbed = (b.images_published || []).includes(name);
          // Derive a phase chip: published → done; otherwise map workflow states.
          let chipPhase = 'pending';
          if (pubbed || concl === 'success') chipPhase = 'done';
          else if (concl && concl !== 'success') chipPhase = 'failed';
          else if (status === 'in_progress') chipPhase = 'testing';
          else if (status === 'queued') chipPhase = 'waiting';
          else if (status === 'not_started') chipPhase = 'pending';
          const label = concl ? status + ' / ' + concl : status;
          const link = w.url ? ' <a href="' + w.url + '" target="_blank" style="font-size:11px;">run&#x2197;</a>' : '';
          const pubMark = pubbed ? ' <span class="ok">&#x2714; on ghcr</span>' : '';
          return '<tr>' +
            '<td class="name" style="font-size:12px;">' + name + '</td>' +
            '<td><span class="phase phase-' + chipPhase + '">' + label + '</span></td>' +
            '<td style="font-size:11px; color:#888;">' + link + pubMark + '</td>' +
          '</tr>';
        }).join('');
        if (wfRows) {
          buildHtml += '<table style="margin-top:10px;font-size:12px;">' +
            '<thead><tr><th>Image</th><th>Workflow</th><th></th></tr></thead>' +
            '<tbody>' + wfRows + '</tbody></table>';
        }
      }
      document.getElementById('build-card').innerHTML = buildHtml;

      const rows = (s.targets || []).map(t => {
        const phase = phaseLabel(t.phase);
        // Started column: epoch seconds → local HH:MM:SS. Dim when the
        // field isn't set yet (pre-chain targets show "pending").
        const startedCell = t.started_at_wall
          ? new Date(t.started_at_wall * 1000).toLocaleTimeString('en-GB', { hour12: false })
          : '<span class="dim">&mdash;</span>';
        return '<tr>' +
          '<td class="name">' + t.name + '</td>' +
          '<td>' + startedCell + '</td>' +
          '<td><span class="phase phase-' + phase + '">' + phase + '</span></td>' +
          '<td>' + versionCell(t, b.version) + '</td>' +
          '<td>' + deployCell(t) + '</td>' +
          '<td>' + testsCell(t) + '</td>' +
          '<td>' + fmtDur(t.total_elapsed) + '</td>' +
          '<td class="url">' + (t.url ? '<a href="' + t.url + '" target="_blank">' + t.url + '</a>' : '') + '</td>' +
        '</tr>';
      }).join('');
      document.getElementById('rows').innerHTML = rows || '<tr><td colspan="8" class="dim">no targets yet</td></tr>';

      renderTabs(s.targets || []);
      renderOutput(s.output || []);
    } catch (e) {
      document.getElementById('conn').classList.remove('ok');
      document.getElementById('conn').textContent = 'disconnected';
    }
  }
  setInterval(tick, 1500);
  tick();
</script>
</body>
</html>
"""


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server convention)
        if self.path == "/" or self.path == "/index.html":
            body = _HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status.json":
            with _output_lock:
                lines = [_strip_ansi(line) for line in _output_lines]
            payload = {
                "build": _read_build_state(),
                "targets": _read_target_states(),
                "output": lines,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress default BaseHTTPRequestHandler access logging; the matrix
        # already has plenty of output.
        return


_DASHBOARD_TERMINAL_PHASES = {"done", "failed", "deploy-failed"}
_DASHBOARD_ACTIVE_PHASES = {"pending", "deploying", "testing"}


def _find_listener_pid(port: int) -> int | None:
    """Return the PID listening on 127.0.0.1:<port>, or None if lsof can't tell."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                return int(line[1:])
            except ValueError:
                pass
    return None


def _try_bind(port: int) -> bool:
    """True if the script can bind 127.0.0.1:<port> right now.

    Sets ``SO_REUSEADDR`` to mirror ``http.server.ThreadingHTTPServer``'s
    default ``allow_reuse_address = True`` — otherwise the probe says
    "still busy" for ~30 s after the prior dashboard exits while the
    kernel holds the socket in TIME_WAIT, even though the real bind in
    ``start_web_server`` would succeed immediately.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _ensure_port_available(port: int) -> None:
    """Reclaim ``port`` from a stale test-matrix dashboard if there is one.

    The ``--web`` mode keeps the HTTP server up after the matrix run
    finishes (``Web UI still at … (Ctrl-C to exit)``) so a developer can
    revisit the final state. That convenience is also the source of the
    every-third-day "OSError: Address already in use" papercut: a window
    closed without Ctrl-C leaves PID N idling on 8099, and the next run
    dies before reaching the matrix code at all.

    Behaviour, in order:
      1. Port free → return.
      2. Port held but the listener doesn't speak our /status.json shape
         → exit non-zero (don't kill an unknown process).
      3. Port held by a finished test-matrix dashboard (every target's
         ``phase`` is terminal, or no targets are recorded yet/anymore)
         → SIGTERM that PID, wait up to 5 s for the port to free, retry.
      4. Port held by a *running* test-matrix run (any target in
         ``pending`` / ``deploying`` / ``testing``) → exit non-zero with
         the offending target list and a pointer to the live dashboard.
         Killing an in-progress run would corrupt half-deployed targets;
         the right move is for the user to wait or stop it themselves.
    """
    if _try_bind(port):
        return

    # Port busy — see whether it's our dashboard speaking.
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status.json", timeout=2) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — broad on purpose; any failure → bail
        sys.stderr.write(
            f"Port {port} is in use, and the listener doesn't respond like a "
            f"test-matrix dashboard ({type(e).__name__}: {e}).\n"
            f"Pick a different port with --web-port, or stop the conflicting process.\n"
        )
        sys.exit(2)

    if not (isinstance(payload, dict) and {"build", "targets", "output"} <= payload.keys()):
        sys.stderr.write(
            f"Port {port} is in use by something that responds to HTTP but isn't a "
            f"test-matrix dashboard. Pick a different port with --web-port, or stop "
            f"the conflicting process.\n"
        )
        sys.exit(2)

    targets = payload.get("targets") or []
    in_flight = [t for t in targets if t.get("phase") in _DASHBOARD_ACTIVE_PHASES]
    if in_flight:
        names = ", ".join(f"{t.get('name', '?')} ({t.get('phase')})" for t in in_flight)
        sys.stderr.write(
            f"Another test-matrix run is still active on port {port}: {names}.\n"
            f"Open http://127.0.0.1:{port}/ to watch it, or wait for it to finish "
            f"before retrying. Pass --web-port to run a second matrix in parallel.\n"
        )
        sys.exit(2)

    # All recorded targets are terminal (or there are none — same shape the
    # idle dashboard shows after `_clear_state_dir` ran on the prior cycle).
    pid = _find_listener_pid(port)
    if pid is None:
        sys.stderr.write(
            f"Port {port} is held by a finished test-matrix dashboard but lsof "
            f"couldn't identify the PID. Stop it manually or pick a different "
            f"--web-port.\n"
        )
        sys.exit(2)

    tprint(
        color("build", f"==> Port {port} held by stale test-matrix dashboard (PID {pid}); terminating..."),
        flush=True,
    )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # Already gone; the port should free shortly.
    except OSError as e:
        sys.stderr.write(f"Could not signal PID {pid}: {e}\n")
        sys.exit(2)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _try_bind(port):
            return
        time.sleep(0.2)

    sys.stderr.write(
        f"Port {port} did not free within 5 s after SIGTERM to PID {pid}. "
        f"Try again or pick another --web-port.\n"
    )
    sys.exit(2)


def start_web_server(port: int) -> None:
    """Start the status server on a background daemon thread."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tprint(color("build", f"==> Web UI: http://localhost:{port}/"), flush=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    version = VERSION_FILE.read_text().strip()
    all_targets = make_targets(version)

    if args.list:
        print("Available targets:")
        for name, t in all_targets.items():
            print(f"  {name:<16} {t.get_open_url()}")
        return 0

    if args.targets:
        names = [n.strip() for n in args.targets.split(",")]
        unknown = [n for n in names if n not in all_targets]
        if unknown:
            sys.stderr.write(f"Unknown target(s): {', '.join(unknown)}\n")
            sys.stderr.write(f"Available: {', '.join(all_targets)}\n")
            return 2
    else:
        names = list(all_targets)

    selected = {n: all_targets[n] for n in names}

    tprint(color("build", f"==> test-matrix.py v{version}  targets: {', '.join(names)}"), flush=True)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)

    # Pre-populate per-target state.json as "pending" so the --web UI
    # shows all target rows up front while the build wait is in flight,
    # instead of an empty targets table until the first deploy starts.
    for name, target in selected.items():
        target_dir = LOG_ROOT / name
        target_dir.mkdir(parents=True, exist_ok=True)
        _write_state(target_dir / "state.json", {
            "name": name,
            "url": target.get_open_url(),
            "phase": "pending",
        })

    # Start background version pollers before the GHCR wait so the web
    # UI can show each target's currently-running addon_version right
    # away (and the user can watch it roll over once deploys land).
    version_pollers = [
        asyncio.create_task(poll_target_version(t))
        for t in selected.values()
    ]

    try:
        # -- Wait for CI-published images ----------------------------------
        if not args.no_wait:
            ok = await wait_for_ghcr_tags(version, timeout_s=args.wait_timeout)
            if not ok:
                sys.stderr.write(
                    "GHCR tags not available — skipping all target deploys.\n"
                )
                return 1

        # -- Run target chains ----------------------------------------------
        # Deploy always parallel. Playwright parallel by default; --seq-tests
        # serializes Playwright to one target at a time (escape hatch for
        # memory pressure). Implemented via a shared semaphore that only the
        # Playwright step inside each chain acquires.
        test_sem = asyncio.Semaphore(1) if args.seq_tests else None

        start = time.monotonic()
        results = await asyncio.gather(
            *[run_target_chain(t, test_sem) for t in selected.values()],
        )
        tprint(color("build", f"==> All targets done in {fmt_duration(time.monotonic() - start)}"), flush=True)

        # -- Summary --------------------------------------------------------
        print_summary(results, selected)

        # Exit code = non-zero if ANY target failed.
        return 0 if all(r.deploy_ok and r.tests_ok for r in results) else 1
    finally:
        for p in version_pollers:
            p.cancel()
        # Drain cancellations so we don't leave "Task was destroyed but
        # it is pending" warnings on the console at interpreter shutdown.
        await asyncio.gather(*version_pollers, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See dev-plans/HOME-LAB.md for target infrastructure.",
    )
    parser.add_argument(
        "--targets",
        help="Comma-separated target names (default: all). See --list.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Skip the GHCR-tag wait and go straight to deploy. Useful when "
             "you know the CI publish workflows have already finished.",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=600,
        help="Seconds to wait for GHCR tags to appear (default: 600 = 10min).",
    )
    parser.add_argument(
        "--seq-tests",
        action="store_true",
        help="Run Playwright one target at a time (deploys still parallel).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available targets and exit.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch a local HTTP server that streams live progress to "
             "a browser (see --web-port). Server stays up after the run "
             "finishes so the final state remains viewable until Ctrl-C.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8099,
        help="Port for --web (default 8099).",
    )
    args = parser.parse_args()

    preflight(skip_wait=args.no_wait or args.list)

    # Tee stdout into the ring buffer so the --web server can replay it.
    # Safe to do unconditionally — cost is one deque append per line.
    sys.stdout = _TeeStdout(sys.stdout)  # type: ignore[assignment]

    if args.web and not args.list:
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        # Reclaim the port from a stale dashboard before clearing state —
        # the probe needs the prior run's targets[] to tell active from
        # finished, and `_clear_state_dir` would erase that signal first.
        _ensure_port_available(args.web_port)
        _clear_state_dir()
        start_web_server(args.web_port)

    try:
        rc = asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130

    if args.web and not args.list:
        tprint(color("build", f"==> Web UI still at http://localhost:{args.web_port}/  (Ctrl-C to exit)"), flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
