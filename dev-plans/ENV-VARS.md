# Environment variables — server and client

Inventory of every environment variable the Fleet for ESPHome server (`ha-addon/server/`) and worker (`ha-addon/client/`) read at runtime. Dev-loop scripts and the HA custom integration are out of scope — the integration reads none; dev scripts are orchestration glue, not app config.

Generated 2026-04-23 from `grep -rnE 'os\.environ|os\.getenv' ha-addon/{server,client}/`.

## Server (`ha-addon/server/`)

| Name | Default | Purpose | Primary read |
|---|---|---|---|
| `PORT` | `8765` | HTTP listen port. Defensive int-parse (PR #64); malformed value logs + falls back. | `app_config.py:56` |
| `ESPHOME_CONFIG_DIR` | `/config/esphome` | Directory scanned for ESPHome YAML targets. | `app_config.py:69` |
| `SUPERVISOR_TOKEN` | — | Bearer for Supervisor API; presence also gates "add-on vs standalone" detection (`helpers.is_standalone()`). Read in 8 places; `SUPERVISOR_TOKEN` is the canonical name. | `helpers.py:32` (canonical), also `ha_auth.py`, `supervisor_discovery.py`, `settings.py`, `ui_api.py`, `main.py` |
| `HA_MODE` | auto-detect | Explicit override of add-on/standalone detection. Accepts `addon` or `standalone`; anything else is ignored and we fall back to the `SUPERVISOR_TOKEN` sniff. | `helpers.py:29` |
| `TZ` | `/etc/timezone` content | IANA timezone name attached to job assignments so workers log in the operator's zone. | `api.py:404` |

## Worker (`ha-addon/client/`)

| Name | Default | Purpose | Primary read |
|---|---|---|---|
| `SERVER_URL` | **required** | Base URL of the Fleet server, e.g. `http://homeassistant.local:8765`. Worker won't start without it. | `client.py:104` |
| `SERVER_TOKEN` | **required** | Bearer token to auth against the server. | `client.py:105` |
| `POLL_INTERVAL` | `1` | Seconds between job-queue polls when idle. | `client.py:106` |
| `HEARTBEAT_INTERVAL` | `10` | Seconds between heartbeats to server. | `client.py:107` |
| `JOB_TIMEOUT` | `600` | Compile timeout (10 min). | `client.py:108` |
| `OTA_TIMEOUT` | `120` | OTA upload timeout (2 min). | `client.py:109` |
| `MAX_ESPHOME_VERSIONS` | `3` | LRU cap on cached ESPHome venvs under `/esphome-versions/`. Read in two places — `client.py:110` and `version_manager.py:17` — both default to 3. | `client.py:110` |
| `MAX_PARALLEL_JOBS` | `2` | Concurrent build slots (0 = paused). Also written by the server-spawned local worker to match the UI-configured value. | `client.py:111` (also written at `client.py:406`) |
| `HOSTNAME` | `socket.gethostname()` | Worker name shown in the UI. Without this, Docker containers report their random container ID. | `client.py:112` |
| `PLATFORM` | `sys.platform` | Reported platform string; display only. | `client.py:113` |
| `ESPHOME_BIN` | unset | If set, worker skips the version-manager venvs and shells out to this binary for every compile. | `client.py:114` |
| `ESPHOME_SEED_VERSION` | unset | Pre-install this ESPHome version on worker startup so first-job latency is lower. | `client.py:115` |
| `ESPHOME_VERSIONS_DIR` | `/esphome-versions` | Parent directory for version-manager venvs. Read in two places (`client.py:117` and `version_manager.py:16`). | `client.py:117` |
| `MIN_FREE_DISK_PCT` | `10` | Refuse to install a new ESPHome venv if free disk % is below this. | `version_manager.py:19` |
| `ESPHOME_INSTALL_TIMEOUT` | `300` | Seconds allowed per `pip install esphome==X` attempt (two attempts). Raise it on slow single-board hosts — #193 measured ~14 min for a full install on a Zimaboard, where the 300 s ceiling meant the version could never install at all. Clamped to a 60 s floor; a non-integer value warns and falls back to 300. | `version_manager.py` (`_install_timeout_from_env`) |
| `HOST_PLATFORM` | auto-detect (`uname`) | Override the OS string displayed in the Workers tab (e.g. `macOS 15.3 (Apple M1 Pro)` when running the worker binary outside a Linux container). | `sysinfo.py:182` |
| `DISTRIBUTED_ESPHOME_CLIENT_ID` | auto-generated + persisted | Persistent worker identity (UUID). Generated on first boot, stored in a state file, then re-exported into the environment for subprocesses. Not typically user-set. | `client.py:232` (written at `client.py:477`) |

## Notes / observed inconsistencies

- **`HOSTNAME` vs `WORKER_HOSTNAME`**: code reads `HOSTNAME`; historical docker-compose snippets elsewhere sometimes show `WORKER_HOSTNAME`. Only `HOSTNAME` is wired.
- **Duplicate reads**: `MAX_ESPHOME_VERSIONS` and `ESPHOME_VERSIONS_DIR` each read in both `client.py` and `version_manager.py`. Defaults match; no divergence today.
- **`POLL_INTERVAL` default drift**: code is `1`, CLAUDE.md's worker table says `5`. CLAUDE.md is stale.
- **Int-parse defensiveness**: `PORT` and `ESPHOME_INSTALL_TIMEOUT` guard against malformed values. Every other `int(os.environ.get(...))` on the worker will raise `ValueError` at import time on a typo.
- **Undocumented internals**: `TZ`, `PLATFORM`, `MIN_FREE_DISK_PCT`, `DISTRIBUTED_ESPHOME_CLIENT_ID` are not user-facing; they're not listed in CLAUDE.md's worker env table or `ha-addon/DOCS.md`. `ESPHOME_INSTALL_TIMEOUT` **is** user-facing (#193) and is listed in both.
