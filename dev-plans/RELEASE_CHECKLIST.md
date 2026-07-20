# Release Checklist

Use when shipping `develop` → `main`. Copy into a GitHub issue and check items off.

The goal here is **what isn't automated**. Anything covered by CI, the pre-push hook, or `./push-to-hass-4.sh` is referenced with a one-liner — don't re-run it by hand.

---

## Pre-release (on `develop`)

### Claude does

- [ ] **Refresh pinned deps**: `bash scripts/refresh-deps.sh`. Review the diff and commit as `chore: refresh pinned deps for X.Y.Z`.
- [ ] **Bump pinned ESPHome version to current latest stable.** Find the latest with:
  ```bash
  curl -s https://pypi.org/pypi/esphome/json | jq -r '.info.version'
  ```
  This is the release's *"latest stable shipped"* pin — distinct from `MIN_ESPHOME_VERSION` in `ha-addon/server/constants.py`, which is the *support floor* and is only bumped when intentionally dropping old versions (a deliberate decision filed as its own workitem, not part of the per-release sweep). The runtime PyPI refresher in `ha-addon/server/main.py` (`_fetch_pypi_versions` / `pypi_version_refresher`) discovers new versions automatically at runtime, so this checklist item is only about static fallbacks/CI/test pins. Touchpoints to update:
  - `.github/workflows/compile-test.yml` — **both** matrix blocks (`compile-client` and `compile-server`); the existing `# latest stable shipped (bump per release)` comment marks the line in the first block, and the second block must be kept in sync with it.
  - `scripts/standalone/bootstrap-esphome.sh` — `ESPHOME_VERSION="${ESPHOME_VERSION:-X.Y.Z}"` default *and* the matching `# (default X.Y.Z — pinned to compile-test.yml)` comment in the header.
  - `scripts/push-to-standalone.sh` — `# ESPHOME_VERSION ... (default X.Y.Z)` header comment.
  - `tests/test_e2e_client.py` — `ESPHOME_TEST_VERSION = os.environ.get("ESPHOME_TEST_VERSION", "X.Y.Z")` default used by the real-ESPHome e2e tests.

  Confirm nothing got missed:
  ```bash
  grep -rn "$OLD_VERSION" --exclude-dir=node_modules --exclude-dir=build --exclude-dir=.git --exclude-dir=.worktrees --exclude-dir=archive .
  ```
  Hits in `.github/workflows/`, `scripts/`, or `ha-addon/` runtime code are not OK — bump them. Hits in `dev-plans/archive/`, historical comments documenting old behavior (`# 2026.4.3 deprecation warning fires for...`), `ha-addon/ui/e2e/fixtures.ts` (mocked Playwright fixtures, cosmetic only), and `tests/test_*.py` mock-data version strings *are* OK to leave — they don't pin upstream behavior. When in doubt: if a Python/shell script actually shells out to `esphome` or installs a venv with this version, bump it; if it's a string label inside a mock dict, leave it.

  Commit as `chore: bump pinned ESPHome to X.Y.Z`.
- [ ] **Dependabot**: confirm no open high/critical alerts. `gh api repos/:owner/:repo/dependabot/alerts --jq '.[] | select(.state=="open" and (.security_advisory.severity=="high" or .security_advisory.severity=="critical"))'` — must be empty. If any are open, upgrade the dep or explicitly accept the risk in WORKITEMS. (`pip-audit` + `npm audit` + ruff + mypy + pytest + invariants + frontend build already gate CI.)
- [ ] **Ensure CI is green on `develop`**: `gh run list --branch develop --limit 3`.
- [ ] **Bump version**: `bash scripts/bump-version.sh X.Y.Z`.
- [ ] **Write changelog entry** in `ha-addon/CHANGELOG.md`. Add a `## X.Y.Z` section. Source material is `dev-plans/WORKITEMS-X.Y.md` (has both completed work items and bug fixes). Group by category (features / improvements / bug fixes) and consolidate dev-iteration noise into clean user-facing descriptions.
- [ ] **Sync user-visible docs** if anything changed:
  - `README.md` — feature list, config tables, architecture.
  - `ha-addon/DOCS.md` — HA add-on panel docs.
  - `ha-addon/config.yaml` — `description`, `map`, `ports`, `options`, `schema`.
  Remove stale content (outdated diagrams, references to removed features, duplication of what the code already says).
