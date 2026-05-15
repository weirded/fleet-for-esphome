import { toast } from 'sonner';
import type { Device, EsphomeVersions, Job, ServerInfo, Target, Worker } from '../types';

// ---------------------------------------------------------------------------
// Response shapes (QS.9)
//
// Named at module top so the wire contract is self-documenting and so callers
// importing them get IntelliSense. Used as the type argument to
// `parseResponse<T>()` instead of inline `as { ... }` casts.
// ---------------------------------------------------------------------------

export interface CompileResponse { enqueued: number }
export interface CancelResponse { cancelled: number }
export interface RetryResponse { retried: number }
export interface RemoveResponse { removed: number }
export interface ClearResponse { cleared: number }
export interface ScheduleResponse { schedule_enabled: boolean }
export interface SaveTargetResponse { renamed_to?: string | null }
export interface CreateTargetResponse { target?: string }
export interface RenameTargetResponse { new_filename: string }
export interface ApiKeyResponse { key?: string }
export interface JobLogResponse { log: string; offset: number; finished: boolean }
export interface ValidateResponse { success?: boolean; output?: string; error?: string }
export interface SecretKeysResponse { keys?: string[] }
export interface EsphomeSchemaResponse { components?: string[] }

// ---------------------------------------------------------------------------
// Response helpers (QS.8 + QS.10)
//
// `parseResponse` does the standard error-handling pattern that was repeated
// ~30 times in this file: parse JSON, throw with the server's `error` message
// when present, fall back to the HTTP status code. Reduces ~150 lines of
// boilerplate and ensures every caller surfaces server-side error detail
// (QS.10 — previously getTargets/getDevices/etc. threw "Failed to fetch X"
// even when the server returned a useful message).
//
// `expectOk` is the bodyless variant — for endpoints that return 200/204 with
// no JSON body that callers care about.
// ---------------------------------------------------------------------------

// #84: typed error so callers can distinguish 401 (session expired) from
// other failures. Previously every non-OK response threw a plain `Error`
// whose message was the only signal — SWR hooks couldn't tell a real
// empty result apart from "you're logged out" and the Devices/Workers/
// Queue tabs ended up rendering "No devices found" after the session
// expired. Keep `Error` as the base so callers that only care about
// `.message` still work unchanged.
export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function isUnauthorizedError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

async function _readError(r: Response, fallback: string): Promise<string> {
  // Try to read the server-provided error string. Falls back to the supplied
  // tag (e.g. "fetching workers") + status code when the body isn't JSON or
  // doesn't contain `error`.
  try {
    const data = await r.json() as { error?: string };
    if (data && typeof data.error === 'string' && data.error) return data.error;
  } catch { /* not JSON or empty body */ }
  return `${fallback} (HTTP ${r.status})`;
}

async function parseResponse<T = unknown>(r: Response, errorTag: string): Promise<T> {
  if (!r.ok) throw new ApiError(await _readError(r, errorTag), r.status);
  return r.json() as Promise<T>;
}

async function expectOk(r: Response, errorTag: string): Promise<void> {
  if (!r.ok) throw new ApiError(await _readError(r, errorTag), r.status);
}

// Version sentinel for auto-reload detection
let _initialAddonVersion: string | null = null;
let _reloadScheduled = false;

export function buildWsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const base = document.querySelector('base')?.getAttribute('href') || '/';
  const a = document.createElement('a');
  a.href = base + path;
  return `${proto}//${location.host}${a.pathname}`;
}

// AU.7: when the UI is loaded via direct-port (8765) — e.g. prod smoke
// tests or power users bypassing Ingress — `/ui/api/*` now requires a
// Bearer. If the initial URL carries `?token=X`, stash it in
// sessionStorage and attach it to every api request. When served via
// Ingress, no token arrives, no token is sent, and Supervisor-peer
// trust handles auth. Idempotent: the lookup runs once on first call.
let _authToken: string | null | undefined;
function _getAuthToken(): string | null {
  if (_authToken === undefined) {
    try {
      const url = new URL(window.location.href);
      const tokenFromUrl = url.searchParams.get('token');
      if (tokenFromUrl) {
        sessionStorage.setItem('esphome_fleet_token', tokenFromUrl);
        // Remove ?token=… from the visible URL so the user doesn't bookmark
        // or share a copy with the credential baked in.
        url.searchParams.delete('token');
        window.history.replaceState({}, '', url.toString());
      }
      _authToken = sessionStorage.getItem('esphome_fleet_token');
    } catch {
      _authToken = null;
    }
  }
  return _authToken;
}

