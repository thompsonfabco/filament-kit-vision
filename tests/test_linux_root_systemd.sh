#!/usr/bin/env bash
set -euo pipefail

# Clean-host root + default prefix (/opt/filament-kit-vision) + default user (filament-kit) + systemd test.
# Run as root on a Linux host with systemd (e.g. Debian/Ubuntu VM or systemd-enabled container):
#   sudo bash tests/test_linux_root_systemd.sh /path/to/release-bundle-dir

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: Must be run as root (sudo bash $0 [local-bundle-dir])" >&2
  exit 1
fi

BUNDLE_DIR="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALLER="${REPO_ROOT}/install/filament-kit-vision.sh"

ARGS=(--tier cpu)
if [[ -n "$BUNDLE_DIR" ]]; then
  ARGS+=(--local-bundle-dir "$BUNDLE_DIR")
fi

echo "[test_linux_root_systemd] Installing with defaults (root, /opt/filament-kit-vision, user filament-kit, systemd)..."
bash "$INSTALLER" "${ARGS[@]}"

echo "[test_linux_root_systemd] Verifying filament-kit user owns only /var/lib/filament-kit-vision..."
id filament-kit >/dev/null
OWNER_PREFIX="$(stat -c '%U' /opt/filament-kit-vision)"
OWNER_STATE="$(stat -c '%U' /var/lib/filament-kit-vision)"
[[ "$OWNER_PREFIX" == "root" ]] || { echo "FAIL: /opt/filament-kit-vision owned by $OWNER_PREFIX, expected root" >&2; exit 1; }
[[ "$OWNER_STATE" == "filament-kit" ]] || { echo "FAIL: /var/lib/filament-kit-vision owned by $OWNER_STATE, expected filament-kit" >&2; exit 1; }

echo "[test_linux_root_systemd] Verifying systemd unit and /v1/health..."
systemctl is-active --quiet filament-kit-vision
curl -fsS http://127.0.0.1:7915/v1/health | python3 -c '
import json, sys
h = json.load(sys.stdin)
assert h["api_version"] == "1.1", h
assert h["capabilities"]["feedback"] is True, h
assert h["tiers"]["cpu"]["accelerator"] == "cpu", h
print("Health check OK:", h["service"], h["module_version"], "api", h["api_version"])
'

echo "[test_linux_root_systemd] Verifying upgrade preserves token and state..."
TOKEN_BEFORE="$(grep -o 'token="[^"]*"' /var/lib/filament-kit-vision/service_config.xml)"
bash "$INSTALLER" "${ARGS[@]}"
TOKEN_AFTER="$(grep -o 'token="[^"]*"' /var/lib/filament-kit-vision/service_config.xml)"
[[ "$TOKEN_BEFORE" == "$TOKEN_AFTER" ]] || { echo "FAIL: token changed on upgrade" >&2; exit 1; }

echo "[test_linux_root_systemd] Verifying --uninstall preserves state and --purge removes it..."
bash "$INSTALLER" --uninstall
[[ -d /var/lib/filament-kit-vision ]] || { echo "FAIL: state dir removed on non-purge uninstall" >&2; exit 1; }
! id filament-kit >/dev/null 2>&1 || { echo "FAIL: installer-created user filament-kit still exists" >&2; exit 1; }

bash "$INSTALLER" --uninstall --purge
[[ ! -d /var/lib/filament-kit-vision ]] || { echo "FAIL: state dir survived --purge" >&2; exit 1; }

echo "[test_linux_root_systemd] PASS"
