#!/usr/bin/env bash
# Regenerate the hash-pinned Python dependency lockfiles (E.1).
#
# We keep ``ha-addon/{server,client}/requirements.txt`` as the human-edited
# input — direct dependencies with ``>=`` ranges. ``pip-compile --generate-hashes``
# resolves these to a fully-pinned, hash-locked ``requirements.lock`` that
# the Dockerfiles install with ``--require-hashes``.
#
# Run locally:
#   bash scripts/refresh-deps.sh
#
# Should be run + committed before every release (the RELEASE_CHECKLIST has
# a step for this) and any time direct deps in requirements.txt change.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# CRITICAL: lockfiles must be generated on the same platform the Dockerfiles
# install on (linux/amd64), otherwise platform-conditional transitive deps
# leak in. The 1.3.1-dev.9 deploy failure was caused by generating the lock
# on macOS, which pulled in PyObjC as a transitive — Linux can't install
# that. We pin to python:3.12-slim because that's what both Dockerfiles
# FROM. Re-run via Docker so the result is reproducible regardless of host.

if ! command -v docker >/dev/null 2>&1; then
    echo "docker not found — required to generate lockfiles on the target platform."
    exit 1
fi

echo "▶ Refreshing lockfiles inside python:3.12-slim (linux/amd64)…"

docker run --rm \
    --platform linux/amd64 \
    -v "$REPO_ROOT":/work \
    -w /work \
    python:3.12-slim \
    bash -c '
        set -e
        apt-get update -qq && apt-get install -qq -y --no-install-recommends gcc libffi-dev libssl-dev git >/dev/null
        pip install --quiet pip-tools
        echo "  ▶ ha-addon/server/requirements.lock"
        # NOTE: --upgrade was attempted in 1.4.1-dev.55 to unstick
        # ESPHome at an old version, but it pulled in pyobjc-core
        # (a macOS-only transitive) WITHOUT the sys_platform == "darwin"
        # marker, breaking the linux/amd64 Docker build. Stays off until
        # we solve the platform-marker leak. #51 reopened.
        pip-compile \
            --generate-hashes \
            --resolver=backtracking \
            --strip-extras \
            --quiet \
            --output-file ha-addon/server/requirements.lock \
            ha-addon/server/requirements.txt
        echo "  ▶ ha-addon/client/requirements.lock"
        pip-compile \
            --generate-hashes \
            --resolver=backtracking \
            --strip-extras \
            --quiet \
            --output-file ha-addon/client/requirements.lock \
            ha-addon/client/requirements.txt
    '

echo ""
echo "✅ Lockfiles regenerated. Review the diff and commit:"
echo "   git diff ha-addon/server/requirements.lock ha-addon/client/requirements.lock"
