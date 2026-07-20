import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ExternalLink, Eye, EyeOff, Moon, Settings as SettingsIcon, Sun } from 'lucide-react';
import useSWR, { useSWRConfig } from 'swr';
import {
  cancelJobs,
  cleanWorkerCache,
  clearQueue,
  commitFile,
  compile,
  deleteTarget,

  removeJobs,
  getDevices,
  getEsphomeVersions,
  refreshEsphomeVersions,
  getInitialAddonVersion,
  getQueue,
  getServerInfo,
  getSettings,
  getTargets,
  getWorkers,
  type AppSettings,
  isUnauthorizedError,
  removeWorker,
  renameTarget,
  setWorkerParallelJobs,
  setWorkerDiskQuota,
  retryAllFailed,
  retryJobs,
  requestServerDiagnostics,
  requestWorkerDiagnostics,
  setEsphomeVersion,
  setInitialAddonVersion,
  validateConfig,
  setTargetSchedule,
  deleteTargetSchedule,
  pinTargetVersion,
  unpinTargetVersion,
} from './api/client';
import { ConnectWorkerModal } from './components/ConnectWorkerModal';
import { EsphomeInstallBanner } from './components/EsphomeInstallBanner';
import { DeviceLogModal } from './components/DeviceLogModal';
import { DevicesTab, RenameModal } from './components/DevicesTab';
import { NewDeviceModal } from './components/NewDeviceModal';
import { UpgradeModal } from './components/UpgradeModal';
import { RenderedConfigModal } from './components/RenderedConfigModal';
import PingDeviceModal from './components/PingDeviceModal';
import InstallToAddressModal from './components/InstallToAddressModal';
// ScheduleModal retired in #22 — absorbed into the unified UpgradeModal.
import { SchedulesTab } from './components/SchedulesTab';
import { EditorModal } from './components/EditorModal';
import { EsphomeVersionDropdown } from './components/EsphomeVersionDropdown';
import { LogModal } from './components/LogModal';
import { QueueTab } from './components/QueueTab';
import { SettingsDrawer } from './components/SettingsDrawer';
import { VersioningOnboardingModal } from './components/VersioningOnboardingModal';
import { HistoryPanel } from './components/HistoryPanel';
import { CompileHistoryPanel } from './components/CompileHistoryPanel';
import { toast } from 'sonner';
import { Toaster } from './components/ui/sonner';
import { Button } from './components/ui/button';
import { Input } from './components/ui/input';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './components/ui/dialog';
import { WorkersTab } from './components/WorkersTab';
import { RoutingRulesModal } from './components/RoutingRulesModal';
import type { Device, Job, Target, Worker } from './types';
import { setDateFormatPref, setTimeFormatPref, stripYaml } from './utils';
import { downloadTextFile } from './utils/terminal';
import esphomeLogoUrl from './assets/esphome-logo.svg';
import i18n from './i18n';
import './theme.css';

// I18N.2 (#141): map `AppSettings.language` to an i18next locale.
// 'auto' resolves to the browser's preferred language; an unknown
// browser locale falls through to 'en' so a Spanish browser doesn't
// see raw keys until we ship a Spanish catalog.
function resolveLanguage(setting: 'auto' | 'en' | 'de'): string {
  if (setting !== 'auto') return setting;
  const nav = (typeof navigator !== 'undefined' ? navigator.language : 'en').toLowerCase();
  if (nav.startsWith('de')) return 'de';
  return 'en';
}

type TabName = 'devices' | 'queue' | 'workers' | 'schedules';

function getTabCount(
  tab: TabName,
  targets: Target[],
  devices: Device[],
  queue: Job[],
  workers: Worker[],
): string {
  if (tab === 'devices') {
    const unmanaged = devices.filter(d => !d.compile_target);
    const totalOnline = targets.filter(t => t.online).length + unmanaged.filter(d => d.online).length;
    const totalKnown = targets.filter(t => t.online != null).length + unmanaged.length;
    return totalKnown ? `${totalOnline}/${totalKnown}` : String(targets.length || '');
  }
  if (tab === 'queue') {
    const active = queue.filter(j => ['pending', 'working'].includes(j.state)).length;
    const failed = queue.filter(j => ['failed', 'timed_out'].includes(j.state)).length;
    if (active) return `${active} active`;
    if (failed) return `${failed} failed`;
    if (queue.length) return `${queue.length} done`;
    return '0';
  }
  if (tab === 'workers') {
    const online = workers.filter(c => c.online).length;
    return `${online}/${workers.length}`;
  }
  if (tab === 'schedules') {
    const scheduled = targets.filter(t => t.schedule || t.schedule_once).length;
    return String(scheduled);
  }
  return '';
}

function getInitialTheme(): 'dark' | 'light' {
  const stored = localStorage.getItem('theme');
  if (stored === 'light' || stored === 'dark') return stored;
  return 'dark';
}

// #31: Reconcile the user's schedule-mode version choice with the device's
// current pin. `desiredVersion === null` means "Latest" (ensure unpinned);
// a specific string means "ensure pinned to this version".
async function applyScheduleVersion(
  target: string,
  currentPin: string | null,
  desiredVersion: string | null,
): Promise<void> {
  if (desiredVersion === null) {
    if (currentPin) await unpinTargetVersion(target);
  } else if (desiredVersion !== currentPin) {
    await pinTargetVersion(target, desiredVersion);
  }
}

const _VALID_TABS: TabName[] = ['devices', 'queue', 'workers', 'schedules'];

function _initialTab(): TabName {
  // QS.27: URL query param wins over sessionStorage so deep-links like
  // `?tab=queue` work reliably from HA sidebar links or bookmarks. HA
  // Ingress strips the query string from the panel URL but users can
  // still land here from the "Open web UI" direct-port link, from a
  // bookmark, or from a row-deep-link shared by another user.
  if (typeof window !== 'undefined') {
    try {
      const q = new URLSearchParams(window.location.search).get('tab');
      if (q && (_VALID_TABS as string[]).includes(q)) {
        return q as TabName;
      }
    } catch {
      // malformed URL — fall through to sessionStorage
    }
  }
  const stored = sessionStorage.getItem('activeTab');
  if (stored && (_VALID_TABS as string[]).includes(stored)) {
    return stored as TabName;
  }
  return 'devices';
}

