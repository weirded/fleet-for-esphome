import { useCallback, useMemo, useRef, useState } from 'react';
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  flexRender,
  createColumnHelper,
  type SortingState,
} from '@tanstack/react-table';
import { HardDrive } from 'lucide-react';
import type { Job, SystemInfo, Target, Worker } from '../types';
import { Button } from './ui/button';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
} from './ui/dropdown-menu';
import { getJobBadge, stripYaml, timeAgo, usePersistedState } from '../utils';
import { StatusDot } from './StatusDot';
import { SortHeader, getAriaSort } from './ui/sort-header';
import { TagChips } from './ui/tag-chips';
import { TagsEditDialog } from './TagsEditDialog';
import { TagFilterBar } from './TagFilterBar';
import { setWorkerTags } from '../api/client';
import { useSWRConfig } from 'swr';
import { SetDiskQuotaDialog } from './SetDiskQuotaDialog';

interface Props {
  workers: Worker[];
  /** Bug #11: device list for the chip-input autocomplete pool. The
   *  TagsEditDialog suggests every tag in use across the fleet (devices
   *  + workers); ``targets`` carries the device side. */
  targets: Target[];
  queue: Job[];
  serverClientVersion?: string;
  minImageVersion?: string;
  onRemove: (id: string) => void;
  onSetParallelJobs: (id: string, count: number) => void;
  // DQ.5: per-worker disk-quota override. Null clears the override so
  // the worker inherits ``default_worker_disk_quota_bytes``.
  onSetDiskQuota: (id: string, bytes: number | null) => void;
  onCleanCache: (id: string) => void;
  onCleanAllCaches: () => void;
  onConnectWorker: (preset?: import('../types').WorkerPreset | null) => void;
  onViewLogs: (clientId: string) => void;
  // #109: "Request diagnostics" runs py-spy on the worker and downloads
  // the thread dump. Online-workers only (offline workers can't reply).
  onRequestDiagnostics: (id: string) => void;
  // TG.9: routing-rules modal is now hoisted to App.tsx so the QueueTab
  // BLOCKED-badge click can target the same instance. Toolbar button calls
  // this with no arg → list mode.
  onOpenRoutingRules: () => void;
}

function workerPlatformHtml(si: SystemInfo): React.ReactNode {
  const lines: React.ReactNode[] = [];
  if (si.os_version) {
    lines.push(<span key="os" className="text-[10px] text-[var(--text-muted)]">{si.os_version}</span>);
  }
  if (si.cpu_model) {
    lines.push(<span key="cpu" className="text-[10px] text-[var(--text-muted)]">{si.cpu_model}</span>);
  }
  const hwParts: string[] = [];
  if (si.cpu_arch) hwParts.push(si.cpu_arch);
  if (si.cpu_cores) hwParts.push(si.cpu_cores + ' cores');
  if (si.total_memory) hwParts.push(si.total_memory);
  if (hwParts.length) {
    lines.push(<span key="hw" className="text-[10px] text-[var(--text-muted)]">{hwParts.join(' · ')}</span>);
  }
  const metrics: string[] = [];
  if (si.perf_score != null) metrics.push(`Score: ${si.perf_score}`);
  if (si.cpu_usage != null) metrics.push(`CPU: ${si.cpu_usage}%`);
  if (metrics.length) {
    lines.push(
      <span key="metrics" className="text-[10px] text-[var(--text-muted)]" title="Perf score (SHA256 benchmark) · CPU utilization">
        {metrics.join(' · ')}
      </span>
    );
  }
  // Disk space on separate line (#109)
  if (si.disk_free && si.disk_total) {
    const pctFree = si.disk_used_pct != null ? 100 - si.disk_used_pct : null;
    const diskColor = (si.disk_used_pct ?? 0) > 90 ? 'var(--danger)' : (si.disk_used_pct ?? 0) > 80 ? 'var(--warn)' : 'var(--text-muted)';
    const pctStr = pctFree != null ? ` (${pctFree}% free)` : '';
    lines.push(
      <span key="disk" className="text-[10px]" style={{ color: diskColor }} title={`Build volume: ${si.disk_free} free of ${si.disk_total} (${si.disk_used_pct ?? '?'}% used)`}>
        Disk: {si.disk_free} / {si.disk_total}{pctStr}
      </span>
    );
  }
  // 5.2: build cache stats
  if (si.cached_targets != null) {
    const cacheStr = si.cache_size_mb != null ? ` (${si.cache_size_mb} MB)` : '';
    lines.push(
      <span key="cache" className="text-[10px] text-[var(--text-muted)]" title={`Build cache: ${si.cached_targets} target(s) cached${cacheStr}`}>
        Cache: {si.cached_targets} target{si.cached_targets !== 1 ? 's' : ''}{cacheStr}
      </span>
    );
  }
  // DQ.11: disk-quota engine view — shown when the worker has surfaced
  // the new system_info fields (post-DQ.6 worker). Yellow when approaching
  // the cap, red only when usage has actually hit it — being below quota
  // is normal operating state, not a warning condition.
  if (si.disk_usage_bytes != null && si.disk_quota_bytes != null && si.disk_quota_bytes > 0) {
    const usageGb = si.disk_usage_bytes / (1024 ** 3);
    const quotaGb = si.disk_quota_bytes / (1024 ** 3);
    const pct = (si.disk_usage_bytes / si.disk_quota_bytes) * 100;
    const quotaColor = pct >= 100 ? 'var(--danger)' : pct > 80 ? 'var(--warn)' : 'var(--text-muted)';
    lines.push(
      <span
        key="quota"
        className="text-[10px]"
        style={{ color: quotaColor }}
        title={`Disk quota: ${usageGb.toFixed(1)} / ${quotaGb.toFixed(0)} GiB (${pct.toFixed(0)}% used)`}
      >
        Quota: {usageGb.toFixed(1)} / {quotaGb.toFixed(0)} GiB
      </span>
    );
  }
  return lines.length === 0 ? null : (
    <>{lines.map((l, i) => <>{i > 0 && <br />}{l}</>)}</>
  );
}