export async function apiFetch(path: string, opts: RequestInit = {}): Promise<Response> {
  // Attach the AU.7 Bearer if we have one (direct-port smoke tests,
  // external tooling pasting a `?token=` URL). Ingress access leaves
  // this path a no-op.
  const token = _getAuthToken();
  let finalOpts = opts;
  if (token) {
    const headers = new Headers(opts.headers);
    if (!headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`);
    }
    finalOpts = { ...opts, headers };
  }
  const r = await fetch(path, finalOpts);
  // Detect server version changes from response header
  const sv = r.headers.get('X-Server-Version');
  if (sv && _initialAddonVersion && sv !== _initialAddonVersion && !_reloadScheduled) {
    _reloadScheduled = true;
    console.log('Server version changed (header):', _initialAddonVersion, '→', sv);
    toast.info('New server version — reloading...');
    setTimeout(() => location.reload(), 1000);
  }
  return r;
}

export function setInitialAddonVersion(version: string) {
  if (_initialAddonVersion === null) {
    _initialAddonVersion = version;
  }
}

export function getInitialAddonVersion(): string | null {
  return _initialAddonVersion;
}

export async function getServerInfo(): Promise<ServerInfo> {
  return parseResponse<ServerInfo>(await apiFetch('./ui/api/server-info'), 'fetching server info');
}

// AV.3 / AV.4 / AV.5 / AV.6 / AV.11 — per-file history + diff + rollback + manual commit.
export interface FileHistoryEntry {
  hash: string;
  short_hash: string;
  date: number;
  author_name: string;
  author_email: string;
  message: string;
  lines_added: number;
  lines_removed: number;
  // #211: when a successful compile at this commit still has its
  // firmware binary on disk, the server attaches the job id + the
  // available variants so the History panel can render a Download chip.
  firmware_job_id?: string;
  firmware_variants?: string[];
}

export interface FileStatus {
  has_uncommitted_changes: boolean;
  head_hash: string | null;
  head_short_hash: string | null;
}

export interface CommitResult {
  committed: boolean;
  hash: string | null;
  short_hash: string | null;
  message: string | null;
}

export interface RollbackResult {
  content: string;
  committed: boolean;
  hash: string | null;
  short_hash: string | null;
}

export async function getFileHistory(filename: string, limit = 50, offset = 0): Promise<FileHistoryEntry[]> {
  const qs = `?limit=${limit}&offset=${offset}`;
  return parseResponse<FileHistoryEntry[]>(
    await apiFetch(`./ui/api/files/${encodeURIComponent(filename)}/history${qs}`),
    'fetching file history',
  );
}

export async function getFileStatus(filename: string): Promise<FileStatus> {
  return parseResponse<FileStatus>(
    await apiFetch(`./ui/api/files/${encodeURIComponent(filename)}/status`),
    'fetching file status',
  );
}

export async function getFileContentAt(filename: string, hash?: string | null): Promise<string> {
  const qs = hash ? `?hash=${encodeURIComponent(hash)}` : '';
  const body = await parseResponse<{ content: string }>(
    await apiFetch(`./ui/api/files/${encodeURIComponent(filename)}/content-at${qs}`),
    'fetching file content at commit',
  );
  return body.content;
}

export async function getFileDiff(
  filename: string,
  from?: string | null,
  to?: string | null,
): Promise<string> {
  const params = new URLSearchParams();
  if (from) params.set('from', from);
  if (to) params.set('to', to);
  const qs = params.toString() ? `?${params.toString()}` : '';
  const body = await parseResponse<{ diff: string }>(
    await apiFetch(`./ui/api/files/${encodeURIComponent(filename)}/diff${qs}`),
    'fetching file diff',
  );
  return body.diff;
}

export async function rollbackFile(filename: string, hash: string): Promise<RollbackResult> {
  return parseResponse<RollbackResult>(
    await apiFetch(`./ui/api/files/${encodeURIComponent(filename)}/rollback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash }),
    }),
    'rolling back file',
  );
}

