#!/usr/bin/env bash
# filament-kit-vision installer / uninstaller for Ubuntu Linux Server (20.04+, 22.04+, 24.04+)
set -euo pipefail

TIER="auto"
PORT="7915"
BIND="127.0.0.1"
INSTALL_USER="${SUDO_USER:-$(id -un)}"
PREFIX="/opt/filament-kit-vision"
ASSETS_DIR=""
UNINSTALL=false

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --tier <cpu|gpu|auto>   Inference engine tier (default: auto)
  --port <PORT>           HTTP server port (default: 7915)
  --bind <ADDRESS>        HTTP bind address (default: 127.0.0.1)
  --user <USER>           User account to own and run the daemon (default: current user / filament-kit)
  --prefix <DIR>          Installation target directory (default: /opt/filament-kit-vision)
  --assets <DIR>          Directory containing pre-downloaded & verified model assets
  --uninstall             Stop service, disable systemd unit, and remove files
  -h, --help              Show this help message
EOF
    exit 1
}

# Parse command line flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tier)
            TIER="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --bind)
            BIND="$2"
            shift 2
            ;;
        --user)
            INSTALL_USER="$2"
            shift 2
            ;;
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --assets)
            ASSETS_DIR="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

SERVICE_NAME="filament-kit-vision.service"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}"

# Handle --uninstall
if [ "${UNINSTALL}" = true ]; then
    echo "[filament-kit-vision] Uninstalling service and files from ${PREFIX}..."
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
        systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
        rm -f "${UNIT_FILE}"
        systemctl daemon-reload 2>/dev/null || true
    fi
    rm -rf "${PREFIX}"
    echo "[filament-kit-vision] Uninstallation complete."
    exit 0
fi

# Determine disk and download estimates upfront
if [ "${TIER}" = "cpu" ]; then
    EST_DL="~120 MB pip dependencies (+ 164.9 MB release assets)"
    EST_DISK="~650 MB"
elif [ "${TIER}" = "gpu" ]; then
    EST_DL="~19.1 GB (PyTorch CUDA wheels + pinned Qwen2.5-VL-7B-Instruct safetensors + 178.0 MB release assets)"
    EST_DISK="~19.5 GB"
else
    # auto
    if command -v nvidia-smi >/dev/null 2>&1; then
        EST_DL="~19.1 GB (PyTorch CUDA wheels + pinned Qwen2.5-VL-7B-Instruct safetensors + 342.7 MB release assets)"
        EST_DISK="~19.8 GB"
    else
        EST_DL="~120 MB pip dependencies (+ 342.7 MB release assets)"
        EST_DISK="~650 MB"
    fi
fi

echo "[filament-kit-vision] === Installation Plan ==="
echo "  Target Prefix: ${PREFIX}"
echo "  Service User:  ${INSTALL_USER}"
echo "  Selected Tier: ${TIER}"
echo "  Listen:        http://${BIND}:${PORT}"
echo "  Estimated DL:  ${EST_DL}"
echo "  Required Disk: ${EST_DISK}"
echo "============================================="

# Ensure target directories exist
mkdir -p "${PREFIX}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Copy release files to prefix
echo "[filament-kit-vision] Copying files to ${PREFIX}..."
cp -r "${SCRIPT_DIR}/pyproject.toml" "${PREFIX}/"
cp -r "${SCRIPT_DIR}/configs" "${PREFIX}/"
cp -r "${SCRIPT_DIR}/src" "${PREFIX}/"
cp -r "${SCRIPT_DIR}/data" "${PREFIX}/"
if [ -d "${SCRIPT_DIR}/models" ]; then
    cp -r "${SCRIPT_DIR}/models" "${PREFIX}/"
