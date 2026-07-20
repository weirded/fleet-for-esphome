import { useState } from 'react';
import useSWR from 'swr';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from './ui/dialog';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { Select } from './ui/select';
import { TagChipInput } from './ui/tag-chip-input';
import { getRoutingRules } from '../api/client';
import { evaluateClause } from '../utils/routing';
import type { RoutingClause, RoutingRule, Worker, RoutingClauseOp } from '../types';

const BROWSER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

/**
 * Unified Upgrade modal (#22).
 *
 * Two modes via radio buttons: "Now" (compile immediately) or "Scheduled"
 * (set a recurring or one-time schedule). Both share the worker + version
 * selectors. The confirm button adapts: "Upgrade" vs "Save Schedule".
 *
 * Entry points:
 * - Row "Upgrade" button → defaultMode: 'now'
 * - Hamburger "Schedule Upgrade..." → defaultMode: 'schedule'
 * - Schedules tab "Edit" → defaultMode: 'schedule', schedule pre-filled
 */

// ---------------------------------------------------------------------------
// Cron builder helpers (from the old ScheduleModal)
// ---------------------------------------------------------------------------

// #90: cron expressions in this modal are timezone-naive — they're stored as
// the user enters them, paired with a `schedule_tz` field that APScheduler
// uses to evaluate them on the server. No client-side hour conversion needed.
function buildCron(interval: string, every: number, time: string, dow: string): string {
  const [hh, mm] = time.split(':').map(Number);
  const minute = isNaN(mm) ? 0 : mm;
  const hour = isNaN(hh) ? 2 : hh;

  if (interval === 'hours') {
    return every === 1 ? `${minute} * * * *` : `${minute} */${every} * * *`;
  }

  switch (interval) {
    case 'days': return every === 1 ? `${minute} ${hour} * * *` : `${minute} ${hour} */${every} * *`;
    case 'weeks': return `${minute} ${hour} * * ${dow}`;
    default: return `${minute} ${hour} * * *`;
  }
}