export async function commitFile(filename: string, message?: string): Promise<CommitResult> {
  return parseResponse<CommitResult>(
    await apiFetch(`./ui/api/files/${encodeURIComponent(filename)}/commit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(message ? { message } : {}),
    }),
    'committing file',
  );
}

// SP.3 — in-app Settings (separate from Supervisor's options.json).
// Keep the shape alphabetical-ish and mirrored from AppSettings in
// ha-addon/server/settings.py. Any rename there is a UI contract change
// — update this interface in the same commit.
export interface AppSettings {
  // #97 + #98: master tristate for the AV.* config-versioning
  // feature. ``'unset'`` = the user hasn't decided yet (show the
  // onboarding modal); ``'on'`` = active; ``'off'`` = explicitly off.
  // Treat anything other than ``'on'`` as disabled when gating UI
  // affordances.
  versioning_enabled: 'on' | 'off' | 'unset';
  auto_commit_on_save: boolean;
  git_author_name: string;
  git_author_email: string;
  job_history_retention_days: number;
  firmware_cache_max_gb: number;
  // Bug #198: time-based eviction for /data/firmware (in days).
  // 0 = unlimited; default 2.
  firmware_retention_days: number;
  job_log_retention_days: number;
  // DQ.1 — fleet-wide default cap on the worker's /esphome-versions/ tree
  // (bytes). Per-worker overrides live on the Worker row's "Set disk
  // quota…" dialog and are POSTed to /ui/api/workers/{id}/disk-quota.
  default_worker_disk_quota_bytes: number;
  // SP.8 — moved from Supervisor options.json in 1.6.
  server_token: string;
  job_timeout: number;
  ota_timeout: number;
  worker_offline_threshold: number;
  device_poll_interval: number;
  require_ha_auth: boolean;
  // #82 — time-of-day presentation. 'auto' defers to the browser's
  // resolved locale; '12h'/'24h' force the format globally.
  time_format: 'auto' | '12h' | '24h';
  // Bug #5 — date presentation. 'auto' defers to the browser's locale;
  // 'iso' = YYYY-MM-DD, 'us' = M/D/YYYY, 'eu' = DD/MM/YYYY,
  // 'long' = "Apr 27, 2026". Wired to App.tsx → setDateFormatPref.
  date_format: 'auto' | 'iso' | 'us' | 'eu' | 'long';
  // I18N.2 (#141) — UI locale. 'auto' resolves to navigator.language;
  // 'en' / 'de' force a specific locale. Wired to App.tsx →
  // i18n.changeLanguage(). Adding a language requires (a) shipping its
  // catalog in src/i18n/locales/, (b) adding it here, and (c) listing
  // it in `_validate_enum` on the server.
  language: 'auto' | 'en' | 'de';
  // #145 — UI font-size scale. 'normal' = today's sizing (byte-identical
  // render to pre-#145). 'small' / 'large' shift the root font-size so
  // Tailwind's whole type ramp scales proportionally; persisted via
  // [data-font-size] attribute on <html>, applied at App.tsx mount.
  font_size: 'small' | 'normal' | 'large';
}

export async function getSettings(): Promise<AppSettings> {
  return parseResponse<AppSettings>(await apiFetch('./ui/api/settings'), 'fetching settings');
}

export async function updateSettings(partial: Partial<AppSettings>): Promise<AppSettings> {
  return parseResponse<AppSettings>(
    await apiFetch('./ui/api/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(partial),
    }),
    'updating settings',
  );
}

export async function getEsphomeVersions(): Promise<EsphomeVersions> {
  return parseResponse<EsphomeVersions>(await apiFetch('./ui/api/esphome-versions'), 'fetching ESPHome versions');
}

export async function refreshEsphomeVersions(): Promise<EsphomeVersions> {
  return parseResponse<EsphomeVersions>(
    await apiFetch('./ui/api/esphome-versions/refresh', { method: 'POST' }),
    'refreshing ESPHome versions',
  );
}

/** SE.8: retry the server-side ESPHome install — wired to the banner's
 * Retry button. Returns immediately; the UI polls /ui/api/server-info
 * for the transition from installing/failed → ready. */
export async function reinstallEsphome(): Promise<void> {
  await expectOk(await apiFetch('./ui/api/esphome/reinstall', { method: 'POST' }),
    'retrying ESPHome install');
}

export async function setEsphomeVersion(version: string): Promise<void> {
  await expectOk(await apiFetch('./ui/api/esphome-version', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ version }),
  }), 'setting ESPHome version');
}

export async function getTargets(): Promise<Target[]> {
  return parseResponse<Target[]>(await apiFetch('./ui/api/targets'), 'fetching targets');
}

export async function getDevices(): Promise<Device[]> {
  return parseResponse<Device[]>(await apiFetch('./ui/api/devices'), 'fetching devices');
}

export async function getWorkers(): Promise<Worker[]> {
  return parseResponse<Worker[]>(await apiFetch('./ui/api/workers'), 'fetching workers');
}

export async function getQueue(): Promise<Job[]> {
  return parseResponse<Job[]>(await apiFetch('./ui/api/queue'), 'fetching queue');
}

/**
 * Trigger a compile run.
 *
 * @param targets       'all', 'outdated', or an explicit list of YAML filenames
 * @param pinnedClientId optional — pin every job to one specific worker
 * @param esphomeVersion optional — override the global default ESPHome version
 *                        for this run only (#16). The server does NOT mutate
 *                        the global default; it just stamps the version onto
 *                        the enqueued jobs.
 */
export async function compile(
  targets: string[] | 'all' | 'outdated',
  pinnedClientId?: string,
  esphomeVersion?: string,
  downloadOnly?: boolean,
  // Bug #97: per-job worker_tag_filter from the Upgrade modal's "Tag
  // expression" worker-selection radio. Mutually exclusive with
  // ``pinnedClientId`` at the UI level (the radio toggles between the
  // two modes); the server accepts both fields independently.
  workerTagFilter?: { op: 'all_of' | 'any_of' | 'none_of'; tags: string[] },
  // Bug #110: when the user explicitly chose a worker / tag expression
  // that conflicts with an active routing rule and confirmed the
  // warning, this flag tells the server to ignore routing rules for
  // *this* job. The user's tag-filter / pin still applies — those are
  // their explicit constraint, not the rule's.
  bypassRoutingRules?: boolean,
  // DM.3: per-job OTA address override. Goes through the same
  // ``Job.ota_address`` field the rename auto-recompile already uses.
  // Single-target only — server returns 400 on multi-target + address.
  address?: string,
): Promise<CompileResponse> {
  const body: Record<string, unknown> = { targets };
  if (pinnedClientId) body.pinned_client_id = pinnedClientId;
  if (esphomeVersion) body.esphome_version = esphomeVersion;
  if (downloadOnly) body.download_only = true;
  if (workerTagFilter && workerTagFilter.tags.length > 0) {
    body.worker_tag_filter = workerTagFilter;
  }
  if (bypassRoutingRules) body.bypass_routing_rules = true;
  if (address && address.trim()) body.address = address.trim();
  return parseResponse<CompileResponse>(
    await apiFetch('./ui/api/compile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
    'enqueuing compile',
  );
}

export async function cancelJobs(ids: string[]): Promise<CancelResponse> {
  return parseResponse<CancelResponse>(
    await apiFetch('./ui/api/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_ids: ids }),
    }),
    'cancelling jobs',
  );
}

export async function retryJobs(ids: string[] | 'all_failed'): Promise<RetryResponse> {
  return parseResponse<RetryResponse>(
    await apiFetch('./ui/api/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_ids: ids }),
    }),
    'retrying jobs',
  );
}

export async function retryAllFailed(): Promise<RetryResponse> {
  return retryJobs('all_failed');
}

export async function removeJobs(ids: string[]): Promise<RemoveResponse> {
  return parseResponse<RemoveResponse>(
    await apiFetch('./ui/api/queue/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    }),
    'removing jobs',
  );
}

export async function clearQueue(
  states: string[],
  requireOtaSuccess?: boolean,
): Promise<ClearResponse> {
  const body: Record<string, unknown> = { states };
  if (requireOtaSuccess) body.require_ota_success = true;
  return parseResponse<ClearResponse>(
    await apiFetch('./ui/api/queue/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
    'clearing queue',
  );
}

export async function getTargetContent(filename: string): Promise<string> {
  const data = await parseResponse<{ content?: string }>(
    await apiFetch(`./ui/api/targets/${encodeURIComponent(filename)}/content`),
    'fetching target content',
  );
  return data.content || '';
}

/**
 * Save YAML content to a target file. Returns the final target name,
 * which may differ from *filename* when saving a staged new device
 * (#62 — ``.pending.<name>.yaml`` → ``<name>.yaml`` on first save).
 *
 * Bug #24: ``commitMessage`` is an optional user-entered subject line
 * that's passed to the auto-commit. When omitted (or auto-commit is
 * off) the server's default ``"save: <file>"`` applies.
 */
export async function saveTargetContent(
  filename: string,
  content: string,
  commitMessage?: string,
  skipCommit?: boolean,
): Promise<{ renamedTo: string | null }> {
  const body: Record<string, unknown> = { content };
  if (commitMessage && commitMessage.trim()) body.commit_message = commitMessage.trim();
  if (skipCommit) body.skip_commit = true;
  const data = await parseResponse<SaveTargetResponse>(
    await apiFetch(`./ui/api/targets/${encodeURIComponent(filename)}/content`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
    'saving target content',
  );
  return { renamedTo: data.renamed_to ?? null };
}

export async function disableWorker(id: string, disabled: boolean): Promise<void> {
  await expectOk(await apiFetch(`./ui/api/workers/${id}/disable`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ disabled }),
  }), 'updating worker');
}

export async function setWorkerParallelJobs(id: string, maxParallelJobs: number): Promise<void> {
  await expectOk(await apiFetch(`./ui/api/workers/${id}/parallel-jobs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ max_parallel_jobs: maxParallelJobs }),
  }), 'setting worker parallel-jobs');
}