function ClientVersionCell({
  ver,
  scv,
  imageVer,
  minImageVer,
  hostname,
  onReinstall,
}: {
  ver?: string;
  scv?: string;
  imageVer?: string | null;
  minImageVer?: string;
  /** Worker hostname — used as the default container name in the
   *  WU.2 refresh-command tooltip. Falls back to a placeholder when
   *  the worker hasn't reported a hostname yet. */
  hostname?: string;
  onReinstall: () => void;
}) {
  // Docker image version is checked first — a stale image blocks source-code
  // auto-updates entirely, so that's the more important warning to surface.
  const imageStale = imageIsStale(imageVer, minImageVer);

  if (!ver) {
    return (
      <span className="text-[var(--text-muted)]">
        —
        {imageStale && <ImageStaleBadge imageVer={imageVer} minImageVer={minImageVer} hostname={hostname} onReinstall={onReinstall} />}
      </span>
    );
  }

  const isOutdated = scv && ver !== scv;
  const color = imageStale ? 'var(--destructive)' : isOutdated ? 'var(--warn)' : 'var(--text-muted)';
  const title = isOutdated ? `Source outdated — server: ${scv}` : undefined;

  return (
    <span className="inline-flex items-center gap-1">
      <code className="text-[11px]" style={{ color }} title={title}>
        {ver}
        {isOutdated && ' ↑'}
      </code>
      {imageStale && <ImageStaleBadge imageVer={imageVer} minImageVer={minImageVer} hostname={hostname} onReinstall={onReinstall} />}
    </span>
  );
}

function ImageStaleBadge({
  imageVer,
  minImageVer,
  hostname,
  onReinstall,
}: {
  imageVer?: string | null;
  minImageVer?: string;
  hostname?: string;
  onReinstall: () => void;
}) {
  const reported = imageVer ?? 'pre-LIB.0';
  // WU.2: surface the two-liner refresh command directly in the tooltip
  // so the user doesn't have to find the Connect Worker modal first.
  // Hostname defaults to a visible placeholder so the user sees where
  // their container name goes even when we haven't got it on file.
  const containerName = hostname || '<your-worker-container>';
  return (
    <button
      type="button"
      onClick={onReinstall}
      title={
        `Worker image out of date ` +
        `(IMAGE_VERSION=${reported}, server requires ${minImageVer}). ` +
        `Source-code auto-updates are disabled until the image is refreshed.\n\n` +
        `Refresh in-place (same token, same slots):\n` +
        `  docker pull ghcr.io/weirded/esphome-dist-client:latest\n` +
        `  docker restart ${containerName}\n\n` +
        `Full re-install (new token / changed host platform / stepping past MIN_IMAGE_VERSION): ` +
        `click this badge to open Connect Worker and copy a fresh snippet.\n\n` +
        `Details: DOCS → Keeping workers up to date.`
      }
      className="inline-flex items-center rounded-full border border-[var(--destructive)] bg-[var(--destructive)]/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-[var(--destructive)] cursor-pointer hover:bg-[var(--destructive)]/20"
    >
      image stale
    </button>
  );
}

