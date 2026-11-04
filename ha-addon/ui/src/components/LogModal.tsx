import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { Download } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { buildWsUrl, getJobLog } from '../api/client';
import { useVersioningEnabled } from '../hooks/useVersioning';
import { copyTerminalText, downloadTerminalText } from '../utils/terminal';
import type { Job, Worker } from '../types';
import { fmtDuration, getJobBadge } from '../utils';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from './ui/dialog';
import { Button } from './ui/button';

/**
 * WL.3: the log viewer is shared between compile-job logs (the original
 * use case) and worker-side logs (pull-when-watched). The caller picks
 * the source via a tagged union; URL builder and download filename key
 * off `source.kind`. Job-specific chrome (Retry/Rerun, Edit, Diff since
 * compile) only renders when `kind === 'job'`.
 */
export type LogSource =
  | { kind: 'job'; jobId: string }
  | { kind: 'worker'; workerId: string };

interface Props {
  source: LogSource | null;
  queue: Job[];
  workers: Worker[];
  onClose: () => void;
  onRetry: (ids: string[]) => void;
  onEdit?: (target: string) => void;
  /**
   * AV.7: open the HistoryPanel deep-linked to "diff since this compile"
   * — from = job.config_hash, to = working tree. When omitted, the
   * button isn't rendered (e.g. job pre-dates AV.7 and has no
   * config_hash, or the parent didn't wire the history panel).
   */
  onOpenHistoryDiff?: (target: string, fromHash: string) => void;
  stacked?: boolean;
}

