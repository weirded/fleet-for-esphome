export interface ServerInfo {
  token: string;
  port: number;
  server_ip?: string;
  server_addresses?: string[];
  addon_version?: string;
  server_client_version?: string;
  min_image_version?: string;
  /** SE.8 — server-side ESPHome lazy-install lifecycle. */
  esphome_install_status?: 'installing' | 'ready' | 'failed';
  /** Version the server is trying to install / has installed. */
  esphome_server_version?: string;
  /**
   * DQ.5: fleet-wide default per-worker disk quota in bytes. Surfaced
   * here so the ConnectWorkerModal's "Use fleet default (X GiB)" radio
   * label renders without a separate /ui/api/settings call.
   */
  default_worker_disk_quota_bytes?: number;
}

/**
 * Where a device's reachable address came from (QS.27). Produced by
 * `scanner.get_device_address` + `device_poller.discover`. Surfaced
 * in the Devices tab as a small gray suffix next to the IP.
 *
 * Keep in sync with `ha-addon/server/scanner.py::get_device_address`
 * and `ha-addon/server/device_poller.py`.
 */
export type AddressSource =
  | 'mdns'
  | 'mdns_default'
  | 'wifi_use_address'
  | 'ethernet_use_address'
  | 'openthread_use_address'
  | 'wifi_static_ip'
  | 'ethernet_static_ip'
  // Bug #7 (1.6.1): MAC→IP fallback via the add-on's host ARP table.
  // Kicks in when mDNS can't resolve but we've seen the device's MAC
  // before (cached from a prior native-API poll).
  | 'arp';

export interface EsphomeVersions {
  selected: string | null;
  detected: string | null;
  available: string[];
}

