import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { toast } from 'sonner';
import { Copy, Eye, EyeOff } from 'lucide-react';

import {
  commitFile,
  getSettings,
  updateSettings,
  type AppSettings,
} from '@/api/client';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import {
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';

// SP.4: the in-app Settings drawer.
//
// Sectioned so the shape scales as more settings land in later releases.
// Save-on-change — no bulk Save button. Each row owns its draft state
// locally, commits on blur (numeric fields) or change (switch), and
// surfaces validation errors as a toast.

interface SettingsDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Bug #17: list of filenames that currently have uncommitted
   * changes. When the user flips auto-commit from on → off with
   * any dirty targets in this list, we show a confirmation dialog
   * offering to commit them first. Empty array when everything's
   * clean or the repo isn't a git repo at all. */
  dirtyTargets?: string[];
  /** #109: the host App owns the toast flow + download trigger for
   * "Request diagnostics", so the drawer just invokes this. */
  onRequestServerDiagnostics?: () => void;
}

export function SettingsDrawer({ open, onOpenChange, dirtyTargets = [], onRequestServerDiagnostics }: SettingsDrawerProps) {
  const { data, error, isLoading, mutate } = useSWR<AppSettings>(
    open ? 'settings' : null,
    getSettings,
    { revalidateOnFocus: false },
  );

  // Bug #21 (supersedes #17): confirmation state for the
  // auto-commit-toggle-on prompt. Fires when the user flips the
  // toggle from OFF → ON with dirty files: from that point on all
  // future saves will auto-commit, but the existing uncommitted
  // state won't unless it gets touched. The prompt asks whether to
  // commit those stragglers before the new behavior kicks in.
  const [turnOnOpen, setTurnOnOpen] = useState(false);
  const [turnOnBusy, setTurnOnBusy] = useState(false);
  // #96: split drawer into Basic / Advanced. "Basic" hosts the
  // settings Pat reaches for often (versioning toggle, auth, display
  // preferences). "Advanced" hosts the plumbing knobs (retention,
  // cache sizes, timeouts, polling). Both live in the same drawer —
  // just behind a tab strip so the default view is short.
  const [activeTab, setActiveTab] = useState<'basic' | 'advanced'>('basic');

  async function patch(partial: Partial<AppSettings>): Promise<boolean> {
    // Bug #21: intercept the auto-commit flip-to-ON when there are
    // uncommitted changes. Instead of patching straight away, we open
    // the confirmation dialog; its buttons will finish the PATCH.
    if (
      partial.auto_commit_on_save === true
      && data?.auto_commit_on_save === false
      && dirtyTargets.length > 0
    ) {
      setTurnOnOpen(true);
      return false;
    }
    try {
      const updated = await updateSettings(partial);
      await mutate(updated, false);
      toast.success('Setting saved');
      return true;
    } catch (err) {
      toast.error((err as Error).message);
      await mutate();
      return false;
    }
  }

  async function patchRaw(partial: Partial<AppSettings>): Promise<void> {
    // Bypass the dirty-check — used by the confirmation dialog's
    // "Turn off anyway" / "Commit and turn off" branches.
    const updated = await updateSettings(partial);
    await mutate(updated, false);
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <div>
            <SheetTitle>Settings</SheetTitle>
            <SheetDescription>Changes take effect immediately.</SheetDescription>
          </div>
        </SheetHeader>
        <SheetBody>
          {error && (
            <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-400">
              Failed to load settings: {error.message}
            </div>
          )}
          {isLoading && <div className="text-xs text-[var(--text-muted)]">Loading…</div>}
          {data && (
            <div className="flex flex-col gap-6">
              {/* #96: two-tab split. Keep the tab strip sticky so it
                  stays visible as the user scrolls the Advanced body. */}
              <div className="sticky top-0 z-10 -mx-4 -mt-3 bg-[var(--surface)] px-4 pt-3 pb-2 border-b border-[var(--border)] flex gap-1">
                <button
                  type="button"
                  onClick={() => setActiveTab('basic')}
                  className={`px-3 py-1 text-xs rounded-md cursor-pointer ${
                    activeTab === 'basic'
                      ? 'bg-[var(--surface2)] text-[var(--text)] font-semibold'
                      : 'text-[var(--text-muted)] hover:text-[var(--text)]'
                  }`}
                >
                  Basic
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab('advanced')}
                  className={`px-3 py-1 text-xs rounded-md cursor-pointer ${
                    activeTab === 'advanced'
                      ? 'bg-[var(--surface2)] text-[var(--text)] font-semibold'
                      : 'text-[var(--text-muted)] hover:text-[var(--text)]'
                  }`}
                >
                  Advanced
                </button>
              </div>
              {activeTab === 'basic' && <>
              <Section title="Config versioning">
                {/* #97 + #98: master tristate. UI presents it as a
                    BoolRow because Pat sees "on vs off" and the
                    ``'unset'`` state is only meaningful as "the
                    onboarding modal will ask on next load". A user
                    flipping this toggle in Settings is always an
                    explicit on/off choice, never unset — the modal
                    is the only producer of ``'unset'`` → ``'on'`` /
                    ``'off'`` transitions. */}
                <BoolRow
                  label="Enable versioning"
                  help="Turn on the local git-backed history for /config/esphome/ — per-file history, diff, and rollback. Off disables all git operations."
                  value={data.versioning_enabled === 'on'}
                  onChange={v => patch({ versioning_enabled: v ? 'on' : 'off' })}
                />
                <div
                  className={data.versioning_enabled === 'on' ? 'flex flex-col gap-6' : 'flex flex-col gap-6 opacity-50 pointer-events-none'}
                  aria-disabled={data.versioning_enabled !== 'on'}
                >
                  <BoolRow
                    label="Auto-commit on save"
                    help="Every save creates a local git commit in /config/esphome/. Turn off if you manage this directory with your own git workflow."
                    value={data.auto_commit_on_save}
                    onChange={v => patch({ auto_commit_on_save: v })}
                  />
                  <StringRow
                    label="Commit author name"
                    help="Used on Fleet-created commits. If /config/esphome/ has its own user.name set (per-repo, global, or system), that wins."
                    maxLength={100}
                    value={data.git_author_name}
                    onCommit={v => patch({ git_author_name: v })}
                  />
                  <StringRow
                    label="Commit author email"
                    help="Paired with the name above. Free-form — no format validation."
                    maxLength={256}
                    value={data.git_author_email}
                    onCommit={v => patch({ git_author_email: v })}
                  />
                </div>
              </Section>
              <Section title="Authentication">
                <SecretRow
                  label="Server token"
                  help="Shared bearer token for build workers and direct-port API access. Changing this will disconnect existing workers until their SERVER_TOKEN env var is updated."
                  value={data.server_token}
                  onCommit={v => patch({ server_token: v })}
                />
                <BoolRow
                  label="Require Home Assistant auth on direct port"
                  help="When on, requests to port 8765 (outside the Home Assistant Ingress tunnel) must carry a valid HA bearer token or this server token. Defaults to off so standalone Docker installs on trusted networks work without a token; turn it on if the direct port is reachable from an untrusted network."
                  value={data.require_ha_auth}
                  onChange={v => patch({ require_ha_auth: v })}
                />
              </Section>
              {/* #82 / UX_REVIEW §3.10 — time-of-day format. Applied
                  app-wide by App.tsx via ``setTimeFormatPref`` the
                  moment this dropdown commits. ``auto`` defers to the
                  browser's resolved locale (hour12 from
                  ``Intl.DateTimeFormat().resolvedOptions()``); ``12h``
                  / ``24h`` override it. */}
              <Section title="Display">
                <EnumRow
                  label="Time format"
                  help="How times render in the Queue, History, and log timestamps."
                  value={data.time_format}
                  options={[
                    { value: 'auto', label: 'Auto (follow browser locale)' },
                    { value: '24h', label: '24-hour (13:45:09)' },
                    { value: '12h', label: '12-hour (1:45:09 PM)' },
                  ]}
                  onCommit={v => patch({ time_format: v as 'auto' | '12h' | '24h' })}
                />
                {/* Bug #5: date format companion to the time format above. */}
                <EnumRow
                  label="Date format"
                  help="How absolute dates render in row tooltips, the Queue, and History."
                  value={data.date_format}
                  options={[
                    { value: 'auto', label: 'Auto (follow browser locale)' },
                    { value: 'iso', label: 'ISO (2026-04-27)' },
                    { value: 'us', label: 'US (4/27/2026)' },
                    { value: 'eu', label: 'EU (27/04/2026)' },
                    { value: 'long', label: 'Long (Apr 27, 2026)' },
                  ]}
                  onCommit={v => patch({ date_format: v as 'auto' | 'iso' | 'us' | 'eu' | 'long' })}
                />
                {/* I18N.2 (#141) — UI language. 'auto' follows the
                    browser's preferred locale (`navigator.language`);
                    explicit values override. At I18N.2 the catalogs
                    are still empty, so every value renders the same
                    English literals — picking Deutsch becomes
                    visible once I18N.4/I18N.9 land. */}
                <EnumRow
                  label="Language"
                  help="Interface language. Translations land progressively across 1.7.2."
                  value={data.language}
                  options={[
                    { value: 'auto', label: 'Auto (follow browser locale)' },
                    { value: 'en', label: 'English' },
                    { value: 'de', label: 'Deutsch' },
                  ]}
                  onCommit={v => patch({ language: v as 'auto' | 'en' | 'de' })}
                />
                {/* #145 — font size scale. 'normal' is byte-identical to
                    pre-#145; 'small' fits the UI to a sub-100 % browser
                    zoom; 'large' is the accessibility step up. */}
                <EnumRow
                  label="Font size"
                  help="Scales the whole UI proportionally. Pick Small if you run Home Assistant at a lower browser zoom."
                  value={data.font_size}
                  options={[
                    { value: 'small', label: 'Small' },
                    { value: 'normal', label: 'Normal' },
                    { value: 'large', label: 'Large' },
                  ]}
                  onCommit={v => patch({ font_size: v as 'small' | 'normal' | 'large' })}
                />
              </Section>
              </>}
              {activeTab === 'advanced' && <>
              <Section title="Job history">
                <IntRow
                  label="Retention (days)"
                  help="How long to keep per-job compile history. 0 = unlimited."
                  min={0}
                  max={3650}
                  defaultValue={365}
                  value={data.job_history_retention_days}
                  onCommit={v => patch({ job_history_retention_days: v })}
                />
              </Section>
              <Section title="Disk management">
                <NumRow
                  label="Firmware cache size (GB)"
                  help="Maximum disk space the server will use to cache compiled firmware binaries."
                  min={0.1}
                  max={1024}
                  step={0.1}
                  defaultValue={2.0}
                  value={data.firmware_cache_max_gb}
                  onCommit={v => patch({ firmware_cache_max_gb: v })}
                />
                <IntRow
                  label="Firmware retention (days)"
                  help="Delete cached firmware binaries older than this. Active queue jobs are protected. 0 = unlimited."
                  min={0}
                  max={3650}
                  defaultValue={2}
                  value={data.firmware_retention_days}
                  onCommit={v => patch({ firmware_retention_days: v })}
                />
                <IntRow
                  label="Job log retention (days)"
                  help="How long to keep per-job build logs on disk. 0 = unlimited."
                  min={0}
                  max={3650}
                  defaultValue={30}
                  value={data.job_log_retention_days}
                  onCommit={v => patch({ job_log_retention_days: v })}
                />
                <IntRow
                  label="Worker disk quota — fleet default (GiB)"
                  help="Per-worker cap on the /esphome-versions/ tree (ESPHome venvs + PlatformIO toolchains + per-target build caches). LRU-evicted between jobs. Per-worker overrides live on each worker's row."
                  min={1}
                  max={1024}
                  defaultValue={10}
                  value={Math.round(data.default_worker_disk_quota_bytes / 1024 ** 3)}
                  onCommit={v => patch({ default_worker_disk_quota_bytes: v * 1024 ** 3 })}
                />
              </Section>
              <Section title="Timeouts">
                <IntRow
                  label="Job timeout (seconds)"
                  help="Maximum wall-clock seconds a single compile job may run before the server marks it timed-out."
                  min={60}
                  max={14400}
                  defaultValue={600}
                  value={data.job_timeout}
                  onCommit={v => patch({ job_timeout: v })}
                />
                <IntRow
                  label="OTA timeout (seconds)"
                  help="Maximum seconds for the OTA upload to a device after a successful compile."
                  min={15}
                  max={1800}
                  defaultValue={120}
                  value={data.ota_timeout}
                  onCommit={v => patch({ ota_timeout: v })}
                />
                <IntRow
                  label="Worker offline threshold (seconds)"
                  help="Seconds without a worker heartbeat before it's flagged offline in the Workers tab."
                  min={15}
                  max={3600}
                  defaultValue={30}
                  value={data.worker_offline_threshold}
                  onCommit={v => patch({ worker_offline_threshold: v })}
                />
              </Section>
              <Section title="Polling">
                <IntRow
                  label="Device poll interval (seconds)"
                  help="How often the server polls each ESPHome device over its native API to refresh online status and running-firmware version."
                  min={10}
                  max={3600}
                  defaultValue={60}
                  value={data.device_poll_interval}
                  onCommit={v => patch({ device_poll_interval: v })}
                />
              </Section>
              {/* DM.1: archived devices live inline in the Devices
                  tab — toggle column picker → Show archived devices. */}
              {/* #109: Diagnostics — one-click thread dump of the
                  server process. Intentionally plain — no knobs, no
                  tabs-within-tabs; the whole flow is click → download
                  a .txt file. */}
              {onRequestServerDiagnostics && (
                <Section title="Diagnostics">
                  <div className="flex flex-col gap-2">
                    <p className="text-xs text-[var(--text-muted)]">
                      Capture a Python thread dump of the server process and download it as a text file.
                      Useful when reporting a hang or runaway-CPU issue.
                    </p>
                    <div>
                      <Button variant="secondary" size="sm" onClick={() => onRequestServerDiagnostics()}>
                        Request diagnostics
                      </Button>
                    </div>
                  </div>
                </Section>
              )}
              <Section title="About">
                <p className="text-xs text-[var(--text-muted)]">
                  Settings are stored in <code className="rounded bg-[var(--surface2)] px-1 py-0.5">/data/settings.json</code>{' '}
                  inside the add-on and persist across updates. Deployment-level options (token, port,{' '}
                  <code className="rounded bg-[var(--surface2)] px-1 py-0.5">require_ha_auth</code>) remain on the
                  Home Assistant add-on Configuration tab.
                </p>
              </Section>
              </>}
            </div>
          )}
        </SheetBody>
      </SheetContent>

      {/* Bug #21: confirmation when the user flips auto-commit OFF →
          ON with uncommitted changes. Subsequent saves will start
          auto-committing, but the existing dirty files won't get
          committed until the user touches them again. Offer to flush
          them now so history stays continuous. */}
      <Dialog
        open={turnOnOpen}
        onOpenChange={(o) => { if (!o && !turnOnBusy) setTurnOnOpen(false); }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn on auto-commit?</DialogTitle>
          </DialogHeader>
          <div className="px-4 py-3 text-sm text-[var(--text)]">
            <p>
              You have uncommitted changes to <strong>{dirtyTargets.length}</strong>{' '}
              file{dirtyTargets.length === 1 ? '' : 's'}. From now on every save will
              auto-commit — but those existing edits won't be committed
              automatically until you touch each file again.
            </p>
            {dirtyTargets.length > 0 && (
              <ul className="mt-2 flex flex-wrap gap-1">
                {dirtyTargets.slice(0, 8).map(t => (
                  <li key={t}>
                    <code className="rounded bg-[var(--surface2)] px-1.5 py-0.5 font-mono text-[11px]">
                      {t}
                    </code>
                  </li>
                ))}
                {dirtyTargets.length > 8 && (
                  <li className="text-xs text-[var(--text-muted)] self-center">
                    …and {dirtyTargets.length - 8} more
                  </li>
                )}
              </ul>
            )}
            <p className="mt-3 text-xs text-[var(--text-muted)]">
              Commit them now to keep history continuous, or turn on
              anyway if you'd rather commit them yourself later.
            </p>
          </div>
          <DialogFooter>
            <DialogClose>
              <Button variant="secondary" size="sm" disabled={turnOnBusy}>Cancel</Button>
            </DialogClose>
            <Button
              variant="outline"
              size="sm"
              disabled={turnOnBusy}
              onClick={async () => {
                setTurnOnBusy(true);
                try {
                  await patchRaw({ auto_commit_on_save: true });
                  toast.success('Auto-commit turned on; existing uncommitted changes left in place');
                  setTurnOnOpen(false);
                } catch (err) {
                  toast.error('Failed to update setting: ' + (err as Error).message);
                } finally {
                  setTurnOnBusy(false);
                }
              }}
            >
              Turn on anyway
            </Button>
            <Button
              size="sm"
              disabled={turnOnBusy}
              onClick={async () => {
                setTurnOnBusy(true);
                try {
                  // One commit per dirty file. Default message on each
                  // matches the manual-commit flow's (manual) marker.
                  const results = await Promise.all(
                    dirtyTargets.map(t => commitFile(t).catch(err => ({ committed: false, err: (err as Error).message, target: t }))),
                  );
                  const committed = results.filter(r => (r as { committed: boolean }).committed).length;
                  const failed = results.length - committed;
                  if (failed === 0) {
                    toast.success(`Committed ${committed} file${committed === 1 ? '' : 's'}`);
                  } else if (committed > 0) {
                    toast.info(`Committed ${committed}, ${failed} failed`);
                  } else {
                    toast.error('No files committed');
                  }
                  await patchRaw({ auto_commit_on_save: true });
                  setTurnOnOpen(false);
                } catch (err) {
                  toast.error('Failed: ' + (err as Error).message);
                } finally {
                  setTurnOnBusy(false);
                }
              }}
            >
              Commit {dirtyTargets.length} and turn on
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Sheet>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">{title}</h3>
      <div className="flex flex-col gap-3">{children}</div>
    </section>
  );
}



