#!/usr/bin/env bash
# Grep-based invariant linter for CLAUDE.md "Enforced invariants" (D.5).
#
# This is intentionally simple — every rule is a single grep call. If a rule
# cannot be expressed as a one-liner it belongs in ruff / mypy / the TS type
# checker, not here. Each failing rule prints the offending file and line
# and the script exits non-zero so CI fails loudly.
#
# Run locally:
#   bash scripts/check-invariants.sh
#
# Wired into the ``test`` job of .github/workflows/ci.yml.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Ensure Homebrew tools (rg, GNU grep) are visible when the script is invoked
# from a child shell that hasn't sourced the user's interactive rc file.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# Prefer ripgrep if available (faster, recursive by default, handles
# .gitignore). Fall back to GNU grep with extended regex. Both understand
# the same POSIX ERE patterns used below.
if command -v rg >/dev/null 2>&1; then
    SEARCH() { rg --no-heading --line-number --with-filename -e "$1" "${@:2}"; }
else
    SEARCH() { grep -RnHE -e "$1" "${@:2}"; }
fi

fail_count=0
rule_count=0

# Run SEARCH, capture output, apply optional allowlist filter, store in $hits.
# Exits non-zero if no matches found; we swallow that with ``|| true`` so the
# outer check can decide whether an empty result means pass or fail.
run_search() {
    local pattern="$1"; shift
    local allow="$1"; shift
    if [[ -n "$allow" ]]; then
        SEARCH "$pattern" "$@" 2>/dev/null | grep -Ev "$allow" || true
    else
        SEARCH "$pattern" "$@" 2>/dev/null || true
    fi
}

fail() {
    local rule_id="$1"; shift
    local message="$1"; shift
    echo ""
    echo "❌ ${rule_id}: ${message}"
    printf '%s\n' "$*"
    fail_count=$((fail_count + 1))
}

# check_absent <rule_id> <description> <pattern> <allowlist|""> <path...>
# Fails if <pattern> is found in any file under <path...>, except lines
# matching the optional <allowlist> regex.
check_absent() {
    local rule_id="$1"; shift
    local description="$1"; shift
    local pattern="$1"; shift
    local allow="$1"; shift
    rule_count=$((rule_count + 1))
    local hits
    hits=$(run_search "$pattern" "$allow" "$@")
    if [[ -n "$hits" ]]; then
        fail "$rule_id" "$description" "$hits"
    fi
}

echo "▶ Checking CLAUDE.md Enforced Invariants…"

# -----------------------------------------------------------------------------
# UI invariants
# -----------------------------------------------------------------------------

# (UI-1) No fetch() outside the api/ layer. Components must never call fetch
# directly — all server calls go through api/client.ts or a sibling module
# under api/. This is what stopped the EditorModal.tsx / schema.esphome.io
# violation from sneaking back in (C.5).
check_absent "UI-1" \
    "fetch() found outside ha-addon/ui/src/api/ — route all server calls through the api/ layer" \
    'fetch\(' \
    '/src/api/|/__tests__/|\.test\.|\.spec\.' \
    ha-addon/ui/src

# (UI-2) No Tailwind @apply directives. The project uses utility classes in
# JSX exclusively; @apply fragments the styling vocabulary across CSS files.
check_absent "UI-2" \
    "@apply directive found — use Tailwind utility classes in JSX, not @apply" \
    '@apply' \
    '' \
    ha-addon/ui/src

# (UI-3) No ``any`` type introduced in new UI code. Flags ``: any`` and
# ``as any`` (with POSIX-portable negative character classes as the
# word-boundary substitute since BSD grep lacks ``\b``). Existing sanctioned
# uses can be allow-listed with an inline ``// ALLOW_ANY`` comment.
check_absent "UI-3" \
    "explicit any type in TS — use unknown or a real type (use // ALLOW_ANY to opt out)" \
    ':[[:space:]]*any([^a-zA-Z_0-9]|$)|as[[:space:]]+any([^a-zA-Z_0-9]|$)' \
    'ALLOW_ANY|\.d\.ts:' \
    ha-addon/ui/src

# (UI-4) No CSS flex on <td>. Table cells must not be flex containers — it
# breaks the table layout model. Came from a real bug.
check_absent "UI-4" \
    "flex/inline-flex on <td> — tables layout, not flex" \
    '<td[^>]*(className|class)="[^"]*(^|[[:space:]])(inline-)?flex([[:space:]]|")' \
    '' \
    ha-addon/ui/src