export interface Target {
  target: string;
  device_name?: string;
  friendly_name?: string;
  comment?: string;
  area?: string;
  project_name?: string;
  project_version?: string;
  ip_address?: string;
  /** How the IP was resolved — see Device.address_source for the value list. */
  address_source?: AddressSource | null;
  running_version?: string;
  online?: boolean | null;
  needs_update?: boolean;
  /**
   * Fallback "config changed locally" signal used when
   * `config_drifted_since_flash` is null (no past flash, etc). In a git
   * repo this reflects `git status` (uncommitted local edits). On a
   * non-repo config dir it falls back to `yaml.mtime > device.compilation_time`.
   * Prefer the `hasDriftedConfig` helper in `components/devices/drift.ts`
   * over reading this directly.
   */
  config_modified?: boolean;
  last_seen?: string;
  compilation_time?: number;
  server_version?: string;
  has_api_key?: boolean;
  has_web_server?: boolean;
  /**
   * #14: true if the resolved YAML has a ``button: - platform: restart``
   * entry. Used to gray out the Restart menu item when no such button
   * exists, instead of letting the click fail.
   */
  has_restart_button?: boolean;
  ha_configured?: boolean;
  ha_connected?: boolean | null;
  /**
   * HA device registry ID — present when we matched the device to HA by MAC.
   * Used by the UI to deep-link the HA column to /config/devices/device/<id>.
   * (#35)
   */
  ha_device_id?: string | null;
  /**
   * Primary network connectivity block (#10). Mirrors ESPHome's own
   * resolver precedence: wifi → ethernet → openthread. Null when none of
   * the three blocks is present in the resolved config.
   */
  network_type?: 'wifi' | 'ethernet' | 'thread' | null;
  /** Any of the connectivity blocks declared a manual_ip.static_ip. */
  network_static_ip?: boolean;
  /** Top-level ``network: {enable_ipv6: true}`` in the resolved config. */
  network_ipv6?: boolean;
  /** wifi.ap fallback access point configured. */
  network_ap_fallback?: boolean;
  /**
   * True when the device participates in Matter — either an explicit
   * top-level ``matter:`` block, or an ``openthread:`` block (ESPHome's
   * openthread component only exists for Matter support, so the latter
   * is treated as a Matter signal too).
   */
  network_matter?: boolean;
  /**
   * Bug #23: ESP chip family parsed from the resolved YAML's
   * ``esp32:`` / ``esp8266:`` / ``rp2040:`` block. ESP32 variants
   * surface as "ESP32-S3", "ESP32-C3", etc. Null when the YAML
   * doesn't pick a platform (rare; usually means resolve failed).
   */
  esp_type?: string | null;
  /**
   * UD.5: PlatformIO board string from inside the chip block
   * (e.g. ``esp32: { board: esp32dev }``). Surfaced as a secondary
   * line on the Devices-tab Platform column; null when the YAML
   * has no board (e.g. host platform) or resolve failed.
   */
  board?: string | null;
  /**
   * Bug #23: ``bluetooth_proxy:`` block state.
   *  - ``off``     — block absent (default).
   *  - ``passive`` — block present, no ``active: true``.
   *  - ``active``  — block present with ``active: true``.
   */
  bluetooth_proxy?: 'off' | 'passive' | 'active';
  /** Per-device pinned ESPHome version from YAML metadata comment. */
  pinned_version?: string | null;
  /** Cron schedule expression (5-field). */
  schedule?: string | null;
  /** Whether the schedule is active. */
  schedule_enabled?: boolean;
  /** ISO datetime of last scheduled run. */
  schedule_last_run?: string | null;
  /** ISO datetime for a one-time scheduled upgrade. Auto-cleared after firing. */
  schedule_once?: string | null;
  /** IANA tz name (e.g. "America/Los_Angeles") that the cron expression is
   * interpreted in. Absent for legacy schedules — the scheduler treats those
   * as UTC. New schedules from the UI always carry the browser tz. */
  schedule_tz?: string | null;
  /** Comma-separated tags from YAML metadata comment. */
  tags?: string | null;
  /** Bug #16: True when the target's YAML has uncommitted changes
   * relative to the git repo under /config/esphome/. Drives the
   * row-level indicator + the conditional "Commit changes…"
   * hamburger item. False when the dir isn't a git repo. */
  has_uncommitted_changes?: boolean;
  /**
   * Bug #32: git HEAD hash of /config/esphome/ at the moment the
   * most-recent successful OTA-flash job was enqueued for this
   * target. Null when there's no flash on record, no git repo, or
   * AV.7 didn't stamp the job.
   */
  last_flashed_config_hash?: string | null;
  /**
   * Bug #32: True when this target's YAML has changed between
   * last_flashed_config_hash and the repo's current HEAD —
   * i.e. "the device is running stale YAML". False when the last
   * flash hash equals HEAD, or when the target isn't among the
   * files that changed in between. Null when the signal is
   * unknown (no last-flashed hash, no git repo, etc).
   *
   * Distinct from `config_modified` — this one is scoped to the
   * last successful flash; the other is a "has the user edited this
   * locally" fallback. Prefer this flag when it's non-null; fall back
   * to `config_modified` when it is. The `hasDriftedConfig` helper in
   * `components/devices/drift.ts` encapsulates that precedence.
   */
  config_drifted_since_flash?: boolean | null;
  /**
   * JH.6: per-target "last compiled" rollup from the persistent job
   * history DAO. Powers the optional "Last compiled" column on the
   * Devices tab. Null when there's neither history nor a running
   * device with a parseable compilation_time. Bug #13: when the
   * SQLite history is empty for this target, the server falls back
   * to the running device's reported ``compilation_time`` and sets
   * ``source: 'device'`` so the UI can render an approximate marker.
   */
  last_compile?: {
    /** Epoch seconds (UTC). */
    at: number;
    state: 'success' | 'failed' | 'cancelled' | 'timed_out';
    ota_result: string | null;
    validate_only: boolean;
    download_only: boolean;
    server_ota?: boolean;
    /** ``history`` = recorded by this server in SQLite. ``device`` = parsed
     * from the running firmware's ESPHome ``compilation_time`` string
     * (approximate; build-host local time, success state assumed). */
    source: 'history' | 'device';
  } | null;
  /**
   * Chip MAC address, lower-case colon-separated (e.g.
   * ``"aa:bb:cc:dd:ee:ff"``). Sourced from mDNS TXT or native API
   * polling. #27 — the HA custom integration attaches this as a
   * ``CONNECTION_NETWORK_MAC`` connection so the target device merges
   * with the native ESPHome integration's device row.
   */
  mac_address?: string | null;
  /**
   * DM.1: True when the row represents a YAML in `<config>/.archive/`.
   * Active rows always carry `archived: false`. Archived rows render
   * at opacity-50, sort below all active rows by `archived_at` desc,
   * and expose only Unarchive + Permanently delete in their action
   * menu. The poller / scheduler / routing engine / queue do not see
   * these rows — archived is purely a UI surface.
   */
  archived?: boolean;
  /** DM.1: epoch seconds (float) the YAML was moved into `.archive/`. Only present when `archived` is true. */
  archived_at?: number;
  /** DM.1: file size in bytes of the archived YAML. Only present when `archived` is true. */
  archived_size?: number;
}

/**
 * An ESPHome device discovered on the network (via mDNS) or reported by
 * Home Assistant. Distinct from `Target` above: a Target is a YAML config
 * we manage; a Device is something physically out there.
 *
 * `compile_target` links the two when we can match a discovered device to
 * one of our YAMLs (by name or MAC). Unmanaged devices (real ESPHome
 * hardware with no local YAML) have `compile_target: null` and still
 * render on the Devices tab under the "Unmanaged" divider.
 */