function Row({
  label,
  help,
  control,
  id,
}: {
  label: string;
  help?: React.ReactNode;
  control: React.ReactNode;
  id?: string;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex flex-col gap-0.5">
        <label htmlFor={id} className="text-sm text-[var(--text)]">
          {label}
        </label>
        {help && <p className="text-xs text-[var(--text-muted)]">{help}</p>}
      </div>
      <div className="shrink-0">{control}</div>
    </div>
  );
}

function BoolRow({
  label,
  help,
  value,
  onChange,
}: {
  label: string;
  help?: string;
  value: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <Row
      label={label}
      help={help}
      control={
        <Switch
          checked={value}
          onCheckedChange={(next: boolean) => onChange(next)}
          aria-label={label}
        />
      }
    />
  );
}

interface NumericRowProps {
  label: string;
  help?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  integer: boolean;
  /**
   * #80 / UX_REVIEW §3.7: the default the server ships, surfaced as
   * part of the hint so Pat knows what "normal" looks like before
   * editing. Single source of truth is ``ha-addon/server/settings.py``
   * (search for ``@dataclass class AppSettings``). Optional so the
   * prop can be omitted on rare / bespoke fields.
   */
  defaultValue?: number;
  onCommit: (v: number) => Promise<boolean>;
}

function NumericRow({
  label,
  help,
  value,
  min,
  max,
  step,
  integer,
  defaultValue,
  onCommit,
}: NumericRowProps) {
  const [draft, setDraft] = useState<string>(String(value));
  const [focused, setFocused] = useState(false);

  // When the upstream value changes (e.g., another tab updated it, or a
  // rejected patch reverted), adopt the new value — but not while the
  // user is mid-edit, so their typing isn't clobbered.
  useEffect(() => {
    if (!focused) setDraft(String(value));
  }, [value, focused]);

  async function commit() {
    setFocused(false);
    const n = Number(draft);
    const valid = Number.isFinite(n) && (!integer || Number.isInteger(n)) && n >= min && n <= max;
    if (!valid) {
      toast.error(`${label} must be ${integer ? 'an integer' : 'a number'} between ${min} and ${max}`);
      setDraft(String(value));
      return;
    }
    if (n === value) return;
    const ok = await onCommit(n);
    if (!ok) setDraft(String(value));
  }

  // #80: append bounds hint so the user sees defaults + limits next
  // to the help copy. Not a full new prop — the existing ``help`` is
  // descriptive ("what does this control?"), this is the numeric shape
  // ("what does it accept?"). Same format across every numeric row.
  const fmt = integer ? (n: number) => String(n) : (n: number) => String(n);
  const boundsParts = [
    defaultValue !== undefined ? `default ${fmt(defaultValue)}` : null,
    `min ${fmt(min)}`,
    `max ${fmt(max)}`,
  ].filter((p): p is string => p !== null);
  const composedHelp = (
    <>
      {help ? <>{help}<br /></> : null}
      <span className="opacity-70">({boundsParts.join(', ')})</span>
    </>
  );

  return (
    <Row
      label={label}
      help={composedHelp}
      control={
        <Input
          type="number"
          min={min}
          max={max}
          step={step}
          className="w-24 text-right"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={commit}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
          }}
        />
      }
    />
  );
}

