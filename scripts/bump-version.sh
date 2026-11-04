#!/usr/bin/env bash
# Bump the version number in all three places that must stay in sync:
#   1. ha-addon/VERSION           (read by server at runtime)
#   2. ha-addon/config.yaml       (required by HA add-on manifest)
#   3. ha-addon/client/client.py  (CLIENT_VERSION constant)
#   4. deploy/kubernetes{,/server}/Chart.yaml appVersion (stable versions only —
#      the charts default image.tag to appVersion, and -dev tags aren't for users)
#
# Usage: bash scripts/bump-version.sh X.Y.Z
set -euo pipefail

VERSION="${1:?Usage: $0 X.Y.Z}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "$VERSION" > "$REPO/ha-addon/VERSION"

# config.yaml — version: "X.Y.Z"
sed -i.bak "s/^version: .*/version: \"$VERSION\"/" "$REPO/ha-addon/config.yaml"
rm -f "$REPO/ha-addon/config.yaml.bak"

# client.py — CLIENT_VERSION = "X.Y.Z"
sed -i.bak "s/^CLIENT_VERSION = .*/CLIENT_VERSION = \"$VERSION\"/" "$REPO/ha-addon/client/client.py"
rm -f "$REPO/ha-addon/client/client.py.bak"

if [[ "$VERSION" != *-* ]]; then
  for chart in "$REPO/deploy/kubernetes/Chart.yaml" "$REPO/deploy/kubernetes/server/Chart.yaml"; do
    sed -i.bak "s/^appVersion: .*/appVersion: \"$VERSION\"/" "$chart"
    rm -f "$chart.bak"
  done
fi

echo "Bumped to $VERSION in:"
echo "  ha-addon/VERSION"
echo "  ha-addon/config.yaml"
echo "  ha-addon/client/client.py"
if [[ "$VERSION" != *-* ]]; then echo "  deploy/kubernetes/{,server/}Chart.yaml (appVersion)"; fi