# (UI-6) No silent "return default on !r.ok" in api/client.ts (CR.5). When
# a handler fails, the UI needs to see an error — not a blank list + a
# feature silently off. Flag any line that returns an array/object literal
# immediately after `if (!r.ok)`. Throw or toast instead.
check_absent "UI-6" \
    "api/client.ts silent error-swallowing — throw or toast, don't return default" \
    'if \(!r\.ok\) return (\[|\{)' \
    '' \
    ha-addon/ui/src/api

# (E2E-1) No `page.waitForTimeout(N)` in e2e specs (CR.6). Fixed sleeps
# are flake factories — the test finishes slower than CI, or faster than
# the page state settles. Always wait on an observable condition
# (`expect.poll`, `toBeVisible`, `toHaveCount(0)`) instead. Allow-listed:
# none — if there's ever a legitimate reason, add it to the allowlist
# here with a short comment.
check_absent "E2E-1" \
    "page.waitForTimeout in e2e specs — replace with a DOM-state wait" \
    'page\.waitForTimeout\(' \
    '' \
    ha-addon/ui/e2e ha-addon/ui/e2e-hass-4

# (UI-7) Icon-only buttons need both aria-label and title (UX.12). Icon
# controls are unlabeled by default — screen readers need aria-label, and
# sighted hover needs a title. If you're reaching for one, you need both.
# Narrow grep: any <button> or *Trigger opening tag that mentions
# aria-label= but doesn't have title= on the same opening tag (or vice
# versa) is flagged. Opening tags can span multiple lines (Prettier style),
# so the single-line grep approximates the rule — full enforcement is
# reviewed. This still catches the common case of adding aria-label and
# forgetting title (or vice versa) on one line.
check_absent "UI-7" \
    "icon-only button has aria-label but no title (or vice versa) on the same line" \
    '<(button|[A-Z][A-Za-z]*Trigger)\b[^>]*aria-label=[^>]*>[^<]*$' \
    'title=' \
    ha-addon/ui/src

# -----------------------------------------------------------------------------
# Python invariants
# -----------------------------------------------------------------------------

# (PY-1) YAML parsing must go through yaml.safe_load — never regex. Hand-rolled
# regex YAML parsers broke device-name detection (#160). The known
# ``_ota_network_diagnostics`` fallback path is allow-listed: it explicitly
# tries safe_load first and only falls back to regex after catching an
# exception, which is the correct pattern.
check_absent "PY-1" \
    "YAML parsed with regex instead of yaml.safe_load" \
    '_re\.(search|match|findall)\(.*(esphome|ssid|password|wifi):' \
    '_ota_network_diagnostics|# ALLOW_REGEX_YAML' \
    ha-addon/server ha-addon/client

# (PY-2) Subprocess invocations must log. Every file that contains a
# ``subprocess.run(`` or ``subprocess.Popen(`` call must also have a
# module-level ``logger = logging.getLogger(…)``. Real subprocess logging
# of the command line is enforced by code review, but this at least catches
# a file that forgot to wire up logging entirely — which is how #176/#177/#180
# became untriageable.
rule_count=$((rule_count + 1))
subproc_files=$(SEARCH 'subprocess\.(run|Popen)\(' ha-addon/client ha-addon/server 2>/dev/null | cut -d: -f1 | sort -u || true)
missing_logger=""
for f in $subproc_files; do
    [[ -f "$f" ]] || continue
    if ! grep -q 'logger = logging.getLogger' "$f" 2>/dev/null; then
        missing_logger="${missing_logger}${f}: subprocess without module-level logger"$'\n'
    fi
done
if [[ -n "$missing_logger" ]]; then
    fail "PY-2" \
        "subprocess.run/Popen in a file without a module-level logger — command lines must be logged" \
        "$missing_logger"
fi

# (PY-3) ``esphome run`` vs ``esphome upload`` argument confusion (#177). The
# retry path in client.py MUST NOT pass --no-logs to ``esphome upload``. The
# test_run_job_ota_retry_uses_upload_without_no_logs unit test already guards
# this at runtime, but we also grep-check here so a refactor that moves the
# command construction still trips an alarm.
check_absent "PY-3" \
    "'esphome upload' invocation passes --no-logs — that flag is run-only (#177)" \
    '"upload",.*--no-logs|--no-logs.*"upload"' \
    '' \
    ha-addon/client

