#!/usr/bin/env python3
"""Build a verified release folder (e.g. release/v0.1.0/) for spoolwarden-vision.

Produces:
  - spoolwarden-vision-<version>.tar.gz (plain files/dirs only, no symlinks/hardlinks)
  - CPU model assets (detector_rfdetr_n.onnx, visual_multihead.onnx, text_parser.npz)
  - GPU LoRA adapter tarball (qwen25_vl_7b_filament_lora.tar.gz)
  - manifest.json
  - SHA256SUMS and SHA256SUMS.sig (signed via tools/sign_release.py)
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

from sign_release import ROOT, generate_sha256sums, sign

VERSION = "0.2.2"
API_VERSION = "1.1"


def _add_plain_file(tf: tarfile.TarFile, src: Path, arcname: str) -> None:
    if src.is_symlink():
        raise RuntimeError(f"Refusing symlink in release tarball: {src}")
    ti = tarfile.TarInfo(name=arcname)
    data = src.read_bytes()
    ti.size = len(data)
    ti.mode = 0o755 if src.suffix == ".sh" else 0o644
    ti.uname = "root"
    ti.gname = "root"
    import io
    tf.addfile(ti, io.BytesIO(data))


def build_release(version: str = VERSION, key_file: Path | None = None) -> Path:
    out_dir = ROOT / "release" / f"v{version}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Source tarball from git tracked files (plain files only, top dir spoolwarden-vision-<version>/)
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    code_tar = out_dir / f"spoolwarden-vision-{version}.tar.gz"
    top_prefix = f"spoolwarden-vision-{version}"
    with tarfile.open(code_tar, "w:gz", format=tarfile.USTAR_FORMAT) as tf:
        for rel in sorted(tracked):
            src = ROOT / rel
            if src.is_file():
                _add_plain_file(tf, src, f"{top_prefix}/{rel.replace(chr(92), '/')}")

    # 2. CPU model assets
    cpu_assets = [
        "detector_rfdetr_n.onnx",
        "visual_multihead.onnx",
        "text_parser.npz",
    ]
    for name in cpu_assets:
        src = ROOT / "models" / "cpu" / name
        if not src.is_file():
            raise FileNotFoundError(f"Missing CPU model asset: {src}")
        shutil.copy2(src, out_dir / name)

    # 3. GPU LoRA adapter tarball + pinned base-model.json manifest
    gpu_src_dir = ROOT / "models" / "gpu" / "qwen25_vl_7b_filament_lora"
    gpu_tar_name = "qwen25_vl_7b_filament_lora.tar.gz"
    gpu_tar = out_dir / gpu_tar_name
    gpu_include = {
        "adapter_config.json",
        "adapter_model.safetensors",
        "preprocessor_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "chat_template.jinja",
        "training_meta.xml",
    }
    with tarfile.open(gpu_tar, "w:gz", format=tarfile.USTAR_FORMAT) as tf:
        for p in sorted(gpu_src_dir.iterdir()):
            if p.is_file() and p.name in gpu_include:
                _add_plain_file(tf, p, f"gpu/qwen25_vl_7b_filament_lora/{p.name}")

    base_model_src = ROOT / "configs" / "base-model.json"
    if not base_model_src.is_file():
        raise FileNotFoundError(f"Missing pinned base model manifest: {base_model_src}")
    shutil.copy2(base_model_src, out_dir / "base-model.json")

    # 4. Compute exact release asset download sizes (1 MB = 1,000,000 bytes)
    def _mb(names: list[str]) -> float:
        total = sum((out_dir / n).stat().st_size for n in names)
        return round(total / 1_000_000, 1)

    cpu_all = [code_tar.name] + cpu_assets
    gpu_all = [code_tar.name, gpu_tar_name, "base-model.json"]
    auto_all = [code_tar.name] + cpu_assets + [gpu_tar_name, "base-model.json"]

    manifest = {
        "version": version,
        "api_version": API_VERSION,
        "tiers": {
            "cpu": {
                "assets": cpu_all,
                "download_mb": _mb(cpu_all),
                "disk_mb": 650,
                "minutes": 2,
                "fetched_at_install": "Python dependencies via pip (~120 MB: onnxruntime, rapidocr-onnxruntime, scikit-learn, opencv-python-headless)",
            },
            "gpu": {
                "assets": gpu_all,
                "download_mb": _mb(gpu_all),
                "disk_mb": 19500,
                "minutes": 8,
                "fetched_at_install": "Pinned Qwen/Qwen2.5-VL-7B-Instruct (~16.6 GB full safetensors cached & SHA-256 verified via base-model.json; ~5.5 GB VRAM 4-bit) + Python CUDA/transformers wheels (~2.5 GB)",
            },
            "auto": {
                "assets": auto_all,
                "download_mb": _mb(auto_all),
                "disk_mb": 19800,
                "minutes": 8,
                "fetched_at_install": "Python dependencies (~120 MB CPU or ~2.5 GB CUDA) + pinned Qwen/Qwen2.5-VL-7B-Instruct (~16.6 GB verified via base-model.json when CUDA GPU is present)",
            },
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # 5. SHA256SUMS + SHA256SUMS.sig
    sums_path = generate_sha256sums(out_dir)
    if key_file:
        sign(sums_path, key_file.read_bytes())

    return out_dir


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--key-file", type=Path, required=True)
    args = ap.parse_args()
    out = build_release(version=args.version, key_file=args.key_file)
    print(f"Release built and signed at: {out}")