- [ ] **Refresh `SECURITY.md`** — bump the Supported Versions table (e.g., `1.5.x → ✅ Current release`, demote the prior line to `✅ Previous stable — security fixes only if trivially backportable`, drop anything older than the previous stable). Re-read the "Security Measures" sections (Supply chain / Web surface / Protocol & validation / Auth & observability) and add bullets for hardening that landed this release; remove any bullets that no longer match shipped code. The "What is *not* in scope" list should stay aligned with `dev-plans/SECURITY_AUDIT.md`'s WONTFIX findings.
- [ ] **Refresh `dev-plans/SECURITY_AUDIT.md`** — bump the `**Last refreshed:**` date and version stamp at the top; add a refresh-note paragraph summarizing what flipped this cycle (OPEN → FIXED, new findings, status downgrades, re-assessments like the 1.6.1 SC.3 WONTFIX flip). Walk every F-* entry: each one whose status changed gets its **Status:** line rewritten with the release tag (e.g., `WONTFIX in 1.6.1 — re-assessed, see §F-18`). New code that opens a finding gets a new F-N entry. Update the OWASP Top 10 table and the Summary Table at the bottom to match. Cross-check against the `WORKITEMS-X.Y.md` SC.* / SA.* / AU.* sections — anything checked off there must be reflected here.
- [ ] **Cross-check security docs ↔ WORKITEMS ↔ code** — the three security surfaces (`SECURITY.md`, `dev-plans/SECURITY_AUDIT.md`, `dev-plans/WORKITEMS-X.Y.md`) and the actual implementation must agree. Drift between them has been the repeated failure mode. Mechanical steps:
  1. **Every security workitem `- [x]` in this release** → must appear in `SECURITY_AUDIT.md`'s Summary Table with a matching FIXED status and release tag. Run:
     ```bash
     grep -nE '^- \[x\] \*\*(SC|SA|AU)\.[0-9]' dev-plans/WORKITEMS-X.Y.md
     ```
     For each hit, `grep` `SECURITY_AUDIT.md` for the `F-N` it claims to close. Both should agree.
  2. **Every `FIXED (X.Y.Z-dev.N)` claim in `SECURITY_AUDIT.md`** → must be backed by real code, not a hopeful claim ahead of the commit. Spot-check the implementation with `grep` on the source (e.g., `grep -rnE 'require-hashes|chmod.*0o600' ha-addon/`) before accepting the FIXED status. If the code isn't there, the audit claim isn't either.
  3. **Every OPEN finding in `SECURITY_AUDIT.md`** → must be queued either as an unchecked workitem in *this* release or carried forward into a later `WORKITEMS-*.md` with a matching `F-N` reference. Grep:
     ```bash
     grep -nE 'F-0?[0-9]+' dev-plans/WORKITEMS-*.md
     ```
  4. **`SECURITY.md`'s "What is not in scope" / "What is not accepted" lists** → must match `SECURITY_AUDIT.md`'s WONTFIX / OPEN sets exactly. If the audit says "F-18 is the only remaining open finding," `SECURITY.md`'s "not accepted" section must have exactly one entry and it must be F-18. If a finding flipped to WONTFIX this cycle, it must also appear in `SECURITY.md`'s "not in scope" list.
  5. **Version stamps line up** — `**Last refreshed:**` in `SECURITY_AUDIT.md`, the Supported Versions row in `SECURITY.md`, and the `# Work Items — X.Y.Z` heading in WORKITEMS all reference the same X.Y.Z target.

  If any of the five spot-checks fails, fix the *docs or code that's wrong* — don't paper over it.