// DQ.5: per-worker disk-quota override. ``null`` clears the override
// so the worker inherits ``default_worker_disk_quota_bytes``.
export async function setWorkerDiskQuota(
  id: string,
  diskQuotaBytes: number | null,
): Promise<void> {
  await expectOk(await apiFetch(`./ui/api/workers/${id}/disk-quota`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ disk_quota_bytes: diskQuotaBytes }),
  }), 'setting worker disk-quota');
}

export async function cleanWorkerCache(id: string): Promise<void> {
  await expectOk(
    await apiFetch(`./ui/api/workers/${id}/clean`, { method: 'POST' }),
    'cleaning worker cache',
  );
}

export async function removeWorker(id: string): Promise<void> {
  await expectOk(
    await apiFetch(`./ui/api/workers/${id}`, { method: 'DELETE' }),
    'removing worker',
  );
}

// #109: diagnostics — return type mirrors the backend's
// `X-Diagnostics-Ok: 1|0` header so the UI can surface a real dump
// vs. an error string using the same download path.
export interface DiagnosticsResponse {
  ok: boolean;
  filename: string;
  body: string;
}

async function readDiagnosticsResponse(resp: Response): Promise<DiagnosticsResponse> {
  const ok = resp.headers.get('X-Diagnostics-Ok') === '1';
  const disp = resp.headers.get('Content-Disposition') || '';
  const match = disp.match(/filename="([^"]+)"/);
  const filename = match ? match[1] : 'diagnostics.txt';
  const body = await resp.text();
  return { ok, filename, body };
}

