# PR draft — GitHub Sync (GS.1–GS.5)

Implements the optional-remote GitHub sync feature scoped in `dev-plans/WORKITEMS-future.md`. Pairs with 1.6's `git_versioning.py` auto-commit so every save also lands on the user's chosen remote.

## Scope of this PR

GS.1 + GS.2 (settings + push) for the initial draft. GS.3, GS.4, GS.5 follow in subsequent PRs once the auth-storage approach is acked by the maintainer.

## Design choices

- **New module** `ha-addon/server/git_remote.py`. Mirrors the defensive contract of `git_versioning.py`: every git op runs through `_run`; failures log at WARNING (or EXCEPTION on unexpected shape) and never propagate. The request handler returns `200` with a `{ok: false, error: "<short reason>"}` body — never 500 — so a flapping remote can't take down `/ui/api/info`.
- **Auth: PAT (HTTPS) and SSH key**, both stored in `/data/settings.json` (Supervisor encrypts at rest). At apply time:
  - **PAT/HTTPS**: write a 700-mode credential helper script to `/data/git-credentials.sh` that echoes `username=x-access-token\npassword=<PAT>`. Configure `git config --local credential.helper '/data/git-credentials.sh'`.
  - **SSH**: write the key to `/data/ssh/id_remote` (mode 600), set `core.sshCommand = ssh -i /data/ssh/id_remote -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/data/ssh/known_hosts`.
  - Both paths are scoped to the repo via `git config --local`, so a user re-pointing to a different remote doesn't leak credentials across repos.
- **Trigger model**: three modes via `git_push_mode` setting:
  - `per_commit` (default) — `git_versioning._commit()` calls `git_remote.push_async()` after a successful commit. Fire-and-forget; failures logged + surfaced on `/ui/api/info`.
  - `every_n_minutes` — background task in `main.py` driven by `git_push_interval_minutes`.
  - `manual` — only via `POST /ui/api/git/push`.
- **Pull on startup** if `git_pull_on_startup=true` and a remote is configured. Also exposed at `POST /ui/api/git/pull`. Always uses `--rebase`. On conflict: `git rebase --abort`, keep local, log WARNING, surface `git_remote_status: "diverged"` in `/ui/api/info` with the conflicting file list.
- **Status surfacing**: extend the existing `/ui/api/info` payload with:
  ```
  git_remote: {
    configured: bool,
    url: str | null,
    auth: "pat" | "ssh" | null,
    last_push_at: iso8601 | null,
    last_pull_at: iso8601 | null,
    last_error: { kind: str, message: str, at: iso8601 } | null,
    pending_push: int,    // commits ahead of upstream
    diverged_files: [str] | null,
  }
  ```
- **`.gitignore` enforcement (GS.5)**: on remote-config save (and on init), ensure `secrets.yaml` is in the repo's `.gitignore`. If `secrets.yaml` is already tracked, log WARNING and emit `git_remote.last_error.kind = "secrets_yaml_committed"` so the UI can show a banner. Don't auto-untrack — that's destructive without consent.

## File-level checklist

### GS.1 — Remote configuration

- [ ] `ha-addon/server/settings.py` — add fields:
  - `git_remote_url: str = ""`
  - `git_remote_auth: Literal["pat","ssh",""] = ""`
  - `git_remote_token: str = ""` (PAT)
  - `git_remote_ssh_key: str = ""` (private key contents)
  - `git_push_mode: Literal["per_commit","every_n_minutes","manual"] = "per_commit"`
  - `git_push_interval_minutes: int = 5`
  - `git_pull_on_startup: bool = True`
- [ ] `ha-addon/server/git_remote.py` — new module:
  - `configure_remote(repo_dir, settings) -> Result` (writes credential files + `git remote set-url`)
  - `validate_connectivity(repo_dir) -> Result` (`git ls-remote --heads`)
  - `push(repo_dir) -> Result`
  - `pull_rebase(repo_dir) -> Result`
  - State holder for `last_push_at`, `last_pull_at`, `last_error`.
- [ ] `ha-addon/server/main.py` — wire startup:
  - On boot: if `git_remote_url` set, call `configure_remote()` then (if `git_pull_on_startup`) `pull_rebase()`.
  - If `git_push_mode == "every_n_minutes"`, spawn background loop.
- [ ] `ha-addon/server/ui_api.py` — settings endpoint validates new fields; redacts token/key on read.
- [ ] Tests: `tests/test_git_remote.py` exercises auth file writing, push, pull, conflict (using a bare repo as fake remote).

### GS.2 — Push

- [ ] `ha-addon/server/ui_api.py` — `POST /ui/api/git/push` (manual trigger).
- [ ] `ha-addon/server/git_versioning.py` — call `git_remote.push_async()` after successful auto-commit when `git_push_mode == "per_commit"`. Wrap in try/except — never let a remote failure block the local commit being acknowledged.
- [ ] `/ui/api/info` extension (`git_remote` block above).
- [ ] Tests: per-commit auto-push, manual push, push-on-divergence (rejected, surfaced).

### GS.3 — Pull (follow-up PR)

- [ ] `POST /ui/api/git/pull` handler.
- [ ] Conflict abort + status surfacing.
- [ ] Tests: clean pull, conflict pull, no-op pull.

### GS.4 — UI panel (follow-up PR)

- [ ] `ha-addon/ui/src/components/SettingsDrawer/GitRemotePanel.tsx`:
  - URL + auth picker (PAT vs SSH) + token/key textarea (write-only, never echoed back).
  - Last push / last pull / error display.
  - "Push now" / "Pull now" buttons.
  - "Test connectivity" button → calls `validate_connectivity()`.

### GS.5 — `.gitignore` management (follow-up PR)

- [ ] `git_versioning.py` — already enforces `secrets.yaml`/`.esphome/` in `GITIGNORE_ENTRIES`. Verify it survives `configure_remote()`.
- [ ] On remote configure: scan `git ls-files` for `secrets.yaml`; if found, set `last_error.kind = "secrets_yaml_committed"`.

## Open questions for the maintainer

1. Token storage: settings.py vs a dedicated `/data/secrets.json` with stricter access? Supervisor encryption is the same either way; prefer settings.py for fewer moving parts unless you'd rather keep it out of the public-ish settings surface.
2. `per_commit` push debounce: I'd default to a 2-second debounce matching `git_versioning.DEBOUNCE_SECONDS` so a multi-file save → one push, not N. Ack?
3. SSH key: support both ed25519 and rsa, but log a WARNING for rsa (deprecated by GitHub on new keys). Ack?
4. PR shape: is one PR for GS.1+GS.2 + follow-up PRs for GS.3/4/5 acceptable, or would you rather see a single bigger PR with all five?

## Out of scope here

- Auto-resolution of merge conflicts.
- Multiple remotes / multiple branches.
- Force-push.
- Submodules.
- LFS.
- A CI hook that prevents `secrets.yaml` from being committed in the first place — server enforces via `.gitignore` only.
