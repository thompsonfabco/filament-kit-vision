# filament-kit-vision

Local dual-tier (CPU ONNX Hybrid + GPU VLM) 3D printer filament spool and box label recognition service for **filament-kit** and **Spoolman**.

Licensed under **GNU General Public License v3.0 (GPL-3.0-or-later)** — see [LICENSE](LICENSE) and [THIRD-PARTY.md](THIRD-PARTY.md).

> **Important:** Recognition results are probabilistic estimates and must be reviewed and verified by the user before committing inventory records. This project is an independent open-source tool and is not affiliated with or endorsed by any filament manufacturer.

## Features

- **CPU Hybrid Tier (`cpu`):** Runs without PyTorch or CUDA using `RF-DETR-N` (Apache-2.0 ONNX) + `RapidOCR` (with circular polar unwarping for spool flange labels) + `MobileNetV3-Large` multi-head classifier (ONNX) + CIE LAB color-space hex estimator + catalog-constrained matching. Automatically uses ONNX Runtime hardware acceleration (`CUDAExecutionProvider`, `OpenVINOExecutionProvider`, `DmlExecutionProvider`, `ROCMExecutionProvider`, `CoreMLExecutionProvider`) when available, falling back to multi-threaded `CPUExecutionProvider`.
- **GPU VLM Tier (`gpu`):** `Qwen/Qwen2.5-VL-7B-Instruct` (Apache-2.0, pinned by revision and SHA-256 in `base-model.json`) quantized in 4-bit NF4 (`bitsandbytes`) with fine-tuned LoRA adapters for 6 GB+ CUDA GPUs, automatically unloading from VRAM after idle timeout.
- **HTTP API Contract v1:** Localhost REST daemon (`127.0.0.1:7915` by default) serving `GET /v1/health`, `POST /v1/read`, and `POST /v1/revise` with Spoolman-aligned item schemas, per-item `confidence`, and `field_confidence` (see [docs/api-contract.md](docs/api-contract.md)).
- **XML-Native Storage:** Catalogs (`data/catalog/filament_catalog.xml`), configs (`configs/*.xml`), and internal representations use UTF-8 XML.

## Training Data & Held-Out Accuracy

Models were trained on a private corpus combining the project owner's own spool/box photos, public product/community photos, and algorithmically generated synthetic label renders. None of the training or evaluation photos are redistributed in this repository or in release assets.

Evaluated on a held-out real-world test split (excluded from all training sets):

| Tier | Samples | Vendor Exact | Base Material | Product Line | Sealed State | Mean Latency |
|---|---|---|---|---|---|---|
| **CPU Hybrid** (`RF-DETR-N` + `MobileNetV3-Large` + `RapidOCR`) | 44 | 84.09% | 95.45% | 93.18% | 79.55% | 6.16 s |
| **GPU VLM** (`Qwen2.5-VL-7B-Instruct` 4-bit NF4 LoRA) | 24 | 91.67% | 91.67% | 95.83% | 83.33% | 42.65 s (incl. cold load) |

## Installation (Ubuntu Linux)

```bash
sudo bash install/filament-kit-vision.sh \
  --tier auto \
  --port 7915 \
  --bind 127.0.0.1 \
  --user filament-kit-vision \
  --prefix /opt/filament-kit-vision
```

Installer flags:
- `--tier <cpu|gpu|auto>`: Inference tier (`auto` detects `nvidia-smi`; `cpu` detects ONNX Runtime accelerators such as OpenVINO or CUDA runtime and falls back to CPU).
- `--port <PORT>`: HTTP listen port (default `7915`).
- `--bind <ADDRESS>`: Bind address (default `127.0.0.1`).
- `--user <USER>`: System user to run the systemd service.
- `--prefix <DIR>`: Install directory (default `/opt/filament-kit-vision`).
- `--assets <DIR>`: Directory of pre-downloaded and verified model assets.
- `--uninstall`: Stop and remove the systemd unit and install directory.

## Local Development & CLI

```bash
python -m venv .venv
pip install -e ".[test]"

# Run unit and daemon tests
pytest -v

# Start HTTP daemon
filament-kit-vision serve --tier auto --host 127.0.0.1 --port 7915

# Check daemon health
filament-kit-vision status

# Predict on a local photo
filament-kit-vision predict path/to/spool.jpg --tier cpu
```

## Release Signing & Model Artifact Security

Release manifests (`SHA256SUMS`) are signed with RFC 8032 Ed25519 (`SHA256SUMS.sig`) via `tools/sign_release.py`.

> **Security Note (`text_parser.joblib`):** The CPU text parser model (`text_parser.joblib`) is stored in `joblib`/pickle format, which executes Python object deserialization on load. It must **only** be loaded from verified release assets after `SHA256SUMS.sig` (Ed25519 signature) and `SHA256SUMS` (SHA-256 digest) have been validated against the pinned public key. Migrating the text parser weights to a non-pickle format (ONNX / JSON) is planned for `v0.2.0`.