# (PY-4) IMAGE_VERSION bump reminder: warn (not fail) if the client Dockerfile
# or requirements.txt is newer than IMAGE_VERSION. See the dev.2 incident in
# WORKITEMS-1.3.1 — the pydantic add broke every deployed worker because
# IMAGE_VERSION wasn't bumped. This check is soft (warn-only) because file
# mtimes aren't reliable across git checkouts.
rule_count=$((rule_count + 1))
reqs_file="ha-addon/client/requirements.txt"
docker_file="ha-addon/client/Dockerfile"
image_ver_file="ha-addon/client/IMAGE_VERSION"
if [[ -f "$reqs_file" && -f "$image_ver_file" ]]; then
    if [[ "$reqs_file" -nt "$image_ver_file" || "$docker_file" -nt "$image_ver_file" ]]; then
        echo ""
        echo "⚠ PY-4 (warning, not blocking): ha-addon/client/{requirements.txt,Dockerfile} modified more recently than IMAGE_VERSION."
        echo "   Did you forget to bump ha-addon/client/IMAGE_VERSION and constants.MIN_IMAGE_VERSION?"
        echo "   See the dev.2 incident in dev-plans/WORKITEMS-1.3.1.md."
    fi
fi

# -----------------------------------------------------------------------------
# (PY-8) Every direct dependency in requirements.txt must also be pinned in
# requirements.lock. Root cause of bug #34 (1.4.0-dev.17): croniter was added
# to ha-addon/server/requirements.txt in the scheduler feature PR but
# scripts/refresh-deps.sh was never run, so requirements.lock never picked
# it up. The Dockerfile installs from the lock only (via --require-hashes),
# so the scheduler task returned silently on croniter ImportError and no
# scheduled upgrades ever fired in prod.
# -----------------------------------------------------------------------------
rule_count=$((rule_count + 1))
for reqs in ha-addon/server/requirements.txt ha-addon/client/requirements.txt; do
    lock="${reqs%.txt}.lock"
    [[ -f "$reqs" && -f "$lock" ]] || continue
    while IFS= read -r line; do
        # Skip blank lines and comments
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        # Extract package name (everything before ==/>=/~=/<=/< /> /!=/<space>)
        pkg="$(echo "$line" | sed -E 's/[[:space:]]*([A-Za-z0-9_.-]+).*/\1/' | tr '[:upper:]' '[:lower:]')"
        [[ -z "$pkg" ]] && continue
        # Lock entries are normalized to lowercase by pip-compile; check for
        # "^pkg==" at the start of a line (before continuation backslash).
        if ! grep -qiE "^${pkg}==" "$lock"; then
            fail "PY-8" "$lock: package '$pkg' from $reqs is not pinned in the lockfile. Run: bash scripts/refresh-deps.sh"
        fi
    done < "$reqs"
done

# -----------------------------------------------------------------------------
# (#56) requirements.lock must not carry macOS-only transitive deps. Regenerating
# the lockfile on a Mac host (instead of via scripts/refresh-deps.sh's Docker
# container on linux/amd64) pulls pyobjc-core + pyobjc-framework-* in as
# platform-conditional transitives WITHOUT their sys_platform markers, which
# then causes "error: PyObjC requires macOS to build" on every Linux Docker
# build. Happened twice — 1.3.1-dev.9 and 1.4.1-dev.55. One-line guard: if any
# of the tell-tale package names appears in the lockfile, fail CI with a
# pointer to the proper regeneration path.
# -----------------------------------------------------------------------------
rule_count=$((rule_count + 1))
for lock in ha-addon/server/requirements.lock ha-addon/client/requirements.lock; do
    [[ -f "$lock" ]] || continue
    if grep -qiE "^(pyobjc|appnope|pyobjc-core|pyobjc-framework-)" "$lock"; then
        offenders=$(grep -iE "^(pyobjc|appnope|pyobjc-core|pyobjc-framework-)" "$lock" | head -5 | paste -sd, -)
        fail "#56" "$lock: macOS-only deps leaked in [$offenders]. Regenerate via: bash scripts/refresh-deps.sh (inside Docker linux/amd64)."
    fi
done