/** Return true iff the reported image version is missing or below the server minimum. */
function imageIsStale(reported: string | null | undefined, minimum: string | undefined): boolean {
  if (!minimum) return false; // server doesn't enforce a minimum
  if (reported == null) return true; // pre-LIB.0 worker
  const r = parseInt(reported, 10);
  const m = parseInt(minimum, 10);
  if (Number.isNaN(r) || Number.isNaN(m)) return false;
  return r < m;
}

/* Debounced slot control (#108) */
function SlotControl({ slots, requested, onSet }: { slots: number; requested: number | null; onSet: (n: number) => void }) {
  const [localValue, setLocalValue] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const displayed = localValue ?? slots;
  const pending = localValue != null ? localValue : (requested != null && requested !== slots ? requested : null);

  const change = useCallback((delta: number) => {
    const next = Math.max(0, Math.min(32, (localValue ?? slots) + delta));
    setLocalValue(next);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      onSet(next);
      setLocalValue(null);
      timerRef.current = null;
    }, 600);
  }, [localValue, slots, onSet]);

  return (
    <span
      className="inline-flex items-center gap-0.5 text-[11px] text-[var(--text-muted)] whitespace-nowrap"
      title="Parallel build slots (0 = paused, accepts no jobs)"
    >
      <Button
        variant="secondary"
        size="sm"
        className="px-1.5 py-px text-[11px] min-w-0"
        disabled={displayed <= 0}
        onClick={() => change(-1)}
      >-</Button>
      <span className="min-w-[16px] text-center">
        {pending != null && pending !== displayed ? `${slots}→${pending}` : String(displayed)}
      </span>
      <Button
        variant="secondary"
        size="sm"
        className="px-1.5 py-px text-[11px] min-w-0"
        onClick={() => change(1)}
      >+</Button>
    </span>
  );
}

const LOCAL_WORKER_HOSTNAME = 'local-worker';

// Sort accessor values for each sortable column
function getWorkerSortValue(w: Worker, colId: string): string {
  if (colId === 'hostname') return w.hostname;
  if (colId === 'status') {
    if ((w.max_parallel_jobs ?? 0) === 0) return 'paused';
    return w.online ? 'online' : 'offline';
  }
  if (colId === 'version') return w.client_version || '';
  return '';
}

const columnHelper = createColumnHelper<Worker>();