/** Run py-spy on the server process and return the thread dump text. */
export async function requestServerDiagnostics(): Promise<DiagnosticsResponse> {
  const resp = await apiFetch('./ui/api/diagnostics/server', { method: 'POST' });
  await expectOk(resp, 'requesting server diagnostics');
  return readDiagnosticsResponse(resp);
}

/**
 * Ask the server to pull a thread dump from a worker, then poll until
 * the worker uploads it. Workers reply via heartbeat/control poll so
 * the round-trip lands in under ~2 s for online workers. `timeoutMs`
 * caps the total wait for a reply — 30 s covers a worker that has to
 * wait out the 10 s heartbeat window plus a slow py-spy.
 */
export async function requestWorkerDiagnostics(
  id: string,
  { timeoutMs = 30_000, pollIntervalMs = 500 }: { timeoutMs?: number; pollIntervalMs?: number } = {},
): Promise<DiagnosticsResponse> {
  const reqResp = await apiFetch(`./ui/api/workers/${id}/request-diagnostics`, { method: 'POST' });
  await expectOk(reqResp, 'starting worker diagnostics request');
  const { request_id } = (await reqResp.json()) as { request_id: string };

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const r = await apiFetch(`./ui/api/workers/${id}/diagnostics/${request_id}`);
    if (r.status === 200) {
      return readDiagnosticsResponse(r);
    }
    if (r.status !== 202) {
      await expectOk(r, 'polling worker diagnostics');
    }
    await new Promise(resolve => setTimeout(resolve, pollIntervalMs));
  }
  throw new Error(`Worker did not return a diagnostics dump within ${Math.round(timeoutMs / 1000)} s.`);
}

export async function getJobLog(jobId: string, offset: number): Promise<JobLogResponse> {
  return parseResponse<JobLogResponse>(
    await apiFetch(`./ui/api/jobs/${jobId}/log?offset=${offset}`),
    'fetching job log',
  );
}


export async function getApiKey(filename: string): Promise<string> {
  // Fetched separately rather than via parseResponse because the success
  // branch needs to validate the `key` field is actually present (QS.4).
  const r = await apiFetch(`./ui/api/targets/${encodeURIComponent(filename)}/api-key`);
  const data = await parseResponse<ApiKeyResponse>(r, 'fetching API key');
  if (!data.key) throw new Error('Server did not return an API key');
  return data.key;
}

/**
 * Validate a target's config via ``esphome config`` (direct subprocess on
 * the server). Returns immediately with the output — no queue, no polling.
 *
 * Bug #25: previously this enqueued a validate-only job on the queue and
 * any worker could pick it up; now it runs directly on the server.
 */
/**
 * RC.1 — fetch the YAML *as ESPHome will compile it* for *target*.
 *
 * Returns the rendered YAML on success or the captured stdout (which
 * holds the parser/validator's error message) on failure. The
 * ``cached`` field is informational: the cache key is `(filename, file
 * mtime, secrets.yaml mtime)` so any save/commit on either file busts
 * the entry automatically.
 *
 * IMPORTANT: the response carries plaintext ``!secret`` values. Don't
 * persist it client-side beyond the modal's lifetime.
 */