function IntRow(props: Omit<NumericRowProps, 'step' | 'integer'>) {
  return <NumericRow {...props} step={1} integer={true} />;
}

function NumRow(props: Omit<NumericRowProps, 'integer'>) {
  return <NumericRow {...props} integer={false} />;
}

// #82: small enum picker row — native <select> styled via our Select
// wrapper. Commits on change since there's no "in-progress" state for a
// dropdown (unlike the NumRow where the user types).
function EnumRow({
  label,
  help,
  value,
  options,
  onCommit,
}: {
  label: string;
  help?: React.ReactNode;
  value: string;
  options: ReadonlyArray<{ value: string; label: string }>;
  onCommit: (v: string) => Promise<boolean>;
}) {
  return (
    <Row
      label={label}
      help={help}
      control={
        <Select
          className="w-[220px]"
          value={value}
          onChange={async e => {
            const v = e.target.value;
            if (v !== value) await onCommit(v);
          }}
        >
          {options.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </Select>
      }
    />
  );
}

function SecretRow({
  label,
  help,
  value,
  onCommit,
}: {
  label: string;
  help?: string;
  value: string;
  onCommit: (v: string) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState<string>(value);
  const [focused, setFocused] = useState(false);
  const [revealed, setRevealed] = useState(false);

  useEffect(() => {
    if (!focused) setDraft(value);
  }, [value, focused]);

  async function commit() {
    setFocused(false);
    const trimmed = draft.trim();
    if (!trimmed) {
      toast.error(`${label} must not be empty`);
      setDraft(value);
      return;
    }
    if (/\s/.test(trimmed)) {
      toast.error(`${label} must not contain whitespace`);
      setDraft(value);
      return;
    }
    if (trimmed === value) return;
    const ok = await onCommit(trimmed);
    if (!ok) setDraft(value);
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      toast.success('Token copied');
    } catch {
      toast.error('Clipboard copy failed');
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-3">
        <label className="text-sm text-[var(--text)]">{label}</label>
      </div>
      <div className="flex items-center gap-2">
        <Input
          type={revealed ? 'text' : 'password'}
          className="flex-1 font-mono text-xs"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={commit}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
          }}
        />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={revealed ? 'Hide token' : 'Show token'}
          title={revealed ? 'Hide token' : 'Show token'}
          onClick={() => setRevealed(r => !r)}
        >
          {revealed ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Copy token"
          title="Copy token"
          onClick={copy}
        >
          <Copy className="size-4" />
        </Button>
      </div>
      {help && <p className="text-xs text-[var(--text-muted)]">{help}</p>}
    </div>
  );
}

function StringRow({
  label,
  help,
  value,
  maxLength,
  onCommit,
}: {
  label: string;
  help?: string;
  value: string;
  maxLength: number;
  onCommit: (v: string) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState<string>(value);
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    if (!focused) setDraft(value);
  }, [value, focused]);

  async function commit() {
    setFocused(false);
    const trimmed = draft.trim();
    if (!trimmed) {
      toast.error(`${label} must not be empty`);
      setDraft(value);
      return;
    }
    if (trimmed.length > maxLength) {
      toast.error(`${label} must be ${maxLength} characters or fewer`);
      setDraft(value);
      return;
    }
    if (trimmed === value) return;
    const ok = await onCommit(trimmed);
    if (!ok) setDraft(value);
  }

  return (
    <Row
      label={label}
      help={help}
      control={
        <Input
          type="text"
          className="w-56"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={commit}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
          }}
        />
      }
    />
  );
}