export default function App() {
  const [activeTab, setActiveTab] = useState<TabName>(_initialTab);
  // SP.4: Settings drawer (gear icon in header). Lifted here so other
  // surfaces can deep-link into Settings later (e.g. a tooltip's
  // "Configure auto-commit →").
  const [settingsOpen, setSettingsOpen] = useState(false);
  // AV.6: per-file history panel. `null` = closed; otherwise the filename.
  const [historyTarget, setHistoryTarget] = useState<string | null>(null);
  // JH.5: which target's Compile History drawer is open (null = closed).
  const [compileHistoryTarget, setCompileHistoryTarget] = useState<string | null>(null);
  // Bug #31: nonce bumped whenever the History panel reports an on-disk
  // change for the currently-edited file (rollback, manual commit, etc).
  // EditorModal depends on this in its fetch effect so the buffer
  // reloads to reflect the restored version.
  const [editorReloadNonce, setEditorReloadNonce] = useState(0);
  // AV.7: optional "from" hash preset passed to HistoryPanel so the
  // "Diff since compile" flow lands on (from=job.config_hash, to=Current).
  const [historyFromHash, setHistoryFromHash] = useState<string | null>(null);
  // Bug #16: manual-commit dialog state. When set, the Commit-Changes
  // dialog renders with this target prefilled.
  const [commitDialogTarget, setCommitDialogTarget] = useState<string | null>(null);
  const [commitDialogMessage, setCommitDialogMessage] = useState('');
  const [commitDialogBusy, setCommitDialogBusy] = useState(false);
  // RC.1: read-only "rendered config" viewer. ``null`` = closed; the
  // string is the target filename. Modal fetches /rendered-config on
  // open and discards the output on close — the response carries
  // plaintext !secret values, so we never persist it.
  const [renderedConfigTarget, setRenderedConfigTarget] = useState<string | null>(null);
  // DM.2: target whose Ping modal is currently open (or null if none).
  const [pingTarget, setPingTarget] = useState<string | null>(null);
  // DM.3: target whose Install-to-address modal is currently open.
  const [installAddressTarget, setInstallAddressTarget] = useState<string | null>(null);
  // QS.6: SWR's default compare (stable-hash) already prevents re-renders
  // when polled data is structurally unchanged. The custom JSON.stringify
  // compare we used to have was strictly worse — O(n) serialization of the
  // full response on every tick, breaks on undefined/circular, and hides
  // legitimate key-order differences.
  // QS.7: bubble errors to console instead of silently swallowing them —
  // previously every SWR poll failure disappeared into a `() => {}` sink.
  const logSwrError = useCallback((key: string) => (err: unknown) => {
    console.error('SWR', key, err);
  }, []);

  // Bug #2: invalidate the archived-configs SWR key after archive/restore so
  // the Devices toolbar's "Restore from archive" button enables/disables
  // without waiting for a manual reload.
  const { mutate: mutateGlobal } = useSWRConfig();

  const { data: serverInfo = { token: '', port: 8765 }, error: serverInfoError, mutate: mutateServerInfo } = useSWR(
    'serverInfo',
    getServerInfo,
    {
      // SE.8: tighten the poll during an in-flight ESPHome install so the
      // banner + /ui/api/esphome-version dropdown refresh promptly once
      // the server transitions to `ready`. 30 s otherwise.
      refreshInterval: (data) =>
        data?.esphome_install_status === 'installing' ? 3_000 : 30_000,
      onError: logSwrError('serverInfo'),
    },
  );
  const { data: esphomeVersions = { selected: null, detected: null, available: [] }, mutate: mutateEsphomeVersions } = useSWR(
    'versions',
    getEsphomeVersions,
    { refreshInterval: 15 * 60_000, onError: logSwrError('versions') },
  );
  // #82 / UX_REVIEW §3.10 — subscribe to /ui/api/settings at the app
  // root so the time-format preference propagates to every surface
  // (Queue, History, Log timestamps) via the module-local pref in
  // utils/format.ts. SWR's default dedupe means this is a single fetch
  // shared with the Settings drawer + EditorModal's own subscribers.
  const { data: appSettings, mutate: mutateAppSettings } = useSWR<AppSettings>('settings', getSettings, {
    refreshInterval: 30_000,
    onError: logSwrError('settings'),
  });
  useEffect(() => {
    if (appSettings?.time_format) setTimeFormatPref(appSettings.time_format);
  }, [appSettings?.time_format]);
  useEffect(() => {
    if (appSettings?.date_format) setDateFormatPref(appSettings.date_format);
  }, [appSettings?.date_format]);
  // I18N.2 (#141): propagate AppSettings.language → i18next. The
  // singleton is already initialised in main.tsx; this effect just
  // calls changeLanguage() when the setting toggles. No-op while
  // appSettings is still loading (first render).
  useEffect(() => {
    if (!appSettings?.language) return;
    const resolved = resolveLanguage(appSettings.language);
    if (i18n.language !== resolved) {
      void i18n.changeLanguage(resolved);
    }
  }, [appSettings?.language]);
  // #145: stamp data-font-size on <html> so the CSS in index.css can
  // pick up the override and scale the Tailwind type ramp. 'normal' is
  // the default; we still set the attribute explicitly so the CSS
  // selector matches and a future stylesheet diff is auditable.
  useEffect(() => {
    const root = document.documentElement;
    const size = appSettings?.font_size ?? 'normal';
    root.setAttribute('data-font-size', size);
  }, [appSettings?.font_size]);
  // Poll at 1 Hz for live-feeling updates. Workers + queue are pure in-memory
  // reads. Targets/devices does a readdir + per-target stat() for mtime cache
  // checks (metadata resolution is cached and only re-fires when a file
  // changes), which is cheap on Linux but not free — if this becomes a
  // concern on large config dirs, add a server-side snapshot cache.
  const { data: workers = [], error: workersError, mutate: mutateWorkers } = useSWR(
    'workers',
    getWorkers,
    { refreshInterval: 1_000, onError: logSwrError('workers') },
  );
  const { data: devicesAndTargets, error: devicesError, mutate: mutateDevices } = useSWR(
    'devices',
    async () => { const [t, d] = await Promise.all([getTargets(), getDevices()]); return { targets: t, devices: d }; },
    { refreshInterval: 1_000, onError: logSwrError('devices') },
  );
  const targets = devicesAndTargets?.targets ?? [];
  const devices = devicesAndTargets?.devices ?? [];
  const { data: queue = [], error: queueError, mutate: mutateQueue } = useSWR(
    'queue',
    getQueue,
    { refreshInterval: 1_000, onError: logSwrError('queue') },
  );

  // #84: if the session expired (or direct-port user has a stale/wrong
  // ?token=), every protected endpoint now returns 401. The tabs would
  // otherwise show "No devices found" / "No workers registered" etc.,
  // which falsely implies the fleet is empty. Detect the 401 on any of
  // the four always-polling SWR hooks and render a dedicated
  // "Session expired" overlay instead.
  const isUnauthenticated = (
    isUnauthorizedError(serverInfoError) ||
    isUnauthorizedError(workersError) ||
    isUnauthorizedError(devicesError) ||
    isUnauthorizedError(queueError)
  );
  // Exclude validation-only jobs from display (they run server-side and auto-prune)
  const displayQueue = useMemo(() => queue.filter(j => !j.validate_only), [queue]);
  // Map of target filename → active (PENDING or WORKING) job, used by the
  // Devices tab to render an "Upgrading…" status on rows whose compile is
  // currently in flight (#32). The most recent active job wins if a target
  // somehow has more than one — the queue dedupes by target so this should
  // be at most one in practice.
  const activeJobsByTarget = useMemo(() => {
    const map = new Map<string, typeof displayQueue[number]>();
    for (const j of displayQueue) {
      if (j.state === 'pending' || j.state === 'working') {
        map.set(j.target, j);
      }
    }
    return map;
  }, [displayQueue]);

  const [theme, setTheme] = useState<'dark' | 'light'>(getInitialTheme);
  const [streamerMode, setStreamerMode] = useState(() => localStorage.getItem('streamerMode') === 'true');

  useEffect(() => {
    document.documentElement.classList.toggle('streamer', streamerMode);
    localStorage.setItem('streamerMode', String(streamerMode));
  }, [streamerMode]);

  const [logJobId, setLogJobId] = useState<string | null>(null);
  // WL.3: separate state for the worker-log dialog; both feed the same
  // `<LogModal>` but only one can be open at a time (they share the
  // xterm + WS transport).
  const [logWorkerId, setLogWorkerId] = useState<string | null>(null);
  const [deviceLogTarget, setDeviceLogTarget] = useState<string | null>(null);
  const [editorTarget, setEditorTarget] = useState<string | null>(null);
  const [connectModalOpen, setConnectModalOpen] = useState(false);
  const [connectModalPreset, setConnectModalPreset] = useState<import('./types').WorkerPreset | null>(null);
  // #22: unified Upgrade modal. Stores target list + display name + which mode to open in.
  // Bug #107: the bulk Upgrade dropdown (Upgrade All / Online / Outdated /
  // Selected) now routes through this modal too — `targets` carries the
  // affected set; single-target callers wrap in a 1-element array.
  // Bug #109: `seed` carries the original job's parameters when the user
  // reruns from the Queue or LogModal so the modal opens pre-populated.
  const [upgradeModalTarget, setUpgradeModalTarget] = useState<{
    targets: string[];
    displayName: string;
    defaultMode: 'now' | 'schedule';
    seed?: {
      pinnedClientId?: string | null;
      workerTagFilter?: { op: 'all_of' | 'any_of' | 'none_of'; tags: string[] } | null;
      esphomeVersion?: string | null;
      action?: 'upgrade-now' | 'download-now';
    };
  } | null>(null);
  const [renameModalTarget, setRenameModalTarget] = useState<string | null>(null);
  // CD.4-CD.6: shared "create / duplicate" modal state. null = closed, object = open.
  // sourceTarget is set when duplicating an existing device.
  const [newDeviceModal, setNewDeviceModal] = useState<{ mode: 'new' | 'duplicate'; sourceTarget?: string } | null>(null);
  // TG.9: lifted out of WorkersTab so the Queue's BLOCKED-badge click can
  // also open it (deep-linked to the offending rule). null = closed, ''
  // = open in list mode, '<id>' = open with that rule pre-selected for
  // edit. The QueueTab's badge passes job.blocked_reason.rule_id.
  const [routingRulesEditId, setRoutingRulesEditId] = useState<string | null>(null);
  // #42: targets that were just created via the NewDeviceModal and haven't
  // been saved yet. If the editor closes without a successful save, the
  // stub/duplicated file is deleted so cancelled-out creates don't leave
  // orphan YAMLs behind. Use a ref so synchronous onClose callbacks in the
  // editor see the latest value.
  const unsavedNewTargetsRef = useRef<Set<string>>(new Set());

  // Apply theme to <html> element on mount and on change
  useEffect(() => {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem('theme', theme);
  }, [theme]);

  // Helper to match the old addToast(msg, type) pattern
  const addToast = useCallback((message: string, type: 'info' | 'success' | 'error' = 'info') => {
    if (type === 'success') toast.success(message);
    else if (type === 'error') toast.error(message);
    else toast.info(message);
  }, []);

  // ---- Version-change detection ----
  // Track addon version across SWR refreshes; reload the page when it changes.
  useEffect(() => {
    const version = serverInfo.addon_version;
    if (!version) return;
    const prev = getInitialAddonVersion();
    setInitialAddonVersion(version);
    if (prev !== null && version !== prev) {
      addToast('New version detected — reloading...', 'info');
      setTimeout(() => location.reload(), 1500);
    }
  }, [serverInfo.addon_version]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Tab navigation ----

  const switchTab = useCallback((name: TabName) => {
    setActiveTab(name);
    sessionStorage.setItem('activeTab', name);
    // QS.27: reflect the tab in the URL so copy-paste / browser-back
    // navigation works. pushState (not setting window.location) avoids
    // a full reload. Skip in HA Ingress where the `X-Ingress-Path`
    // ownership means we shouldn't rewrite the URL.
    try {
      const url = new URL(window.location.href);
      url.searchParams.set('tab', name);
      window.history.replaceState(null, '', url);
    } catch {
      // non-browser env or malformed URL — sessionStorage carries it.
    }
  }, []);

  // ---- Actions ----

  // QS.20: handlers passed to DevicesTab / other child components are
  // memoized so the columns hook (useDeviceColumns) can keep its memo cache
  // across SWR polls. Without useCallback they'd be fresh refs every render
  // and the columns block would rebuild on every 1Hz tick.

  // #22: open the unified Upgrade modal. defaultMode controls whether it
  // opens on "Now" or "Schedule" tab.
  const handleOpenUpgradeModal = useCallback((target: string, defaultMode: 'now' | 'schedule' = 'now') => {
    const t = targets.find(x => x.target === target);
    const displayName = t?.friendly_name || stripYaml(target);
    setUpgradeModalTarget({ targets: [target], displayName, defaultMode });
  }, [targets]);

  // Bug #107: open the Upgrade modal for a multi-device set. Used by the
  // four bulk-upgrade items in the Devices toolbar (All / Online /
  // Outdated / Selected). Caller is responsible for materialising the
  // target list and a human-readable displayName ("12 devices",
  // "5 outdated devices", etc.).
  const handleOpenUpgradeModalMany = useCallback((targets_: string[], displayName: string, defaultMode: 'now' | 'schedule' = 'now') => {
    if (targets_.length === 0) return;
    setUpgradeModalTarget({ targets: targets_, displayName, defaultMode });
  }, []);

  async function handleUpgradeConfirm(params: {
    pinnedClientId: string | null;
    esphomeVersion: string | null;
    updatePin?: string | null;
    downloadOnly?: boolean;
    // Bug #97: per-job worker_tag_filter from the Upgrade modal's
    // "Tag expression" worker-selection radio. Mutually exclusive
    // with pinnedClientId at the UI level.
    workerTagFilter?: { op: 'all_of' | 'any_of' | 'none_of'; tags: string[] } | null;
    // Bug #110: true when the user confirmed the routing-rule
    // conflict warning — the server will skip routing-rule
    // eligibility checks for this job.
    bypassRoutingRules?: boolean;
  }) {
    const ctx = upgradeModalTarget;
    if (!ctx) return;
    setUpgradeModalTarget(null);
    try {
      // #12: if the user changed the version on a pinned device, update
      // the pin first. Pin updates only happen in the single-target
      // case — the modal suppresses the pin warning when there's no
      // single device to compare against (multi-target sets have no
      // shared "current pin" to bump).
      if (params.updatePin && ctx.targets.length === 1) {
        await pinTargetVersion(ctx.targets[0], params.updatePin);
      }
      // Bug #107: bulk path enqueues the entire set in one POST so the
      // server-side counter / toast reads correctly (`Queued N device(s)`).
      await compile(
        ctx.targets,
        params.pinnedClientId ?? undefined,
        params.esphomeVersion ?? undefined,
        params.downloadOnly ?? false,
        params.workerTagFilter ?? undefined,
        params.bypassRoutingRules ?? undefined,
      );
      const versionSuffix = params.esphomeVersion ? ` (ESPHome ${params.esphomeVersion})` : '';
      const workerSuffix = params.pinnedClientId
        ? ` on ${workers.find(w => w.client_id === params.pinnedClientId)?.hostname ?? params.pinnedClientId}`
        : params.workerTagFilter
          ? ` (workers ${params.workerTagFilter.op.replace('_', ' ')} [${params.workerTagFilter.tags.join(', ')}])`
          : '';
      const pinSuffix = params.updatePin && ctx.targets.length === 1 ? ` (pin updated to ${params.updatePin})` : '';
      // FD.3: different toast verb when producing a downloadable binary
      // so the user understands the device won't be OTA'd this round.
      const verb = params.downloadOnly ? 'Compile-and-download queued for' : 'Queued';
      addToast(`${verb} ${ctx.displayName}${workerSuffix}${versionSuffix}${pinSuffix}`, 'success');
      switchTab('queue');
      mutateQueue();
      mutateDevices();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  // #25/#26: validation result returned directly to the caller (the editor)
  // so it can show the output inline.
  async function handleValidate(target: string): Promise<{ success: boolean; output: string } | null> {
    try {
      return await validateConfig(target);
    } catch (err) {
      addToast('Validate failed: ' + (err as Error).message, 'error');
      return null;
    }
  }

  async function handleCancelJobs(ids: string[]) {
    try {
      const data = await cancelJobs(ids);
      if (data.cancelled > 0) {
        const msg = data.cancelled === 1
          ? `Cancelled ${stripYaml(queue.find(j => j.id === ids[0])?.target ?? ids[0])}`
          : `Cancelled ${data.cancelled} jobs`;
        addToast(msg, 'success');
      }
      mutateQueue();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  // Bug #109: open the UpgradeModal seeded with a previous job's
  // worker / version / action / tag-filter so a single-job rerun goes
  // through the same picker the user originally saw — but with the
  // chance to tweak any field before re-submitting. Bulk reruns
  // (Rerun All Failed / Rerun Selected) keep the immediate
  // re-enqueue path because there's no single set of params to seed.
  function rerunSingleJobViaModal(job: Job) {
    const t = targets.find(x => x.target === job.target);
    const displayName = t?.friendly_name || stripYaml(job.target);
    setUpgradeModalTarget({
      targets: [job.target],
      displayName,
      defaultMode: 'now',
      seed: {
        pinnedClientId: job.pinned_client_id ?? null,
        workerTagFilter: job.worker_tag_filter ?? null,
        esphomeVersion: job.esphome_version ?? null,
        action: job.download_only ? 'download-now' : 'upgrade-now',
      },
    });
  }

  async function handleRetryJobs(ids: string[]) {
    // Bug #109: when the user reruns a single existing job from the
    // Queue / LogModal, open the UpgradeModal pre-populated with that
    // job's worker / version / action / tag-filter so the user can
    // tweak before submitting. Bulk reruns (>1 id) keep the immediate
    // re-enqueue path — there's no single set of params to seed a
    // modal with.
    if (ids.length === 1) {
      const job = queue.find(j => j.id === ids[0]);
      if (job) {
        rerunSingleJobViaModal(job);
        return;
      }
    }
    try {
      const data = await retryJobs(ids);
      if (data.retried > 0) {
        const msg = data.retried === 1
          ? `Rerunning ${stripYaml(queue.find(j => j.id === ids[0])?.target ?? ids[0])}`
          : `Rerunning ${data.retried} jobs`;
        addToast(msg, 'success');
      }
      mutateQueue();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  async function handleRetryAllFailed() {
    try {
      const data = await retryAllFailed();
      if (data.retried > 0) {
        const msg = data.retried === 1 ? 'Rerunning 1 job' : `Rerunning ${data.retried} failed jobs`;
        addToast(msg, 'success');
      }
      mutateQueue();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  async function handleClearSucceeded() {
    try {
      const data = await clearQueue(['success'], true);
      if (data.cleared > 0) {
        const msg = data.cleared === 1 ? 'Cleared 1 succeeded job' : `Cleared ${data.cleared} succeeded jobs`;
        addToast(msg, 'success');
      }
      mutateQueue();
    } catch {
      addToast('Clear failed', 'error');
    }
  }

  async function handleClearJobs(ids: string[]) {
    try {
      await removeJobs(ids);
      if (ids.length > 1) {
        addToast(`Cleared ${ids.length} jobs`, 'success');
      }
      mutateQueue();
    } catch {
      addToast('Clear failed', 'error');
    }
  }

  async function handleClearFinished() {
    try {
      const data = await clearQueue(['success', 'failed', 'timed_out', 'cancelled']);
      if (data.cleared > 0) {
        const msg = data.cleared === 1 ? 'Cleared 1 finished job' : `Cleared ${data.cleared} finished jobs`;
        addToast(msg, 'success');
      }
      mutateQueue();
    } catch {
      addToast('Clear failed', 'error');
    }
  }

  // #54: cancel all active + clear all terminal in one action
  async function handleClearAll() {
    try {
      const activeIds = displayQueue
        .filter(j => j.state === 'pending' || j.state === 'working')
        .map(j => j.id);
      if (activeIds.length > 0) {
        await cancelJobs(activeIds);
      }
      await clearQueue(['success', 'failed', 'timed_out', 'cancelled']);
      addToast('Queue cleared', 'success');
      mutateQueue();
    } catch {
      addToast('Clear failed', 'error');
    }
  }


  async function handleCleanWorkerCache(id: string) {
    try {
      await cleanWorkerCache(id);
      const workerName = workers.find(w => w.client_id === id)?.hostname || id;
      addToast(`Clean build cache requested for ${workerName}`, 'success');
      // #11: mutate so the worker's pending_clean flag shows in the UI
      // immediately rather than after the next 1Hz tick.
      mutateWorkers();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  async function handleCleanAllCaches() {
    const onlineWorkers = workers.filter(w => w.online);
    if (!onlineWorkers.length) return;
    try {
      await Promise.all(onlineWorkers.map(w => cleanWorkerCache(w.client_id)));
      addToast(`Clean build cache requested for ${onlineWorkers.length} worker${onlineWorkers.length > 1 ? 's' : ''}`, 'success');
      mutateWorkers();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  async function handleRemoveWorker(id: string) {
    try {
      await removeWorker(id);
      addToast('Worker removed', 'success');
      mutateWorkers();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  // #109: "Request diagnostics" — fires either the server self-dump
  // path or the round-trip worker path, downloads the resulting text
  // file, and surfaces worker-side failures (py-spy denied, etc.)
  // inline in the toast rather than blowing up the whole action.
  async function handleRequestWorkerDiagnostics(id: string) {
    const workerName = workers.find(w => w.client_id === id)?.hostname || id;
    addToast(`Requesting diagnostics from ${workerName}…`, 'info');
    try {
      const { ok, filename, body } = await requestWorkerDiagnostics(id);
      downloadTextFile(body, filename);
      if (ok) {
        addToast(`Diagnostics downloaded from ${workerName}`, 'success');
      } else {
        addToast(`Diagnostics request returned an error — see the downloaded file for details`, 'error');
      }
    } catch (err) {
      addToast('Diagnostics failed: ' + (err as Error).message, 'error');
    }
  }

  async function handleRequestServerDiagnostics() {
    addToast('Requesting server diagnostics…', 'info');
    try {
      const { ok, filename, body } = await requestServerDiagnostics();
      downloadTextFile(body, filename);
      if (ok) {
        addToast('Server diagnostics downloaded', 'success');
      } else {
        addToast('Server diagnostics returned an error — see the downloaded file for details', 'error');
      }
    } catch (err) {
      addToast('Server diagnostics failed: ' + (err as Error).message, 'error');
    }
  }

  async function handleSetParallelJobs(id: string, count: number) {
    try {
      await setWorkerParallelJobs(id, count);
      addToast(`Set to ${count} slot${count !== 1 ? 's' : ''} — worker will restart`, 'success');
      mutateWorkers();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  async function handleSetDiskQuota(id: string, bytes: number | null) {
    try {
      await setWorkerDiskQuota(id, bytes);
      const label = bytes == null
        ? 'Cleared override — using fleet default'
        : `Set quota to ${Math.round(bytes / (1024 ** 3))} GiB`;
      addToast(label, 'success');
      mutateWorkers();
    } catch (err) {
      addToast('Error: ' + (err as Error).message, 'error');
    }
  }

  const handleDeleteDevice = useCallback(async (target: string, archive: boolean) => {
    try {
      await deleteTarget(target, archive);
      addToast(`${archive ? 'Archived' : 'Deleted'} ${stripYaml(target)}`, 'success');
      mutateDevices();
      // Bug #2: archive populated → enable "Restore from archive" without a reload.
      if (archive) mutateGlobal('archived-configs');
    } catch (err) {
      addToast('Delete failed: ' + (err as Error).message, 'error');
    }
  }, [addToast, mutateDevices, mutateGlobal]);

  const handleRenameDevice = useCallback(async (oldTarget: string, newName: string) => {
    try {
      const result = await renameTarget(oldTarget, newName);
      addToast(`Renamed to ${stripYaml(result.new_filename)} — compiling new firmware...`, 'success');
      mutateDevices();
      mutateQueue();
      switchTab('queue');
    } catch (err) {
      addToast('Rename failed: ' + (err as Error).message, 'error');
    }
  }, [addToast, mutateDevices, mutateQueue, switchTab]);

  async function handleSelectEsphomeVersion(version: string) {
    try {
      await setEsphomeVersion(version);
      mutateEsphomeVersions({ ...esphomeVersions, selected: version }, false);
      addToast('ESPHome version set to ' + version, 'success');
    } catch (err) {
      addToast('Failed to set version: ' + (err as Error).message, 'error');
    }
  }

  // ---- Render ----

  const devicesCount = getTabCount('devices', targets, devices, displayQueue, workers);
  const queueCount = getTabCount('queue', targets, devices, displayQueue, workers);
  const workersCount = getTabCount('workers', targets, devices, displayQueue, workers);
  const schedulesCount = getTabCount('schedules', targets, devices, displayQueue, workers);

  // Seed version for connect modal: prefer selected esphome version, fall back to server_version field
  const seedVersion = esphomeVersions.selected ||
    (targets.length > 0 ? (targets[0].server_version ?? null) : null);

  return (
    <>
      <header>
        {/* #85: replaced the CDN-hotlinked ESPHome wordmark with just the
            house glyph served locally, paired with our own "Fleet for ESPHome"
            wordmark rendered in the app's own type. Also removes the
            dependency on a third-party CDN at page-load time (wordmark
            was served from media.esphome.io). */}
        <img
          src={esphomeLogoUrl}
          alt="Fleet for ESPHome"
          /* actual size is driven by `header img { height: 40px }` in
             theme.css — attributes here are just intrinsic-aspect hints
             for the browser (square → width = height). */
          height={40}
          width={40}
          style={{ display: 'block', flexShrink: 0 }}
        />
        <span
          style={{
            fontSize: 20,
            fontWeight: 600,
            color: 'var(--text)',
            whiteSpace: 'nowrap',
            letterSpacing: '-0.01em',
          }}
        >
          Fleet for ESPHome
        </span>
        <span className="rounded-full border border-[var(--border)] bg-[var(--surface2)] px-2 py-0.5 text-[11px] text-[var(--text-muted)] whitespace-nowrap">
          {serverInfo.addon_version ? `v${serverInfo.addon_version}` : 'v?'}
        </span>
        <EsphomeVersionDropdown
          versions={esphomeVersions}
          onSelect={handleSelectEsphomeVersion}
          onRefresh={async () => {
            addToast('Refreshing ESPHome versions...', 'info');
            // Bug #19: force the server to re-fetch PyPI (bypassing the
            // 1h TTL), then hand the response directly to SWR so the
            // dropdown reflects the fresh list immediately instead of
            // waiting for the background refresher to run.
            try {
              const fresh = await refreshEsphomeVersions();
              await mutateEsphomeVersions(fresh, false);
              addToast(`ESPHome version list updated (${fresh.available.length} versions)`, 'success');
            } catch (err) {
              addToast('Refresh failed: ' + (err as Error).message, 'error');
            }
          }}
        />
        {/* QS.3: <span onClick> → <button> for Secrets, theme, streamer. */}
        <button
          type="button"
          className="rounded-full border border-[var(--border)] bg-[var(--surface2)] px-2 py-0.5 text-[11px] text-[var(--text-muted)] whitespace-nowrap cursor-pointer"
          onClick={() => setEditorTarget('secrets.yaml')}
          title="Edit secrets.yaml"
        >
          Secrets
        </button>
        {/* QS.2/QS.15: aria-label on icon-only buttons; Lucide icons. */}
        <button
          type="button"
          aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          aria-pressed={theme === 'light'}
          className="inline-flex items-center justify-center w-7 h-7 rounded-full border border-[var(--border)] bg-[var(--surface2)] text-[var(--text-muted)] cursor-pointer hover:bg-[var(--border)]"
          onClick={() => setTheme(t => t === 'dark' ? 'light' : 'dark')}
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {theme === 'dark' ? <Sun className="size-3.5" /> : <Moon className="size-3.5" />}
        </button>
        <button
          type="button"
          aria-label={streamerMode ? 'Disable streamer mode' : 'Enable streamer mode (blur sensitive data)'}
          aria-pressed={streamerMode}
          className={`inline-flex items-center justify-center w-7 h-7 rounded-full border border-[var(--border)] bg-[var(--surface2)] cursor-pointer hover:bg-[var(--border)] ${streamerMode ? 'text-[var(--accent)]' : 'text-[var(--text-muted)]'}`}
          onClick={() => setStreamerMode(s => !s)}
          title={streamerMode ? 'Disable streamer mode' : 'Enable streamer mode (blur sensitive data)'}
        >
          {streamerMode ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
        </button>
        {/* #52: quick link to ESPHome Web for users who need to do a
            serial / USB flash as a short-term workaround (e.g. first-
            time provisioning, bricked device with no OTA path). Opens
            in a new tab — web.esphome.io is a Google-hosted tool, HA
            Ingress has no reason to tunnel it. */}
        <a
          href="https://web.esphome.io/"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 rounded-full border border-[var(--border)] bg-[var(--surface2)] px-2 py-0.5 text-[11px] text-[var(--text-muted)] whitespace-nowrap hover:bg-[var(--border)]"
          title="Open ESPHome Web (serial / USB flashing)"
        >
          ESPHome Web
          <ExternalLink className="size-3" aria-hidden />
        </a>
        {/* SP.4: Settings drawer — gear icon follows the same icon-button
            shape as theme and streamer above. */}
        <button
          type="button"
          aria-label="Settings"
          className="inline-flex items-center justify-center w-7 h-7 rounded-full border border-[var(--border)] bg-[var(--surface2)] text-[var(--text-muted)] cursor-pointer hover:bg-[var(--border)]"
          onClick={() => setSettingsOpen(true)}
          title="Settings"
        >
          <SettingsIcon className="size-3.5" />
        </button>
        <span className="spacer" />
        <span className="status-dot" title="Server online" />
      </header>

      <EsphomeInstallBanner serverInfo={serverInfo} onRefresh={() => void mutateServerInfo()} />

      <nav className="sticky top-[52px] z-40 flex overflow-x-auto border-b border-[var(--border)] bg-[var(--surface)] px-5">
        {(['devices', 'queue', 'workers', 'schedules'] as TabName[]).map(tab => {
          const count = tab === 'devices' ? devicesCount
            : tab === 'queue' ? queueCount
            : tab === 'workers' ? workersCount
            : schedulesCount;
          return (
            <button
              key={tab}
              className={`inline-flex items-center gap-1.5 px-4 h-11 bg-transparent border-none border-b-[3px] border-b-transparent text-[13px] font-medium cursor-pointer whitespace-nowrap transition-colors ${activeTab === tab ? 'text-[var(--text)] border-b-[var(--accent)]' : 'text-[var(--text-muted)] hover:text-[var(--text)]'}`}
              onClick={() => switchTab(tab)}
            >
              {tab.charAt(0).toUpperCase() + tab.slice(1)}{' '}
              <span className={`inline-block rounded-full px-1.5 py-px text-[11px] font-semibold ${activeTab === tab ? 'bg-[var(--accent)] text-white' : 'bg-[var(--surface2)] text-[var(--text-muted)]'}`}>
                {count}
              </span>
            </button>
          );
        })}
      </nav>

      <main>
        {activeTab === 'devices' && (
          <DevicesTab
            targets={targets}
            devices={devices}
            workers={workers}
            streamerMode={streamerMode}
            activeJobsByTarget={activeJobsByTarget}
            onUpgradeOne={handleOpenUpgradeModal}
            onUpgradeMany={handleOpenUpgradeModalMany}
            onEdit={setEditorTarget}
            onLogs={setDeviceLogTarget}
            onToast={addToast}
            onDelete={handleDeleteDevice}
            onRename={handleRenameDevice}
            onSchedule={(t) => handleOpenUpgradeModal(t, 'schedule')}
            onNewDevice={() => setNewDeviceModal({ mode: 'new' })}
            onDuplicate={(sourceTarget) => setNewDeviceModal({ mode: 'duplicate', sourceTarget })}
            onOpenHistory={(target) => setHistoryTarget(target)}
            onOpenCompileHistory={(target) => setCompileHistoryTarget(target)}
            onCommitChanges={(target) => { setCommitDialogMessage(''); setCommitDialogTarget(target); }}
            onViewRenderedConfig={(target) => setRenderedConfigTarget(target)}
            onPing={(target) => setPingTarget(target)}
            onInstallToAddress={(target) => setInstallAddressTarget(target)}
            onRefresh={() => mutateDevices()}
          />
        )}
        {activeTab === 'queue' && (
          <QueueTab
            queue={displayQueue}
            targets={targets}
            workers={workers}
            onCancel={handleCancelJobs}
            onRetry={handleRetryJobs}
            onClear={handleClearJobs}
            onRetryAllFailed={handleRetryAllFailed}
            onClearSucceeded={handleClearSucceeded}
            onClearFinished={handleClearFinished}
            onClearAll={handleClearAll}
            onOpenLog={setLogJobId}
            onEdit={(target) => setEditorTarget(target)}
            onOpenHistoryDiff={(target, fromHash) => {
              setHistoryFromHash(fromHash);
              setHistoryTarget(target);
            }}
            // TG.9: BLOCKED-badge click opens the routing-rules editor
            // pre-selected to the rule that fired.
            onOpenRoutingRule={(ruleId) => setRoutingRulesEditId(ruleId)}
            // #209: device-section actions mirrored from the Devices tab.
            onToast={addToast}
            onLogs={setDeviceLogTarget}
            onOpenCompileHistory={(target) => setCompileHistoryTarget(target)}
            onPing={(target) => setPingTarget(target)}
            onInstallToAddress={(target) => setInstallAddressTarget(target)}
          />
        )}
        {activeTab === 'workers' && (
          <WorkersTab
            workers={workers}
            targets={targets}
            queue={displayQueue}
            serverClientVersion={serverInfo.server_client_version}
            minImageVersion={serverInfo.min_image_version}
            onRemove={handleRemoveWorker}
            onSetParallelJobs={handleSetParallelJobs}
            onSetDiskQuota={handleSetDiskQuota}
            onCleanCache={handleCleanWorkerCache}
            onCleanAllCaches={handleCleanAllCaches}
            onConnectWorker={(preset) => { setConnectModalPreset(preset ?? null); setConnectModalOpen(true); }}
            onViewLogs={setLogWorkerId}
            onRequestDiagnostics={handleRequestWorkerDiagnostics}
            onOpenRoutingRules={() => setRoutingRulesEditId('')}
          />
        )}
        {activeTab === 'schedules' && (
          <SchedulesTab
            targets={targets}
            workers={workers}
            onSchedule={(t) => handleOpenUpgradeModal(t, 'schedule')}
            onRefresh={() => mutateDevices()}
            onToast={addToast}
          />
        )}
      </main>

      {isUnauthenticated && (
        <div
          role="alertdialog"
          aria-labelledby="unauth-heading"
          className="fixed inset-0 z-50 flex items-center justify-center bg-[rgba(0,0,0,0.55)] backdrop-blur-sm"
        >
          <div className="w-[min(440px,92vw)] rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 shadow-xl">
            <h2 id="unauth-heading" className="mb-2 text-lg font-semibold text-[var(--text)]">
              Session expired
            </h2>
            <p className="mb-4 text-sm text-[var(--text-muted)]">
              Fleet for ESPHome requires a valid Home Assistant session. Your
              session likely timed out, or the <code>?token=</code> you used
              for direct-port access is no longer valid. Reload to
              re-authenticate through Home Assistant.
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => location.reload()}
                className="inline-flex h-9 items-center rounded-md bg-[var(--accent)] px-4 text-sm font-medium text-white hover:opacity-90"
              >
                Reload
              </button>
            </div>
          </div>
        </div>
      )}

      <Toaster />

      {/* #98: first-login onboarding for config versioning. Rendered
          only when the server reports versioning_enabled='unset' —
          the modal writes 'on' or 'off' and then unmounts via the
          SWR mutate. No Esc/outside-click dismiss — the user has to
          make an explicit choice (see VersioningOnboardingModal). */}
      {appSettings?.versioning_enabled === 'unset' && (
        <VersioningOnboardingModal onDecided={() => void mutateAppSettings()} />
      )}

      <LogModal
        source={logJobId ? { kind: 'job', jobId: logJobId } : logWorkerId ? { kind: 'worker', workerId: logWorkerId } : null}
        queue={queue}
        workers={workers}
        onClose={() => { setLogJobId(null); setLogWorkerId(null); }}
        onRetry={handleRetryJobs}
        onEdit={(target) => { setLogJobId(null); setEditorTarget(target); }}
        onOpenHistoryDiff={(target, fromHash) => {
          setLogJobId(null);
          setHistoryFromHash(fromHash);
          setHistoryTarget(target);
        }}
        stacked={!!editorTarget}
      />

      {deviceLogTarget && (
        <DeviceLogModal
          target={deviceLogTarget}
          onClose={() => setDeviceLogTarget(null)}
        />
      )}

      {editorTarget && (
        <EditorModal
          target={editorTarget}
          // #42: on close, if this target was a just-created (unsaved) new
          // device, delete the stub file so cancelling out doesn't leave
          // an orphan YAML behind. onSaved fires first for successful saves
          // and removes the target from the unsaved set, so a saved close
          // won't trip the delete.
          onClose={() => {
            const closed = editorTarget;
            setEditorTarget(null);
            if (closed && unsavedNewTargetsRef.current.has(closed)) {
              unsavedNewTargetsRef.current.delete(closed);
              deleteTarget(closed, false).catch((err: Error) => {
                addToast('Cleanup of unsaved new device failed: ' + err.message, 'error');
              }).finally(() => mutateDevices());
            } else {
              mutateDevices();
            }
          }}
          onSaved={(target) => { unsavedNewTargetsRef.current.delete(target); }}
          onToast={addToast}
          onValidate={handleValidate}
          // #18: Save & Upgrade now goes through the same UpgradeModal as
          // the per-row Upgrade button, so the user can pick a worker and
          // ESPHome version before triggering the build. The editor still
          // saves first (in handleSaveAndUpgrade) — this just changes what
          // happens AFTER the save.
          onCompile={(target) => handleOpenUpgradeModal(target)}
          onOpenHistory={(target) => setHistoryTarget(target)}
          onViewRenderedConfig={(target) => setRenderedConfigTarget(target)}
          monacoTheme={theme === 'light' ? 'vs' : 'vs-dark'}
          esphomeVersion={esphomeVersions.selected ?? esphomeVersions.detected ?? undefined}
          // Bug #31: bump to trigger a re-fetch after History Restore.
          reloadNonce={editorReloadNonce}
        />
      )}

      {connectModalOpen && (
        <ConnectWorkerModal
          serverInfo={serverInfo}
          esphomeVersion={seedVersion}
          preset={connectModalPreset}
          // Bug #96 (was #27): worker tag pool only — the dialog
          // configures a *worker*, so suggesting device tags
          // (`kitchen`, `cosy`, `ratgdo`) is misleading and dilutes
          // the autocomplete signal. Worker-only matches the routing-
          // rule worker_match autocomplete in `RoutingRuleBuilder`.
          tagSuggestions={(() => {
            const pool = new Set<string>();
            for (const w of workers) {
              if (w.tags) for (const x of w.tags) pool.add(x);
            }
            return Array.from(pool).sort();
          })()}
          onClose={() => { setConnectModalOpen(false); setConnectModalPreset(null); }}
        />
      )}

      {/* SP.4: Settings drawer — always mounted so the Sheet component
          can animate its own open/close; internal SWR fetch is gated
          on the `open` prop so closed state is zero network traffic.
          Bug #17: dirtyTargets drives the toggle-off confirmation. */}
      <SettingsDrawer
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        dirtyTargets={targets.filter(t => t.has_uncommitted_changes).map(t => t.target)}
        onRequestServerDiagnostics={handleRequestServerDiagnostics}
      />

      {/* JH.5: per-device Compile History drawer. Mounted once;
          internal SWR gates on the `target !== null` open state. */}
      <CompileHistoryPanel
        target={compileHistoryTarget}
        onOpenChange={(open) => { if (!open) setCompileHistoryTarget(null); }}
        // Bug #41: hash-cell → AV.6 History drawer preset to
        // `from=hash, to=Current`. Matches Queue tab's Commit column.
        onOpenHistoryDiff={(target, fromHash) => {
          setHistoryFromHash(fromHash);
          setHistoryTarget(target);
        }}
      />

      {/* AV.6: per-file History + diff panel. Same Sheet pattern —
          mounted once, internal SWR gates on `filename !== null`.
          AV.7 passes an initialFromHash when opened via "Diff since
          compile" so the panel lands on the right comparison. */}
      <HistoryPanel
        filename={historyTarget}
        initialFromHash={historyFromHash}
        // #100: thread the app theme into the DiffEditor so the history
        // panel matches the surrounding surface in light mode instead
        // of hard-coding vs-dark. Same expression EditorModal uses.
        monacoTheme={theme === 'light' ? 'vs' : 'vs-dark'}
        onOpenChange={(open) => { if (!open) { setHistoryTarget(null); setHistoryFromHash(null); } }}
        // Bug #31: bump the nonce on rollback / manual commit so any
        // open EditorModal re-fetches the file content. Conditioned on
        // the editor being open for the same target — no point bumping
        // when the editor is closed (the next open will fetch fresh).
        onFileChanged={() => {
          if (editorTarget && historyTarget === editorTarget) {
            setEditorReloadNonce(n => n + 1);
          }
        }}
      />

      {/* Bug #16: manual-commit Dialog for the Devices-row "Commit
          changes…" hamburger action. Optional message defaults to the
          server-side "Manually committed from UI" marker when left blank. */}
      <Dialog
        open={commitDialogTarget !== null}
        onOpenChange={(open) => { if (!open) setCommitDialogTarget(null); }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Commit changes to {commitDialogTarget}</DialogTitle>
          </DialogHeader>
          <div className="px-4 py-3 flex flex-col gap-2 text-sm text-[var(--text)]">
            <p className="text-xs text-[var(--text-muted)]">
              Optional commit message. Leave blank to use the default{' '}
              <code className="font-mono text-xs">Manually committed from UI</code>.
            </p>
            <Input
              type="text"
              className="font-mono text-xs"
              placeholder="Manually committed from UI"
              value={commitDialogMessage}
              onChange={e => setCommitDialogMessage(e.target.value)}
              autoFocus
            />
          </div>
          <DialogFooter>
            <DialogClose>
              <Button variant="secondary" size="sm" disabled={commitDialogBusy}>Cancel</Button>
            </DialogClose>
            <Button
              size="sm"
              disabled={commitDialogBusy || commitDialogTarget === null}
              onClick={async () => {
                const target = commitDialogTarget;
                if (!target) return;
                setCommitDialogBusy(true);
                try {
                  const result = await commitFile(target, commitDialogMessage.trim() || undefined);
                  if (result.committed) {
                    addToast(`Committed ${result.short_hash}`, 'success');
                  } else {
                    addToast('Nothing to commit', 'info');
                  }
                  setCommitDialogTarget(null);
                  setCommitDialogMessage('');
                  await mutateDevices();
                } catch (err) {
                  addToast('Commit failed: ' + (err as Error).message, 'error');
                } finally {
                  setCommitDialogBusy(false);
                }
              }}
            >
              Commit
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* #22: Unified Upgrade modal — handles both immediate upgrades and scheduling.
          Bug #107: now also handles multi-device sets — `t` is undefined when
          there's no single device to read pin/schedule context from, which
          implicitly suppresses the modal's pin warning + the "Remove existing
          schedule" button. Schedule saves fan out across the target list. */}
      {upgradeModalTarget && (() => {
        const isMulti = upgradeModalTarget.targets.length > 1;
        const t = isMulti ? undefined : targets.find(x => x.target === upgradeModalTarget.targets[0]);
        // Bug #110: materialise per-target tag lists so the modal can
        // detect routing-rule conflicts client-side. parseTags
        // duplicates the trim/dedupe logic the server applies on the
        // YAML metadata round-trip.
        const parseTags = (s: string | null | undefined): string[] => {
          if (!s) return [];
          const seen = new Set<string>();
          const out: string[] = [];
          for (const part of s.split(',')) {
            const v = part.trim();
            if (!v || seen.has(v)) continue;
            seen.add(v);
            out.push(v);
          }
          return out;
        };
        const affectedTargetTags = upgradeModalTarget.targets.map(name => {
          const tt = targets.find(x => x.target === name);
          return parseTags(tt?.tags);
        });
        return (
          <UpgradeModal
            target={upgradeModalTarget.targets[0] ?? ''}
            displayName={upgradeModalTarget.displayName}
            workers={workers}
            esphomeVersions={esphomeVersions.available}
            defaultEsphomeVersion={esphomeVersions.selected ?? esphomeVersions.detected ?? null}
            pinnedVersion={t?.pinned_version}
            currentSchedule={t?.schedule}
            currentScheduleEnabled={t?.schedule_enabled}
            currentScheduleTz={t?.schedule_tz}
            currentOnce={t?.schedule_once}
            defaultMode={upgradeModalTarget.defaultMode}
            seed={upgradeModalTarget.seed}
            affectedTargetTags={affectedTargetTags}
            onUpgradeNow={handleUpgradeConfirm}
            onSaveSchedule={async (cron, version, tz) => {
              try {
                await Promise.all(upgradeModalTarget.targets.map(async (target) => {
                  const tt = targets.find(x => x.target === target);
                  await applyScheduleVersion(target, tt?.pinned_version ?? null, version);
                  await setTargetSchedule(target, cron, tz);
                }));
                addToast(`Schedule set for ${upgradeModalTarget.displayName}`, 'success');
                setUpgradeModalTarget(null);
                mutateDevices();
              } catch (err) {
                addToast('Schedule failed: ' + (err as Error).message, 'error');
              }
            }}
            onSaveOnce={async (datetime, version) => {
              try {
                const { setTargetScheduleOnce } = await import('./api/client');
                await Promise.all(upgradeModalTarget.targets.map(async (target) => {
                  const tt = targets.find(x => x.target === target);
                  await applyScheduleVersion(target, tt?.pinned_version ?? null, version);
                  await setTargetScheduleOnce(target, datetime);
                }));
                addToast(`One-time upgrade scheduled for ${upgradeModalTarget.displayName}`, 'success');
                setUpgradeModalTarget(null);
                mutateDevices();
              } catch (err) {
                addToast('Schedule failed: ' + (err as Error).message, 'error');
              }
            }}
            onDeleteSchedule={async () => {
              // Single-target only — the modal hides this button when
              // there's no current schedule to remove (which is the
              // multi-target case).
              try {
                await Promise.all(upgradeModalTarget.targets.map(target => deleteTargetSchedule(target)));
                addToast(`Schedule removed for ${upgradeModalTarget.displayName}`, 'success');
                setUpgradeModalTarget(null);
                mutateDevices();
              } catch (err) {
                addToast('Delete failed: ' + (err as Error).message, 'error');
              }
            }}
            onClose={() => setUpgradeModalTarget(null)}
          />
        );
      })()}

      {/* RC.1: read-only YAML viewer for `esphome config <yaml>` output. */}
      {renderedConfigTarget && (() => {
        const t = targets.find(x => x.target === renderedConfigTarget);
        return (
          <RenderedConfigModal
            target={renderedConfigTarget}
            displayName={t?.friendly_name || stripYaml(renderedConfigTarget)}
            monacoTheme={theme === 'light' ? 'vs' : 'vs-dark'}
            onClose={() => setRenderedConfigTarget(null)}
          />
        );
      })()}

      {/* DM.2: ICMP ping diagnostic for the per-row hamburger entry. */}
      {pingTarget && (
        <PingDeviceModal
          target={pingTarget}
          onClose={() => setPingTarget(null)}
          onToast={addToast}
        />
      )}

      {/* DM.3: install-to-specific-address from the per-row hamburger.
          Pre-fills with the device's resolved IP from the poller; the
          user can edit before triggering the OTA. */}
      {installAddressTarget && (() => {
        const t = targets.find(x => x.target === installAddressTarget);
        return (
          <InstallToAddressModal
            target={installAddressTarget}
            defaultAddress={t?.ip_address ?? null}
            onClose={() => setInstallAddressTarget(null)}
            onToast={addToast}
          />
        );
      })()}

      {renameModalTarget && (
        <RenameModal
          currentName={renameModalTarget}
          onConfirm={newName => {
            const t = renameModalTarget;
            setRenameModalTarget(null);
            handleRenameDevice(t, newName);
          }}
          onClose={() => setRenameModalTarget(null)}
        />
      )}

      {newDeviceModal && (
        <NewDeviceModal
          mode={newDeviceModal.mode}
          sourceTarget={newDeviceModal.sourceTarget}
          existingTargets={targets.map(t => t.target)}
          onCreate={(target) => {
            setNewDeviceModal(null);
            mutateDevices();
            // #42: remember this target is unsaved — if the editor closes
            // without a successful save we'll delete the file.
            unsavedNewTargetsRef.current.add(target);
            // Open the editor on the new target so the user can add content.
            setEditorTarget(target);
          }}
          onClose={() => setNewDeviceModal(null)}
          onToast={addToast}
        />
      )}

      {/* TG.9: shared routing-rules editor — same instance is reached
          from the Workers tab toolbar AND the Queue tab BLOCKED-badge
          click. ``initialEditRuleId`` lets the Queue path deep-link to
          the rule that's blocking the job. */}
      <RoutingRulesModal
        open={routingRulesEditId !== null}
        onOpenChange={(o) => { if (!o) setRoutingRulesEditId(null); }}
        targets={targets}
        workers={workers}
        initialEditRuleId={routingRulesEditId || null}
      />
    </>
  );
}