export async function getRenderedConfig(target: string): Promise<{ success: boolean; output: string; cached: boolean }> {
  const r = await apiFetch(`./ui/api/targets/${encodeURIComponent(target)}/rendered-config`);
  let data: { success?: boolean; output?: string; cached?: boolean; error?: string };
  try {
    data = await r.json() as typeof data;
  } catch {
    if (!r.ok) throw new Error(`rendering failed (HTTP ${r.status})`);
    throw new Error('rendered-config response was not valid JSON');
  }
  if (!r.ok && r.status !== 200) {
    // 503 (ESPHome installing) carries a useful body; surface it
    // through the same shape so the modal can render the message.
    if (data.output) return { success: false, output: data.output, cached: false };
    throw new Error(data.error || `rendering failed (HTTP ${r.status})`);
  }
  return { success: !!data.success, output: data.output || '', cached: !!data.cached };
}

export async function validateConfig(target: string): Promise<{ success: boolean; output: string }> {
  // Bespoke handling: validate may return non-OK status with a useful `output`
  // body (e.g. "config has 3 errors..."). We fall through to .output on error.
  // CR.5: parsing the body might itself throw (non-JSON on a 500, truncated
  // response, etc.). Isolate the parse so we always surface a meaningful
  // error string rather than a swallowed `SyntaxError: Unexpected token`.
  const r = await apiFetch('./ui/api/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target }),
  });
  let data: ValidateResponse;
  try {
    data = await r.json() as ValidateResponse;
  } catch {
    if (!r.ok) throw new Error(`validation failed (HTTP ${r.status})`);
    throw new Error('validate response was not valid JSON');
  }
  if (!r.ok) throw new Error(data.error || data.output || `validation failed (HTTP ${r.status})`);
  return { success: !!data.success, output: data.output || '' };
}

// CR.5: `getSecretKeys` and `getEsphomeSchema` used to silently return []
// on any error, which looked like "no autocomplete suggestions" to the user
// — the editor appeared to work but autocomplete was dead. Throw instead
// so the SWR `onError` path (QS.7's `logSwrError`) logs it with the key
// attached and the caller can surface a real error state.
export async function getSecretKeys(): Promise<string[]> {
  const r = await apiFetch('./ui/api/secret-keys');
  const data = await parseResponse<SecretKeysResponse>(r, 'getSecretKeys');
  return data.keys || [];
}

export async function getEsphomeSchema(): Promise<string[]> {
  const r = await apiFetch('./ui/api/esphome-schema');
  const data = await parseResponse<EsphomeSchemaResponse>(r, 'getEsphomeSchema');
  return data.components || [];
}

export interface ArchivedConfig {
  filename: string;
  size: number;
  archived_at: number;
}

export async function getArchivedConfigs(): Promise<ArchivedConfig[]> {
  return parseResponse<ArchivedConfig[]>(
    await apiFetch('./ui/api/archive'),
    'fetching archived configs',
  );
}

export async function restoreArchivedConfig(filename: string): Promise<void> {
  await expectOk(
    await apiFetch(`./ui/api/archive/${encodeURIComponent(filename)}/restore`, { method: 'POST' }),
    'restoring archived config',
  );
}

export async function deleteArchivedConfig(filename: string): Promise<void> {
  await expectOk(
    await apiFetch(`./ui/api/archive/${encodeURIComponent(filename)}`, { method: 'DELETE' }),
    'deleting archived config',
  );
}

/**
 * Create a new YAML target (CD.3). Without ``source``, creates a minimal
 * stub YAML. With ``source``, duplicates the source file and rewrites
 * ``esphome.name`` to the new filename. Returns the created target name,
 * which is staged as ``.pending.<name>.yaml`` until the first save promotes
 * it to ``<name>.yaml`` (#62). Cancelling the editor deletes the dotfile.
 */
export async function createTarget(
  filename: string,
  source?: string,
): Promise<string> {
  const body: Record<string, string> = { filename };
  if (source) body.source = source;
  const data = await parseResponse<CreateTargetResponse>(
    await apiFetch('./ui/api/targets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
    'creating target',
  );
  if (!data.target) throw new Error('Server did not return a target name');
  return data.target;
}

export async function deleteTarget(filename: string, archive = true): Promise<void> {
  await expectOk(
    await apiFetch(
      `./ui/api/targets/${encodeURIComponent(filename)}?archive=${archive}`,
      { method: 'DELETE' },
    ),
    'deleting target',
  );
}

export async function restartDevice(filename: string): Promise<void> {
  await expectOk(
    await apiFetch(
      `./ui/api/targets/${encodeURIComponent(filename)}/restart`,
      { method: 'POST' },
    ),
    'restarting device',
  );
}

// DM.2: ICMP ping result returned by /ui/api/targets/{filename}/ping.
// Worst-case ~3.8 s wall time on an unreachable host; resolve_ota_address
// resolution is the same one the OTA path uses so we hit what an upload
// would target.
export interface PingResult {
  target: string;
  address: string;
  ran_at: number;
  is_alive: boolean;
  packets_sent: number;
  packets_received: number;
  packet_loss: number;
  min_rtt: number;
  avg_rtt: number;
  max_rtt: number;
  jitter: number;
}

export async function pingDevice(filename: string): Promise<PingResult> {
  return parseResponse<PingResult>(
    await apiFetch(
      `./ui/api/targets/${encodeURIComponent(filename)}/ping`,
      { method: 'POST' },
    ),
    'pinging device',
  );
}

export async function renameTarget(
  filename: string,
  newName: string,
): Promise<RenameTargetResponse> {
  return parseResponse<RenameTargetResponse>(
    await apiFetch(
      `./ui/api/targets/${encodeURIComponent(filename)}/rename`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: newName }),
      },
    ),
    'renaming target',
  );
}