export interface Device {
  name: string;
  mac_address?: string;
  ip_address?: string;
  running_version?: string;
  online?: boolean;
  /** YAML filename of the managed Target this device corresponds to, or
   *  null for unmanaged devices (no matching YAML). */
  compile_target?: string | null;
  last_seen?: string;
  compilation_time?: number;
  /**
   * How the IP address was resolved. One of: "mdns", "wifi_use_address",
   * "ethernet_use_address", "openthread_use_address", "wifi_static_ip",
   * "ethernet_static_ip", "mdns_default" (the {name}.local fallback).
   * Surfaced under the IP in the Devices tab so users can see at a glance
   * how each device's address was determined.
   */
  address_source?: AddressSource | null;
  /**
   * True when Home Assistant confirms this device exists (MAC in the HA
   * ESPHome-device MAC set, or a matching entity in the HA registry).
   * Populated for both managed and unmanaged devices — particularly
   * useful on unmanaged rows to distinguish "random mDNS broadcast" from
   * "real ESPHome device we don't have YAML for yet".
   */
  ha_configured?: boolean;
  /**
   * HA-reported connectivity: true if the device is currently reachable
   * via HA, false if not, null if HA doesn't expose a status entity for it.
   * Only meaningful when ha_configured is true.
   */
  ha_connected?: boolean | null;
  /** HA device registry ID for deep-linking. See Target.ha_device_id (#35). */
  ha_device_id?: string | null;
}

/**
 * TG.2 / TG.8: routing rule shapes mirrored from `ha-addon/server/routing.py`.
 * A clause is a single predicate against a tag set; a rule pairs a
 * `device_match` (when this fires) with a `worker_match` (then the worker
 * must satisfy this). Severity is reserved for future expansion — only
 * `"required"` is accepted by the API in 1.7.0.
 */
export type RoutingClauseOp = 'all_of' | 'any_of' | 'none_of';

export interface RoutingClause {
  op: RoutingClauseOp;
  tags: string[];
}

export interface RoutingRule {
  id: string;
  name: string;
  severity: 'required';
  device_match: RoutingClause[];
  worker_match: RoutingClause[];
}

export interface SystemInfo {
  os_version?: string;
  cpu_model?: string;
  cpu_arch?: string;
  cpu_cores?: number;
  total_memory?: string;
  uptime?: string;
  perf_score?: number;
  cpu_usage?: number;
  disk_total?: string;
  disk_free?: string;
  disk_used_pct?: number;
  /** Number of target build directories with cached .esphome/ artifacts. */
  cached_targets?: number;
  /** Total size of the build cache in MB. */
  cache_size_mb?: number;
  /**
   * DQ.6: worker's view of the disk-quota engine's most recent state.
   * ``disk_usage_bytes`` is the engine's measured byte total under
   * ``/esphome-versions/`` (venvs + caches + slots + pio-slots).
   * ``disk_quota_bytes`` echoes the effective quota the worker is
   * enforcing against. ``last_eviction_freed_bytes`` is the bytes
   * freed by the most recent post-job sweep.
   */
  disk_usage_bytes?: number;
  disk_quota_bytes?: number;
  last_eviction_freed_bytes?: number;
}

/**
 * Fields used to pre-populate the Connect Worker modal when re-connecting
 * an existing worker (e.g. from the "Image Stale" badge). All optional —
 * missing fields fall back to the modal's default state.
 */
export interface WorkerPreset {
  hostname?: string;
  max_parallel_jobs?: number;
  host_platform?: string;
}

export interface Worker {
  client_id: string;
  hostname: string;
  online: boolean;
  disabled: boolean;
  max_parallel_jobs?: number;
  requested_max_parallel_jobs?: number | null;
  client_version?: string;
  image_version?: string | null;
  system_info?: SystemInfo;
  current_job_id?: string;
  last_seen?: string;
  /**
   * #219: server-side self-pause when the worker reports disk pressure.
   * `null` / undefined = healthy; `"disk_full"` = the worker's heartbeat
   * disk_used_pct crossed the enter threshold and the server is refusing
   * to assign new jobs until it drops back below the exit threshold.
   * Distinct from `disabled`, which is a sticky operator-set pause.
   */
  health_blocked_reason?: string | null;
  /**
   * TG.1: user-managed worker tags. Initially seeded from ``WORKER_TAGS``
   * env on first registration; thereafter authoritative on the server side
   * (UI edits via TG.4/TG.6 are not clobbered by worker restarts unless
   * the worker also sets ``WORKER_TAGS_OVERWRITE=1``).
   */
  tags?: string[];
  /**
   * DQ.5: effective per-worker disk-quota in bytes. The server sends the
   * effective value (override ?? fleet default) on every /ui/api/workers
   * GET so the UI can render "X / Y GiB" without a second fetch.
   */
  disk_quota_bytes?: number;
  /**
   * DQ.5: persisted per-worker override (may be ``null`` = inherit fleet
   * default). The dialog uses this to drive the radio state ("Use fleet
   * default" vs "Custom").
   */
  disk_quota_override_bytes?: number | null;
  /**
   * DQ.5: fleet-wide default; bundled into every row so the Workers tab
   * + Connect dialog can render the default-radio without a separate
   * /ui/api/settings call.
   */
  default_worker_disk_quota_bytes?: number;
}

