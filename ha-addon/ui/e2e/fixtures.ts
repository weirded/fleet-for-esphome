import { type Page } from '@playwright/test';

// C.6: import the canonical TS types from the app and assert each fixture
// against them. A field rename in src/types/index.ts now triggers a TS error
// in the e2e tests, so the contract between mocks and runtime types stays
// in lockstep. Without these annotations the mocks were duck-typed and a
// rename would silently desynchronize them from the real client.
import type {
  ServerInfo,
  EsphomeVersions,
  Target,
  Device,
  Worker,
  Job,
  RoutingRule,
} from '../src/types';

// --- Mock API data ---

export const serverInfo: ServerInfo = {
  token: 'test-token',
  port: 8765,
  addon_version: '1.3.0-dev.4',
  server_client_version: '1.3.0-dev.4',
  min_image_version: '3',
  // DQ.5: fleet default — used by the ConnectWorkerModal "Use fleet
  // default (X GiB)" radio label and as the dialog's default value.
  default_worker_disk_quota_bytes: 10 * 1024 ** 3,
};

export const esphomeVersions: EsphomeVersions = {
  selected: '2026.3.2',
  detected: '2026.3.2',
  available: ['2026.3.2', '2026.2.0', '2026.1.0'],
};

export const targets: Target[] = [
  {
    target: 'living-room.yaml',
    device_name: 'living-room',
    friendly_name: 'Living Room Sensor',
    ip_address: '192.168.1.10',
    running_version: '2026.3.2',
    online: true,
    needs_update: false,
    server_version: '2026.3.2',
    has_api_key: true,
    has_web_server: false,
    area: 'Living Room',
    // Bug #8: gives the bulk-tag-edit spec a "common" tag (kitchen
    // appears on living-room AND bedroom-light) and a "partial" tag
    // (cosy is only on living-room).
    tags: 'kitchen,cosy',
  },
  {
    target: 'bedroom-light.yaml',
    device_name: 'bedroom-light',
    friendly_name: 'Bedroom Light',
    ip_address: '192.168.1.11',
    running_version: '2026.2.0',
    online: true,
    needs_update: true,
    server_version: '2026.3.2',
    has_api_key: false,
    area: 'Bedroom',
    // PT.12 — pinned to a specific version so the pin-unpin spec can verify
    // the 📌 icon appears in the version cell.
    pinned_version: '2026.2.0',
    tags: 'kitchen,sleeping',
  },
  {
    target: 'garage-door.yaml',
    device_name: 'garage-door',
    friendly_name: 'Garage Door',
    ip_address: '192.168.1.12',
    running_version: '2026.3.2',
    online: false,
    needs_update: false,
    server_version: '2026.3.2',
    // PT.12 — recurring schedule so the schedules tab has at least one row.
    schedule: '0 3 * * *',
    schedule_enabled: true,
    schedule_tz: 'UTC',
  },
  {
    target: 'office.yaml',
    device_name: 'office',
    friendly_name: 'Office Sensor',
    ip_address: '192.168.1.13',
    running_version: '2026.3.2',
    online: true,
    needs_update: false,
    server_version: '2026.3.2',
    // PT.12 — one-time scheduled upgrade. Far enough in the future that it
    // doesn't fire during a test run.
    schedule_once: new Date(Date.now() + 24 * 3600_000).toISOString(),
  },
];

export const devices: Device[] = [
  { name: 'living-room', ip_address: '192.168.1.10', online: true, compile_target: 'living-room.yaml' },
  { name: 'bedroom-light', ip_address: '192.168.1.11', online: true, compile_target: 'bedroom-light.yaml' },
  { name: 'garage-door', ip_address: '192.168.1.12', online: false, compile_target: 'garage-door.yaml' },
];