// ---------------------------------------------------------------------------
// Per-device metadata + schedule
// ---------------------------------------------------------------------------

export async function updateTargetMeta(
  filename: string,
  meta: Record<string, unknown>,
): Promise<void> {
  await expectOk(await apiFetch(
    `./ui/api/targets/${encodeURIComponent(filename)}/meta`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(meta),
    },
  ), 'updating target metadata');
}

/**
 * TG.4: authoritative worker-tag edit. Server normalises (trim / drop
 * empties / dedupe) and persists to ``/data/worker-tags.json``.
 */
export async function setWorkerTags(
  clientId: string,
  tags: string[],
): Promise<void> {
  await expectOk(await apiFetch(
    `./ui/api/workers/${encodeURIComponent(clientId)}/tags`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tags }),
    },
  ), 'setting worker tags');
}

// ---------------------------------------------------------------------------
// TG.4 / TG.8 — routing rule CRUD
// ---------------------------------------------------------------------------

import type { RoutingRule } from '../types';

export async function getRoutingRules(): Promise<RoutingRule[]> {
  const data = await parseResponse<{ rules: RoutingRule[] }>(
    await apiFetch('./ui/api/routing-rules'),
    'fetching routing rules',
  );
  return data.rules ?? [];
}

export async function createRoutingRule(rule: Omit<RoutingRule, 'id'> & { id?: string }): Promise<RoutingRule> {
  return parseResponse<RoutingRule>(
    await apiFetch('./ui/api/routing-rules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rule),
    }),
    'creating routing rule',
  );
}

export async function updateRoutingRule(id: string, rule: RoutingRule): Promise<RoutingRule> {
  return parseResponse<RoutingRule>(
    await apiFetch(`./ui/api/routing-rules/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rule),
    }),
    'updating routing rule',
  );
}

export async function deleteRoutingRule(id: string): Promise<void> {
  await expectOk(await apiFetch(`./ui/api/routing-rules/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  }), 'deleting routing rule');
}

export async function setTargetSchedule(
  filename: string,
  cron: string,
  tz?: string,
): Promise<ScheduleResponse> {
  const data = await parseResponse<Partial<ScheduleResponse>>(
    await apiFetch(
      `./ui/api/targets/${encodeURIComponent(filename)}/schedule`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(tz ? { cron, tz } : { cron }),
      },
    ),
    'setting schedule',
  );
  return { schedule_enabled: data.schedule_enabled ?? true };
}

export async function deleteTargetSchedule(filename: string): Promise<void> {
  await expectOk(
    await apiFetch(
      `./ui/api/targets/${encodeURIComponent(filename)}/schedule`,
      { method: 'DELETE' },
    ),
    'deleting schedule',
  );
}

export async function toggleTargetSchedule(filename: string): Promise<ScheduleResponse> {
  const data = await parseResponse<Partial<ScheduleResponse>>(
    await apiFetch(
      `./ui/api/targets/${encodeURIComponent(filename)}/schedule/toggle`,
      { method: 'POST' },
    ),
    'toggling schedule',
  );
  return { schedule_enabled: data.schedule_enabled ?? false };
}

// ---------------------------------------------------------------------------
// Version pinning
// ---------------------------------------------------------------------------

export async function pinTargetVersion(filename: string, version: string): Promise<void> {
  await expectOk(await apiFetch(
    `./ui/api/targets/${encodeURIComponent(filename)}/pin`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version }),
    },
  ), 'pinning version');
}

export async function unpinTargetVersion(filename: string): Promise<void> {
  await expectOk(
    await apiFetch(
      `./ui/api/targets/${encodeURIComponent(filename)}/pin`,
      { method: 'DELETE' },
    ),
    'unpinning version',
  );
}

export async function setTargetScheduleOnce(filename: string, datetime: string): Promise<void> {
  await expectOk(await apiFetch(
    `./ui/api/targets/${encodeURIComponent(filename)}/schedule/once`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ datetime }),
    },
  ), 'setting one-time schedule');
}

export interface ScheduleHistoryEntry {
  fired_at: string;
  job_id: string;
  outcome: string;
}