fi
if [ -n "${ASSETS_DIR}" ] && [ -d "${ASSETS_DIR}" ]; then
    echo "[filament-kit-vision] Installing pre-verified model assets from ${ASSETS_DIR}..."
    mkdir -p "${PREFIX}/models/cpu" "${PREFIX}/models/gpu/qwen25_vl_7b_filament_lora"
    for f in "${ASSETS_DIR}"/*; do
        [ -e "$f" ] || continue
        fname="$(basename "$f")"
        case "${fname}" in
            *.onnx|*.joblib)
                cp -f "$f" "${PREFIX}/models/cpu/"
                ;;
            base-model.json)
                cp -f "$f" "${PREFIX}/models/gpu/base-model.json"
                cp -f "$f" "${PREFIX}/configs/base-model.json"
                ;;
            *.tar.gz)
                tar -xzf "$f" -C "${PREFIX}/models/"
                ;;
            *)
                cp -rf "$f" "${PREFIX}/models/"
                ;;
        esac
    done
fi
if [ -f "${SCRIPT_DIR}/README.md" ]; then
    cp -f "${SCRIPT_DIR}/README.md" "${PREFIX}/"
fi
if [ -f "${SCRIPT_DIR}/THIRD-PARTY.md" ]; then
    cp -f "${SCRIPT_DIR}/THIRD-PARTY.md" "${PREFIX}/"
fi

# Ensure Python 3 venv exists
VENV_DIR="${PREFIX}/.venv"
if [ ! -d "${VENV_DIR}" ]; then
    echo "[filament-kit-vision] Creating Python virtual environment in ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
fi

echo "[filament-kit-vision] Installing dependencies..."
"${VENV_DIR}/bin/pip" install --upgrade pip wheel setuptools --quiet
"${VENV_DIR}/bin/pip" install -e "${PREFIX}" --quiet

if [ "${TIER}" = "gpu" ] || ( [ "${TIER}" = "auto" ] && command -v nvidia-smi >/dev/null 2>&1 ); then
    echo "[filament-kit-vision] Installing GPU VLM dependencies..."
    "${VENV_DIR}/bin/pip" install -e "${PREFIX}[gpu]" --quiet
    echo "[filament-kit-vision] Fetching & verifying pinned GPU base model (SHA-256)..."
    "${VENV_DIR}/bin/python" -c "from filament_kit_vision.gpu_tier.pipeline import verify_pinned_base_model; verify_pinned_base_model()"
else
    # Detect hardware acceleration for ONNX Runtime on CPU/NPU/iGPU
    if command -v nvidia-smi >/dev/null 2>&1 && ldconfig -p 2>/dev/null | grep -q "libcudart"; then
        echo "[filament-kit-vision] CUDA runtime detected; enabling onnxruntime-gpu acceleration..."
        "${VENV_DIR}/bin/pip" install onnxruntime-gpu --quiet || true
    elif grep -qi "GenuineIntel" /proc/cpuinfo 2>/dev/null; then
        echo "[filament-kit-vision] Intel CPU detected; enabling onnxruntime-openvino acceleration..."
        "${VENV_DIR}/bin/pip" install onnxruntime-openvino --quiet || true
    fi
fi

# Configure service settings under /etc/filament-kit-vision (preserve existing settings on upgrade)
ETC_DIR="/etc/filament-kit-vision"
mkdir -p "${ETC_DIR}"
if [ ! -f "${PREFIX}/configs/service_config.xml" ]; then
    "${VENV_DIR}/bin/filament-kit-vision" config --tier "${TIER}" --host "${BIND}" --port "${PORT}"
fi
cp -n "${PREFIX}/configs/service_config.xml" "${ETC_DIR}/service_config.xml" 2>/dev/null || true

# Ensure permissions
if id "${INSTALL_USER}" >/dev/null 2>&1; then
    chown -R "${INSTALL_USER}:${INSTALL_USER}" "${PREFIX}" "${ETC_DIR}"
fi

# Install / update systemd service unit
if command -v systemctl >/dev/null 2>&1; then
    echo "[filament-kit-vision] Writing systemd unit ${UNIT_FILE}..."
    cat <<UNIT > "${UNIT_FILE}"
[Unit]
Description=filament-kit-vision local vision intake service
After=network.target

[Service]
Type=simple
User=${INSTALL_USER}
WorkingDirectory=${PREFIX}
ExecStart=${VENV_DIR}/bin/filament-kit-vision serve --tier ${TIER} --host ${BIND} --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
fi

# Verify health endpoint answers before exiting 0
echo "[filament-kit-vision] Verifying /v1/health endpoint on http://${BIND}:${PORT}..."
MAX_WAIT=30
WAITED=0
HEALTH_OK=false

while [ ${WAITED} -lt ${MAX_WAIT} ]; do
    if command -v curl >/dev/null 2>&1; then
        RESP=$(curl -sf "http://${BIND}:${PORT}/v1/health" 2>/dev/null || true)
        if [[ "${RESP}" =~ "\"status\":"[[:space:]]*\"ok\" ]] || [[ "${RESP}" =~ "\"status\":"[[:space:]]*\"degraded\" ]]; then
            HEALTH_OK=true
            break
        fi
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

if [ "${HEALTH_OK}" = true ]; then
    echo "[filament-kit-vision] Installation successful! Service is healthy and responding on http://${BIND}:${PORT}/v1/health."
    exit 0
else
    echo "[filament-kit-vision] Warning: Service started but /v1/health did not answer within ${MAX_WAIT}s." >&2
    exit 1
fi