export function LogModal({ source, queue, workers, onClose, onRetry, onEdit, onOpenHistoryDiff, stacked }: Props) {
  const isOpen = source !== null;
  const isJob = source?.kind === 'job';
  const isWorker = source?.kind === 'worker';
  // Bug #111: hide the "Diff since compile" button when versioning is off.
  // With no git history, the button opens an empty diff drawer.
  const versioningEnabled = useVersioningEnabled();
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerMounted, setContainerMounted] = useState(false);
  const containerCallbackRef = useCallback((node: HTMLDivElement | null) => {
    containerRef.current = node;
    setContainerMounted(!!node);
  }, []);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollOffsetRef = useRef(0);
  const pollJobIdRef = useRef<string | null>(null);
  const mountedRef = useRef(false);

  // Live header updates (QS.27): the job header shows a running
  // "elapsed since start" counter for in-flight compiles. The job
  // object itself only re-renders when SWR polls (1 Hz for queue),
  // but the human-readable elapsed value needs to tick every second
  // so the user sees a smooth counter instead of a jittery stair-step
  // tied to the polling cadence. `forceUpdate` bumps a dummy state
  // counter purely to re-run `timeAgo` against the current wall-clock
  // time. Cleaned up on modal close — no wasted timer while hidden.
  const [, forceUpdate] = useState(0);
  const headerTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const job = isJob && source ? queue.find(j => j.id === source.jobId) ?? null : null;
  const worker = isWorker && source ? workers.find(w => w.client_id === source.workerId) ?? null : null;
  // #137: the log can be opened for a job the queue poll hasn't
  // delivered yet; re-run the stream setup once it shows up.
  const jobPresent = job !== null;
  // Server-side OTA (Thread / unreachable device) appends its diagnostics
  // to job.log after the worker's stream has ended, without a WS frame.
  // Re-load the finished log whenever the OTA outcome changes so a failed
  // flash shows up without closing and reopening the dialog.
  const otaRefreshKey = job && !['working', 'pending'].includes(job.state)
    ? `${job.state}:${job.ota_result ?? ''}`
    : '';

  // Derive a stable string identity for the source so the xterm/WS
  // effect below only re-runs when the *target* changes, not on every
  // parent re-render. App.tsx rebuilds the `source={...}` literal on
  // each SWR poll (1 Hz) — without this, the terminal would tear down
  // and re-init once a second, flickering the dialog visibly.
  const sourceKey = source
    ? source.kind === 'job'
      ? `job:${source.jobId}`
      : `worker:${source.workerId}`
    : null;

  useEffect(() => {
    if (!isOpen) return;
    headerTimerRef.current = setInterval(() => forceUpdate(n => n + 1), 1000);
    return () => {
      if (headerTimerRef.current) clearInterval(headerTimerRef.current);
    };
  }, [isOpen]);

  function stopWs() {
    if (wsRef.current) {
      try { wsRef.current.close(); } catch { /* ignore */ }
      wsRef.current = null;
    }
  }

  function stopPolling() {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    pollJobIdRef.current = null;
  }

  function disposeTerminal() {
    if (termRef.current) {
      termRef.current.dispose();
      termRef.current = null;
    }
  }

  const startJobPolling = useCallback((pJobId: string) => {
    pollOffsetRef.current = 0;
    pollJobIdRef.current = pJobId;

    async function poll() {
      if (!pollJobIdRef.current || !mountedRef.current) return;
      try {
        const data = await getJobLog(pollJobIdRef.current, pollOffsetRef.current);
        if (data.log && termRef.current) termRef.current.write(data.log);
        pollOffsetRef.current = data.offset;
        if (!data.finished) {
          pollTimerRef.current = setTimeout(poll, 500);
          return;
        }
      } catch { /* ignore */ }
      pollTimerRef.current = setTimeout(poll, 500);
    }

    poll();
  }, []);

  // Initialize terminal and connections when source changes or container mounts
  useEffect(() => {
    mountedRef.current = true;
    if (!source || !containerMounted || !containerRef.current) return;

    // Clean up previous
    stopWs();
    stopPolling();
    disposeTerminal();

    const term = new Terminal({
      cursorBlink: false,
      disableStdin: true,
      scrollback: 50000,
      fontSize: 12,
      fontFamily: "'Fira Code', 'Cascadia Code', Consolas, monospace",
      theme: {
        background: '#0d1117',
        foreground: '#e2e8f0',
        cursor: '#0d1117',
      },
      convertEol: true,
    });

    containerRef.current.innerHTML = '';
    term.open(containerRef.current);
    termRef.current = term;

    // Fit terminal to container — retry a few times as modal opens
    function fit() {
      if (!termRef.current) return;
      try {
        // Access internal render dimensions for precise cell size
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const core = (term as any)._core; // ALLOW_ANY: xterm internal
        const cellW = core?._renderService?.dimensions?.css?.cell?.width;
        const cellH = core?._renderService?.dimensions?.css?.cell?.height;
        if (cellW > 0 && cellH > 0 && containerRef.current) {
          const dims = containerRef.current.getBoundingClientRect();
          const cols = Math.max(40, Math.floor(dims.width / cellW));
          const rows = Math.max(10, Math.floor(dims.height / cellH));
          term.resize(cols, rows);
        }
      } catch { /* ignore */ }
    }
    requestAnimationFrame(fit);
    setTimeout(fit, 100);
    setTimeout(fit, 300);

    if (source.kind === 'job') {
      const currentJob = queue.find(j => j.id === source.jobId);
      const isLive = currentJob && ['working', 'pending'].includes(currentJob.state);

      if (isLive) {
        // #137: a just-enqueued job (auto-opened after Upgrade) has no
        // output yet — show a dim hint instead of a blank terminal. The
        // WS stays subscribed and the first real frame replaces it.
        let hintShown = false;
        if (currentJob.state !== 'working' && termRef.current) {
          termRef.current.write('\x1b[2mWaiting for a worker to pick up this job…\x1b[0m\r\n');
          hintShown = true;
        }
        // Prefer WebSocket, fall back to HTTP polling
        try {
          const ws = new WebSocket(buildWsUrl(`ui/api/jobs/${source.jobId}/log/ws`));
          ws.onmessage = (e) => {
            if (!termRef.current) return;
            if (hintShown) {
              termRef.current.reset();
              hintShown = false;
            }
            termRef.current.write(e.data as string);
          };
          ws.onerror = () => {
            stopWs();
            startJobPolling(source.jobId);
          };
          ws.onclose = () => { wsRef.current = null; };
          wsRef.current = ws;
        } catch {
          startJobPolling(source.jobId);
        }
      } else if (currentJob) {
        // SP.2: queue list no longer carries `log`. For terminal jobs, fetch
        // via /ui/api/jobs/{id}/log — startPolling does one full read and
        // stops as soon as the response says finished:true.
        startJobPolling(source.jobId);
      }
    } else {
      // WL.3 worker-log path: hydration snapshot AND live tail flow
      // through the same WS connection. Server sends the current
      // buffer as the first frame, then every subsequent push. That
      // removes the hydration/live race a separate GET would create.
      const workerId = source.workerId;
      // Dim hint while the WS handshake is in flight so the user
      // isn't staring at a blank terminal for a beat.
      if (termRef.current) {
        termRef.current.write('\x1b[2mConnecting to worker…\x1b[0m\r\n');
      }
      let firstFrameReceived = false;
      try {
        const ws = new WebSocket(buildWsUrl(`ui/api/workers/${workerId}/logs/ws`));
        ws.onmessage = (e) => {
          if (!termRef.current) return;
          if (!firstFrameReceived) {
            // Replace the dim "Connecting…" line with the real content
            // on the first frame so the hint doesn't persist at the top.
            termRef.current.reset();
            firstFrameReceived = true;
          }
          termRef.current.write(e.data as string);
        };
        ws.onclose = () => { wsRef.current = null; };
        wsRef.current = ws;
      } catch { /* ignore */ }
    }

    return () => {
      mountedRef.current = false;
      stopWs();
      stopPolling();
      disposeTerminal();
    };
    // Deps are the *stable* sourceKey string, NOT the source object —
    // App.tsx re-creates the object every render so using it here
    // would tear down xterm 1 Hz.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceKey, containerMounted, startJobPolling, jobPresent, otaRefreshKey]);

  // Compute header contents from current state
  let modalTitle = '';
  let badgeEl: React.ReactNode = null;
  let metaEl: React.ReactNode = null;
  let retryEl: React.ReactNode = null;

  if (job) {
    modalTitle = job.target;
    const { label, cls } = getJobBadge(job);
    badgeEl = <span className={cls}>{label}</span>;

    const clientName =
      job.assigned_hostname ||
      (job.assigned_client_id
        ? workers.find(c => c.client_id === job.assigned_client_id)?.hostname ||
          job.assigned_client_id.slice(0, 8)
        : null);
    const dur = job.duration_seconds != null ? fmtDuration(job.duration_seconds) + ' duration' : null;
    const meta = [clientName, dur].filter(Boolean).join(' · ');
    if (meta) {
      metaEl = <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{meta}</span>;
    }

    if (!job.validate_only && ['failed', 'timed_out', 'success'].includes(job.state)) {
      // Bug #108: every "redo this job" action reads as "Rerun" in the
      // UI. Success rows keep success-green; failure rows surface the
      // same verb in warn-amber so the colour still signals the source
      // state. Same convention as the queue table.
      const isSuccess = job.state === 'success';
      retryEl = (
        <Button
          variant={isSuccess ? 'success' : 'warn'}
          size="sm"
          onClick={() => { onRetry([job.id]); onClose(); }}
        >
          Rerun
        </Button>
      );
    }
  } else if (worker) {
    modalTitle = worker.hostname;
    metaEl = (
      <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
        {worker.online ? 'online' : 'offline'}
      </span>
    );
  }

  const handleCopy = () => copyTerminalText(termRef.current);
  const handleDownload = () => {
    if (job) {
      downloadTerminalText(termRef.current, job.target);
    } else if (worker) {
      const ts = new Date().toISOString().replace(/[:.]/g, '-');
      downloadTerminalText(termRef.current, `worker-${worker.hostname}-${ts}`);
    } else {
      downloadTerminalText(termRef.current, 'log');
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className={`dialog-lg${stacked ? ' stacked' : ''}`} style={stacked ? { zIndex: 500 } : undefined}>
        <DialogHeader>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1, minWidth: 0 }}>
            <DialogTitle style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{modalTitle}</DialogTitle>
            {badgeEl}
            {metaEl}
            {retryEl}
          </div>
          {isJob && onEdit && job && !job.validate_only && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => { onEdit(job.target); onClose(); }}
              title="Open config in editor"
            >
              Edit
            </Button>
          )}
          {isJob && onOpenHistoryDiff && versioningEnabled && job && !job.validate_only && job.config_hash && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => { onOpenHistoryDiff(job.target, job.config_hash!); onClose(); }}
              title="Show what's changed in the config since this compile started"
            >
              Diff since compile
            </Button>
          )}
          <Button variant="secondary" size="sm" onClick={handleCopy} title="Copy log to clipboard">
            Copy
          </Button>
          <Button variant="secondary" size="sm" onClick={handleDownload} title="Download build log as text file">
            <Download className="size-3.5 mr-1" aria-hidden="true" /> Download log
          </Button>
        </DialogHeader>
        <div style={{ flex: 1, padding: 0, overflow: 'hidden' }}>
          <div ref={containerCallbackRef} className="xterm-container" style={{ width: '100%', height: '100%' }} />
        </div>
      </DialogContent>
    </Dialog>
  );
}
