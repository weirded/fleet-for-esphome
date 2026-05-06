"""Persistent job history (JH.*).

The live :mod:`job_queue` persists the *current* state of each target's
most-recent job in ``/data/queue.json`` — which means per-target
coalescing in :meth:`JobQueue.enqueue` wipes earlier terminal jobs on
every re-enqueue. Users can still read "what did this target just do?"
from the Queue tab, but the *history* of past attempts is gone.

This module is the append-only counterpart: every time a job reaches a
terminal state, we snapshot it into a small SQLite table. The table
survives queue clears, coalescing, and restarts. It powers:

- Per-device "compile history" drawer (JH.5).
- Per-device "Last compiled" column (JH.6).
- /ui/api/history + /ui/api/history/stats (JH.4).

Storage model notes:

- **SQLite, not JSON** — thousands of rows in a growing JSON file
  becomes slow to read/write. SQLite queries by target + finished_at are
  O(log n) via the indexes below. ``sqlite3`` is stdlib on every
  supported Python.
- **Idempotent writes** — ``INSERT OR IGNORE`` on ``id`` so a double-
  record (e.g. retries that share an id, or the coalescing path
  recording a job that was already final) never double-counts.
- **Log excerpt, not full log** — the last ~2 KB of the live log is
  enough to see the compile error or OTA address without blowing the
  DB up. Full logs remain accessible via /ui/api/jobs/{id}/log while
  the queue still holds the job; once coalesced away, the excerpt is
  what's left.

Concurrency: one DAO instance per app, synchronous SQLite via a
short-lived connection per operation. Writes from async contexts are
~sub-millisecond on local disk — no executor needed for the sizes we
expect.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from job_queue import Job  # noqa: F401


logger = logging.getLogger(__name__)


DEFAULT_DB_PATH = Path("/data/job-history.db")

# Last ~8 KB of the live log is what we store per row. Originally 2 KB
# (bug #37) but most PlatformIO error lines are long enough that 2 KB
# only captured 3–5 lines of tail — not enough context to diagnose a
# failed compile without reopening the live log. At 8 KB we're still
# well under 100 MB for a year's worth of daily compiles at fleet-scale
# retention defaults (default 365 d × ~100 compiles/day × 8 KB ≈ 290 MB
# on an extreme upper bound; typical home use is 1–2 orders lower).
LOG_EXCERPT_BYTES = 8192

TERMINAL_STATES: frozenset[str] = frozenset(
    {"success", "failed", "timed_out", "cancelled"},
)


def _epoch(dt: datetime | None) -> int | None:
    """Return epoch seconds (UTC) or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _log_excerpt(log: str | None) -> str | None:
    """Return the tail of *log* up to ``LOG_EXCERPT_BYTES`` bytes.

    We trim by byte count (encoded as UTF-8) so a pathological log full
    of emoji can't quietly balloon the excerpt. Keeps the head-of-tail
    clean by stepping backward to the nearest newline — avoids dumping
    a half-line of ANSI noise into the column.
    """
    if not log:
        return None
    encoded = log.encode("utf-8", errors="replace")
    if len(encoded) <= LOG_EXCERPT_BYTES:
        return log
    tail = encoded[-LOG_EXCERPT_BYTES:]
    # Step forward to the first newline so we don't start mid-escape-
    # sequence. If no newline exists in the tail (unbroken blob), keep
    # the raw tail rather than returning empty.
    newline_idx = tail.find(b"\n")
    if 0 <= newline_idx < len(tail) - 1:
        tail = tail[newline_idx + 1:]
    try:
        return tail.decode("utf-8", errors="replace")
    except Exception:
        return tail.decode("utf-8", errors="ignore")


def _triggered_by(job: "Job") -> tuple[str, str | None]:
    """Classify *job* into (triggered_by, trigger_detail).

    Ordering matters because ``scheduled`` runs originating from an HA
    service action (unusual but possible in theory) would carry both
    flags; the user sees the HA integration as the more informative
    label, so it wins. Bug #61 adds the ``api`` source for direct
    system-token callers (curl, scripts) that aren't the HA
    integration — split from ``ha_action`` by User-Agent at enqueue
    time in ``/ui/api/compile``.
    """
    if getattr(job, "ha_action", False):
        return ("ha_action", None)
    if getattr(job, "api_triggered", False):
        return ("api", None)
    if getattr(job, "scheduled", False):
        return ("schedule", getattr(job, "schedule_kind", None))
    return ("user", None)


