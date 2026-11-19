#!/usr/bin/env bash
# Refresh Fleet for ESPHome client containers on a hardcoded list of remote hosts.
# Mirrors the docker-run template from ConnectWorkerModal.tsx (bash branch).
# Continues past unreachable / non-Docker hosts.

set -uo pipefail

# PR #80 review: read server URL + token from env so no credentials
# live in the repo. Fail fast with a clear message if either's missing.
: "${SERVER_URL:?set SERVER_URL (e.g. http://hass-4.local:8765)}"
: "${SERVER_TOKEN:?set SERVER_TOKEN (fleet server bearer from /data/settings.json on the add-on host)}"
# IMAGE_TAG defaults to `latest` (stable) so end users copying this
# out of the repo get the stable image. For dev-loop refreshes, invoke
# with IMAGE_TAG=develop so workers match the develop-branch server
# instead of lagging on the previous stable.
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE="ghcr.io/weirded/esphome-dist-client:${IMAGE_TAG}"
CONTAINER="esphome-dist-client"

SSH_OPTS=(-o ConnectTimeout=5 -o BatchMode=yes -o LogLevel=ERROR -o StrictHostKeyChecking=accept-new)

ok=(); fail=(); skipped=()

refresh() {
    local ssh_host="$1" hostname="$2" host_platform="$3"
    echo
    echo "=== $ssh_host ==="

    # Double-quoted heredoc — $(printf '%q' …) is evaluated locally and
    # emits shell-escaped values into the script the remote bash runs.
    # Avoids SSH → remote-login-shell re-parsing issues (zsh on the Macs
    # was misreading parens in "macOS 26 (M1 Mac Mini)" as glob qualifiers).
    ssh "${SSH_OPTS[@]}" "$ssh_host" bash -s <<REMOTE
        set -uo pipefail
        CONTAINER=$(printf '%q' "$CONTAINER")
        IMAGE=$(printf '%q' "$IMAGE")
        SERVER_URL=$(printf '%q' "$SERVER_URL")
        SERVER_TOKEN=$(printf '%q' "$SERVER_TOKEN")
        HOSTNAME_VAL=$(printf '%q' "$hostname")
        HOST_PLATFORM=$(printf '%q' "$host_platform")

        # Non-interactive ssh gets a minimal PATH. Docker Desktop on macOS
        # installs at /usr/local/bin/docker (Intel) or /opt/homebrew/bin/docker
        # (Apple Silicon) — both missing from the default PATH, which would
        # otherwise make \`command -v docker\` wrongly report docker absent.
        export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"

        if ! command -v docker >/dev/null 2>&1; then
            echo "  skipping — docker not installed"
            exit 20
        fi

        echo "  pulling \$IMAGE..."
        docker pull "\$IMAGE" || echo "  (pull failed; using local)"

        docker rm -f "\$CONTAINER" >/dev/null 2>&1 || true

        docker run -d \\
            --name "\$CONTAINER" \\
            --restart unless-stopped \\
            --network host \\
            --hostname "\$HOSTNAME_VAL" \\
            -e SERVER_URL="\$SERVER_URL" \\
            -e SERVER_TOKEN="\$SERVER_TOKEN" \\
            -e MAX_PARALLEL_JOBS=2 \\
            -e HOST_PLATFORM="\$HOST_PLATFORM" \\
            -v esphome-versions:/esphome-versions \\
            "\$IMAGE" >/dev/null

        echo "  OK"
REMOTE
    case $? in
        0)  ok+=("$ssh_host") ;;
        20) skipped+=("$ssh_host") ;;
        *)  fail+=("$ssh_host") ;;
    esac
}

refresh "macdaddy"          "macdaddy.localdomain"       "macOS 26 (M1 Mac Mini)"
refresh "ai-mac"            "AI-MacBook-Pro.localdomain" "macOS 26 (M3 Pro MacBook Pro)"
refresh "docker-pve-1"      "docker-pve"                 "Proxmox on Debian"
refresh "docker-pve-3"      "docker-optiplex-5"          "Proxmox on Debian"

echo
echo "========== summary =========="
printf "OK      (%d): %s\n" "${#ok[@]}"      "${ok[*]:-none}"
printf "SKIPPED (%d): %s\n" "${#skipped[@]}" "${skipped[*]:-none}"
printf "FAIL    (%d): %s\n" "${#fail[@]}"    "${fail[*]:-none}"
[[ ${#fail[@]} -eq 0 ]]
