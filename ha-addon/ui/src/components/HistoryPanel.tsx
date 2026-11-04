import { useEffect, useMemo, useState } from 'react';
import useSWR from 'swr';
import { toast } from 'sonner';
import { DiffEditor } from '@monaco-editor/react';
import { History, RotateCcw, AlertTriangle } from 'lucide-react';

import {
  commitFile,
  getFileContentAt,
  getFileHistory,
  getFileStatus,
  getSettings,
  rollbackFile,
  type AppSettings,
  type FileHistoryEntry,
  type FileStatus,
} from '@/api/client';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FirmwareDownloadMenu } from './FirmwareDownloadMenu';
import {
  Sheet,
  SheetBody,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

// AV.6: per-file history + diff panel. Opens as a drawer from the
// Editor modal's toolbar or the Devices hamburger. Combines the history
// endpoint (AV.3), diff endpoint (AV.4), rollback (AV.5), manual
// commit (AV.11), and status probe into one coherent surface.

interface HistoryPanelProps {
  /** Fully-qualified filename, e.g. "bedroom.yaml". `null` = closed. */
  filename: string | null;
  onOpenChange: (open: boolean) => void;
  /**
   * Called after a successful rollback or manual commit, so the caller
   * (typically the Editor modal) can refresh its buffer to match the
   * new on-disk content.
   */
  onFileChanged?: () => void;
  /**
   * AV.7: optional preset for the From/To pickers. Used by the Queue
   * tab's "Diff since last compile" entry point to deep-link the
   * panel with `from = job.config_hash` and `to = "Current"` (working
   * tree) so the user lands directly on the right comparison. When
   * omitted, the panel picks sensible defaults (parent of HEAD ↔
   * Current).
   */
  initialFromHash?: string | null;
  initialToHash?: string | null;
  /**
   * #100: Monaco theme — pass through the app's current theme so the
   * diff editor matches. Default ``vs-dark`` for backwards compat;
   * consumers should explicitly pass ``theme === 'light' ? 'vs' :
   * 'vs-dark'`` (same expression App.tsx uses for EditorModal).
   */
  monacoTheme?: 'vs' | 'vs-dark';
}

// Sentinel used in the From/To dropdowns to mean "current working tree".
const WORKING_TREE = '__WORKING_TREE__';

export function HistoryPanel({
  filename,
  onOpenChange,
  onFileChanged,
  initialFromHash,
  initialToHash,
  monacoTheme = 'vs-dark',
}: HistoryPanelProps) {
  const open = filename !== null;

  // SWR: history + status are fetched while the drawer is open.
  const historyKey = open ? ['fileHistory', filename] : null;
  const statusKey = open ? ['fileStatus', filename] : null;

  const {
    data: entries,
    error: historyError,
    mutate: mutateHistory,
    isLoading: historyLoading,
  } = useSWR<FileHistoryEntry[]>(
    historyKey,
    () => getFileHistory(filename!),
    { revalidateOnFocus: false },
  );

  const {
    data: status,
    mutate: mutateStatus,
  } = useSWR<FileStatus>(
    statusKey,
    () => getFileStatus(filename!),
    { revalidateOnFocus: false },
  );

  // #75 / UX_REVIEW §1.1: the Restore confirmation branched its "a new
  // revert commit will be created" vs "no commit will be created"
  // copy on ``has_uncommitted_changes`` — which happened to be wrong.
  // Whether a revert commit is written is purely a function of the
  // ``auto_commit_on_save`` setting (see AV.5 backend). Subscribing to
  // the live setting here so the dialog body reports it correctly.
  // Same SWR key the Settings drawer + EditorModal use → one fetch.
  const { data: appSettings } = useSWR<AppSettings>(open ? 'settings' : null, getSettings);
  const autoCommit = appSettings?.auto_commit_on_save ?? true;

  // Compare selector — From/To hold commit hashes, or the WORKING_TREE sentinel.
  const [fromHash, setFromHash] = useState<string>('');
  const [toHash, setToHash] = useState<string>(WORKING_TREE);

  // Bug 14: "older version always on the left" is a hard UX rule.
  // Return 1 if `a` is newer than `b`, -1 if older, 0 if same.
  // Uses the `entries` list (newest-first) to compare; the
  // WORKING_TREE sentinel is treated as the newest of all.
  function compareRecency(a: string, b: string): number {
    if (a === b) return 0;
    if (a === WORKING_TREE) return 1;
    if (b === WORKING_TREE) return -1;
    if (!entries) return 0;
    const aIdx = entries.findIndex(e => e.hash === a);
    const bIdx = entries.findIndex(e => e.hash === b);
    if (aIdx < 0 && bIdx < 0) return 0;
    if (aIdx < 0) return -1;
    if (bIdx < 0) return 1;
    // Lower index = newer (newest-first ordering).
    return bIdx - aIdx;
  }

  // Normalise the pair so From is always older than To. Silent swap
  // — the user picked two commits; we present them in the canonical
  // older-on-left order so the diff reads as "what changed going
  // from older → newer."
  useEffect(() => {
    if (!fromHash || !toHash) return;
    if (compareRecency(fromHash, toHash) > 0) {
      setFromHash(toHash);
      setToHash(fromHash);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromHash, toHash, entries]);

  // Each time the history list shows up (or changes), pick a sensible
  // default: From = parent of HEAD (= entries[1]) or HEAD itself if
  // only one commit exists; To = working tree. AV.7 callers can
  // override via initialFromHash / initialToHash props to deep-link
  // to a specific comparison (e.g. "diff since last compile").
  useEffect(() => {
    if (!entries || entries.length === 0) {
      setFromHash(initialFromHash || '');
      setToHash(initialToHash ?? WORKING_TREE);
      return;
    }
    // Initial-hash props win if the caller supplied them AND the hash
    // is recognised in the current entries list (or the sentinel).
    if (initialFromHash && entries.some(e => e.hash === initialFromHash)) {
      setFromHash(initialFromHash);
    } else {
      setFromHash(prev => prev && entries.some(e => e.hash === prev) ? prev : entries[0].hash);
    }
    if (initialToHash === WORKING_TREE || initialToHash === null || initialToHash === undefined) {
      setToHash(prev => (prev === WORKING_TREE || entries.some(e => e.hash === prev)) ? prev : WORKING_TREE);
    } else if (entries.some(e => e.hash === initialToHash)) {
      setToHash(initialToHash);
    }
  // We only want the preset to take effect when the drawer re-opens
  // for a new (filename, initialFromHash, initialToHash) triple — not
  // on every entries re-fetch, which would yank the user's manual
  // selection back. Limit to the props explicitly.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entries, filename, initialFromHash, initialToHash]);

  // Bug #10: side-by-side diff. Fetch the file's content at both
  // hashes separately (or the working tree for WORKING_TREE / unset)
  // and feed them to Monaco's DiffEditor with renderSideBySide on.
  //
  // Bug #15 (reopened): the WORKING_TREE sentinel is a UI-only value.
  // Both From and To must translate it to `null` before the fetch,
  // otherwise the server 400s with "Rejected content-at request with
  // malformed hash: '__WORKING_TREE__'" (which, even though it's only
  // a log line, pollutes the add-on log). Helper below normalises.
  function hashForApi(h: string): string | null {
    if (!h || h === WORKING_TREE) return null;
    return h;
  }
  const fromKey = open && filename ? ['fileContentAt', filename, hashForApi(fromHash)] : null;
  const toKey = open && filename ? ['fileContentAt', filename, hashForApi(toHash)] : null;

  const { data: fromContent = '', isLoading: fromLoading } = useSWR<string>(
    fromKey,
    () => getFileContentAt(filename!, hashForApi(fromHash)),
    { revalidateOnFocus: false },
  );
  const { data: toContent = '', isLoading: toLoading } = useSWR<string>(
    toKey,
    () => getFileContentAt(filename!, hashForApi(toHash)),
    { revalidateOnFocus: false },
  );
  const diffLoading = fromLoading || toLoading;

  // --- Actions ---

  const [busy, setBusy] = useState(false);
  // Bug 15: Restore confirmation is a proper shadcn Dialog, not a
  // native window.confirm (which looks like a Jetsons-era browser
  // popup). Holds the entry under review; null = closed.
  const [restoreCandidate, setRestoreCandidate] = useState<FileHistoryEntry | null>(null);
  // #211: lift the per-row firmware Download dropdown's open state out of
  // the row cell so it survives SWR refreshes — same pattern the Devices
  // tab and Queue tab use (#2, #71). Keyed by commit hash so only one
  // dropdown is open at a time.
  const [downloadMenuOpenHash, setDownloadMenuOpenHash] = useState<string | null>(null);

  async function handleRollback(entry: FileHistoryEntry) {
    if (!filename) return;
    setBusy(true);
    try {
      const result = await rollbackFile(filename, entry.hash);
      toast.success(`Restored ${result.short_hash ?? entry.short_hash}`);
      await Promise.all([mutateHistory(), mutateStatus()]);
      onFileChanged?.();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Manual commit prompt
  const [commitPromptOpen, setCommitPromptOpen] = useState(false);
  const [commitMsg, setCommitMsg] = useState('');

  async function handleManualCommit() {
    if (!filename) return;
    setBusy(true);
    try {
      const result = await commitFile(filename, commitMsg.trim() || undefined);
      if (!result.committed) {
        toast.info('Nothing to commit');
      } else {
        toast.success(`Committed ${result.short_hash}`);
      }
      setCommitPromptOpen(false);
      setCommitMsg('');
      await Promise.all([mutateHistory(), mutateStatus()]);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const fromLabel = useMemo(() => labelForHash(fromHash, entries), [fromHash, entries]);
  const toLabel = useMemo(() => labelForHash(toHash, entries), [toHash, entries]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="!w-[min(920px,100vw)]">
        <SheetHeader>
          <div className="flex items-center gap-2">
            <History className="size-4 text-[var(--text-muted)]" />
            <SheetTitle>{filename ?? 'History'}</SheetTitle>
          </div>
        </SheetHeader>
        <SheetBody>
          {historyError && (
            <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-400 mb-3">
              Failed to load history: {historyError.message}
            </div>
          )}

          {status?.has_uncommitted_changes && (
            <div className="mb-3 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-xs flex items-start gap-2">
              <AlertTriangle className="size-4 text-yellow-400 mt-0.5 shrink-0" />
              <div className="flex-1">
                <div className="text-yellow-200">You have uncommitted changes.</div>
                <div className="text-yellow-300/80 mt-0.5">
                  These won't show up in history until committed.
                </div>
              </div>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => setCommitPromptOpen(v => !v)}
                disabled={busy}
              >
                {commitPromptOpen ? 'Cancel' : 'Commit…'}
              </Button>
            </div>
          )}

          {commitPromptOpen && (
            <div className="mb-3 flex gap-2 items-center">
              <Input
                type="text"
                placeholder="Manually committed from UI"
                className="flex-1 font-mono text-xs"
                value={commitMsg}
                onChange={e => setCommitMsg(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') { e.preventDefault(); void handleManualCommit(); }
                }}
                autoFocus
              />
              <Button type="button" onClick={handleManualCommit} disabled={busy}>
                Commit
              </Button>
            </div>
          )}

          {/* Compare pills — side by side, each aligned above its DiffEditor pane (bug #10). */}
          <div className="grid grid-cols-2 gap-2 mb-2 text-xs">
            <HashPicker
              label="From (left pane)"
              value={fromHash}
              entries={entries ?? []}
              onChange={setFromHash}
              allowWorkingTree
            />
            <HashPicker
              label="To (right pane)"
              value={toHash}
              entries={entries ?? []}
              onChange={setToHash}
              allowWorkingTree
            />
          </div>
          <div className="text-xs text-[var(--text-muted)] mb-2">
            Comparing <code className="font-mono">{fromLabel}</code> → <code className="font-mono">{toLabel}</code>
          </div>

          {/* Diff viewer — Monaco DiffEditor in side-by-side mode (bug #10). */}
          <div className="h-[360px] border border-[var(--border)] rounded-md overflow-hidden mb-4">
            {diffLoading ? (
              <div className="p-3 text-xs text-[var(--text-muted)]">Loading diff…</div>
            ) : fromContent === toContent ? (
              <div className="p-3 text-xs text-[var(--text-muted)]">
                No differences between {fromLabel} and {toLabel}.
              </div>
            ) : (
              <DiffEditor
                original={fromContent}
                modified={toContent}
                language="yaml"
                theme={monacoTheme}
                options={{
                  readOnly: true,
                  renderSideBySide: true,
                  // Bug #12: Monaco's default is to collapse to inline/unified
                  // view when the diff editor's width is below ~900px,
                  // regardless of `renderSideBySide`. The history drawer is
                  // ~870px of pane-width, so we have to opt out of the
                  // auto-inline fallback to actually see two panes.
                  useInlineViewWhenSpaceIsLimited: false,
                  // #204 (1): the overview ruler paints every change as a
                  // coloured tick in the scrollbar, so a long config shows
                  // at a glance where the edits are instead of forcing a
                  // scroll-hunt. It was off because the drawer is narrow;
                  // the ruler costs a few px and is the cheapest possible
                  // answer to "where did this diff actually change?".
                  renderOverviewRuler: true,
                  // #204 (2): GitHub-style collapse of untouched lines.
                  // A one-line edit to a 300-line YAML rendered 300 lines
                  // and made the user find the changed one. Monaco folds
                  // the unchanged runs behind a click-to-expand band,
                  // keeping 3 lines of context either side of each hunk.
                  // minimumLineCount guards the opposite failure: don't
                  // fold a 4-line gap between two hunks, where the band
                  // takes more room than the lines it hides.
                  hideUnchangedRegions: {
                    enabled: true,
                    contextLineCount: 3,
                    minimumLineCount: 5,
                    revealLineCount: 20,
                  },
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  fontSize: 12,
                  lineNumbers: 'on',
                  originalEditable: false,
                }}
              />
            )}
          </div>

          {/* Commit list */}
          <div className="flex flex-col gap-1">
            <div className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)] mb-1">
              Commits
            </div>
            {historyLoading && <div className="text-xs text-[var(--text-muted)]">Loading…</div>}
            {entries && entries.length === 0 && (
              <div className="text-xs text-[var(--text-muted)]">
                No saved versions yet — your first edit will show up here.
              </div>
            )}
            {entries?.map((entry, idx) => {
              // #81 / UX_REVIEW §3.9: HEAD-and-clean → Restore is a
              // no-op. Entries are newest-first so idx === 0 is HEAD.
              // Disable the per-row Restore and tell the user why.
              const isHeadRow = idx === 0;
              const restoreIsNoop = isHeadRow && !status?.has_uncommitted_changes;
              return (
                <CommitRow
                  key={entry.hash}
                  entry={entry}
                  isFrom={fromHash === entry.hash}
                  isTo={toHash === entry.hash}
                  downloadMenuOpen={downloadMenuOpenHash === entry.hash}
                  onDownloadMenuOpenChange={(o) =>
                    setDownloadMenuOpenHash(o ? entry.hash : null)
                  }
                  onClick={(e) => {
                    if (e.shiftKey) {
                      setFromHash(entry.hash);
                    } else {
                      // Default: show what this commit changed — parent ↔ this.
                      // Bug 23: the earliest commit has no parent (entries[idx+1]
                      // is undefined); diffing that against nothing/working tree
                      // was surprising. Fall back to from=to=entry which renders
                      // an empty "no differences" panel — the honest answer when
                      // we can't compute a predecessor.
                      const parent = entries[idx + 1]?.hash;
                      if (parent) {
                        setFromHash(parent);
                        setToHash(entry.hash);
                      } else {
                        setFromHash(entry.hash);
                        setToHash(entry.hash);
                      }
                    }
                  }}
                  onSetFrom={() => setFromHash(entry.hash)}
                  onSetTo={() => setToHash(entry.hash)}
                  onRestore={() => setRestoreCandidate(entry)}
                  disabled={busy}
                  restoreNoop={restoreIsNoop}
                />
              );
            })}
          </div>
        </SheetBody>
      </SheetContent>

      {/* Bug 15: Restore confirmation. Proper shadcn Dialog; no window.confirm. */}
      <Dialog
        open={restoreCandidate !== null}
        onOpenChange={(open) => { if (!open) setRestoreCandidate(null); }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Restore {restoreCandidate?.short_hash}?</DialogTitle>
          </DialogHeader>
          <div className="px-4 py-3 text-sm text-[var(--text)]">
            {status?.has_uncommitted_changes ? (
              <p>
                Your <strong>uncommitted changes</strong> to{' '}
                <code className="font-mono text-xs">{filename}</code> will be overwritten.
              </p>
            ) : (
              <p>
                This will replace the current content of{' '}
                <code className="font-mono text-xs">{filename}</code> with its version at{' '}
                <code className="font-mono">{restoreCandidate?.short_hash}</code>.
              </p>
            )}
            {/* #75: describe the commit side-effect based on the LIVE
                auto-commit setting, not a hard-coded string. */}
            <p className="text-[var(--text-muted)] mt-2 text-xs">
              {autoCommit ? (
                <>
                  A new{' '}<code className="font-mono">revert: …</code>{' '}commit will be
                  created so the rollback is itself recorded in history.
                </>
              ) : (
                <>
                  No new commit will be created (auto-commit is off); the working tree
                  will be left dirty until you commit manually.
                </>
              )}
            </p>
          </div>
          <DialogFooter>
            <DialogClose>
              <Button variant="secondary" size="sm" disabled={busy}>Cancel</Button>
            </DialogClose>
            <Button
              variant="destructive"
              size="sm"
              disabled={busy}
              onClick={async () => {
                const entry = restoreCandidate;
                setRestoreCandidate(null);
                if (entry) await handleRollback(entry);
              }}
            >
              Restore this version
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Sheet>
  );
}

function labelForHash(hash: string, entries: FileHistoryEntry[] | undefined): string {
  if (hash === WORKING_TREE || hash === '') return 'Current';
  const e = entries?.find(x => x.hash === hash);
  return e ? e.short_hash : hash.slice(0, 7);
}

function HashPicker({
  label,
  value,
  entries,
  onChange,
  allowWorkingTree,
}: {
  label: string;
  value: string;
  entries: FileHistoryEntry[];
  onChange: (v: string) => void;
  allowWorkingTree: boolean;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="text-[var(--text-muted)]">{label}</span>
      <select
        className="w-full rounded border border-[var(--border)] bg-[var(--surface2)] px-2 py-1 font-mono text-xs text-[var(--text)] outline-none"
        value={value}
        onChange={e => onChange(e.target.value)}
      >
        {allowWorkingTree && <option value={WORKING_TREE}>Current (working tree)</option>}
        {entries.map(e => (
          <option key={e.hash} value={e.hash}>
            {e.short_hash} · {e.message}
          </option>
        ))}
      </select>
    </label>
  );
}

function CommitRow({
  entry,
  isFrom,
  isTo,
  onClick,
  onSetFrom,
  onSetTo,
  onRestore,
  disabled,
  restoreNoop = false,
  downloadMenuOpen,
  onDownloadMenuOpenChange,
}: {
  entry: FileHistoryEntry;
  isFrom: boolean;
  isTo: boolean;
  onClick: (e: React.MouseEvent) => void;
  onSetFrom: () => void;
  onSetTo: () => void;
  onRestore: () => void;
  disabled: boolean;
  restoreNoop?: boolean;
  /** #211: per-row firmware Download menu open state, controlled by the panel. */
  downloadMenuOpen: boolean;
  onDownloadMenuOpenChange: (open: boolean) => void;
}) {
  const when = formatRelativeTime(entry.date);
  // Bug 22: absolute local time alongside the relative one. Short form
  // ("Apr 18, 14:32") stays readable on narrow rows; full ISO appears
  // on hover as a tooltip for anyone who needs the exact timestamp.
  const whenAbsolute = formatAbsoluteTime(entry.date);
  const whenIso = entry.date ? new Date(entry.date * 1000).toISOString() : '';
  const ringClass = isTo
    ? 'border-[var(--accent)]'
    : isFrom
    ? 'border-yellow-500/60'
    : 'border-[var(--border)]';

  return (
    <div
      className={`rounded-md border ${ringClass} bg-[var(--surface2)] px-2.5 py-1.5 flex items-center gap-2 text-xs cursor-pointer hover:bg-[var(--border)]`}
      onClick={onClick}
      role="button"
      tabIndex={0}
    >
      <code className="font-mono text-[var(--text-muted)] shrink-0">{entry.short_hash}</code>
      <span className="truncate flex-1">{entry.message}</span>
      <span
        className="text-[var(--text-muted)] whitespace-nowrap flex flex-col items-end leading-tight"
        title={whenIso}
      >
        <span>{when}</span>
        <span className="text-[10px] opacity-70">{whenAbsolute}</span>
      </span>
      <span className="text-[var(--text-muted)] whitespace-nowrap font-mono">
        <span className="text-green-400">+{entry.lines_added}</span>{' '}
        <span className="text-red-400">-{entry.lines_removed}</span>
      </span>
      <div className="flex gap-1 ml-1" onClick={e => e.stopPropagation()}>
        {/* #211: when a stored firmware binary still matches this
            commit's config_hash, surface a Download chip alongside
            From / To / Restore. Same icon-only treatment the
            Compile-history drawer uses to keep the row width tight. */}
        {entry.firmware_job_id && entry.firmware_variants && entry.firmware_variants.length > 0 && (
          <FirmwareDownloadMenu
            jobId={entry.firmware_job_id}
            variants={entry.firmware_variants}
            open={downloadMenuOpen}
            onOpenChange={onDownloadMenuOpenChange}
            size="icon"
          />
        )}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-xs"
          onClick={onSetFrom}
          disabled={disabled}
          title="Set as From (compare side)"
        >
          From
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-xs"
          onClick={onSetTo}
          disabled={disabled}
          title="Set as To (compare side)"
        >
          To
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-xs gap-1"
          onClick={onRestore}
          disabled={disabled || restoreNoop}
          title={restoreNoop ? 'Already at this version' : 'Restore this version'}
        >
          <RotateCcw className="size-3" />
          Restore
        </Button>
      </div>
    </div>
  );
}

function formatRelativeTime(epochSeconds: number): string {
  if (!epochSeconds) return '';
  const ms = Date.now() - epochSeconds * 1000;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

// Bug 22: "Apr 18, 14:32" for commits in the current year, "Apr 18
// 2025, 14:32" otherwise. Keeps the cell narrow while still being
// unambiguous — the full ISO timestamp is on the row title attribute.
function formatAbsoluteTime(epochSeconds: number): string {
  if (!epochSeconds) return '';
  const d = new Date(epochSeconds * 1000);
  const now = new Date();
  const datePart = d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  });
  const timePart = d.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  return `${datePart}, ${timePart}`;
}
