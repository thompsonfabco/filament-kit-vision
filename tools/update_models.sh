#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "=== spoolwarden-vision: One-Click Model Training Update ==="
.venv/bin/spoolwarden-vision update-models --min-new-samples 10 "$@"