export const workers: Worker[] = [
  {
    client_id: 'worker-1',
    hostname: 'build-server-1',
    online: true,
    disabled: false,
    max_parallel_jobs: 2,
    requested_max_parallel_jobs: null,
    client_version: '1.3.0-dev.4',
    image_version: '2',
    // TG.1/TG.6 — at least one worker carries tags so the Workers tab
    // renders the filter pill bar and the routing-rule builder's worker
    // pool autocomplete has something to suggest.
    tags: ['linux', 'fast'],
    system_info: {
      os_version: 'Debian 12',
      cpu_model: 'Intel i7-12700',
      cpu_cores: 8,
      total_memory: '32 GB',
      disk_total: '500 GB',
      disk_free: '350 GB',
      disk_used_pct: 30,
      // DQ.6: worker reports its view of the disk-quota engine state.
      // 2.1 / 10 GiB → ~21 % used — under the 80 % yellow threshold.
      disk_usage_bytes: Math.round(2.1 * 1024 ** 3),
      disk_quota_bytes: 10 * 1024 ** 3,
      last_eviction_freed_bytes: 0,
    },
    // DQ.5: GET /ui/api/workers includes both the effective quota and
    // the persisted override (null = inherits fleet default).
    disk_quota_bytes: 10 * 1024 ** 3,
    disk_quota_override_bytes: null,
    default_worker_disk_quota_bytes: 10 * 1024 ** 3,
  },
  {
    client_id: 'worker-2',
    hostname: 'build-server-2',
    online: false,
    disabled: false,
    max_parallel_jobs: 1,
    client_version: '1.3.0-dev.3',
    image_version: null, // pre-LIB.0 worker
    last_seen: new Date(Date.now() - 15 * 60_000).toISOString(), // 15 min ago
    tags: ['macos'],
  },
];

// TG.10 — initial seed of routing rules. ``mockApi`` mutates a working
// copy in place (POST appends, DELETE splices, PUT replaces) so the
// in-memory state survives a SWR revalidation round-trip within a
// single test. The working copy is reset at the top of every
// ``mockApi`` call so specs don't leak state across each other (since
// ``mockApi`` runs once per ``beforeEach``).
const routingRulesSeed: readonly RoutingRule[] = [
  {
    id: 'kitchen-only',
    name: 'Kitchen devices need an arm64 worker',
    severity: 'required',
    // No online worker has the ``arm64`` tag in our fixtures, so this
    // rule's worker side is intentionally un-satisfiable — that's what
    // makes ``job-009`` BLOCKED and gives the click-through test a real
    // rule to land on in edit mode.
    device_match: [{ op: 'all_of', tags: ['kitchen'] }],
    worker_match: [{ op: 'all_of', tags: ['arm64'] }],
  },
];
export const routingRules: RoutingRule[] = [];

// All job states are exercised so a regression in any badge / row class
// path is caught by the existing Playwright tests. Order: success, failed,
// working, pending, timed_out — covers the full state machine.
export const queue: Job[] = [
  {
    id: 'job-001',
    target: 'bedroom-light.yaml',
    state: 'success',
    assigned_client_id: 'worker-1',
    assigned_hostname: 'build-server-1',
    created_at: new Date(Date.now() - 600_000).toISOString(),
    duration_seconds: 120,
    ota_result: 'success',
    // AV.7: config_hash stamped at enqueue time so the log modal can
    // offer a "Diff since compile" shortcut into the History panel.
    config_hash: 'fedcba9876543210fedcba9876543210fedcba98',
  },
  {
    id: 'job-002',
    target: 'garage-door.yaml',
    state: 'failed',
    assigned_client_id: 'worker-1',
    assigned_hostname: 'build-server-1',
    created_at: new Date(Date.now() - 300_000).toISOString(),
    duration_seconds: 45,
  },
  {
    id: 'job-003',
    target: 'living-room.yaml',
    state: 'working',
    assigned_client_id: 'worker-1',
    assigned_hostname: 'build-server-1',
    created_at: new Date(Date.now() - 60_000).toISOString(),
    status_text: 'Compiling...',
  },
  {
    id: 'job-004',
    target: 'kitchen.yaml',
    state: 'pending',
    created_at: new Date(Date.now() - 10_000).toISOString(),
  },
  {
    id: 'job-005',
    target: 'office.yaml',
    state: 'timed_out',
    assigned_client_id: 'worker-1',
    assigned_hostname: 'build-server-1',
    created_at: new Date(Date.now() - 900_000).toISOString(),
    duration_seconds: 600,
  },
  // PT.12 — cancelled job so the queue spec can verify the Cancelled badge
  // and that "Clear Succeeded" doesn't touch cancelled rows.
  {
    id: 'job-006',
    target: 'living-room.yaml',
    state: 'cancelled',
    assigned_client_id: 'worker-1',
    assigned_hostname: 'build-server-1',
    created_at: new Date(Date.now() - 1200_000).toISOString(),
    duration_seconds: 12,
  },
  // PT.12 — scheduled (recurring) job so the Triggered column renders the
  // Clock icon path. Terminal state (success) so it doesn't bump active count.
  {
    id: 'job-007',
    target: 'garage-door.yaml',
    state: 'success',
    scheduled: true,
    schedule_kind: 'recurring',
    assigned_client_id: 'worker-1',
    assigned_hostname: 'build-server-1',
    created_at: new Date(Date.now() - 1800_000).toISOString(),
    finished_at: new Date(Date.now() - 1700_000).toISOString(),
    duration_seconds: 100,
    ota_result: 'success',
  },
  // FD.8 — download-only job with a firmware binary ready. Queue-tab
  // renders the Download button exclusively for this row.
  {
    id: 'job-008',
    target: 'office.yaml',
    state: 'success',
    download_only: true,
    has_firmware: true,
    assigned_client_id: 'worker-1',
    assigned_hostname: 'build-server-1',
    created_at: new Date(Date.now() - 2100_000).toISOString(),
    finished_at: new Date(Date.now() - 2000_000).toISOString(),
    duration_seconds: 100,
  },
  // TG.9/TG.10 — BLOCKED job: a kitchen-tagged device with no online
  // worker that satisfies the kitchen-only rule's worker side. Drives
  // the QueueTab BLOCKED-badge tooltip + click-through assertion.
  {
    id: 'job-009',
    target: 'living-room.yaml',
    state: 'blocked',
    created_at: new Date(Date.now() - 5_000).toISOString(),
    blocked_reason: {
      rule_id: 'kitchen-only',
      rule_name: 'Kitchen devices need an arm64 worker',
      summary: 'all of [arm64]',
    },
  },
];

