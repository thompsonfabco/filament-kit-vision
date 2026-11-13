#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "=== filament-kit-vision: One-Click Model Training Update ==="
.venv/bin/filament-kit-vision update-models --min-new-samples 10 "$@"