def _job_to_row(job: "Job") -> dict[str, object]:
    """Project a :class:`Job` into the column layout used by this table."""
    triggered_by, trigger_detail = _triggered_by(job)
    state_val = getattr(job.state, "value", str(job.state))
    submitted = _epoch(job.created_at)
    started = _epoch(job.assigned_at)
    finished = _epoch(job.finished_at)
    duration: float | None = None
    if started is not None and finished is not None:
        duration = float(finished - started)
    elif submitted is not None and finished is not None:
        # Bug #47: cancelled / immediately-failed jobs never reach a
        # worker so ``started_at`` is None. Fall back to submit → finish
        # so the history view can still show *something* for duration
        # instead of a blank cell. This is the "time the job existed"
        # rather than "time it ran" — documented in the UI tooltip.
        duration = float(finished - submitted)
    elif hasattr(job, "duration_seconds"):
        # Last-resort fallback: the Job's own computed duration. Used
        # when both timestamps came in as None (rare but observed on
        # CR.4's PENDING requeue path that clears ``assigned_at``).
        try:
            d = job.duration_seconds()
            duration = float(d) if d is not None else None
        except Exception:
            duration = None
    return {
        "id": job.id,
        "target": job.target,
        "state": state_val,
        "triggered_by": triggered_by,
        "trigger_detail": trigger_detail,
        "download_only": 1 if getattr(job, "download_only", False) else 0,
        "validate_only": 1 if getattr(job, "validate_only", False) else 0,
        "server_ota": 1 if getattr(job, "server_ota", False) else 0,
        "pinned_client_id": getattr(job, "pinned_client_id", None),
        "esphome_version": getattr(job, "esphome_version", None),
        "assigned_client_id": getattr(job, "assigned_client_id", None),
        "assigned_hostname": getattr(job, "assigned_hostname", None),
        "submitted_at": submitted,
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": duration,
        "ota_result": getattr(job, "ota_result", None),
        "config_hash": getattr(job, "config_hash", None),
        "retry_count": getattr(job, "retry_count", 0),
        "log_excerpt": _log_excerpt(getattr(job, "log", None)),
        # Bug #38: records whether the job produced firmware at all.
        # A subsequent firmware-budget eviction can remove the `.bin`
        # from disk while this column stays truthy — the UI uses the
        # combination (``has_firmware`` + live ``firmware_available``
        # stat) to distinguish "never had firmware" from "had it, now
        # evicted".
        "has_firmware": 1 if getattr(job, "has_firmware", False) else 0,
        # Bug #8 (1.6.1): worker-selection reason persisted on the
        # history row. ``None`` for jobs that predated the column.
        "selection_reason": getattr(job, "selection_reason", None),
    }