function parseCron(cron: string): { interval: string; every: number; time: string; dow: string } | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [min, hour, dom, , dow] = parts;
  const minute = parseInt(min, 10);
  if (isNaN(minute)) return null;
  if (hour.startsWith('*/') && dom === '*' && dow === '*') {
    return { interval: 'hours', every: parseInt(hour.slice(2), 10), time: `00:${String(minute).padStart(2, '0')}`, dow: '0' };
  }
  if (hour === '*' && dom === '*' && dow === '*') {
    return { interval: 'hours', every: 1, time: `00:${String(minute).padStart(2, '0')}`, dow: '0' };
  }
  const h = parseInt(hour, 10);
  if (isNaN(h)) return null;
  const timeStr = `${String(h).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;

  if (dow === '*') {
    if (dom === '*') return { interval: 'days', every: 1, time: timeStr, dow: '0' };
    if (dom.startsWith('*/')) return { interval: 'days', every: parseInt(dom.slice(2), 10), time: timeStr, dow: '0' };
    return null;
  }
  if (dom === '*') return { interval: 'weeks', every: 1, time: timeStr, dow };
  return null;
}

const DAY_OPTIONS = [
  { label: 'Sunday', value: '0' },
  { label: 'Monday', value: '1' },
  { label: 'Tuesday', value: '2' },
  { label: 'Wednesday', value: '3' },
  { label: 'Thursday', value: '4' },
  { label: 'Friday', value: '5' },
  { label: 'Saturday', value: '6' },
];

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface Props {
  target: string;
  displayName: string;
  workers: Worker[];
  esphomeVersions: string[];
  defaultEsphomeVersion: string | null;
  pinnedVersion?: string | null;
  /** Pre-existing recurring schedule (cron expression). */
  currentSchedule?: string | null;
  currentScheduleEnabled?: boolean;
  /** IANA tz the existing cron is interpreted in. Absent means legacy/UTC. */
  currentScheduleTz?: string | null;
  /** Pre-existing one-time schedule (ISO datetime). */
  currentOnce?: string | null;
  /** Which mode to open in: 'now' for immediate upgrade, 'schedule' for scheduling. */
  defaultMode?: 'now' | 'schedule';
  /** If true, only show the schedule UI — hide mode radios and worker/version pickers.
   *  Used for bulk "Schedule Selected" where version/worker are per-device concerns. */
  scheduleOnly?: boolean;
  /** Bug #109: when the user reruns an existing queue job, App.tsx
   *  passes the original job's parameters here so the modal opens with
   *  the same worker / version / action / tag-filter pre-selected. The
   *  user can tweak any field before submitting. All fields optional;
   *  absent values fall back to the same defaults a fresh upgrade uses. */
  seed?: {
    pinnedClientId?: string | null;
    workerTagFilter?: { op: RoutingClauseOp; tags: string[] } | null;
    esphomeVersion?: string | null;
    action?: 'upgrade-now' | 'download-now';
  };
  /** Bug #110: tag-string lists for each affected target (one entry per
   *  filename in App.tsx's `upgradeModalTarget.targets`). Empty array
   *  is treated as "no per-target tags known" — the conflict check
   *  short-circuits to "no conflict". App.tsx materialises this from
   *  the matched `Target.tags` strings, parsed/trimmed/deduped. */
  affectedTargetTags?: string[][];
  onUpgradeNow: (params: {
    pinnedClientId: string | null;
    esphomeVersion: string | null;
    updatePin?: string | null;
    /** FD.3: when true, enqueue a compile-and-download job instead of compile+OTA. */
    downloadOnly?: boolean;
    /** Bug #97: per-job worker tag filter from the "Tag expression"
     *  worker-selection radio. Mutually exclusive with
     *  ``pinnedClientId`` at the UI level. */
    workerTagFilter?: { op: RoutingClauseOp; tags: string[] } | null;
    /** Bug #110: true when the user confirmed the routing-rule
     *  conflict warning. The server-side eligibility check ignores
     *  global / per-device routing rules for this job; the user's
     *  pin / tag-filter still applies. */
    bypassRoutingRules?: boolean;
  }) => void;
  /**
   * Save a recurring cron schedule. `version` is the user's pin choice —
   * `null` means "Latest" (unpin / use server default at run time), a
   * specific string means "pin the device to this version". `tz` is the
   * IANA tz the cron is interpreted in (#90).
   */
  onSaveSchedule: (cron: string, version: string | null, tz: string) => void;
  onSaveOnce: (datetime: string, version: string | null) => void;
  onDeleteSchedule: () => void;
  onClose: () => void;
}

export function UpgradeModal({
  target: _target,
  displayName,
  workers,
  esphomeVersions,
  defaultEsphomeVersion,
  pinnedVersion,
  currentSchedule,
  currentScheduleEnabled: _currentScheduleEnabled,
  currentScheduleTz,
  currentOnce,
  defaultMode = 'now',
  scheduleOnly = false,
  seed,
  affectedTargetTags = [],
  onUpgradeNow,
  onSaveSchedule,
  onSaveOnce,
  onDeleteSchedule,
  onClose,
}: Props) {
  void _target;

  // Bug #110: fetch the active global routing rules so we can warn
  // when the user's worker / tag-expression choice conflicts with
  // them. SWR scoped to this modal so a fresh rule list is loaded
  // every time the modal opens (rules change rarely; cheap fetch).
  const { data: routingRules } = useSWR<RoutingRule[]>('routing-rules', getRoutingRules, {
    revalidateOnFocus: false,
  });

  // --- Shared state: worker + version ---
  const eligibleWorkers = workers
    .filter(w => w.online && !w.disabled && (w.max_parallel_jobs ?? 0) > 0)
    .slice()
    .sort((a, b) => a.hostname.localeCompare(b.hostname, undefined, { sensitivity: 'base' }));

  // Bug #97: worker-selection radio with three modes:
  //   any         → no filter; routing rules + claim_next decide
  //   specific    → pin to one worker_id (back-compat with the old
  //                 single dropdown)
  //   tag         → ad-hoc per-job worker_tag_filter (op + tags)
  type WorkerMode = 'any' | 'specific' | 'tag';
  // Bug #109: derive the initial worker mode from the rerun seed when
  // present — pinned_client_id wins, then worker_tag_filter, else any.
  const initialWorkerMode: WorkerMode = (() => {
    if (seed?.pinnedClientId) return 'specific';
    if (seed?.workerTagFilter && seed.workerTagFilter.tags.length > 0) return 'tag';
    return 'any';
  })();
  const [selectedWorker, setSelectedWorker] = useState<string>(seed?.pinnedClientId ?? '');
  const [workerMode, setWorkerMode] = useState<WorkerMode>(initialWorkerMode);
  const [tagFilterOp, setTagFilterOp] = useState<RoutingClauseOp>(seed?.workerTagFilter?.op ?? 'all_of');
  const [tagFilterTags, setTagFilterTags] = useState<string[]>(seed?.workerTagFilter?.tags ?? []);
  // Worker-tag autocomplete pool — same union the
  // RoutingRuleBuilder's worker side uses.
  const workerTagPool = (() => {
    const pool = new Set<string>();
    for (const w of workers) {
      if (w.tags) for (const t of w.tags) pool.add(t);
    }
    return Array.from(pool).sort();
  })();
  // #31: selectedVersion = '' means "Latest" (no pin / use current default at
  // run time). If the device is currently pinned, default to that pin. Otherwise
  // default to "Latest" so the schedule auto-updates with new ESPHome releases.
  // Bug #109: when reruning a previous job, seed the version with the one
  // the original job ran against so the user sees what they're reusing.
  const [selectedVersion, setSelectedVersion] = useState<string>(seed?.esphomeVersion ?? pinnedVersion ?? '');

  const versionList: string[] = [];
  if (defaultEsphomeVersion) versionList.push(defaultEsphomeVersion);
  for (const v of esphomeVersions) {
    if (v && !versionList.includes(v)) versionList.push(v);
  }

  // #64: searchable + beta-filterable version list
  const [versionSearch, setVersionSearch] = useState('');
  const [showBetas, setShowBetas] = useState(false);
  // #131 sub-bullet 4: hide ESPHome versions that won't pip install on
  // the current Python runtime. Floor mirrors EsphomeVersionDropdown.
  const [installableOnly, setInstallableOnly] = useState(true);
  const INSTALLABLE_FLOOR = '2023.7.0';
  const isBeta = (v: string) => /\d(a|b|rc|dev)\d/i.test(v);
  const compareVersion = (a: string, b: string): number => {
    const parse = (v: string): number[] => {
      const base = v.split(/[a-z]/i)[0];
      return base.split('.').map(p => parseInt(p, 10) || 0);
    };
    const av = parse(a);
    const bv = parse(b);
    const len = Math.max(av.length, bv.length);
    for (let i = 0; i < len; i++) {
      const diff = (av[i] ?? 0) - (bv[i] ?? 0);
      if (diff !== 0) return diff;
    }
    return 0;
  };
  const isInstallable = (v: string) => compareVersion(v, INSTALLABLE_FLOOR) >= 0;
  const filteredVersions = versionList.filter(v => {
    if (!showBetas && isBeta(v)) return false;
    if (installableOnly && !isInstallable(v)) return false;
    if (versionSearch && !v.toLowerCase().includes(versionSearch.toLowerCase())) return false;
    return true;
  });

  // #215: collapse the version picker into a two-radio surface ("Current"
  // vs "Other"). The search box + scrollable list + show-betas only
  // unfolds when the user picks "Other", which removes ~80 vertical
  // pixels of clutter from the common case where the default version
  // is fine. ``versionMode === 'other'`` is also implied when a non-
  // default version was pre-seeded (rerun seed, existing pin) so the
  // modal opens to the picker the user previously interacted with.
  type VersionMode = 'current' | 'other';
  const initialVersionMode: VersionMode =
    selectedVersion && selectedVersion !== defaultEsphomeVersion ? 'other' : 'current';
  const [versionMode, setVersionMode] = useState<VersionMode>(initialVersionMode);

  // UX.8 + #79: One 4-option action radio. `Schedule Upgrade` was
  // earlier a single radio with a nested Recurring/Once sub-toggle —
  // that sub-toggle is now promoted into two first-class radios so
  // there's exactly one decision point:
  //   upgrade-now         → mode=now, nowAction=ota      (default)
  //   download-now        → mode=now, nowAction=download
  //   schedule-recurring  → mode=schedule, scheduleType=recurring
  //   schedule-once       → mode=schedule, scheduleType=once
  type Action = 'upgrade-now' | 'download-now' | 'schedule-recurring' | 'schedule-once';
  const initialAction: Action = (() => {
    if (scheduleOnly) return currentOnce ? 'schedule-once' : 'schedule-recurring';
    if (defaultMode === 'schedule') return currentOnce ? 'schedule-once' : 'schedule-recurring';
    // Bug #109: seed.action lets a rerun preserve "Download Now" vs the
    // default "Upgrade Now" so the same job intent comes back.
    if (seed?.action === 'download-now') return 'download-now';
    return 'upgrade-now';
  })();
  const [action, setAction] = useState<Action>(initialAction);
  const mode: 'now' | 'schedule' = action.startsWith('schedule-') ? 'schedule' : 'now';
  const nowAction: 'ota' | 'download' = action === 'download-now' ? 'download' : 'ota';

  // --- Schedule state ---
  // #90/#91: cron is shown literally in the picker — no client-side hour
  // conversion. For schedules with `currentScheduleTz` set, the literal
  // cron is what fires in that tz. For legacy schedules without a tz
  // (interpreted as UTC server-side), we still show the literal cron — the
  // user re-saves to claim it for their browser tz, which is honest about
  // what's stored.
  void currentScheduleTz;
  const seedCron = currentSchedule ?? '';
  const parsed = seedCron ? parseCron(seedCron) : null;
  // #79: scheduleType is now derived from `action` — the "recurring vs
  // once" choice is surfaced directly in the main Action radio group.
  const scheduleType: 'recurring' | 'once' = action === 'schedule-once' ? 'once' : 'recurring';
  const [interval, setInterval] = useState(parsed?.interval ?? 'days');
  const [every, setEvery] = useState(parsed?.every ?? 1);
  const [time, setTime] = useState(parsed?.time ?? '02:00');
  const [dow, setDow] = useState(parsed?.dow ?? '0');
  const [rawCron, setRawCron] = useState(seedCron);
  const [cronMode, setCronMode] = useState<'friendly' | 'cron'>(parsed || !currentSchedule ? 'friendly' : 'cron');
  // #33: datetime-local expects a *local* wall-clock value (no timezone). Using
  // `toISOString()` returns UTC, so east-of-UTC users would see a time in the
  // past and west-of-UTC users (e.g. the author) would see a time many hours
  // in the future. Build the value from local components instead.
  const [onceDate, setOnceDate] = useState(() => {
    const pad = (n: number) => String(n).padStart(2, '0');
    const toLocalInput = (d: Date) =>
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return toLocalInput(currentOnce ? new Date(currentOnce) : new Date());
  });

  const effectiveCron = cronMode === 'cron'
    ? rawCron.trim()
    : buildCron(interval, every, time, dow);
  const hasExistingSchedule = !!(currentSchedule || currentOnce);

  // --- Pin warning ---
  // Shows when the user's version choice in "Now" mode would change an
  // existing pin. selectedVersion === '' means "Latest" which is treated as
  // leaving the pin alone in Now mode (don't auto-unpin a manual pin on a
  // one-off upgrade).
  const shouldUpdatePin = pinnedVersion && selectedVersion && selectedVersion !== pinnedVersion;

  // For schedule saves: '' ("Latest") → null (unpin), otherwise the string.
  const scheduleVersion: string | null = selectedVersion || null;

  // --- Bug #110: routing-rule conflict detection ---
  // Mirror the server-side conditional-rule semantics from
  // ``routing.evaluate_rule``: a rule "fires" for a (device, worker)
  // pair when its ``device_match`` clauses all hold for the device's
  // tags; once it fires, the worker must satisfy the rule's
  // ``worker_match``. We surface a warning when the user's chosen
  // worker (``specific``) or tag-expression (``tag``) would *not*
  // satisfy at least one rule that fires for at least one affected
  // target. Confirm under the warning sends ``bypass_routing_rules``
  // through to the server so the job is enqueued anyway.
  function clauseSatisfiesAllOf(clause: RoutingClause, candidateOp: RoutingClauseOp, candidateTags: string[]): boolean {
    // Returns true when every worker satisfying the candidate clause
    // also satisfies the rule clause. Used for ``tag`` mode where the
    // user's clause defines a *set* of acceptable workers.
    //
    // Conservative rule: we only accept "satisfies" for the trivial
    // cases — `all_of` candidate that includes the rule's required
    // tags. For everything else we drop into the "is there any online
    // worker that satisfies both" fallback below.
    if (candidateOp === 'all_of' && clause.op === 'all_of') {
      return clause.tags.every(t => candidateTags.includes(t));
    }
    return false;
  }
  function tagFilterCanSatisfyRuleClause(
    clause: RoutingClause,
    candidateOp: RoutingClauseOp,
    candidateTags: string[],
  ): boolean {
    if (clauseSatisfiesAllOf(clause, candidateOp, candidateTags)) return true;
    // Fallback: search the live worker pool for one that satisfies
    // both the user's filter AND the rule's clause. If at least one
    // such worker exists, the chosen tag-expression is compatible
    // with this rule clause — no conflict.
    const candidateClause: RoutingClause = { op: candidateOp, tags: candidateTags };
    return eligibleWorkers.some(w => {
      const wt = new Set(w.tags ?? []);
      return evaluateClause(candidateClause, wt) && evaluateClause(clause, wt);
    });
  }
  const conflictingRules: { rule: RoutingRule; targetIndex: number }[] = (() => {
    if (mode !== 'now') return [];
    if (!routingRules || routingRules.length === 0) return [];
    if (workerMode === 'any') return [];
    const out: { rule: RoutingRule; targetIndex: number }[] = [];
    if (workerMode === 'specific') {
      const w = workers.find(x => x.client_id === selectedWorker);
      if (!w) return [];
      const wt = new Set(w.tags ?? []);
      affectedTargetTags.forEach((dt, idx) => {
        const dtSet = new Set(dt);
        for (const rule of routingRules) {
          const fires = rule.device_match.every(c => evaluateClause(c, dtSet));
          if (!fires) continue;
          const ok = rule.worker_match.every(c => evaluateClause(c, wt));
          if (!ok) out.push({ rule, targetIndex: idx });
        }
      });
    } else if (workerMode === 'tag' && tagFilterTags.length > 0) {
      affectedTargetTags.forEach((dt, idx) => {
        const dtSet = new Set(dt);
        for (const rule of routingRules) {
          const fires = rule.device_match.every(c => evaluateClause(c, dtSet));
          if (!fires) continue;
          // Conflict when the user's tag filter cannot satisfy this
          // rule's worker_match — i.e. no candidate worker the filter
          // accepts also satisfies the rule.
          const canSatisfy = rule.worker_match.every(c => tagFilterCanSatisfyRuleClause(c, tagFilterOp, tagFilterTags));
          if (!canSatisfy) out.push({ rule, targetIndex: idx });
        }
      });
    }
    return out;
  })();
  const hasConflict = conflictingRules.length > 0;

  function handleConfirm() {
    if (mode === 'now') {
      // Bug #97: pinnedClientId vs workerTagFilter is decided by the
      // worker-mode radio. "any" sends neither; "specific" sends only
      // the pin; "tag" sends only the filter. The server accepts both
      // independently — at the UI level we keep them mutually exclusive
      // so the user has one source of truth.
      const pinned = workerMode === 'specific' ? (selectedWorker || null) : null;
      const filter = workerMode === 'tag' && tagFilterTags.length > 0
        ? { op: tagFilterOp, tags: tagFilterTags }
        : null;
      onUpgradeNow({
        pinnedClientId: pinned,
        esphomeVersion: selectedVersion && selectedVersion !== defaultEsphomeVersion ? selectedVersion : null,
        // FD.3: don't update the pin when we're only producing a
        // binary to download — the device state hasn't changed.
        updatePin: nowAction === 'ota' && shouldUpdatePin ? selectedVersion : null,
        downloadOnly: nowAction === 'download',
        workerTagFilter: filter,
        // Bug #110: when the user clicks confirm under a visible
        // routing-rule conflict warning, treat the click as the "yes,
        // override" answer and tell the server to bypass rules for
        // this job. With no conflict, the flag stays absent.
        bypassRoutingRules: hasConflict || undefined,
      });
    } else {
      if (scheduleType === 'once') {
        onSaveOnce(new Date(onceDate).toISOString(), scheduleVersion);
      } else {
        onSaveSchedule(effectiveCron, scheduleVersion, BROWSER_TZ);
      }
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent style={{ maxWidth: 600 }}>
        <DialogHeader>
          <DialogTitle>
            {/* UX.8: title matches the selected action verb. */}
            {action === 'schedule-recurring'
              ? 'Schedule Recurring Upgrade'
              : action === 'schedule-once' ? 'Schedule Upgrade'
              : action === 'download-now' ? 'Download'
              : 'Upgrade'}{' '}— {displayName}
          </DialogTitle>
        </DialogHeader>
        <div className="p-[18px] flex flex-col gap-4">

          {/* #215: Action → Worker → Version order. Action goes first
              because it's the verb that drives every other decision
              (Upgrade vs Download vs Schedule), Worker narrows who
              runs it, Version is the fine-grain knob most users leave
              at the default. */}
          {!scheduleOnly && (
            <>
              {/* UX.8 + #79: single Action radio (4 options). Schedule is
                  split into recurring vs one-time so there's no nested
                  sub-toggle inside the schedule form. #218: each row
                  uses `flex-wrap` so the descriptive `<span>` wraps to a
                  second line if it overflows the modal — pre-fix we
                  used `whitespace-nowrap` and the action descriptions
                  pushed past 600 px, triggering a horizontal scrollbar.
                  The radio + label itself is short enough to never wrap,
                  preserving #78's original "Schedule Recurring stays on
                  one line" intent. */}
              <div className="flex flex-col gap-1.5">
                <Label>Action</Label>
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-action"
                    checked={action === 'upgrade-now'}
                    onChange={() => setAction('upgrade-now')}
                  />
                  Upgrade Now
                  <span className="text-[11px] text-[var(--text-muted)]">— compile + OTA flash</span>
                </label>
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-action"
                    checked={action === 'download-now'}
                    onChange={() => setAction('download-now')}
                  />
                  Download Now
                  <span className="text-[11px] text-[var(--text-muted)]">— compile only, no OTA; grab the .bin from the Queue tab</span>
                </label>
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-action"
                    checked={action === 'schedule-recurring'}
                    onChange={() => setAction('schedule-recurring')}
                  />
                  Schedule Recurring
                  <span className="text-[11px] text-[var(--text-muted)]">— run the OTA upgrade on a cron</span>
                </label>
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-action"
                    checked={action === 'schedule-once'}
                    onChange={() => setAction('schedule-once')}
                  />
                  Schedule Once
                  <span className="text-[11px] text-[var(--text-muted)]">— run the OTA upgrade at a specific timestamp</span>
                </label>
              </div>

              {/* Bug #97: worker-selection radio. "Any" lets routing
                  rules + the scheduler decide; "Specific" pins to one
                  worker (legacy behaviour); "Tag expression" adds a
                  per-job worker_tag_filter clause that participates in
                  claim_next eligibility. The three are mutually
                  exclusive at the UI level so the user has one source
                  of truth. */}
              <div className="flex flex-col gap-1.5 pt-2 border-t border-[var(--border)]">
                <Label>Worker</Label>
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-worker-mode"
                    checked={workerMode === 'any'}
                    onChange={() => setWorkerMode('any')}
                  />
                  Any available worker
                  <span className="text-[11px] text-[var(--text-muted)]">— scheduler picks at compile time</span>
                </label>
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-worker-mode"
                    checked={workerMode === 'specific'}
                    onChange={() => setWorkerMode('specific')}
                  />
                  Specific worker
                </label>
                {workerMode === 'specific' && (
                  <Select
                    id="upgrade-worker-select"
                    value={selectedWorker}
                    onChange={e => setSelectedWorker(e.target.value)}
                    className="ml-5"
                  >
                    <option value="">— select a worker —</option>
                    {eligibleWorkers.map(w => (
                      <option key={w.client_id} value={w.client_id}>{w.hostname}</option>
                    ))}
                  </Select>
                )}
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-worker-mode"
                    checked={workerMode === 'tag'}
                    onChange={() => setWorkerMode('tag')}
                  />
                  Tag expression
                  <span className="text-[11px] text-[var(--text-muted)]">— same shape as a routing-rule clause</span>
                </label>
                {workerMode === 'tag' && (
                  <div className="ml-5 flex items-start gap-2">
                    <Select
                      value={tagFilterOp}
                      onChange={e => setTagFilterOp(e.target.value as RoutingClauseOp)}
                      className="w-[100px]"
                    >
                      <option value="all_of">All of</option>
                      <option value="any_of">Any of</option>
                      <option value="none_of">None of</option>
                    </Select>
                    <div className="flex-1">
                      <TagChipInput
                        tags={tagFilterTags}
                        onChange={setTagFilterTags}
                        suggestions={workerTagPool}
                        placeholder="worker tag (e.g. windows)…"
                      />
                    </div>
                  </div>
                )}
              </div>

              {/* #215: collapsed version picker. The radio surface keeps
                  the common "stick with the current version" case to
                  one short line; the search box + scrollable list +
                  show-betas only unfold under "Other". */}
              <div className="flex flex-col gap-1.5 pt-2 border-t border-[var(--border)]">
                <Label>ESPHome version</Label>
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-version-mode"
                    checked={versionMode === 'current'}
                    onChange={() => {
                      setVersionMode('current');
                      setSelectedVersion('');
                    }}
                  />
                  Current{defaultEsphomeVersion ? ` (${defaultEsphomeVersion})` : ''}
                  <span className="text-[11px] text-[var(--text-muted)]">— use the server-default version at compile time</span>
                </label>
                <label className="flex items-center gap-1.5 text-[13px] cursor-pointer flex-wrap">
                  <input
                    type="radio"
                    name="upgrade-version-mode"
                    checked={versionMode === 'other'}
                    onChange={() => setVersionMode('other')}
                  />
                  Other
                  {versionMode === 'other' && selectedVersion && (
                    <span className="text-[11px] text-[var(--accent)]">— {selectedVersion}</span>
                  )}
                </label>
                {versionMode === 'other' && (
                  <div className="ml-5">
                    <input
                      type="text"
                      value={versionSearch}
                      onChange={e => setVersionSearch(e.target.value)}
                      placeholder="Search versions..."
                      className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface2)] px-2.5 py-1 text-[12px] text-[var(--text)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] mb-1"
                    />
                    {/* #73: scrollable list matching the header dropdown style */}
                    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] overflow-y-auto" style={{ maxHeight: 160 }}>
                      {filteredVersions.map(v => (
                        <button
                          key={v}
                          type="button"
                          className={`w-full text-left px-2.5 py-1.5 text-[12px] cursor-pointer hover:bg-[var(--surface2)] ${selectedVersion === v ? 'text-[var(--accent)] font-semibold' : 'text-[var(--text)]'}`}
                          onClick={() => setSelectedVersion(v)}
                        >
                          {v}
                        </button>
                      ))}
                      {filteredVersions.length === 0 && (
                        <div className="px-2.5 py-1.5 text-[12px] text-[var(--text-muted)]">No matches</div>
                      )}
                    </div>
                    <label className="flex items-center gap-1.5 mt-1 text-[11px] text-[var(--text-muted)] cursor-pointer">
                      <input type="checkbox" checked={showBetas} onChange={e => setShowBetas(e.target.checked)} />
                      Show betas
                    </label>
                    <label
                      className="flex items-center gap-1.5 mt-1 text-[11px] text-[var(--text-muted)] cursor-pointer"
                      title={`Hides ESPHome versions older than ${INSTALLABLE_FLOOR} that won't pip install on the current Python runtime.`}
                    >
                      <input type="checkbox" checked={installableOnly} onChange={e => setInstallableOnly(e.target.checked)} />
                      Installable only
                    </label>
                  </div>
                )}
              </div>

              {/* Pin warning */}
              {shouldUpdatePin && mode === 'now' && (
                <div className="rounded-lg border border-[var(--accent)] bg-[var(--accent)]/10 px-3 py-2 text-[12px]" style={{ color: 'var(--accent)' }}>
                  <strong>Pin update.</strong> Currently pinned to <code className="bg-[var(--surface)] px-1 rounded">{pinnedVersion}</code>. Upgrading will update the pin to <code className="bg-[var(--surface)] px-1 rounded">{selectedVersion}</code>.
                </div>
              )}

              {/* Bug #110: routing-rule conflict warning. Surfaces the
                  rule names that fire for the affected device(s) but
                  the chosen worker / tag-expression doesn't satisfy.
                  Confirming "Upgrade" under this banner sends
                  ``bypass_routing_rules: true`` so the server
                  enqueues the job anyway. */}
              {hasConflict && mode === 'now' && (
                <div className="rounded-lg border border-[#fb923c] bg-[#3f1d1d] px-3 py-2 text-[12px] text-[#fb923c]">
                  <strong>Routing-rule conflict.</strong>{' '}
                  {workerMode === 'specific'
                    ? 'The selected worker does not satisfy:'
                    : 'No worker matching this tag expression satisfies:'}
                  <ul className="mt-1 ml-4 list-disc">
                    {Array.from(new Set(conflictingRules.map(c => c.rule.id))).map(id => {
                      const r = conflictingRules.find(c => c.rule.id === id)!.rule;
                      return <li key={id}><code className="bg-[var(--surface)] px-1 rounded">{r.name || id}</code></li>;
                    })}
                  </ul>
                  <div className="mt-1.5">Confirming will override the rule for this run only — the rule itself is unchanged.</div>
                </div>
              )}
            </>
          )}

          {/* Schedule options (only visible when a schedule-* action is active) */}
          {mode === 'schedule' && (
            <div className="flex flex-col gap-3 pt-1 border-t border-[var(--border)]">
              {/* #79: recurring/once is now selected via the main Action
                  radio above, so the inline sub-toggle is gone. The only
                  remaining inline control is the "Advanced (cron)" toggle
                  for recurring — right-aligned above the inputs. */}
              {scheduleType === 'recurring' && (
                <div className="flex justify-end">
                  <button
                    className="text-[10px] text-[var(--text-muted)] cursor-pointer hover:text-[var(--text)]"
                    onClick={() => setCronMode(cronMode === 'friendly' ? 'cron' : 'friendly')}
                  >
                    {cronMode === 'friendly' ? 'Advanced (cron)' : 'Simple'}
                  </button>
                </div>
              )}

              {scheduleType === 'recurring' ? (
                cronMode === 'friendly' ? (
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[12px]">Every</span>
                    <Input type="number" min={1} max={30} value={every} onChange={e => setEvery(Math.max(1, parseInt(e.target.value, 10) || 1))} className="w-[60px]" />
                    <Select value={interval} onChange={e => setInterval(e.target.value)} className="w-[100px]">
                      <option value="hours">hour(s)</option>
                      <option value="days">day(s)</option>
                      <option value="weeks">week(s)</option>
                    </Select>
                    {interval === 'weeks' && (
                      <>
                        <span className="text-[12px]">on</span>
                        <Select value={dow} onChange={e => setDow(e.target.value)} className="w-[120px]">
                          {DAY_OPTIONS.map(d => <option key={d.value} value={d.value}>{d.label}</option>)}
                        </Select>
                      </>
                    )}
                    {interval !== 'hours' && (
                      <>
                        <span className="text-[12px]">at</span>
                        <Input type="time" value={time} onChange={e => setTime(e.target.value)} className="w-[100px]" />
                      </>
                    )}
                  </div>
                ) : (
                  <div>
                    <Input type="text" value={rawCron} placeholder="0 2 * * *" onChange={e => setRawCron(e.target.value)} />
                    <div className="mt-1 text-[10px] text-[var(--text-muted)]">minute hour day-of-month month day-of-week — interpreted in {BROWSER_TZ}</div>
                  </div>
                )
              ) : (
                <div>
                  <Input type="datetime-local" value={onceDate} onChange={e => setOnceDate(e.target.value)} />
                  <div className="mt-1 text-[10px] text-[var(--text-muted)]">Upgrades once at this time, then the schedule is removed.</div>
                </div>
              )}

              {scheduleType === 'recurring' && cronMode === 'friendly' && (
                <div className="text-[10px] text-[var(--text-muted)]">
                  Cron: <code className="bg-[var(--surface)] px-1 rounded">{effectiveCron}</code> <span className="opacity-70">({BROWSER_TZ})</span>
                </div>
              )}

              {hasExistingSchedule && (
                <button
                  className="text-[11px] text-[var(--destructive)] cursor-pointer hover:underline self-start"
                  onClick={() => { onDeleteSchedule(); onClose(); }}
                >
                  Remove existing schedule
                </button>
              )}
            </div>
          )}

          {/* Confirm */}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={onClose}>Cancel</Button>
            <Button
              variant={mode === 'now' ? 'success' : 'default'}
              disabled={
                (mode === 'schedule' && scheduleType === 'once' && !onceDate)
                // Bug #97: don't let the user submit a half-set
                // worker-mode choice — "specific" needs a worker,
                // "tag" needs at least one tag in the clause.
                || (mode === 'now' && workerMode === 'specific' && !selectedWorker)
                || (mode === 'now' && workerMode === 'tag' && tagFilterTags.length === 0)
              }
              onClick={handleConfirm}
            >
              {/* UX.8: confirm-button label mirrors the action verb.
                  Bug #110: when a rule conflict is showing, append
                  "& override rules" so the click is unambiguous. */}
              {action === 'upgrade-now' && (hasConflict ? 'Upgrade & override rules' : 'Upgrade')}
              {action === 'download-now' && (hasConflict ? 'Download & override rules' : 'Compile & Download')}
              {action === 'schedule-recurring' && 'Save Schedule'}
              {action === 'schedule-once' && 'Save Schedule'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
