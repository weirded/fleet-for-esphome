/**
 * Job-state predicates + badge styling (QS.23).
 *
 * All funcs take a minimal structural type (the subset of Job fields they
 * actually read) so callers don't have to pass a full Job object when they
 * only have e.g. state + ota_result.
 */

type JobStatusLike = {
  state: string;
  ota_result?: string;
  validate_only?: boolean;
  download_only?: boolean;
  server_ota?: boolean;
};

/** Job is fully done and successful.
 *
 * Three terminal-success paths:
 *   - compile + OTA: state=success AND ota_result=success
 *   - validate-only: state=success (OTA is N/A)
 *   - download-only (#23): state=success (OTA deliberately skipped —
 *     the binary is stored server-side for the user to download)
 */
export function isJobSuccessful(job: JobStatusLike): boolean {
  if (job.state !== 'success') return false;
  if (job.validate_only || job.download_only) return true;
  return job.ota_result === 'success';
}

// SOTA.1: server_ota jobs are "in progress" while the server is performing
// the OTA push (state=success, ota_result=null). Terminal once ota_result
// is set.
export function isServerOtaPending(job: JobStatusLike): boolean {
  return (
    !!job.server_ota &&
    job.state === 'success' &&
    job.ota_result == null
  );
}

/** Job is still in progress (not yet reached a terminal state).
 *
 * TG.9: BLOCKED is *not* terminal — it's a Pending-with-a-reason. A
 * blocked job will move back to Pending the moment an eligible worker
 * comes online or the rule changes; it must not count as Failed.
 */
export function isJobInProgress(job: JobStatusLike): boolean {
  if (job.state === 'pending' || job.state === 'working' || job.state === 'blocked') return true;
  // Compile succeeded but OTA hasn't finished yet. validate_only /
  // download_only jobs don't have an OTA phase and are terminal on
  // state=success (#23).
  // SOTA.1: server_ota jobs stay "in progress" until the server OTA push
  // completes (ota_result is set).
  if (
    job.state === 'success' &&
    !job.validate_only &&
    !job.download_only &&
    job.ota_result !== 'success' &&
    job.ota_result !== 'failed'
  ) {
    return true;
  }
  return false;
}

/** Job is in a terminal failed state (not running, not successful, not cancelled) */
export function isJobFailed(job: JobStatusLike): boolean {
  if (job.state === 'cancelled') return false;
  return !isJobInProgress(job) && !isJobSuccessful(job);
}

export function isJobCancelled(job: { state: string }): boolean {
  return job.state === 'cancelled';
}

/** Job is in a terminal state (not running) */
export function isJobFinished(job: JobStatusLike): boolean {
  return !isJobInProgress(job);
}

/** Job can be retried (any terminal state — failed or successful) */
export function isJobRetryable(job: JobStatusLike): boolean {
  return isJobFinished(job);
}

// UX.3: badges now render in title case — labels declared in
// getJobBadge below are already title case ("Pending", "Failed",
// "Timed Out", etc.), so dropping `uppercase` here gives the UI the
// case the source code actually declares.
const BADGE_BASE = 'inline-block rounded-full px-2 py-0.5 text-[11px] font-semibold tracking-wide';
const BADGE_VARIANTS: Record<string, string> = {
  pending:   `${BADGE_BASE} bg-[#374151] text-[#9ca3af]`,
  working:   `${BADGE_BASE} bg-[#1e3a5f] text-[#60a5fa]`,
  success:   `${BADGE_BASE} bg-[#14532d] text-[#4ade80]`,
  failed:    `${BADGE_BASE} bg-[#450a0a] text-[#f87171]`,
  timed_out: `${BADGE_BASE} bg-[#431407] text-[#fb923c]`,
  cancelled: `${BADGE_BASE} bg-[#374151] text-[#9ca3af]`,
  // TG.9: BLOCKED is its own badge — distinct red-orange so the user can
  // tell at a glance that a job is held by routing rules, not just sitting
  // pending in the queue. Click opens the routing-rules editor.
  blocked:   `${BADGE_BASE} bg-[#3f1d1d] text-[#fb7185]`,
};

export function getJobBadge(job: {
  state: string;
  ota_only?: boolean;
  validate_only?: boolean;
  download_only?: boolean;
  server_ota?: boolean;
  ota_result?: string;
  status_text?: string;
}): { label: string; cls: string } {
  if (job.state === 'pending' && job.validate_only) {
    return { label: 'Validate', cls: BADGE_VARIANTS.pending };
  } else if (job.state === 'pending' && job.download_only) {
    return { label: 'Download', cls: BADGE_VARIANTS.pending };
  } else if (job.state === 'pending' && job.server_ota) {
    // SOTA.1: pending server_ota job — compile hasn't started yet
    return { label: 'Server OTA', cls: BADGE_VARIANTS.pending };
  } else if (job.state === 'pending' && job.ota_only) {
    return { label: 'OTA Retry', cls: BADGE_VARIANTS.timed_out };
  } else if (job.state === 'pending') {
    return { label: 'Pending', cls: BADGE_VARIANTS.pending };
  } else if (job.state === 'blocked') {
    // TG.9: distinct from Pending — surfaces routing-rule constraint.
    return { label: 'Blocked', cls: BADGE_VARIANTS.blocked };
  } else if (job.state === 'working' && job.validate_only) {
    return { label: job.status_text || 'Validating', cls: BADGE_VARIANTS.working };
  } else if (job.state === 'working' && (job.download_only || job.server_ota)) {
    // SOTA.1: during compile phase, server_ota looks identical to download_only
    return { label: job.status_text || 'Compiling', cls: BADGE_VARIANTS.working };
  } else if (job.state === 'working') {
    return { label: job.status_text || 'Working', cls: BADGE_VARIANTS.working };
  } else if (job.state === 'failed') {
    return { label: 'Failed', cls: BADGE_VARIANTS.failed };
  } else if (job.state === 'success' && job.validate_only) {
    return { label: 'Valid', cls: BADGE_VARIANTS.success };
  } else if (job.state === 'success' && job.download_only && !job.server_ota) {
    // #23: compile-and-download is terminal on state=success — no OTA
    // phase, so "OTA Pending" was misleading. "Ready" reads as "your
    // binary is ready to download".
    return { label: 'Ready', cls: BADGE_VARIANTS.success };
  } else if (job.state === 'success' && job.server_ota) {
    // SOTA.1: server_ota — compile done, server is pushing or has pushed OTA
    if (job.ota_result === 'success') {
      return { label: 'Success', cls: BADGE_VARIANTS.success };
    } else if (job.ota_result === 'failed') {
      return { label: 'OTA Failed', cls: BADGE_VARIANTS.timed_out };
    } else {
      return { label: 'Server OTA', cls: BADGE_VARIANTS.working };
    }
  } else if (job.state === 'success') {
    if (job.ota_result === 'success') {
      return { label: 'Success', cls: BADGE_VARIANTS.success };
    } else if (job.ota_result === 'failed') {
      return { label: 'OTA Failed', cls: BADGE_VARIANTS.timed_out };
    } else {
      return { label: 'OTA Pending', cls: BADGE_VARIANTS.working };
    }
  } else if (job.state === 'timed_out') {
    return { label: 'Timed Out', cls: BADGE_VARIANTS.timed_out };
  } else if (job.state === 'cancelled') {
    return { label: 'Cancelled', cls: BADGE_VARIANTS.cancelled };
  } else {
    return { label: job.state, cls: BADGE_VARIANTS[job.state] || BADGE_VARIANTS.pending };
  }
}
