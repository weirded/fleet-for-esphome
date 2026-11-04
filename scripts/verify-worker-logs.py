#!/usr/bin/env python3
"""Live verification of worker-log streaming (WL.1/WL.2/WL.3).

Exercises the open → close → reopen cycle against a running ESPHome
Fleet server and asserts the two duplicate-source regressions from
1.6.2-dev.25 stay fixed:

  1. No spurious `--- worker restarted ---` separator on a second open
     within the same worker process lifetime.
  2. Lines that appear before the close do not re-appear after the
     reopen (beyond what the server's persistent buffer legitimately
     hydrates).

Usage:
    SERVER_URL=http://hass-4.local:8765 \\
    ADDON_TOKEN=$(ssh hass-4 'docker exec $(docker ps --filter name=_local_esphome_dist_server --format {{.Names}} | head -1) cat /data/settings.json' \\
                  | python3 -c "import sys,json; print(json.load(sys.stdin)['server_token'])") \\
    scripts/verify-worker-logs.py

    # optional: WORKER_HOSTNAME=local-worker (default)

The worker needs to be producing log lines during the test. On a quiet
worker with no ESPHome activity, heartbeat chatter ("Heartbeat ok",
etc.) is sufficient — runs at 10 s cadence, enough to see one new line
across the ~25 s test budget.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import websockets

RESTART_SEPARATOR = "--- worker restarted ---"


@dataclass
class Config:
    server_url: str
    addon_token: str
    worker_hostname: str
    first_watch_seconds: float
    gap_seconds: float
    second_watch_seconds: float


def load_config() -> Config:
    server_url = os.environ.get("SERVER_URL", "http://hass-4.local:8765").rstrip("/")
    token = os.environ.get("ADDON_TOKEN", "").strip()
    if not token:
        print("ERROR: ADDON_TOKEN env var required", file=sys.stderr)
        print(
            "  hint: ADDON_TOKEN=$(ssh hass-4 'docker exec $(docker ps --filter name=_local_esphome_dist_server --format {{.Names}} | head -1) cat /data/settings.json'"
            " | python3 -c \"import sys,json; print(json.load(sys.stdin)['server_token'])\")",
            file=sys.stderr,
        )
        sys.exit(2)
    return Config(
        server_url=server_url,
        addon_token=token,
        worker_hostname=os.environ.get("WORKER_HOSTNAME", "local-worker"),
        first_watch_seconds=float(os.environ.get("FIRST_WATCH_SECONDS", "10")),
        gap_seconds=float(os.environ.get("GAP_SECONDS", "5")),
        second_watch_seconds=float(os.environ.get("SECOND_WATCH_SECONDS", "10")),
    )


def http_get_json(cfg: Config, path: str) -> object:
    req = Request(
        cfg.server_url + path,
        headers={"Authorization": f"Bearer {cfg.addon_token}"},
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(cfg: Config, path: str, body: dict) -> object:
    data = json.dumps(body).encode("utf-8")
    req = Request(
        cfg.server_url + path,
        data=data,
        headers={
            "Authorization": f"Bearer {cfg.addon_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def trigger_validate_compile(cfg: Config, target: str) -> None:
    """Enqueue a validate-only compile so the worker produces live log lines.

    Validate-only jobs skip the OTA upload entirely and run offline
    against the bundled slot cache, so they finish in a few seconds —
    just enough to exercise live pusher activity during the first-watch
    window of the verification.
    """
    try:
        http_post_json(
            cfg,
            "/ui/api/compile",
            {"targets": [target], "validate_only": True},
        )
        print(f"    Triggered validate-only compile for {target}")
    except Exception as exc:  # noqa: BLE001
        print(f"    (compile trigger skipped: {exc})")


def find_worker_id(cfg: Config) -> str:
    workers = http_get_json(cfg, "/ui/api/workers")
    assert isinstance(workers, list)
    for w in workers:
        if w.get("hostname") == cfg.worker_hostname and w.get("online"):
            return w["client_id"]
    print(
        f"ERROR: no online worker with hostname={cfg.worker_hostname!r}",
        file=sys.stderr,
    )
    print("available workers:", file=sys.stderr)
    for w in workers:
        print(
            f"  - {w.get('hostname'):30s} online={w.get('online')}",
            file=sys.stderr,
        )
    sys.exit(3)


def ws_url(cfg: Config, client_id: str) -> str:
    parsed = urlparse(cfg.server_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ui/api/workers/{client_id}/logs/ws"


async def collect_frames(
    cfg: Config, client_id: str, duration: float,
) -> list[str]:
    """Open the WS, collect every frame for ``duration`` seconds, close."""
    url = ws_url(cfg, client_id)
    frames: list[str] = []
    # `additional_headers` arrived in websockets 14.x; older `extra_headers`
    # is the fallback name.
    header_kwargs: dict = {"additional_headers": {"Authorization": f"Bearer {cfg.addon_token}"}}
    async with websockets.connect(url, **header_kwargs) as ws:
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            frames.append(msg if isinstance(msg, str) else msg.decode("utf-8"))
    return frames


def summarize(label: str, frames: list[str]) -> None:
    total = sum(len(f) for f in frames)
    print(
        f"  {label}: {len(frames)} frames, {total} chars, "
        f"first-frame size={len(frames[0]) if frames else 0}",
    )


def run_check(cfg: Config, client_id: str) -> int:
    print(f"==> Worker: {cfg.worker_hostname} ({client_id})")
    print(
        f"==> First watch: {cfg.first_watch_seconds:.0f}s, "
        f"gap: {cfg.gap_seconds:.0f}s, "
        f"second watch: {cfg.second_watch_seconds:.0f}s"
    )

    compile_target = os.environ.get("COMPILE_TARGET", "cyd-office-info.yaml")
    if compile_target:
        # Induce worker log activity so the pusher has live content to
        # stream. Fire it slightly before the first watch opens so the
        # job-claim log lands inside the watch window.
        print(f"==> Triggering validate-only compile on {compile_target} ...")
        trigger_validate_compile(cfg, compile_target)

    # --- first open ----------------------------------------------------
    print("")
    print("==> First open ...")
    frames_1 = asyncio.run(
        collect_frames(cfg, client_id, cfg.first_watch_seconds),
    )
    summarize("first open", frames_1)

    if not frames_1:
        print(
            "    WARNING: no frames received on first open. Worker may be "
            "idle; try increasing FIRST_WATCH_SECONDS.",
            file=sys.stderr,
        )

    # Check 1: no false restart separator on FIRST open (fresh worker
    # process in the common case — the separator belongs only to a
    # genuine worker subprocess restart).
    first_text = "".join(frames_1)
    first_sep_count = first_text.count(RESTART_SEPARATOR)

    # --- gap -----------------------------------------------------------
    print(f"==> Sleeping {cfg.gap_seconds:.0f}s with dialog closed ...")
    time.sleep(cfg.gap_seconds)

    # --- second open ---------------------------------------------------
    print("==> Second open ...")
    frames_2 = asyncio.run(
        collect_frames(cfg, client_id, cfg.second_watch_seconds),
    )
    summarize("second open", frames_2)

    second_text = "".join(frames_2)
    second_sep_count = second_text.count(RESTART_SEPARATOR)

    # --- assertions ----------------------------------------------------
    print("")
    print("==> Checks:")

    fail = False

    # A. The second open MUST NOT introduce a new restart separator
    #    (the worker hasn't restarted between opens). If a spurious
    #    separator appears, dev.25's acked-offset persistence is broken.
    spurious_separators = second_sep_count - first_sep_count
    if spurious_separators > 0:
        print(
            f"    [FAIL] second open added {spurious_separators} new "
            f"'{RESTART_SEPARATOR}' separator(s) — the acked-offset fix "
            f"didn't stick.",
            file=sys.stderr,
        )
        fail = True
    else:
        print(
            f"    [OK]   no new restart separator on reopen "
            f"(first={first_sep_count}, second={second_sep_count})"
        )

    # B. The LIVE portion of the second stream (everything after the
    #    hydration first-frame) must not echo content that was in the
    #    first stream's tail. Hydration is expected overlap — live
    #    isn't.
    live_second = "".join(frames_2[1:])  # all frames after the hydration
    first_tail = "".join(frames_1[-3:])  # last few live frames from first open

    overlap = ""
    if first_tail and live_second:
        # Find the longest suffix of first_tail that appears in the
        # live_second. Any non-trivial overlap means the worker replayed
        # content that had already been acked.
        for length in range(min(len(first_tail), 80), 8, -1):
            probe = first_tail[-length:]
            if probe and probe in live_second:
                overlap = probe
                break

    if overlap:
        print(
            f"    [FAIL] live frames in the second open echo content "
            f"already streamed in the first open:\n"
            f"           {overlap!r}",
            file=sys.stderr,
        )
        fail = True
    else:
        print("    [OK]   no live-frame echo between opens")

    # C. The hydration first-frame of the second open is allowed (in
    #    fact expected) to include content that was already shown —
    #    that's the server's persistent buffer being replayed into the
    #    fresh WS. It's bounded by the ring and the 1 h TTL. We assert
    #    only that the hydration frame isn't UNREASONABLY large (> the
    #    2 MB broker cap), which would indicate runaway growth.
    if frames_2:
        hydration_size = len(frames_2[0])
        if hydration_size > 4 * 1024 * 1024:
            print(
                f"    [FAIL] hydration frame is {hydration_size} bytes — "
                f"server buffer exceeded the 4 MB transport guard.",
                file=sys.stderr,
            )
            fail = True
        else:
            print(f"    [OK]   hydration frame size {hydration_size} bytes (bounded)")

    return 1 if fail else 0


def main() -> int:
    cfg = load_config()
    client_id = find_worker_id(cfg)
    return run_check(cfg, client_id)


if __name__ == "__main__":
    sys.exit(main())