const configContent = `esphome:
  name: living-room
  friendly_name: "Living Room Sensor"

esp32:
  board: esp32dev

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

logger:
api:
ota:
`;

// --- Route interceptor ---

export async function mockApi(page: Page) {
  // TG.10 — reset mutable fixtures so per-test mutations from previous
  // specs don't leak. Worker tags and routing-rule CRUD both mutate
  // their fixtures in place; everything else is read-only.
  routingRules.length = 0;
  routingRules.push(...routingRulesSeed.map(r => ({
    ...r,
    device_match: r.device_match.map(c => ({ ...c, tags: [...c.tags] })),
    worker_match: r.worker_match.map(c => ({ ...c, tags: [...c.tags] })),
  })));
  // Snapshot of the worker tag arrays so a tag-edit spec doesn't bleed
  // into a later test that expects the seed values.
  for (const w of workers) {
    if (w.client_id === 'worker-1') w.tags = ['linux', 'fast'];
    if (w.client_id === 'worker-2') w.tags = ['macos'];
  }

  await page.route('**/ui/api/server-info', route =>
    route.fulfill({ json: serverInfo }),
  );
  await page.route('**/ui/api/esphome-versions', route =>
    route.fulfill({ json: esphomeVersions }),
  );
  await page.route('**/ui/api/targets', async (route) => {
    const method = route.request().method();
    if (method === 'POST') {
      // CD.3: create/duplicate. Echo the requested filename back as the
      // canonical target name so the client can open the editor on it.
      let body: { filename?: string; source?: string } = {};
      try {
        body = JSON.parse(route.request().postData() ?? '{}');
      } catch {
        /* empty */
      }
      const raw = (body.filename ?? '').trim();
      const slug = raw.toLowerCase().endsWith('.yaml') ? raw.slice(0, -5) : raw;
      // #62: server returns .pending. prefix; editor strips it for display
      return route.fulfill({ json: { ok: true, target: `.pending.${slug}.yaml` } });
    }
    return route.fulfill({ json: targets });
  });
  await page.route('**/ui/api/devices', route =>
    route.fulfill({ json: devices }),
  );
  await page.route('**/ui/api/workers', route =>
    route.fulfill({ json: workers }),
  );
  await page.route('**/ui/api/queue', route =>
    route.fulfill({ json: queue }),
  );
  await page.route('**/ui/api/secret-keys', route =>
    route.fulfill({ json: { keys: ['wifi_ssid', 'wifi_password', 'api_key'] } }),
  );
  await page.route('**/ui/api/esphome-schema', route =>
    route.fulfill({ json: { components: ['wifi', 'logger', 'api', 'ota', 'esp32'] } }),
  );
  await page.route('**/ui/api/targets/*/content', route =>
    route.fulfill({ json: { content: configContent } }),
  );
  await page.route('**/ui/api/compile', route =>
    route.fulfill({ json: { enqueued: 1 } }),
  );
  await page.route('**/ui/api/cancel', route =>
    route.fulfill({ json: { cancelled: 1 } }),
  );
  await page.route('**/ui/api/retry', route =>
    route.fulfill({ json: { retried: 1 } }),
  );
  await page.route('**/ui/api/validate', route =>
    route.fulfill({ json: { job_id: 'validate-001' } }),
  );
  await page.route('**/ui/api/queue/clear', route =>
    route.fulfill({ json: { cleared: 1 } }),
  );
  await page.route('**/ui/api/queue/remove', route =>
    route.fulfill({ json: { removed: 1 } }),
  );
  await page.route('**/ui/api/jobs/*/log*', route =>
    route.fulfill({ json: { log: 'INFO Compiling...\nINFO Done.\n', offset: 100, finished: true } }),
  );
  // FD.6 — firmware download. Short tiny payload; tests just verify the
  // request reaches this URL, not the byte content.
  await page.route('**/ui/api/jobs/*/firmware', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/octet-stream',
      headers: { 'Content-Disposition': 'attachment; filename="firmware.bin"' },
      body: 'FIRMWARE_BYTES',
    }),
  );
  await page.route('**/ui/api/targets/*/rename', route =>
    route.fulfill({ json: { new_filename: 'renamed.yaml' } }),
  );
  // PT.12 — pin / unpin: server returns no body on success.
  await page.route('**/ui/api/targets/*/pin', route =>
    route.fulfill({ status: 200, json: {} }),
  );
  // PT.12 — schedule routes: set/delete recurring + one-time + toggle.
  await page.route('**/ui/api/targets/*/schedule/once', route =>
    route.fulfill({ status: 200, json: {} }),
  );
  await page.route('**/ui/api/targets/*/schedule/toggle', route =>
    route.fulfill({ json: { schedule_enabled: false } }),
  );
  await page.route('**/ui/api/targets/*/schedule', route =>
    route.fulfill({ status: 200, json: {} }),
  );
  // PT.12 — schedule history (per-target outcome list) — empty by default.
  await page.route('**/ui/api/schedule-history', route =>
    route.fulfill({ json: {} }),
  );
  // PT.12 — meta updates (area, comment) used by hamburger-menu actions.
  await page.route('**/ui/api/targets/*/meta', route =>
    route.fulfill({ status: 200, json: {} }),
  );
  await page.route('**/ui/api/workers/*/clean', route =>
    route.fulfill({ status: 200, json: {} }),
  );
  // DQ.5 — per-worker disk-quota override. ``null`` clears the override
  // so the worker inherits the fleet default. Body shape:
  // ``{disk_quota_bytes: int | null}``. Mutates the in-memory worker so
  // a follow-up GET sees the change (mirrors the real server flow).
  await page.route('**/ui/api/workers/*/disk-quota', async (route) => {
    const url = new URL(route.request().url());
    const m = url.pathname.match(/\/ui\/api\/workers\/([^/]+)\/disk-quota$/);
    if (!m) return route.fulfill({ status: 400, json: { error: 'bad path' } });
    const id = decodeURIComponent(m[1]);
    let body: { disk_quota_bytes?: number | null } = {};
    try {
      body = JSON.parse(route.request().postData() ?? '{}');
    } catch {
      return route.fulfill({ status: 400, json: { error: 'bad json' } });
    }
    const w = workers.find(x => x.client_id === id);
    if (w) {
      w.disk_quota_override_bytes = body.disk_quota_bytes ?? null;
      w.disk_quota_bytes = body.disk_quota_bytes ?? w.default_worker_disk_quota_bytes ?? 10 * 1024 ** 3;
    }
    return route.fulfill({ json: { ok: true, disk_quota_bytes: body.disk_quota_bytes ?? null } });
  });
  // TG.4 — worker tag edit endpoint. Body shape: ``{tags: [str]}``. We
  // mutate the in-memory ``workers`` array so a follow-up GET reflects
  // the edit (matches how the real server's broadcast → SWR refetch
  // round-trips through the UI). Reset between specs because mockApi
  // runs once per beforeEach.
  await page.route('**/ui/api/workers/*/tags', async (route) => {
    const url = new URL(route.request().url());
    const m = url.pathname.match(/\/ui\/api\/workers\/([^/]+)\/tags$/);
    if (!m) return route.fulfill({ status: 400, json: { error: 'bad path' } });
    const id = decodeURIComponent(m[1]);
    let body: { tags?: string[] } = {};
    try {
      body = JSON.parse(route.request().postData() ?? '{}');
    } catch {
      return route.fulfill({ status: 400, json: { error: 'bad json' } });
    }
    const w = workers.find(x => x.client_id === id);
    if (w) w.tags = body.tags ?? [];
    return route.fulfill({ json: { ok: true, tags: w?.tags ?? [] } });
  });
  // TG.4/TG.10 — routing rules CRUD. List + create at the collection
  // URL; PUT/DELETE on the per-id URL. POST auto-slugs the id from
  // the name when omitted (matches server-side _slugify).
  await page.route('**/ui/api/routing-rules', async (route) => {
    const method = route.request().method();
    if (method === 'POST') {
      let body: Partial<RoutingRule> = {};
      try {
        body = JSON.parse(route.request().postData() ?? '{}');
      } catch {
        return route.fulfill({ status: 400, json: { error: 'bad json' } });
      }
      const slug = (s: string) =>
        s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
      const id = body.id || slug(body.name ?? 'rule');
      if (routingRules.some(r => r.id === id)) {
        return route.fulfill({ status: 400, json: { error: `id ${id} exists` } });
      }
      const rule: RoutingRule = {
        id,
        name: body.name ?? '(unnamed)',
        severity: 'required',
        device_match: body.device_match ?? [],
        worker_match: body.worker_match ?? [],
      };
      routingRules.push(rule);
      return route.fulfill({ status: 200, json: rule });
    }
    return route.fulfill({ json: { rules: routingRules } });
  });
  await page.route('**/ui/api/routing-rules/*', async (route) => {
    const url = new URL(route.request().url());
    const m = url.pathname.match(/\/ui\/api\/routing-rules\/([^/]+)$/);
    if (!m) return route.fulfill({ status: 404, json: { error: 'no match' } });
    const id = decodeURIComponent(m[1]);
    const method = route.request().method();
    if (method === 'DELETE') {
      const idx = routingRules.findIndex(r => r.id === id);
      if (idx >= 0) routingRules.splice(idx, 1);
      return route.fulfill({ json: { ok: true } });
    }
    if (method === 'PUT') {
      let body: Partial<RoutingRule> = {};
      try {
        body = JSON.parse(route.request().postData() ?? '{}');
      } catch {
        return route.fulfill({ status: 400, json: { error: 'bad json' } });
      }
      const idx = routingRules.findIndex(r => r.id === id);
      if (idx < 0) return route.fulfill({ status: 404, json: { error: 'not found' } });
      const updated: RoutingRule = {
        id,
        name: body.name ?? routingRules[idx].name,
        severity: 'required',
        device_match: body.device_match ?? routingRules[idx].device_match,
        worker_match: body.worker_match ?? routingRules[idx].worker_match,
      };
      routingRules[idx] = updated;
      return route.fulfill({ json: updated });
    }
    return route.fulfill({ status: 405, json: { error: 'method not allowed' } });
  });
  await page.route('**/ui/api/targets/*', route => {
    if (route.request().method() === 'DELETE') {
      return route.fulfill({ status: 200, json: {} });
    }
    return route.fallback();
  });

  // SP.3 — in-app Settings store. In-memory so GET + PATCH behave like
  // the real server during a spec: a PATCH returns the updated blob,
  // and a subsequent GET reflects the change. Reset on each test by
  // virtue of mockApi running once per beforeEach.
  // AV.6 mock state — history + status + diff + rollback + manual commit.
  // Trailing `*` catches any `?limit=...&offset=...` query string.
  await page.route('**/ui/api/files/*/history*', route => {
    route.fulfill({
      json: [
        {
          hash: 'fedcba9876543210fedcba9876543210fedcba98',
          short_hash: 'fedcba9',
          date: Math.floor(Date.now() / 1000) - 60,
          author_name: 'HA User',
          author_email: 'ha@distributed-esphome.local',
          message: 'save: living-room.yaml',
          lines_added: 3,
          lines_removed: 1,
        },
        {
          hash: '0123456789abcdef0123456789abcdef01234567',
          short_hash: '0123456',
          date: Math.floor(Date.now() / 1000) - 3600,
          author_name: 'HA User',
          author_email: 'ha@distributed-esphome.local',
          message: 'pin: living-room.yaml',
          lines_added: 1,
          lines_removed: 0,
        },
      ],
    });
  });
  await page.route('**/ui/api/files/*/status', route => {
    route.fulfill({
      json: {
        has_uncommitted_changes: false,
        head_hash: 'fedcba9876543210fedcba9876543210fedcba98',
        head_short_hash: 'fedcba9',
      },
    });
  });
  await page.route('**/ui/api/files/*/diff*', route => {
    route.fulfill({
      json: {
        diff: '--- a/living-room.yaml\n+++ b/living-room.yaml\n@@ -1,3 +1,3 @@\n-# old line\n+# new line\n',
      },
    });
  });
  // Bug #10: content-at endpoint for side-by-side diff.
  await page.route('**/ui/api/files/*/content-at*', route => {
    const url = new URL(route.request().url());
    const hash = url.searchParams.get('hash');
    route.fulfill({
      json: {
        content: hash
          ? `esphome:\n  name: living-room\n# content-at ${hash.slice(0, 7)}\n`
          : 'esphome:\n  name: living-room\n# current working tree\n',
      },
    });
  });
  await page.route('**/ui/api/files/*/rollback', route => {
    route.fulfill({
      json: {
        content: '# restored\n',
        committed: true,
        hash: 'cafeba5ecafeba5ecafeba5ecafeba5ecafeba5e',
        short_hash: 'cafeba5',
      },
    });
  });
  await page.route('**/ui/api/files/*/commit', route => {
    route.fulfill({
      json: {
        committed: true,
        hash: '1111111111111111111111111111111111111111',
        short_hash: '1111111',
        message: 'save: living-room.yaml (manual)',
      },
    });
  });

  const settingsState: Record<string, unknown> = {
    // #97 + #98: master versioning tristate. ``'on'`` keeps the
    // existing specs' assumptions (Config-versioning-section inputs
    // enabled). Specs that exercise the onboarding modal override
    // this to ``'unset'`` via their own page.route overlay.
    versioning_enabled: 'on',
    auto_commit_on_save: true,
    git_author_name: 'HA User',
    git_author_email: 'ha@distributed-esphome.local',
    job_history_retention_days: 365,
    firmware_cache_max_gb: 2.0,
    job_log_retention_days: 30,
    // SP.8 fields (formerly in Supervisor options.json).
    server_token: 'test-token-abc',
    job_timeout: 600,
    ota_timeout: 120,
    worker_offline_threshold: 30,
    device_poll_interval: 60,
    require_ha_auth: true,
    // #82: default to 'auto' (follow browser locale).
    time_format: 'auto',
    // I18N.2 (#141): UI locale picker. ``'auto'`` defers to
    // navigator.language; the Settings drawer renders an EnumRow
    // wired against this field.
    language: 'auto',
    // #145: font-size scale picker. ``'normal'`` is byte-identical
    // to pre-#145; the EnumRow under Display lets the user pick
    // Small/Normal/Large.
    font_size: 'normal',
  };
  await page.route('**/ui/api/settings', async (route) => {
    const method = route.request().method();
    if (method === 'PATCH') {
      let body: Record<string, unknown> = {};
      try {
        body = JSON.parse(route.request().postData() ?? '{}');
      } catch {
        return route.fulfill({ status: 400, json: { error: 'bad json' } });
      }
      // Reject unknown keys the way the real endpoint does.
      for (const key of Object.keys(body)) {
        if (!(key in settingsState)) {
          return route.fulfill({ status: 400, json: { error: `${key}: unknown settings key`, field: key } });
        }
        settingsState[key] = body[key];
      }
      return route.fulfill({ json: settingsState });
    }
    return route.fulfill({ json: settingsState });
  });
}