export function WorkersTab({ workers, targets, queue, serverClientVersion, minImageVersion, onRemove, onSetParallelJobs, onSetDiskQuota, onCleanCache, onCleanAllCaches, onConnectWorker, onViewLogs, onRequestDiagnostics, onOpenRoutingRules }: Props) {
  // WL.3: lift the actions-dropdown open state out of the TanStack row
  // cell so the 1 Hz SWR poll doesn't tear it down mid-click (bug #2
  // / #71 class — see Design Judgment in CLAUDE.md). Keyed by
  // client_id so only one dropdown is open at a time.
  const [actionsMenuOpenClientId, setActionsMenuOpenClientId] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  // TG.6 inline edit — same lift-out-of-row pattern as the Actions menu.
  const [tagsEditClientId, setTagsEditClientId] = useState<string | null>(null);
  // DQ.11: same lift-out-of-row pattern — keep the dialog state at the
  // tab level so the 1 Hz SWR poll doesn't tear it down on row remount.
  const [diskQuotaEditClientId, setDiskQuotaEditClientId] = useState<string | null>(null);
  // TG.6 filter pills — selected tag set persisted across reloads.
  const [tagFilter, setTagFilter] = usePersistedState<string[]>('workers-tag-filter', []);
  // TG.9: rules modal is now lifted to App.tsx so the QueueTab can also
  // open it (deep-linked to the rule that fired). Toolbar button calls
  // onOpenRoutingRules() to flip the App-level state.
  const { mutate } = useSWRConfig();
  // QS.27: persist sort across reloads via localStorage.
  const [sorting, setSorting] = usePersistedState<SortingState>(
    'workers-sort',
    [{ id: 'hostname', desc: false }],
  );


  // TG.6 filter pills: fleet-wide worker-tag pool with usage counts.
  const tagPool = useMemo(() => {
    const counts = new Map<string, number>();
    for (const w of workers) {
      if (w.tags) for (const tag of w.tags) {
        counts.set(tag, (counts.get(tag) ?? 0) + 1);
      }
    }
    return Array.from(counts.entries())
      .map(([tag, count]) => ({ tag, count }))
      .sort((a, b) => a.tag.localeCompare(b.tag));
  }, [workers]);

  // Filter before handing to TanStack — keeps filter state local, same as DevicesTab pattern
  const filteredWorkers = useMemo(() => {
    // TG.6 pill filter first. #222: AND-logic (was OR pre-#222) — a
    // worker matches only when it carries *every* selected tag, so
    // picking `linux` then `fast` narrows to linux-AND-fast workers.
    const tagged = tagFilter.length === 0
      ? workers
      : workers.filter(w => {
          const ts = new Set(w.tags ?? []);
          return tagFilter.every(t => ts.has(t));
        });
    if (!filter) return tagged;
    const q = filter.toLowerCase();
    return tagged.filter(w =>
      w.hostname.toLowerCase().includes(q)
      || (w.system_info?.os_version || '').toLowerCase().includes(q)
      || (w.system_info?.cpu_model || '').toLowerCase().includes(q)
      || (w.client_version || '').toLowerCase().includes(q)
      // TG.6: tags participate in the existing search box too on top of
      // the pill filter (substring, case-insensitive).
      || (w.tags ?? []).some(t => t.toLowerCase().includes(q))
    );
  }, [workers, filter, tagFilter]);

  // UX.2: wrap every sortable column header in SortHeader so the sort
  // glyph renders consistently with Devices/Queue/Schedules (QS.21).
  const columns = useMemo(() => [
    columnHelper.accessor(w => getWorkerSortValue(w, 'hostname'), {
      id: 'hostname',
      header: ({ column }) => <SortHeader label="Hostname" column={column} />,
      sortingFn: 'alphanumeric',
    }),
    columnHelper.accessor(w => getWorkerSortValue(w, 'status'), {
      id: 'status',
      header: ({ column }) => <SortHeader label="Status" column={column} />,
      sortingFn: 'alphanumeric',
    }),
    columnHelper.accessor(w => getWorkerSortValue(w, 'version'), {
      id: 'version',
      header: ({ column }) => <SortHeader label="Version" column={column} />,
      sortingFn: 'alphanumeric',
    }),
    // Non-sortable display columns — included so flexRender can handle headers uniformly
    columnHelper.display({ id: 'platform', header: 'Platform' }),
    columnHelper.display({ id: 'currentJob', header: 'Current Job' }),
    columnHelper.display({
      id: 'tags',
      header: () => (
        <span title="Tags initially seeded from WORKER_TAGS env var; now stored on the server. Edits here will be authoritative.">
          Tags
        </span>
      ),
    }),
    columnHelper.display({ id: 'slots', header: 'Slots' }),
    columnHelper.display({ id: 'actions', header: '' }),
  ], []);

  const table = useReactTable({
    data: filteredWorkers,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    // Disable multi-sort; simple single-column sort
    enableMultiSort: false,
  });

  // TanStack sorted rows, then pin local-worker to top (#107)
  const sortedWorkers = useMemo(() => {
    const tanstackRows = table.getSortedRowModel().rows.map(r => r.original);
    return [
      ...tanstackRows.filter(w => w.hostname === LOCAL_WORKER_HOSTNAME),
      ...tanstackRows.filter(w => w.hostname !== LOCAL_WORKER_HOSTNAME),
    ];
  }, [table.getSortedRowModel().rows]); // eslint-disable-line react-hooks/exhaustive-deps

  // Expand each worker into per-slot rows after TanStack sorting
  const rows: React.ReactNode[] = [];

  for (const c of sortedWorkers) {
    const slots = c.max_parallel_jobs ?? 0;
    const isLocal = c.hostname === LOCAL_WORKER_HOSTNAME;
    // Workers never leave the list on their own — not on a clean shutdown,
    // not on an add-on restart. Delete is the only exit, so the item is
    // always rendered; it's disabled (with the reason as a tooltip) when
    // deleting would be pointless: a running worker re-registers on its
    // next heartbeat, and the built-in worker is respawned by the add-on.
    const deleteBlockedReason = isLocal
      ? "The built-in worker can't be deleted"
      : c.online
        ? 'Stop the worker first — a running worker re-registers on its next heartbeat'
        : null;
    const paused = slots === 0;
    const statusEl = paused
      ? <StatusDot status="paused" />
      : c.online
        ? <StatusDot status="online" />
        : <StatusDot status="offline" />;

    const rowStyle: React.CSSProperties = {
      ...(paused ? { opacity: 0.6 } : {}),
    };
    const rowClass = isLocal ? 'local-worker-row' : '';

    const displaySlots = Math.max(slots, 1); // show at least 1 row even if 0 slots
    for (let slot = 1; slot <= displaySlots; slot++) {
      const slotJob = slots > 0 ? queue.find(
        j =>
          j.assigned_client_id === c.client_id &&
          (j.worker_id === slot || (slot === 1 && j.worker_id == null)) &&
          j.state === 'working',
      ) : null;

      // UX.6: render the slot on a separate muted line rather than
      // gluing `/N` to the hostname (which reads as "version N"). The
      // tooltip spells out what the slot number means.
      const slotNameEl = slots > 1
        ? (
          <>
            {c.hostname}
            <br />
            <span
              className="text-[10px] text-[var(--text-muted)]"
              title={`Build slot ${slot} of ${slots} on this worker.`}
            >
              slot {slot}
            </span>
          </>
        )
        : <>{c.hostname}</>;

      // UX.3: render the same state badge on the Workers Current Job
      // cell as the one used on the Queue tab, so a state label looks
      // identical regardless of where it appears.
      const jobEl = slots === 0
        ? <span className="text-[12px] italic text-[var(--text-muted)]">Paused</span>
        : slotJob
          ? (() => {
              const { label, cls } = getJobBadge(slotJob);
              return (
                <div className="flex flex-col gap-0.5">
                  <code className="text-[12px]">{stripYaml(slotJob.target)}</code>
                  <span className={cls}>{label}</span>
                </div>
              );
            })()
          : <span className="text-[12px] text-[var(--text-muted)]">Idle</span>;

      // When offline, show how long it's been gone instead of stale process uptime.
      // When online, show worker process uptime from the last heartbeat.
      let uptimeEl: React.ReactNode = null;
      if (!c.online && c.last_seen) {
        const duration = timeAgo(c.last_seen).replace(/ ago$/, '');
        uptimeEl = (
          <><br /><span className="text-[10px] text-[var(--text-muted)]" title={`Last heartbeat: ${new Date(c.last_seen).toLocaleString()}`}>offline for {duration}</span></>
        );
      } else if (c.online && c.system_info?.uptime) {
        uptimeEl = (
          <><br /><span className="text-[10px] text-[var(--text-muted)]" title="Worker process uptime">up {c.system_info.uptime}</span></>
        );
      }

      // #219: surface the server's self-imposed disk-pressure pause as
      // an explanatory badge so the user understands why a worker is
      // online but not claiming. The icon is decorative; aria-label +
      // title carry the meaning (UI-7 invariant).
      let healthBlockEl: React.ReactNode = null;
      if (c.health_blocked_reason === 'disk_full') {
        const usedPct = c.system_info?.disk_used_pct;
        const free = c.system_info?.disk_free;
        const tip = `Auto-paused: worker disk is full. ${
          usedPct != null ? `Currently ${usedPct}% used` : 'Disk usage above 95%'
        }${free ? `, ${free} free` : ''}. Won't claim new jobs until usage drops to 90% or below — use Clean cache to recover, or grow the host's disk.`;
        healthBlockEl = (
          <>
            <br />
            <span
              className="inline-flex items-center gap-1 text-[10px] font-medium"
              style={{ color: 'var(--danger)' }}
              aria-label="Auto-paused: disk full"
              title={tip}
            >
              <HardDrive className="size-3" aria-hidden="true" />
              disk full
            </span>
          </>
        );
      }

      if (slot === 1) {
        rows.push(
          <tr key={`${c.client_id}-1`} style={rowStyle} className={rowClass}>
            <td>
              {slotNameEl}
              {isLocal && <span className="ml-1.5 text-[9px] font-semibold uppercase text-[var(--accent)]">built-in</span>}
            </td>
            <td>
              <button
                type="button"
                onClick={() => setTagsEditClientId(c.client_id)}
                className="cursor-pointer rounded-md border border-transparent px-1 py-px text-left hover:border-[var(--border)] hover:bg-[var(--surface2)]"
                aria-label={`Tags for ${c.hostname}`}
                title="Click to edit tags"
              >
                {(c.tags && c.tags.length > 0)
                  ? <TagChips tags={c.tags} />
                  : <span className="text-[10px] text-[var(--text-muted)] italic">+ tags</span>}
              </button>
            </td>
            <td>{c.system_info ? workerPlatformHtml(c.system_info) : null}</td>
            <td>{statusEl}{uptimeEl}{healthBlockEl}</td>
            <td>{jobEl}</td>
            <td><ClientVersionCell ver={c.client_version} scv={serverClientVersion} imageVer={c.image_version} minImageVer={minImageVersion} hostname={c.hostname} onReinstall={() => onConnectWorker({
              hostname: c.hostname,
              max_parallel_jobs: c.max_parallel_jobs,
              host_platform: c.system_info?.os_version,
            })} /></td>
            <td>
              <SlotControl
                slots={slots}
                requested={c.requested_max_parallel_jobs ?? null}
                onSet={(n) => onSetParallelJobs(c.client_id, n)}
              />
            </td>
            <td>
              <DropdownMenu
                open={actionsMenuOpenClientId === c.client_id}
                onOpenChange={(open) => setActionsMenuOpenClientId(open ? c.client_id : null)}
              >
                <DropdownMenuTrigger
                  className="inline-flex items-center gap-1 rounded-lg border border-border bg-background px-2.5 h-7 text-[0.8rem] font-medium text-foreground hover:bg-muted cursor-pointer"
                  aria-label={`Actions for ${c.hostname}`}
                  title="Actions"
                >
                  Actions ▾
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuGroup>
                    <DropdownMenuItem onClick={() => onViewLogs(c.client_id)}>
                      View logs
                    </DropdownMenuItem>
                    {c.online && (
                      <DropdownMenuItem onClick={() => onRequestDiagnostics(c.client_id)}>
                        Request diagnostics
                      </DropdownMenuItem>
                    )}
                    {c.online && (
                      <DropdownMenuItem onClick={() => onCleanCache(c.client_id)}>
                        Clean cache
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem onClick={() => setDiskQuotaEditClientId(c.client_id)}>
                      Set disk quota…
                    </DropdownMenuItem>
                    {/* Disabled menu items swallow pointer events, so the
                        tooltip lives on a wrapper the cursor can still hit. */}
                    <div title={deleteBlockedReason ?? undefined}>
                      <DropdownMenuItem
                        disabled={deleteBlockedReason !== null}
                        onClick={() => onRemove(c.client_id)}
                        className="text-[var(--danger,#ef4444)]"
                      >
                        Delete
                      </DropdownMenuItem>
                    </div>
                  </DropdownMenuGroup>
                </DropdownMenuContent>
              </DropdownMenu>
            </td>
          </tr>
        );
      } else {
        rows.push(
          <tr key={`${c.client_id}-${slot}`} style={rowStyle} className={rowClass}>
            <td>{slotNameEl}</td>
            <td></td>
            <td></td>
            <td></td>
            <td>{jobEl}</td>
            <td></td>
            <td></td>
            <td></td>
          </tr>
        );
      }
    }
  }

  // Build header cells from TanStack column defs in the order we want to render them
  // Column order: hostname, tags, platform, status, currentJob, version, slots, actions
  // Bug #104: Tags promoted to position 2 to mirror the Devices tab's #16
  // layout — tags are how users group workers in routing rules and the
  // bulk-actions UX, so they belong adjacent to the identity column.
  const HEADER_ORDER = ['hostname', 'tags', 'platform', 'status', 'currentJob', 'version', 'slots', 'actions'];
  const headerCells = table.getHeaderGroups()[0].headers;
  const headerByid = Object.fromEntries(headerCells.map(h => [h.id, h]));

  function renderHeader(id: string) {
    const h = headerByid[id];
    if (!h) return <th key={id}></th>;
    // UX.2: click + sort indicators now come from the SortHeader child
    // button (mirroring Devices/Queue/Schedules); the <th> only carries
    // the aria-sort state. Old cell-wide onClick + inline arrow removed.
    const canSort = h.column.getCanSort();
    return (
      <th key={id} aria-sort={canSort ? getAriaSort(h.column) : undefined}>
        {flexRender(h.column.columnDef.header, h.getContext())}
      </th>
    );
  }

  return (
    <div className="block" id="tab-workers">
      <div className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)] shadow-sm">
        <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] bg-[var(--surface2)] px-4 py-3">
          <h2 className="text-[13px] font-semibold uppercase tracking-wide text-[var(--text-muted)] mr-1">Workers</h2>
          <div className="relative max-w-[280px]">
            <input
              type="text"
              value={filter}
              onChange={e => setFilter(e.target.value)}
              placeholder="Search workers..."
              className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface2)] px-2.5 py-1 pr-7 text-[13px] text-[var(--text)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--accent)]"
            />
            {filter && (
              <button
                onClick={() => setFilter('')}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 border-none bg-transparent text-sm leading-none text-[var(--text-muted)] cursor-pointer px-0.5"
              >&times;</button>
            )}
          </div>
          <div className="actions">
            {/* #88: standardized layout — primary "add new" action FIRST, Actions dropdown LAST */}
            <Button size="sm" onClick={() => onConnectWorker()}>+ Connect Worker</Button>
            {/* TG.6/TG.8 — primary entry point for the routing rules editor.
                Lives with the workers because that's where the user thinks
                about routing (per the spec). */}
            <Button variant="secondary" size="sm" onClick={() => onOpenRoutingRules()}>
              Routing rules…
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger className="inline-flex items-center gap-1 rounded-lg border border-border bg-background px-2.5 h-7 text-[0.8rem] font-medium text-foreground hover:bg-muted cursor-pointer">
                Actions <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuGroup>
                  <DropdownMenuItem
                    onClick={onCleanAllCaches}
                    disabled={!workers.some(w => w.online)}
                    title={!workers.some(w => w.online) ? 'No workers are online' : undefined}
                  >
                    Clean All Caches
                  </DropdownMenuItem>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
        {tagsEditClientId && (() => {
          const w = workers.find(x => x.client_id === tagsEditClientId);
          if (!w) return null;
          // Bug #11: fleet-wide pool — union of every worker's tags + every
          // device's comma-separated tags string, sorted, deduped.
          const pool = new Set<string>();
          for (const wx of workers) {
            if (wx.tags) for (const t of wx.tags) pool.add(t);
          }
          for (const t of targets) {
            if (t.tags) for (const x of t.tags.split(',').map(s => s.trim()).filter(Boolean)) pool.add(x);
          }
          const suggestions = Array.from(pool).sort();
          return (
            <TagsEditDialog
              open
              onOpenChange={(open) => { if (!open) setTagsEditClientId(null); }}
              subject={`Worker ${w.hostname}`}
              initial={w.tags ?? []}
              suggestions={suggestions}
              onSave={async (tags) => {
                await setWorkerTags(w.client_id, tags);
                // SWR key is 'workers' (see App.tsx); 1Hz poll would catch
                // up on its own but mutate() snaps the UI immediately.
                await mutate('workers');
              }}
            />
          );
        })()}
        {diskQuotaEditClientId && (() => {
          const w = workers.find(x => x.client_id === diskQuotaEditClientId);
          if (!w) return null;
          return (
            <SetDiskQuotaDialog
              key={w.client_id}
              hostname={w.hostname}
              currentOverrideBytes={w.disk_quota_override_bytes ?? null}
              defaultBytes={w.default_worker_disk_quota_bytes ?? 10 * 1024 ** 3}
              onSave={async (bytes) => {
                await onSetDiskQuota(w.client_id, bytes);
                await mutate('workers');
              }}
              onClose={() => setDiskQuotaEditClientId(null)}
            />
          );
        })()}
        {/* TG.6 filter pills — same shape as the Devices tab. Hidden when
            workers have no tags yet so the bar doesn't show empty. */}
        <TagFilterBar tags={tagPool} selected={tagFilter} onChange={setTagFilter} />
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {HEADER_ORDER.map(id => renderHeader(id))}
              </tr>
            </thead>
            <tbody>
              {workers.length === 0 ? (
                <tr className="empty-row">
                  <td colSpan={8}>No workers registered — click &quot;+ Connect Worker&quot; to add one</td>
                </tr>
              ) : rows}
            </tbody>
          </table>
        </div>
      </div>

      {/* TG.9: RoutingRulesModal moved to App.tsx so the Queue's
          BLOCKED-badge click can target the same instance. */}
    </div>
  );
}