export interface Job {
  id: string;
  target: string;
  state: string;
  /** ESPHome version this job will compile against. */
  esphome_version?: string;
  assigned_client_id?: string;
  assigned_hostname?: string;
  worker_id?: number | null;
  pinned_client_id?: string;
  /**
   * #23: true when this job is a "follow-up" — created while another job
   * for the same target was already running. Follow-ups are blocked from
   * claiming until the predecessor finishes; surfaced in the queue UI as
   * a "Queued" badge so the user can see "next compile is waiting".
   */
  is_followup?: boolean;
  /** True when this job was triggered by the cron scheduler, not a manual action. */
  scheduled?: boolean;
  /** When `scheduled`, distinguishes recurring (cron) from one-time fires (#92). */
  schedule_kind?: 'recurring' | 'once' | null;
  /** Bug 27: True when the job was enqueued by Home Assistant's
   * esphome_fleet.compile (or similar) service action — i.e. the
   * caller authenticated with the add-on's system-token Bearer as
   * ``esphome_fleet_integration`` AND the request carried a
   * ``HomeAssistant/*`` User-Agent (i.e. the HA integration's
   * coordinator, not a direct API call). Drives a distinct badge in
   * the Queue tab's Triggered column. */
  ha_action?: boolean;
  /** Bug #61: True when the job was enqueued through /ui/api/compile
   * with the server-token Bearer but NOT from the HA integration —
   * e.g. a curl call, a script, Postman. Mutually exclusive with
   * ``ha_action`` by construction. Shown in the Triggered column
   * with a distinct terminal icon so "fleet automation" and
   * "ad-hoc external API use" read differently at a glance. */
  api_triggered?: boolean;
  /** AV.7: git HEAD hash of /config/esphome/ at enqueue time. Used by
   * the "Diff since compile" button in the log modal to open the
   * History panel pre-set to (from=this_hash, to=working tree). */
  config_hash?: string | null;
  /** Bug #8 (1.6.1): why this worker got the job. One of
   *  "pinned_to_worker" / "only_online_worker" /
   *  "only_eligible_worker" / "fewer_jobs_than_others" /
   *  "higher_perf_score" / "first_available".
   *  Null on jobs that predate the field. */
  selection_reason?: string | null;
  duration_seconds?: number | null;
  assigned_at?: string;
  created_at: string;
  finished_at?: string;
  status_text?: string;
  ota_only?: boolean;
  validate_only?: boolean;
  /** FD.1: compile-and-download mode — skips OTA, binary uploaded to server. */
  download_only?: boolean;
  /** SOTA.1: compile on worker, OTA pushed from server. Auto-set for Thread devices. */
  server_ota?: boolean;
  /** FD.1: true once the worker has POSTed the .bin and it's available
   *  from GET /ui/api/jobs/{id}/firmware. Drives the Queue tab's Download
   *  button visibility. */
  has_firmware?: boolean;
  /** #69: variant names currently stored for this job (e.g. ["factory", "ota"]).
   *  ESP32 produces both; ESP8266 produces only "ota". Legacy pre-#69 blobs
   *  surface as ["firmware"]. Drives which entries the Download dropdown
   *  renders. Empty when has_firmware is false. */
  firmware_variants?: string[];
  ota_result?: string;
  log?: string;
  /** TG.3: when state="blocked", the rule that disqualified every online worker.
   *  Drives the QueueTab BLOCKED-badge tooltip and the click-through to the
   *  routing-rules editor. Cleared when the job leaves BLOCKED. */
  blocked_reason?: {
    rule_id: string;
    rule_name: string;
    summary: string;
  } | null;
  /** Bug #97: per-job worker-tag filter set at enqueue time from the
   *  Upgrade modal's "Tag expression" worker-selection radio. Same
   *  shape as a routing-rule clause — claim_next consults it via the
   *  same per-worker eligibility predicate that drives global rules. */
  worker_tag_filter?: {
    op: RoutingClauseOp;
    tags: string[];
  } | null;
}
