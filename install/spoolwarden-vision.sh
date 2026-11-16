#!/usr/bin/env bash
set -euo pipefail

# spoolwarden-vision verified Linux installer & uninstaller
# Usage:
#   curl -sSL https://github.com/thompsonfabco/spoolwarden-vision/releases/latest/download/spoolwarden-vision.sh | bash -s -- --tier auto
#   bash spoolwarden-vision.sh --tier cpu --prefix /opt/spoolwarden-vision --no-systemd
#   bash spoolwarden-vision.sh --uninstall [--purge]

REPO="thompsonfabco/spoolwarden-vision"
VERSION="latest"
TIER="auto"
PREFIX="/opt/spoolwarden-vision"
HOST="127.0.0.1"
PORT="7915"
INSTALL_USER="spoolwarden"
USER_SPECIFIED=0
NO_SYSTEMD=0
UNINSTALL=0
PURGE=0
FEEDBACK_OPT_IN="false"
FEEDBACK_ENDPOINT=""
LOCAL_BUNDLE_DIR=""
PUBKEY_HEX="69d905c146629fbb1d8592757ddafdb0e9fc06d14816e8851eb9b67793b44cf5"

CREATED_PREFIX=0
CREATED_UNIT=0
CREATED_USER=0
CREATED_GROUP=0
SERVICE_NAME="spoolwarden-vision"
UNIT_FILE=""
SYSTEMD_USER_FLAG=""

log() { printf '[spoolwarden-vision] %s\n' "$*"; }
warn() { printf '[spoolwarden-vision] WARNING: %s\n' "$*" >&2; }
die() { printf '[spoolwarden-vision] ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tier) TIER="${2:?}"; shift 2 ;;
    --version) VERSION="${2:?}"; shift 2 ;;
    --prefix) PREFIX="${2:?}"; shift 2 ;;
    --host) HOST="${2:?}"; shift 2 ;;
    --port) PORT="${2:?}"; shift 2 ;;
    --user) INSTALL_USER="${2:?}"; USER_SPECIFIED=1; shift 2 ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    --feedback-opt-in) FEEDBACK_OPT_IN="${2:?}"; shift 2 ;;
    --feedback-endpoint) FEEDBACK_ENDPOINT="${2:?}"; shift 2 ;;
    --local-bundle-dir) LOCAL_BUNDLE_DIR="${2:?}"; shift 2 ;;
    -h|--help)
      sed -n '3,10p' "$0"
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

case "$TIER" in
  auto|cpu|gpu) ;;
  *) die "Invalid --tier '$TIER' (must be auto, cpu, or gpu)" ;;
esac

if [[ $EUID -eq 0 ]]; then
  STATE_DIR="/var/lib/spoolwarden-vision"
  LEGACY_STATE_DIR="/var/lib/filament-kit-vision"
  LEGACY_PREFIX="/opt/filament-kit-vision"
  UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
  LEGACY_UNIT_FILE="/etc/systemd/system/filament-kit-vision.service"
  SYSTEMD_USER_FLAG=""
  INSTALL_GROUP="$INSTALL_USER"
else
  if [[ "$USER_SPECIFIED" -eq 0 ]]; then
    INSTALL_USER="$(id -un)"
  fi
  INSTALL_GROUP="$(id -gn "$INSTALL_USER" 2>/dev/null || echo "$INSTALL_USER")"
  STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/spoolwarden-vision"
  LEGACY_STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/filament-kit-vision"
  LEGACY_PREFIX="${HOME}/.local/share/filament-kit-vision"
  UNIT_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
  LEGACY_UNIT_FILE="${HOME}/.config/systemd/user/filament-kit-vision.service"
  SYSTEMD_USER_FLAG="--user"
  if [[ "$PREFIX" == "/opt/spoolwarden-vision" ]]; then
    PREFIX="${HOME}/.local/share/spoolwarden-vision"
  fi
fi

MARKER_FILE="${STATE_DIR}/.installer_created_identity"

count_waiting_feedback() {
  local qxml="${STATE_DIR}/feedback_queue/pending_feedback.xml"
  if [[ -f "$qxml" ]]; then
    grep -c '<entry ' "$qxml" 2>/dev/null || echo "0"
  else
    echo "0"
  fi
}