class JobHistoryDAO:
    """Synchronous SQLite DAO.

    Instances are cheap — a single one per app is enough, but multiple
    are fine (each operation opens and closes its own connection). The
    module-level :class:`RLock` covers the init-if-missing race when
    the DB file is shared across threads; per-operation connections are
    otherwise parallel-safe because SQLite serialises writers at the
    file level.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            state TEXT NOT NULL,
            triggered_by TEXT,
            trigger_detail TEXT,
            download_only INTEGER NOT NULL DEFAULT 0,
            validate_only INTEGER NOT NULL DEFAULT 0,
            server_ota INTEGER NOT NULL DEFAULT 0,
            pinned_client_id TEXT,
            esphome_version TEXT,
            assigned_client_id TEXT,
            assigned_hostname TEXT,
            submitted_at INTEGER,
            started_at INTEGER,
            finished_at INTEGER,
            duration_seconds REAL,
            ota_result TEXT,
            config_hash TEXT,
            retry_count INTEGER DEFAULT 0,
            log_excerpt TEXT,
            has_firmware INTEGER NOT NULL DEFAULT 0,
            -- Bug #8 (1.6.1): why this worker was selected for the
            -- job. Nullable — old rows inserted before the column
            -- existed carry NULL, which the UI renders as "—".
            selection_reason TEXT
        );
        -- Bug #38: late-added column. ADD COLUMN is idempotent-safe via
        -- OR IGNORE's error on existing column, so we run it unconditionally
        -- inside a try/except at init time rather than here.
        CREATE INDEX IF NOT EXISTS idx_jobs_target_finished
            ON jobs(target, finished_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_finished
            ON jobs(finished_at DESC);
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._init_lock = RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        # PARSE_DECLTYPES isn't useful for our columns (they're INTEGER
        # epoch + TEXT), so stick with the plain connection. ``row_factory
        # = Row`` gives dict-like access in query().
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Journal-mode WAL improves read-while-write concurrency on
        # devices with slow I/O (HA on rotational disk). Idempotent —
        # SQLite rewrites it on every open but that's cheap.
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    def init(self) -> None:
        """Create the schema if missing. Safe to call repeatedly.

        Assumes ``self._db_path.parent`` already exists — on HA the
        parent is ``/data`` which the Supervisor always mounts, and in
        tests callers pass a tmp_path that pytest pre-creates. Avoids
        the mkdir that used to live here because CI runs can't write
        to ``/`` when the DAO is instantiated with the default ``/data``
        path under a test harness that never reaches any write path.
        """
        with self._init_lock:
            if self._initialized:
                return
            try:
                with self._connect() as conn:
                    conn.executescript(self._SCHEMA)
                    # Bug #38 migration: add has_firmware to any pre-
                    # existing DBs shipped before the column landed. Safe
                    # to run on every init — "duplicate column" is the
                    # only error we ignore.
                    try:
                        conn.execute(
                            "ALTER TABLE jobs ADD COLUMN has_firmware INTEGER NOT NULL DEFAULT 0"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                    # Bug #8 (1.6.1): selection_reason migration. Same
                    # idempotent-safe pattern as has_firmware above.
                    try:
                        conn.execute(
                            "ALTER TABLE jobs ADD COLUMN selection_reason TEXT"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                    # SOTA.1: server_ota migration.
                    try:
                        conn.execute(
                            "ALTER TABLE jobs ADD COLUMN server_ota INTEGER NOT NULL DEFAULT 0"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                    conn.commit()
                self._initialized = True
                logger.debug("Job-history DB ready at %s", self._db_path)
            except sqlite3.Error:
                # Parent dir missing / read-only filesystem / locked.
                # Leaving ``_initialized = False`` causes the next call
                # to retry, which is what we want on transient failures.
                logger.warning(
                    "Could not open job-history DB at %s; history "
                    "will be unavailable until the path is writable.",
                    self._db_path,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record_terminal(self, job: "Job") -> bool:
        """Snapshot *job* as a history row. Returns True on change, False
        when the row was not modified.

        **Upsert semantics**: repeat calls for the same ``job.id`` overwrite
        the existing row. This handles the OTA-patch path in
        :meth:`job_queue.JobQueue.submit_result` — the first call after
        compile stores ``state=success, ota_result=NULL``; the second
        call after OTA completes updates ``ota_result='success'`` /
        ``'failed'``. Without upsert, we'd keep the stale NULL.

        Only writes when the job is in a terminal state — callers that
        hit this from the enqueue-coalesce path where the evictee was
        still PENDING would produce a misleading "success" count
        otherwise. No-op + False for non-terminal jobs.
        """
        self.init()
        if not self._initialized:
            return False  # DB unavailable; history is best-effort.
        state_val = getattr(job.state, "value", str(job.state))
        if state_val not in TERMINAL_STATES:
            return False
        row = _job_to_row(job)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs(
                    id, target, state, triggered_by, trigger_detail,
                    download_only, validate_only, server_ota, pinned_client_id,
                    esphome_version, assigned_client_id, assigned_hostname,
                    submitted_at, started_at, finished_at, duration_seconds,
                    ota_result, config_hash, retry_count, log_excerpt,
                    has_firmware, selection_reason
                ) VALUES (
                    :id, :target, :state, :triggered_by, :trigger_detail,
                    :download_only, :validate_only, :server_ota, :pinned_client_id,
                    :esphome_version, :assigned_client_id, :assigned_hostname,
                    :submitted_at, :started_at, :finished_at, :duration_seconds,
                    :ota_result, :config_hash, :retry_count, :log_excerpt,
                    :has_firmware, :selection_reason
                )
                ON CONFLICT(id) DO UPDATE SET
                    state = excluded.state,
                    triggered_by = excluded.triggered_by,
                    trigger_detail = excluded.trigger_detail,
                    assigned_client_id = excluded.assigned_client_id,
                    assigned_hostname = excluded.assigned_hostname,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    duration_seconds = excluded.duration_seconds,
                    ota_result = excluded.ota_result,
                    config_hash = excluded.config_hash,
                    retry_count = excluded.retry_count,
                    log_excerpt = excluded.log_excerpt,
                    has_firmware = excluded.has_firmware,
                    selection_reason = excluded.selection_reason
                """,
                row,
            )
            conn.commit()
            changed = cur.rowcount > 0
        if changed:
            logger.debug(
                "Recorded job history: id=%s target=%s state=%s ota=%s",
                row["id"], row["target"], row["state"], row["ota_result"],
            )
        return changed

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    # Bug #53: whitelist the columns that can be used for ORDER BY, so
    # a caller can't inject arbitrary SQL through the `sort` query param
    # on /ui/api/history. Every entry here is an indexed or small
    # enough field that sorting is fast in practice.
    _SORT_COLUMNS: frozenset[str] = frozenset({
        "finished_at", "started_at", "submitted_at", "duration_seconds",
        "target", "state", "esphome_version", "assigned_hostname",
        "triggered_by",
    })

    def query(
        self,
        target: str | None = None,
        state: str | None = None,
        since: int | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "finished_at",
        sort_desc: bool = True,
        until: int | None = None,
    ) -> list[dict[str, object]]:
        """Return history rows filtered by the given dims.

        *since* / *until* are epoch seconds; rows outside the window
        are excluded. *limit* is clamped to [1, 500]. *offset* is
        clamped to ≥ 0. *sort_by* is whitelisted against
        :attr:`_SORT_COLUMNS`; anything else falls back to
        ``finished_at``.

        Unknown filter values (state not in the set, sort_by outside
        the whitelist) yield an empty list rather than a DB error.
        """
        self.init()
        if not self._initialized:
            return []
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        where: list[str] = []
        params: list[object] = []
        if target is not None:
            where.append("target = ?")
            params.append(target)
        if state is not None:
            if state not in TERMINAL_STATES:
                return []
            where.append("state = ?")
            params.append(state)
        if since is not None:
            where.append("finished_at >= ?")
            params.append(int(since))
        if until is not None:
            where.append("finished_at <= ?")
            params.append(int(until))
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Bug #53: whitelisted sort. Tie-break on submitted_at so two
        # rows finishing in the same second (back-to-back failures,
        # validate-only coalescing) stay stable across paginated fetches.
        sort_col = sort_by if sort_by in self._SORT_COLUMNS else "finished_at"
        direction = "DESC" if sort_desc else "ASC"
        sql += f" ORDER BY {sort_col} {direction}, submitted_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._connect() as conn:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]

        # Bug #38: for rows that originally produced firmware, probe
        # the storage layer at read time to tell the UI whether the
        # `.bin` is still on disk (vs evicted by the budget task).
        # `has_firmware` stays 1 either way; `firmware_variants` carries
        # the live-available variants (empty list = evicted).
        #
        # TODO(PH.1): ``list_variants`` is an ``os.listdir`` per row,
        # so at ``limit=50`` + QueueHistoryDialog's infinite scroll we
        # N+1 stat the firmware dir O(rows × N) per session. Tolerable
        # at home-lab scale; revisit when someone accumulates hundreds
        # of retained firmwares. Tracked in WORKITEMS-future.md → Perf
        # hardening → PH.1 (fix shapes documented there).
        try:
            from firmware_storage import list_variants  # noqa: PLC0415
        except Exception:
            list_variants = None  # type: ignore[assignment]
        for r in rows:
            if r.get("has_firmware") and list_variants is not None:
                try:
                    r["firmware_variants"] = list_variants(str(r["id"]))
                except Exception:
                    r["firmware_variants"] = []
            else:
                r["firmware_variants"] = []
        return rows

    def latest_firmware_by_hash(
        self, target: str, hashes: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        """#211 — Map config_hash → most-recent successful job_id + variants.

        Used by ``/ui/api/files/<file>/history`` so the History panel can
        surface a Download chip on rows whose firmware binary is still on
        disk. Only rows with ``state='success'`` and ``has_firmware=1``
        are considered; binaries evicted by the retention task surface as
        ``firmware_variants=[]`` and the UI suppresses the chip.

        Returns ``{hash: {"job_id", "firmware_variants"}}`` for the
        subset of *hashes* that have a matching firmware-bearing job.
        Hashes without a match are absent from the result.
        """
        self.init()
        result: dict[str, dict[str, object]] = {}
        if not self._initialized:
            return result
        hash_set = {h for h in hashes if h}
        if not hash_set:
            return result
        # SQLite doesn't take Python sets; spell out a placeholder list.
        placeholders = ",".join("?" * len(hash_set))
        sql = (
            "SELECT id, config_hash, finished_at FROM jobs "
            "WHERE target = ? AND state = 'success' AND has_firmware = 1 "
            f"AND config_hash IN ({placeholders}) "
            "ORDER BY finished_at DESC, submitted_at DESC"
        )
        params: list[object] = [target, *hash_set]
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        try:
            from firmware_storage import list_variants  # noqa: PLC0415
        except Exception:
            list_variants = None  # type: ignore[assignment]
        for r in rows:
            h = r.get("config_hash")
            if not h or h in result:
                continue  # ORDER BY puts newest first — keep that one
            variants: list[str] = []
            if list_variants is not None:
                try:
                    variants = list_variants(str(r["id"]))
                except Exception:
                    variants = []
            if not variants:
                continue  # binary evicted — skip the row
            result[str(h)] = {"job_id": str(r["id"]), "firmware_variants": variants}
        return result

    def get(self, job_id: str) -> dict[str, object] | None:
        """Return a single history row keyed by *job_id*, or ``None``.

        Bug #1 (1.6.1): firmware download from history needs the target
        name + has_firmware flag for a job that's been coalesced out of
        the live queue. Populated fields mirror :meth:`query` — in
        particular ``firmware_variants`` is a live ``list_variants`` probe
        rather than a DB column, so an evicted binary surfaces as ``[]``
        even if ``has_firmware`` is still 1 from the row's original write.
        """
        self.init()
        if not self._initialized:
            return None
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
        if row is None:
            return None
        r = dict(row)
        try:
            from firmware_storage import list_variants  # noqa: PLC0415
        except Exception:
            list_variants = None  # type: ignore[assignment]
        if r.get("has_firmware") and list_variants is not None:
            try:
                r["firmware_variants"] = list_variants(str(r["id"]))
            except Exception:
                r["firmware_variants"] = []
        else:
            r["firmware_variants"] = []
        return r

    def stats(self, target: str | None = None, window_days: int = 30) -> dict[str, object]:
        """Return a rollup for *target* (or fleet-wide if None) over the
        last *window_days* days.

        Keys: ``total``, ``success``, ``failed``, ``cancelled``,
        ``timed_out``, ``avg_duration_seconds``, ``p95_duration_seconds``,
        ``last_success_at``, ``last_failure_at``. Every numeric field is
        0/None when there are no matching rows.
        """
        self.init()
        empty_stats: dict[str, object] = {
            "total": 0, "success": 0, "failed": 0, "cancelled": 0, "timed_out": 0,
            "avg_duration_seconds": None, "p95_duration_seconds": None,
            "last_success_at": None, "last_failure_at": None,
            "window_days": max(1, min(int(window_days), 3650)),
        }
        if not self._initialized:
            return empty_stats
        window_days = max(1, min(int(window_days), 3650))
        now = int(datetime.now(timezone.utc).timestamp())
        since = now - window_days * 86400

        params: list[object] = [since]
        target_clause = ""
        if target is not None:
            target_clause = " AND target = ?"
            params.append(target)

        # One SELECT for the per-state counts + avg duration.
        count_sql = f"""
            SELECT
                state,
                COUNT(*) AS n,
                AVG(duration_seconds) AS avg_dur
            FROM jobs
            WHERE finished_at >= ?{target_clause}
            GROUP BY state
        """
        # Separate SELECT for last_success_at / last_failure_at so the
        # per-state GROUP BY above stays clean.
        last_sql = f"""
            SELECT state, MAX(finished_at) AS last_at
            FROM jobs
            WHERE finished_at >= ?{target_clause}
              AND state IN ('success', 'failed', 'timed_out')
            GROUP BY state
        """

        with self._connect() as conn:
            per_state = {row["state"]: row for row in conn.execute(count_sql, params)}
            lasts = {row["state"]: row["last_at"] for row in conn.execute(last_sql, params)}
            # p95 via LIMIT/OFFSET: easier to read than a window function
            # and works on the sqlite that ships with every Python we
            # support. Rounded to nearest 1% of the matching count.
            count_all_sql = f"""
                SELECT COUNT(*) AS n
                FROM jobs
                WHERE finished_at >= ?{target_clause}
                  AND duration_seconds IS NOT NULL
            """
            total_with_dur = conn.execute(count_all_sql, params).fetchone()["n"] or 0
            p95: float | None = None
            if total_with_dur > 0:
                # Offset to the 95th percentile row — clamp to the last row.
                off = max(0, int(round(total_with_dur * 0.95)) - 1)
                p95_row = conn.execute(
                    f"""
                    SELECT duration_seconds
                    FROM jobs
                    WHERE finished_at >= ?{target_clause}
                      AND duration_seconds IS NOT NULL
                    ORDER BY duration_seconds ASC
                    LIMIT 1 OFFSET ?
                    """,
                    params + [off],
                ).fetchone()
                if p95_row and p95_row["duration_seconds"] is not None:
                    p95 = float(p95_row["duration_seconds"])

        # Reshape per-state grouping into the flat rollup the UI wants.
        total = sum(int(r["n"]) for r in per_state.values())
        # Weighted average across states, ignoring rows without duration.
        durations = [
            (int(r["n"]), float(r["avg_dur"]))
            for r in per_state.values()
            if r["avg_dur"] is not None
        ]
        avg: float | None = None
        if durations:
            weight_sum = sum(n for n, _ in durations)
            if weight_sum > 0:
                avg = sum(n * d for n, d in durations) / weight_sum

        return {
            "total": total,
            "success": int(per_state.get("success", {"n": 0})["n"]) if "success" in per_state else 0,
            "failed": int(per_state.get("failed", {"n": 0})["n"]) if "failed" in per_state else 0,
            "cancelled": int(per_state.get("cancelled", {"n": 0})["n"]) if "cancelled" in per_state else 0,
            "timed_out": int(per_state.get("timed_out", {"n": 0})["n"]) if "timed_out" in per_state else 0,
            "avg_duration_seconds": avg,
            "p95_duration_seconds": p95,
            "last_success_at": lasts.get("success"),
            "last_failure_at": max(
                (lasts[s] for s in ("failed", "timed_out") if s in lasts),
                default=None,
            ),
            "window_days": window_days,
        }

    def last_per_target(self, targets: Iterable[str] | None = None) -> dict[str, dict[str, object]]:
        """Return the most recent terminal row per target as a dict.

        Used by JH.6 to stamp ``last_compile`` onto the /ui/api/targets
        payload without N+1 queries. When *targets* is provided, only
        those rows are returned — otherwise the entire last-per-target
        set is. Uses a correlated MAX(finished_at) subquery because it's
        simpler than a window function and fast under the
        ``(target, finished_at DESC)`` index.
        """
        self.init()
        if not self._initialized:
            return {}
        params: list[object] = []
        target_clause = ""
        if targets is not None:
            targets = list(targets)
            if not targets:
                return {}
            placeholders = ",".join(["?"] * len(targets))
            target_clause = f" WHERE target IN ({placeholders})"
            params.extend(targets)

        sql = f"""
            SELECT j.*
            FROM jobs j
            INNER JOIN (
                SELECT target, MAX(finished_at) AS mx
                FROM jobs{target_clause}
                GROUP BY target
            ) last ON j.target = last.target AND j.finished_at = last.mx
        """
        with self._connect() as conn:
            out: dict[str, dict[str, object]] = {}
            for row in conn.execute(sql, params):
                out[str(row["target"])] = dict(row)
        return out

    # ------------------------------------------------------------------
    # Retention (JH.3)
    # ------------------------------------------------------------------

    def evict_older_than(self, days: int) -> list[str]:
        """Delete rows with ``finished_at`` older than *days* days ago.

        Returns the IDs of evicted rows so the caller can clean up
        coupled artifacts (firmware `.bin` blobs) — bug #198. No-op and
        returns ``[]`` when days <= 0.
        """
        if days <= 0:
            return []
        self.init()
        if not self._initialized:
            return []
        cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        with self._connect() as conn:
            # Single DELETE ... RETURNING is atomic in SQLite (3.35+,
            # well below what Python 3.11 ships): the evicted ids and
            # the deletion are one statement, so a concurrent writer
            # can't slip a row in between. SELECT-then-DELETE on the
            # same connection would NOT be atomic without an explicit
            # BEGIN IMMEDIATE.
            evicted = [
                str(row[0]) for row in conn.execute(
                    "DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ? RETURNING id",
                    (cutoff,),
                ).fetchall()
            ]
            conn.commit()
        if evicted:
            logger.info(
                "Evicted %d job-history row(s) older than %d day(s)",
                len(evicted), days,
            )
        return evicted