# -----------------------------------------------------------------------------
# (SC.1) Every `uses: <org>/<repo>@…` in .github/workflows/*.yml must reference
# a 40-char commit SHA (not a moving tag like @v6). Tag refs are an attack
# vector — whoever controls the tag controls what our CI runs. SHA pins
# freeze the exact commit Dependabot already understands how to bump.
# Closes F-19. Any new workflow action needs to be resolved via:
#     gh api repos/<org>/<repo>/git/ref/tags/<tag> --jq .object.sha
# and the result committed with a trailing "# <tag>" comment.
# -----------------------------------------------------------------------------
rule_count=$((rule_count + 1))
for wf in .github/workflows/*.yml; do
    [[ -f "$wf" ]] || continue
    # Extract every non-local "uses:" reference. Skip ./ (local composite
    # actions) since they live in the same repo and don't have a remote SHA.
    while IFS= read -r line; do
        # "uses: org/repo@REF [# comment]" — pull the REF token.
        ref="$(echo "$line" | awk -F'@' '{print $2}' | awk '{print $1}')"
        [[ -z "$ref" ]] && continue
        # Require 40-char lowercase hex (full SHA).
        if [[ ! "$ref" =~ ^[0-9a-f]{40}$ ]]; then
            fail "SC.1" "$wf: uses-line does not SHA-pin: $line"
        fi
    done < <(grep -E "^\s*(- )?uses:[[:space:]]+[^./]" "$wf")
done

# -----------------------------------------------------------------------------
# (#57) Workflow YAML sanity check. The SC.1 mass-rewrite in eec0511 jammed
# two `uses:` directives onto one line because the replacement regex included
# `\s*` in a trailing group and swallowed newlines. SC.1 still saw a valid
# SHA on each broken line so the rule didn't catch it — all four workflows
# failed at GitHub's workflow-load step instead.
#
# Primary guard: no line can contain two `uses:` tokens — that's always a
# bug, and it's what actually regressed. Secondary (optional) guard: if
# PyYAML is available in the current Python, also run yaml.safe_load on
# each workflow as a broader sanity check. CI's Python has pyyaml; the
# local dev shell's Homebrew python often doesn't — don't fail the whole
# invariant run just because yaml isn't importable locally.
# -----------------------------------------------------------------------------
rule_count=$((rule_count + 1))
yaml_available=0
if python3 -c "import yaml" 2>/dev/null; then
    yaml_available=1
fi
for wf in .github/workflows/*.yml; do
    [[ -f "$wf" ]] || continue
    # Primary: two `uses:` on one line = jammed-line regression.
    if grep -En '(^|[^a-zA-Z_])uses:.*[^a-zA-Z_]uses:' "$wf" >/dev/null 2>&1; then
        offender=$(grep -En '(^|[^a-zA-Z_])uses:.*[^a-zA-Z_]uses:' "$wf" | head -1)
        fail "#57" "$wf: line has two \`uses:\` directives — jammed-line regression (see SC.1 commit eec0511). Offender: $offender"
    fi
    # Secondary: yaml.safe_load parse check, only when pyyaml is present.
    if [[ $yaml_available -eq 1 ]]; then
        if ! python3 -c "import sys, yaml; yaml.safe_load(open(sys.argv[1]))" "$wf" 2>/dev/null; then
            fail "#57" "$wf: does not parse as valid YAML. Run: python3 -c 'import yaml; yaml.safe_load(open(\"$wf\"))' for the error."
        fi
    fi
done

# -----------------------------------------------------------------------------
# (PY-10) tests/test_integration_*.py files that don't have a ``_logic``
# suffix must import ``pytest_homeassistant_custom_component`` — the HA
# custom-integration pytest plugin. Rationale (IT.1 from 1.6 punchlist):
# the plain ``test_integration_*`` name reads as "real integration test
# against a running HA" but the existing files are mock-based helper
# tests using SimpleNamespace + MagicMock. The naming drift led reviewers
# to trust coverage that wasn't there — CR.12 class bugs (async_setup_entry
# misuse, unique_id collisions, config-flow regressions) passed mocked
# tests but broke real HA.
#
# Rule: filename ending in ``_logic.py`` is exempt (it's an honest helper
# test); everything else matching ``test_integration_*.py`` must show an
# import of pytest_homeassistant_custom_component somewhere in the file.
# -----------------------------------------------------------------------------
rule_count=$((rule_count + 1))
for f in tests/test_integration_*.py; do
    [[ -f "$f" ]] || continue
    # Exempt _logic.py files — those are intentionally mock-based.
    if [[ "$f" == *"_logic.py" ]]; then
        continue
    fi
    if ! grep -q 'pytest_homeassistant_custom_component' "$f"; then
        fail "#PY-10" "$f: test_integration_*.py without '_logic' suffix must import pytest_homeassistant_custom_component (or rename to *_logic.py if it's a helper test)."
    fi
done

# -----------------------------------------------------------------------------
# (PY-10b / CI.5) Skipped-integration-test ratio.
#
# PY-10 above guarantees non-_logic test_integration_*.py files import the
# HA custom-integration pytest plugin. That makes them *real* tests on
# paper — but the invariant doesn't catch a future regression where every
# real test gets ``@pytest.mark.skip``-decorated and the plugin import is
# the only honest part left. Same coverage-mirage failure mode IT.1
# documented for the mock-based files.
#
# Rule: across every non-_logic test_integration_*.py file, count
# ``@pytest.mark.skip`` decorators vs total ``def test_`` /
# ``async def test_`` declarations. If skip / total > 50 %, fail.
# Empty file set is a pass (no integration suite at all → not this
# invariant's problem).
# -----------------------------------------------------------------------------
rule_count=$((rule_count + 1))
total_tests=0
total_skips=0
for f in tests/test_integration_*.py; do
    [[ -f "$f" ]] || continue
    if [[ "$f" == *"_logic.py" ]]; then
        continue
    fi
    file_tests=$(grep -cE '^(async )?def test_' "$f")
    file_skips=$(grep -cE '@pytest\.mark\.skip' "$f")
    total_tests=$((total_tests + file_tests))
    total_skips=$((total_skips + file_skips))
done
if [[ $total_tests -gt 0 ]]; then
    # Bash integer math — compare 100*skips vs 50*tests to avoid floats.
    if [[ $((total_skips * 100)) -gt $((total_tests * 50)) ]]; then
        fail "#PY-10b" "Skipped-test ratio in non-_logic test_integration_*.py files is $total_skips/$total_tests (>50%) — the suite is mostly pass-through, defeating PY-10's coverage guarantee. Either un-skip or move the helper-test cases into a *_logic.py companion file."
    fi
fi

# -----------------------------------------------------------------------------
# (PY-12) BR.1 anti-drift: "ESPHome Fleet" must not appear in user-facing
# strings outside the rebrand allowlist. The 1.7.1 brand refresh renamed
# every customer-visible mention to "Fleet for ESPHome" (BR.1); this rule
# keeps the old wording from creeping back via a forgotten string, a
# copy-pasted log line, or a refactor that lifts a stale comment into a
# live label.
#
# Allowlisted because the old literal is intentional there:
#   - dev-plans/archive/             (frozen historical plans, including the 1.7.1 rebrand plan)
#   - ha-addon/CHANGELOG.md          (entries describing past releases)
#   - any line containing the marker "br1-allow: <reason>" (per-line opt-out)
#
# To opt out a single legitimate reference (back-compat string for old
# user YAMLs, brand-history sentence in a top-level doc, etc.), add an
# inline "br1-allow: <reason>" comment and explain *why* the old literal
# is the right text there. Aim for fewer than ten markers across the repo;
# beyond that, prefer rephrasing.
# -----------------------------------------------------------------------------
rule_count=$((rule_count + 1))
br1_hits=$(git ls-files \
    | grep -vE '^dev-plans/archive/' \
    | grep -vE '^ha-addon/CHANGELOG\.md$' \
    | grep -vE '^scripts/check-invariants\.sh$' \
    | xargs grep -nHI 'ESPHome Fleet' 2>/dev/null \
    | grep -v 'br1-allow:' \
    || true)
if [[ -n "$br1_hits" ]]; then
    fail "PY-12" \
        "'ESPHome Fleet' literal found outside the BR.1 allowlist (Fleet for ESPHome rebrand). Rename it to 'Fleet for ESPHome', or add an inline 'br1-allow: <reason>' marker comment if the old wording is intentional." \
        "$br1_hits"
fi

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------

echo ""
if [[ $fail_count -eq 0 ]]; then
    echo "✅ All $rule_count enforced invariants pass."
    exit 0
else
    echo "💥 $fail_count of $rule_count enforced invariants failed."
    echo "   See CLAUDE.md → Enforced Invariants for the rationale behind each rule."
    exit 1
fi