// ---------------------------------------------------------------------------
// JH.4 — Job history (persistent append-only)
// ---------------------------------------------------------------------------

/** One row from the persistent /ui/api/history table. Mirrors the SQL
 *  shape in ha-addon/server/job_history.py — keep in sync. */
export interface JobHistoryEntry {
  id: string;
  target: string;
  state: 'success' | 'failed' | 'cancelled' | 'timed_out';
  // PR #64 review: server's `_triggered_by` in `job_history.py` also
  // emits `'api'` for direct system-token callers (the non-HA bearer
  // path). Type has to include it so the Triggered-badge helpers and
  // filters don't narrow incorrectly.
  triggered_by: 'user' | 'schedule' | 'ha_action' | 'api' | null;
  trigger_detail: string | null;
  download_only: 0 | 1;
  validate_only: 0 | 1;
  pinned_client_id: string | null;
  esphome_version: string | null;
  assigned_client_id: string | null;
  assigned_hostname: string | null;
  /** Epoch seconds (UTC). */
  submitted_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  duration_seconds: number | null;
  ota_result: string | null;
  config_hash: string | null;
  retry_count: number;
  log_excerpt: string | null;
  /** Bug #38: 1 when the job produced firmware. Stays 1 even after the
   *  .bin has been evicted by the firmware budget task — use
   *  `firmware_variants.length > 0` to know whether the binary is
   *  still downloadable right now.
   *
   *  PR #64 review: server always includes this field in the SELECT
   *  projection, so it's required (not optional). Keeping it optional
   *  would force defensive `?? 0` chains in callers and mask contract
   *  regressions if the server ever stops emitting it. */
  has_firmware: 0 | 1;
  /** Bug #38: live list of variants still on disk (e.g. ["factory","ota"]).
   *  Empty when has_firmware is 0, OR when the firmware has been evicted
   *  by the budget enforcer. Drives the Download button's visibility on
   *  history rows. Server always emits a list (possibly empty). */
  firmware_variants: string[];
  /** Bug #8 (1.6.1): worker-selection reason persisted at claim time.
   *  ``null`` on rows that predate the column. */
  selection_reason: string | null;
}

export interface JobHistoryStats {
  total: number;
  success: number;
  failed: number;
  cancelled: number;
  timed_out: number;
  avg_duration_seconds: number | null;
  p95_duration_seconds: number | null;
  last_success_at: number | null;
  last_failure_at: number | null;
  window_days: number;
}

export async function getJobHistory(params: {
  target?: string;
  state?: JobHistoryEntry['state'];
  since?: number;
  /** Bug #49: upper epoch bound for the finished-at window. */
  until?: number;
  limit?: number;
  offset?: number;
  /** Bug #53: column to sort by. Server whitelist enforces valid values. */
  sort?: 'finished_at' | 'started_at' | 'submitted_at' | 'duration_seconds'
    | 'target' | 'state' | 'esphome_version' | 'assigned_hostname' | 'triggered_by';
  /** Bug #53: ``true`` for descending (default). */
  desc?: boolean;
} = {}): Promise<JobHistoryEntry[]> {
  const qs = new URLSearchParams();
  if (params.target) qs.set('target', params.target);
  if (params.state) qs.set('state', params.state);
  if (params.since !== undefined) qs.set('since', String(params.since));
  if (params.until !== undefined) qs.set('until', String(params.until));
  if (params.limit !== undefined) qs.set('limit', String(params.limit));
  if (params.offset !== undefined) qs.set('offset', String(params.offset));
  if (params.sort) qs.set('sort', params.sort);
  if (params.desc !== undefined) qs.set('desc', params.desc ? '1' : '0');
  const url = qs.toString() ? `./ui/api/history?${qs}` : './ui/api/history';
  return parseResponse<JobHistoryEntry[]>(await apiFetch(url), 'fetching job history');
}

export async function getJobHistoryStats(params: {
  target?: string;
  window_days?: number;
} = {}): Promise<JobHistoryStats> {
  const qs = new URLSearchParams();
  if (params.target) qs.set('target', params.target);
  if (params.window_days !== undefined) qs.set('window_days', String(params.window_days));
  const url = qs.toString() ? `./ui/api/history/stats?${qs}` : './ui/api/history/stats';
  return parseResponse<JobHistoryStats>(await apiFetch(url), 'fetching job-history stats');
}

export async function getScheduleHistory(): Promise<Record<string, ScheduleHistoryEntry[]>> {
  const r = await apiFetch('./ui/api/schedule-history');
  // CR.5/UI-6: route through parseResponse so SWR's onError path logs
  // the failure with the endpoint name attached, instead of silently
  // reporting an empty map.
  return parseResponse<Record<string, ScheduleHistoryEntry[]>>(r, 'getScheduleHistory');
}