- [ ] **Produce a per-release UX review at `dev-plans/UX_REVIEW_X.Y.md`** (or `UX_REVIEW-X.Y.md` for patch releases; match whatever convention the last one used). If the prior release's review file is still at the active path (`dev-plans/UX_REVIEW-X.Y.md`), move it to `dev-plans/archive/` in the same PR to keep the active directory to the current release only. Re-do the UI walkthrough against the new release. The goal is "what an experienced UX reviewer would say *today*", not patching the prior version. Use Playwright against `http://hass-4.local:8765/` after deploy: each primary tab, every modal, the per-row hamburger, bulk-action dropdowns, mobile viewport, light + streamer mode. Update screenshots in `.playwright-mcp/ux-*.png`. For each finding from the previous review: mark as resolved (and remove) if shipped, keep + restate if still present. Add new findings the release introduced. The Prioritized Recommendations table should be re-numbered with **UX.N** entries the next release file can pick from. Update the version stamp + dev-build tag in the H1.
- [ ] **Check `docs/screenshot.png` is still representative** — compare the current Devices tab on hass-4 (`http://192.168.225.112:8765`) against the image in `docs/screenshot.png`. If columns, toolbar buttons, badges, or layout have changed meaningfully, take a fresh screenshot at ~1280px wide and replace the file. The screenshot is the GitHub README's primary hook — stale is worse than missing.

    **Canonical shape (bug #17, 1.6.1):** the hero screenshot must show the **Devices tab with the History drawer open on a representative file**, diff view selected. The point is to demonstrate that config history + rollback is real — it's the tentpole feature that differentiates Fleet from the stock ESPHome dashboard, and leading with a plain device list misses that in the five seconds a reader spends above the fold. Concrete recipe: open any device on hass-4 that has two+ commits, trigger *Config history…* from the hamburger, pick the second-most-recent commit, switch to the diff view. Crop at ~1280px wide with the Devices table still visible behind the drawer so the reader sees both surfaces at once. Keep the same shape on every refresh so month-over-month screenshots stay comparable.

    `scripts/capture-readme-screenshot.js` automates the whole flow (`PW_TOKEN=… node scripts/capture-readme-screenshot.js`; copy the resulting `/tmp/screenshot-history-diff.png` into `docs/screenshot.png`) so a releaser doesn't have to reproduce the click-path by hand.
- [ ] **Regenerate add-on / integration / brands artwork from the SVG source.** Bug #PR.1/PR.2/PR.5 (1.6.1) landed a canonical regeneration recipe so the shield + wordmark stay in sync across three surfaces (Supervisor add-on card, HA Integrations card, `brands.home-assistant.io`). Run when the SVG source changes, the release wants fresh artwork, or a size convention shifts. Requires `magick` (ImageMagick 7+ via `brew install imagemagick`). Source: `ha-addon/ui/src/assets/esphome-logo.svg`.

    ```bash
    # Add-on icons (Supervisor store card + in-app sidebar) + SVG sibling.
    magick -background none ha-addon/ui/src/assets/esphome-logo.svg -resize 128x128 ha-addon/icon.png
    cp ha-addon/ui/src/assets/esphome-logo.svg ha-addon/icon.svg

    # Add-on + integration logo.png: fetched verbatim from the v1.6.0
    # tag on GitHub. Do not regenerate from the SVG, do not resize, do
    # not make "improvements" — 1.6.1 PR #80 tried a landscape wordmark
    # and a 512×512 upscale, both rejected. Use the exact bytes that
    # shipped in 1.6.0.
    curl -sSL -o ha-addon/logo.png \
      https://raw.githubusercontent.com/weirded/fleet-for-esphome/v1.6.0/ha-addon/logo.png
    cp ha-addon/logo.png ha-addon/custom_integration/esphome_fleet/logo.png

    # home-assistant/brands submission. Icons are the square glyph; the
    # logo slot is a landscape wordmark per brands convention (rendered
    # on the Integrations picker where there's horizontal room).
    magick -background none ha-addon/ui/src/assets/esphome-logo.svg -resize 256x256 docs/brands-submission/custom_integrations/esphome_fleet/icon.png
    magick -background none ha-addon/ui/src/assets/esphome-logo.svg -resize 512x512 docs/brands-submission/custom_integrations/esphome_fleet/icon@2x.png
    WORDMARK() { magick -background none -fill "#18BCF2" -font "Helvetica-Bold" \
      -pointsize "$1" label:"Fleet for ESPHome" \
      -bordercolor none -border "$2" "$3"; }
    WORDMARK 64   20x16  docs/brands-submission/custom_integrations/esphome_fleet/logo.png
    WORDMARK 128  40x32  docs/brands-submission/custom_integrations/esphome_fleet/logo@2x.png
    ```

    Integration-local icon rendered at 256×256 so HA's Integrations card has a crisp render even before the `home-assistant/brands` PR merges. The 256×256 integration icon matches the 1× brands submission by design — when the brands PR lands, HA fetches the identical raster from the CDN instead of the package-local file, so there's no visual cutover.
- [ ] **Close out `dev-plans/WORKITEMS-X.Y.md`**: mark all completed and carry forward anything that didn't ship. Concrete steps:
  1. `grep -nE '^- \[ \]' dev-plans/WORKITEMS-X.Y.md` — every unchecked box must be either (a) checked, (b) struck-through with `~~**ID**~~ WONTFIX —` + reason, or (c) **moved verbatim** to `dev-plans/WORKITEMS-X.Y+1.md` (or a later release file) under a `## Carried forward from X.Y` heading. Don't just delete — losing context across releases is what this checklist exists to prevent.
  2. `grep -niE 'defer|TODO|follow.?up|nice to have|tracked for later|future iteration' dev-plans/WORKITEMS-X.Y.md` — for each hit, decide: shipped + obsolete (delete the note), or still pending (move to the successor file). The phrase "deferred to <Y>" only counts as resolved if `WORKITEMS-Y.md` actually lists it.
  3. After both grep passes are clean, `git mv dev-plans/WORKITEMS-X.Y.md dev-plans/archive/`.
- [ ] **Grep TODO/FIXME/HACK in source** — `grep -rnE '\b(TODO|FIXME|HACK|XXX)\b' ha-addon/ scripts/ tests/ .github/` (excluding `node_modules`, `dist`, lockfiles, and `ha-addon/server/static/assets/` generated bundles). Resolve, document as known issues, or move to a successor WORKITEMS file. Same forwarding rule as the dev-plans grep above — don't lose context.
- [ ] **Verify every in-source TODO points to a valid workitem** — per `CLAUDE.md` → Project Tracking, each `TODO(<ID>): …` must reference an identifier that resolves under `dev-plans/`. Run:
  ```bash
  # Extract first-party TODO pointers and check each one appears under dev-plans/.
  # Excludes node_modules (vendored code's own TODOs), __pycache__ (binary .pyc),
  # dist/build/.venv (generated), and server/static/assets (Vite bundles).
  grep -rnhoE '\b(TODO|FIXME|HACK|XXX)\([^)]+\)' \
      --exclude-dir=node_modules --exclude-dir=__pycache__ \
      --exclude-dir=dist --exclude-dir=build --exclude-dir=.venv \
      --exclude-dir=assets \
      ha-addon/ scripts/ tests/ .github/ |
    sed -E 's/.*\(([^)]+)\).*/\1/' |
    sort -u |
    while IFS= read -r id; do
      if ! grep -rq -- "$id" dev-plans/; then
        echo "STALE: $id — not referenced anywhere under dev-plans/"
      fi
    done
  ```
  Zero output = clean. Any `STALE:` line means the pointer doesn't resolve: either file the underlying work in a `WORKITEMS-*.md`, update the TODO to reference an existing entry, or delete the TODO (and fix the code, if keeping it means fixing it). PR numbers, reviewer names, and commit SHAs are NOT valid pointers. Pointer IDs should be terse (`IT.2`, `PH.1`, `#NNN`) — avoid free-form trailing qualifiers inside the parens (`TODO(IT.2 follow-up)`) so the grep matches cleanly.

### You do

- [ ] **Drain PR review feedback before merge.** If a release PR (`develop → main`) is open, every review thread on it must be either addressed with a code change or closed with a pointer to a tracked workitem — and in both cases **marked resolved** on GitHub. Unresolved threads on a merged release PR look like open concerns that nobody looked at; they clutter the PR sidebar until archive. Concrete steps:
  ```bash
  # List unresolved threads:
  gh api graphql -f query='
    query($owner:String!,$repo:String!,$pr:Int!) {
      repository(owner:$owner, name:$repo) {
        pullRequest(number:$pr) {
          reviewThreads(first:50) {
            nodes { id isResolved path line comments(first:1) { nodes { body } } }
          }
        }
      }
    }' -F owner=weirded -F repo=distributed-esphome -F pr=<PR> |
    jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false)'
  ```
  For each one: fix the code (or file it as a WORKITEMS entry with an explicit scope-deferral rationale), push, then resolve:
  ```bash
  gh api graphql -f query='
    mutation($id:ID!) { resolveReviewThread(input:{threadId:$id}) { thread { isResolved } } }
  ' -F id=<PRRT_id>
  ```
  Re-run the list query until zero unresolved threads. "Fix and leave the thread open" doesn't count as done (see `CLAUDE.md` → PR Review Loop).
- [ ] **Deploy + smoke test all three install paths**: `python scripts/test-matrix.py`. Builds+pushes the three dev-tagged images to GHCR and runs the `e2e-hass-4` Playwright suite against hass-4, the HAOS VM on `pve`, and the standalone Docker container on `docker-pve` in parallel. This is the authoritative release smoke — a green matrix exercises all three ways a user can install, not just the happy-path hass-4 one. Per-target logs + reports land under `build/test-matrix/<target>/`. `./push-to-hass-4.sh` alone is the per-turn fast path, not the release smoke.
- [ ] **Read the changelog draft** — does it represent what users care about?
- [ ] **Sanity-check editor autocomplete on a real config** — the only thing Playwright can't verify end-to-end.
- [ ] Note any config changes that need migration notes for users upgrading.
- [ ] Decide: merge all `develop` commits, or cherry-pick?

---

## Release (merge to `main`)

### Claude does

- [ ] Create release branch if needed: `git checkout -b release/X.Y.Z develop`.
- [ ] Final commit with version + changelog + docs on `develop` (or release branch).
- [ ] Merge to main: `git checkout main && git merge develop` (or merge the release branch).
- [ ] Push: `git push origin main`. Pre-push hook runs tests + mypy + changelog check. GHCR publish workflows fire automatically.
- [ ] Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
- [ ] Verify: `gh run list --branch main --limit 3` and `gh api /orgs/{owner}/packages/container/{name}/versions --jq '.[0]'`.

---

## Post-release

### Claude does

- [ ] **Create GitHub release**: `gh release create vX.Y.Z --title "X.Y.Z — <short theme>" --notes-file <(awk '/^## X\.Y\.Z/{f=1;next} /^## /{f=0} f' ha-addon/CHANGELOG.md)`. Uses the changelog section as the release body so the GitHub Releases page stays in sync with CHANGELOG.md. Mark as latest unless it's a point release behind an active major line.
- [ ] Start next dev cycle: `git checkout develop && bash scripts/bump-dev.sh`.
- [ ] Create `dev-plans/WORKITEMS-X.Y+1.md` — copy structure from the previous file, leave items unchecked.

### You do

- [ ] Update the HA add-on repo (if using a separate repo for distribution).
- [ ] Verify the add-on updates cleanly on hass-4 from the published image.
- [ ] Post release notes if desired (GitHub release, Reddit, Discord).

---

## Reference

**`scripts/bump-version.sh X.Y.Z`** keeps these in sync:

| File | Field |
|------|-------|
| `ha-addon/VERSION` | entire content |
| `ha-addon/config.yaml` | `version:` field |
| `ha-addon/client/client.py` | `CLIENT_VERSION` constant |

**`.githooks/pre-push`** runs `pytest` + `mypy` on every push, plus a `CHANGELOG.md` entry check when pushing to `main`. Install with `bash scripts/install-hooks.sh`.

**Integration-test home lab** — this checklist assumes SSH access to `hass-4`, `docker-pve`, `docker-optiplex-5`, `pve`, and `optiplex-5`, all on the `192.168.224.0/22` flat network. Full inventory and SSH setup in `dev-plans/HOME-LAB.md`.