cleanup_created_identity() {
  local rm_user="${1:-0}"
  local rm_group="${2:-0}"
  if [[ $EUID -eq 0 ]]; then
    if [[ "$rm_user" -eq 1 ]] && id -u "$INSTALL_USER" >/dev/null 2>&1; then
      userdel "$INSTALL_USER" 2>/dev/null || true
    fi
    if [[ "$rm_group" -eq 1 ]] && getent group "$INSTALL_GROUP" >/dev/null 2>&1; then
      groupdel "$INSTALL_GROUP" 2>/dev/null || true
    fi
  fi
}

if [[ "$UNINSTALL" -eq 1 ]]; then
  log "Uninstalling spoolwarden-vision from ${PREFIX}..."
  if command -v systemctl >/dev/null 2>&1; then
    systemctl $SYSTEMD_USER_FLAG stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl $SYSTEMD_USER_FLAG disable "$SERVICE_NAME" 2>/dev/null || true
    systemctl $SYSTEMD_USER_FLAG stop "filament-kit-vision" 2>/dev/null || true
    systemctl $SYSTEMD_USER_FLAG disable "filament-kit-vision" 2>/dev/null || true
    rm -f "$UNIT_FILE" "$LEGACY_UNIT_FILE"
    systemctl $SYSTEMD_USER_FLAG daemon-reload 2>/dev/null || true
  fi
  rm -rf "$PREFIX"
  if [[ -n "$LEGACY_PREFIX" && "$LEGACY_PREFIX" != "$PREFIX" && -d "$LEGACY_PREFIX" ]]; then
    rm -rf "$LEGACY_PREFIX"
  fi

  RM_U=0
  RM_G=0
  if [[ -f "$MARKER_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$MARKER_FILE"
    RM_U="${INSTALLER_CREATED_USER:-0}"
    RM_G="${INSTALLER_CREATED_GROUP:-0}"
    rm -f "$MARKER_FILE"
  fi

  WAITING_COUNT="$(count_waiting_feedback)"
  if [[ "$PURGE" -eq 1 ]]; then
    rm -rf "$STATE_DIR"
    if [[ -n "$LEGACY_STATE_DIR" && "$LEGACY_STATE_DIR" != "$STATE_DIR" && -d "$LEGACY_STATE_DIR" ]]; then
      rm -rf "$LEGACY_STATE_DIR"
    fi
    cleanup_created_identity "$RM_U" "$RM_G"
    log "Uninstalled and purged state directory (${STATE_DIR})."
  else
    cleanup_created_identity "$RM_U" "$RM_G"
    log "Uninstalled application files. Preserved external state in ${STATE_DIR} (waiting feedback corrections: ${WAITING_COUNT})."
    log "Pass --purge on --uninstall to delete config, keys, token, and queued corrections."
  fi
  exit 0
fi

TMP_WORK="$(mktemp -d)"
cleanup_on_failure() {
  local rc=$?
  rm -rf "$TMP_WORK"
  if [[ $rc -ne 0 ]]; then
    warn "Installation failed (exit ${rc}); rolling back partial changes..."
    if [[ "$CREATED_UNIT" -eq 1 ]] && command -v systemctl >/dev/null 2>&1; then
      systemctl $SYSTEMD_USER_FLAG stop "$SERVICE_NAME" 2>/dev/null || true
      systemctl $SYSTEMD_USER_FLAG disable "$SERVICE_NAME" 2>/dev/null || true
      rm -f "$UNIT_FILE"
      systemctl $SYSTEMD_USER_FLAG daemon-reload 2>/dev/null || true
    fi
    if [[ "$CREATED_PREFIX" -eq 1 ]]; then
      rm -rf "$PREFIX"
    fi
    cleanup_created_identity "$CREATED_USER" "$CREATED_GROUP"
  fi
}
trap cleanup_on_failure EXIT

if [[ $EUID -eq 0 ]]; then
  if ! getent group "$INSTALL_GROUP" >/dev/null 2>&1; then
    groupadd --system "$INSTALL_GROUP"
    CREATED_GROUP=1
    log "Created system group '${INSTALL_GROUP}'."
  fi
  if ! id -u "$INSTALL_USER" >/dev/null 2>&1; then
    NOLOGIN_BIN="/usr/sbin/nologin"
    [[ -x "$NOLOGIN_BIN" ]] || NOLOGIN_BIN="/sbin/nologin"
    [[ -x "$NOLOGIN_BIN" ]] || NOLOGIN_BIN="/bin/false"
    useradd --system --no-create-home --shell "$NOLOGIN_BIN" --gid "$INSTALL_GROUP" "$INSTALL_USER"
    CREATED_USER=1
    log "Created system user '${INSTALL_USER}' (no home, ${NOLOGIN_BIN})."
  fi
fi

# Resolve release base URL
if [[ -n "$LOCAL_BUNDLE_DIR" ]]; then
  BUNDLE_SRC="$LOCAL_BUNDLE_DIR"
  log "Using local release bundle directory: ${BUNDLE_SRC}"
else
  if [[ "$VERSION" == "latest" ]]; then
    BASE_URL="https://github.com/${REPO}/releases/latest/download"
  else
    BASE_URL="https://github.com/${REPO}/releases/download/${VERSION}"
  fi
  BUNDLE_SRC="$TMP_WORK"
  log "Fetching release manifest and signatures from ${BASE_URL}..."
  curl -fsSL "${BASE_URL}/SHA256SUMS" -o "${TMP_WORK}/SHA256SUMS"
  curl -fsSL "${BASE_URL}/SHA256SUMS.sig" -o "${TMP_WORK}/SHA256SUMS.sig"
  curl -fsSL "${BASE_URL}/manifest.json" -o "${TMP_WORK}/manifest.json"
fi

# Verify Ed25519 signature over SHA256SUMS when public key is embedded
if [[ "$PUBKEY_HEX" != "REPLACE_WITH_RELEASE_ED25519_PUBLIC_KEY_HEX" ]]; then
  python3 - "$PUBKEY_HEX" "${BUNDLE_SRC}/SHA256SUMS" "${BUNDLE_SRC}/SHA256SUMS.sig" <<'PYEOF'
import base64, sys, pathlib
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
pub_hex, sums_p, sig_p = sys.argv[1], sys.argv[2], sys.argv[3]
pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
pub.verify(base64.b64decode(pathlib.Path(sig_p).read_bytes().strip()), pathlib.Path(sums_p).read_bytes())
print("[spoolwarden-vision] Ed25519 signature verified on SHA256SUMS.")
PYEOF
else
  warn "Embedded release public key placeholder detected; skipping Ed25519 check."
fi

# Determine assets for requested tier from manifest.json
ASSETS=$(python3 - "${BUNDLE_SRC}/manifest.json" "$TIER" <<'PYEOF'
import json, sys
m = json.loads(open(sys.argv[1], encoding="utf-8").read())
for a in m["tiers"][sys.argv[2]]["assets"]:
    print(a)
PYEOF
)

if [[ -z "$LOCAL_BUNDLE_DIR" ]]; then
  for asset in $ASSETS; do
    log "Downloading ${asset}..."
    curl -fsSL "${BASE_URL}/${asset}" -o "${BUNDLE_SRC}/${asset}"
  done
fi

# Verify SHA-256 of every downloaded asset
(cd "$BUNDLE_SRC" && for asset in $ASSETS manifest.json; do
  grep "  ${asset}\$" SHA256SUMS | sha256sum -c -
done)

# Create/upgrade prefix directory (preserve external STATE_DIR across upgrades)
if [[ ! -d "$PREFIX" ]]; then
  CREATED_PREFIX=1
fi
mkdir -p "$PREFIX"

PKG_TAR=$(echo "$ASSETS" | tr ' ' '\n' | grep '^spoolwarden-vision-.*\.tar\.gz$' | head -n1)
[[ -n "$PKG_TAR" ]] || die "Package tarball not found in manifest assets"
tar -xzf "${BUNDLE_SRC}/${PKG_TAR}" -C "$PREFIX"

# Place CPU ONNX/parser assets
mkdir -p "${PREFIX}/models/cpu"
for f in rf_detr_n.onnx visual_classifier.onnx text_parser.npz; do
  if [[ -f "${BUNDLE_SRC}/${f}" ]]; then
    cp -f "${BUNDLE_SRC}/${f}" "${PREFIX}/models/cpu/${f}"
  fi
done

# Place GPU LoRA assets if downloaded
if [[ -f "${BUNDLE_SRC}/qwen25_vl_7b_filament_lora.tar.gz" ]]; then
  mkdir -p "${PREFIX}/models/gpu"
  tar -xzf "${BUNDLE_SRC}/qwen25_vl_7b_filament_lora.tar.gz" -C "${PREFIX}/models/gpu"
fi
if [[ -f "${BUNDLE_SRC}/base-model.json" ]]; then
  mkdir -p "${PREFIX}/models/gpu"
  cp -f "${BUNDLE_SRC}/base-model.json" "${PREFIX}/models/gpu/base-model.json"
fi

# Create Python venv and install package
VENV_DIR="${PREFIX}/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
"${VENV_DIR}/bin/pip" install --upgrade pip wheel >/dev/null
if [[ "$TIER" == "gpu" ]] || { [[ "$TIER" == "auto" ]] && command -v nvidia-smi >/dev/null 2>&1; }; then
  "${VENV_DIR}/bin/pip" install -e "${PREFIX}[gpu]"
else
  "${VENV_DIR}/bin/pip" install -e "${PREFIX}"
fi

# Set up external state directory and migrate legacy configs/feedback if upgrading
mkdir -p "${STATE_DIR}/feedback_queue"
if [[ -n "$LEGACY_STATE_DIR" && "$LEGACY_STATE_DIR" != "$STATE_DIR" && -d "$LEGACY_STATE_DIR" ]]; then
  for item in service_config.xml install_ed25519.pem install_ed25519.pub .installer_created_identity; do
    if [[ ! -f "${STATE_DIR}/${item}" ]] && [[ -f "${LEGACY_STATE_DIR}/${item}" ]]; then
      cp -a "${LEGACY_STATE_DIR}/${item}" "${STATE_DIR}/${item}"
    fi
  done
  if [[ -d "${LEGACY_STATE_DIR}/feedback_queue" ]] && [[ -z "$(ls -A "${STATE_DIR}/feedback_queue" 2>/dev/null)" ]]; then
    cp -a "${LEGACY_STATE_DIR}/feedback_queue/." "${STATE_DIR}/feedback_queue/" 2>/dev/null || true
  fi
  log "Migrated legacy filament-kit-vision state from ${LEGACY_STATE_DIR} into ${STATE_DIR}."
fi
if [[ -n "$LEGACY_PREFIX" && "$LEGACY_PREFIX" != "$PREFIX" && -d "$LEGACY_PREFIX" ]]; then
  if [[ ! -f "${STATE_DIR}/service_config.xml" ]] && [[ -f "${LEGACY_PREFIX}/configs/service_config.xml" ]]; then
    cp -f "${LEGACY_PREFIX}/configs/service_config.xml" "${STATE_DIR}/service_config.xml"
  fi
  if [[ -d "${LEGACY_PREFIX}/data/feedback_queue" ]] && [[ -z "$(ls -A "${STATE_DIR}/feedback_queue" 2>/dev/null)" ]]; then
    cp -a "${LEGACY_PREFIX}/data/feedback_queue/." "${STATE_DIR}/feedback_queue/" 2>/dev/null || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl $SYSTEMD_USER_FLAG stop "filament-kit-vision" 2>/dev/null || true
    systemctl $SYSTEMD_USER_FLAG disable "filament-kit-vision" 2>/dev/null || true
    rm -f "$LEGACY_UNIT_FILE"
  fi
fi
if [[ ! -f "${STATE_DIR}/service_config.xml" ]] && [[ -f "${PREFIX}/configs/service_config.xml" ]]; then
  cp -f "${PREFIX}/configs/service_config.xml" "${STATE_DIR}/service_config.xml"
  log "Migrated legacy service_config.xml into external state dir ${STATE_DIR}."
fi
if [[ -d "${PREFIX}/data/feedback_queue" ]] && [[ -z "$(ls -A "${STATE_DIR}/feedback_queue" 2>/dev/null)" ]]; then
  cp -a "${PREFIX}/data/feedback_queue/." "${STATE_DIR}/feedback_queue/" 2>/dev/null || true
  log "Migrated legacy feedback_queue into external state dir ${STATE_DIR}."
fi

export SPOOLWARDEN_VISION_STATE_DIR="${STATE_DIR}"
CFG_FILE="${STATE_DIR}/service_config.xml"
if [[ ! -f "$CFG_FILE" ]]; then
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  python3 - "$CFG_FILE" "$TIER" "$HOST" "$PORT" "$TOKEN" "$FEEDBACK_OPT_IN" "$FEEDBACK_ENDPOINT" <<'PYEOF'
import sys, xml.etree.ElementTree as ET
cfg_path, tier, host, port, token, fb_opt, fb_ep = sys.argv[1:]
attrs = {
    "version": "1",
    "tier": tier,
    "host": host,
    "port": port,
    "idle-unload-seconds": "300",
    "token": token,
    "feedback-opt-in": "true" if fb_opt == "true" else "false",
}
if fb_ep:
    attrs["feedback-endpoint"] = fb_ep
root = ET.Element("service-config", attrib=attrs)
ET.ElementTree(root).write(cfg_path, encoding="utf-8", xml_declaration=True)
PYEOF
  chmod 600 "$CFG_FILE"
else
  log "Preserving existing config, token, keys, and feedback queue in ${STATE_DIR}."
  "${VENV_DIR}/bin/spoolwarden-vision" config --tier "$TIER" --host "$HOST" --port "$PORT" >/dev/null
  if [[ "$FEEDBACK_OPT_IN" == "true" ]]; then
    "${VENV_DIR}/bin/spoolwarden-vision" config --feedback-opt-in true >/dev/null
  fi
  if [[ -n "$FEEDBACK_ENDPOINT" ]]; then
    "${VENV_DIR}/bin/spoolwarden-vision" config --feedback-endpoint "$FEEDBACK_ENDPOINT" >/dev/null
  fi
fi

if [[ "$CREATED_USER" -eq 1 || "$CREATED_GROUP" -eq 1 ]]; then
  cat > "$MARKER_FILE" <<EOF
INSTALLER_CREATED_USER=${CREATED_USER}
INSTALLER_CREATED_GROUP=${CREATED_GROUP}
EOF
  chmod 600 "$MARKER_FILE"
fi

if [[ $EUID -eq 0 ]]; then
  chown -R "${INSTALL_USER}:${INSTALL_GROUP}" "${STATE_DIR}"
fi

# Install and start systemd unit unless --no-systemd
if [[ "$NO_SYSTEMD" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$(dirname "$UNIT_FILE")"
  if [[ $EUID -eq 0 ]]; then
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=spoolwarden-vision Local Filament Label Recognition Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${INSTALL_USER}
Group=${INSTALL_GROUP}
WorkingDirectory=${PREFIX}
Environment=PYTHONUNBUFFERED=1
Environment=SPOOLWARDEN_VISION_STATE_DIR=${STATE_DIR}
ExecStart=${VENV_DIR}/bin/spoolwarden-vision serve --host ${HOST} --port ${PORT} --tier ${TIER}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${STATE_DIR}

[Install]
WantedBy=multi-user.target
EOF
  else
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=spoolwarden-vision Local Filament Label Recognition Daemon
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${PREFIX}
Environment=PYTHONUNBUFFERED=1
Environment=SPOOLWARDEN_VISION_STATE_DIR=${STATE_DIR}
ExecStart=${VENV_DIR}/bin/spoolwarden-vision serve --host ${HOST} --port ${PORT} --tier ${TIER}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  fi
  CREATED_UNIT=1

  systemctl $SYSTEMD_USER_FLAG daemon-reload
  systemctl $SYSTEMD_USER_FLAG enable --now "$SERVICE_NAME"

  if [[ "$TIER" == "gpu" ]]; then
    MAX_WAIT=240
  else
    MAX_WAIT=90
  fi
  log "Waiting for daemon /v1/health at http://${HOST}:${PORT}/v1/health (timeout ${MAX_WAIT}s)..."
  WAITED=0
  HEALTHY=0
  while [[ $WAITED -lt $MAX_WAIT ]]; do
    if systemctl $SYSTEMD_USER_FLAG is-failed --quiet "$SERVICE_NAME"; then
      warn "Systemd unit '${SERVICE_NAME}' entered failed state."
      journalctl $SYSTEMD_USER_FLAG -u "$SERVICE_NAME" -n 60 --no-pager >&2 || true
      die "Service failed during startup."
    fi
    if curl -fsS "http://${HOST}:${PORT}/v1/health" >/dev/null 2>&1; then
      HEALTHY=1
      break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
    if (( WAITED % 10 == 0 )); then
      log "Still waiting for daemon health... (${WAITED}s/${MAX_WAIT}s)"
    fi
  done

  if [[ "$HEALTHY" -ne 1 ]]; then
    journalctl $SYSTEMD_USER_FLAG -u "$SERVICE_NAME" -n 60 --no-pager >&2 || true
    die "Service did not respond to /v1/health within ${MAX_WAIT}s."
  fi
  log "Daemon is healthy at http://${HOST}:${PORT}/v1/health"
fi

log "Installation complete!"
log "  Prefix: ${PREFIX}"
log "  State:  ${STATE_DIR} (preserved across upgrades)"
log "  Config: ${CFG_FILE}"
