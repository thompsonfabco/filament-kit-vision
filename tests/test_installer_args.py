from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install" / "spoolwarden-vision.sh"


def _run_installer_args(args: list[str]) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        wsl = shutil.which("wsl")
        if not wsl:
            pytest.skip("wsl not available")
        drive = INSTALLER.drive.rstrip(":").lower()
        wsl_path = f"/mnt/{drive}{INSTALLER.as_posix()[2:]}"
        return subprocess.run(
            [wsl, "-d", "Ubuntu", "bash", wsl_path, *args, "--check-args"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    return subprocess.run(
        ["bash", str(INSTALLER), *args, "--check-args"],
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_installer_accepts_exact_hub_argument_lists():
    cases = [
        [
            "--tier", "cpu",
            "--port", "7915",
            "--host", "127.0.0.1",
            "--user", "spoolwarden-vision",
            "--prefix", "/opt/spoolwarden-vision",
            "--assets", "/tmp/assets",
        ],
        [
            "--tier", "auto",
            "--port", "7915",
            "--bind", "127.0.0.1",
            "--user", "spoolwarden-vision",
            "--prefix", "/opt/spoolwarden-vision",
            "--assets", "/tmp/assets",
        ],
        [
            "--uninstall",
            "--prefix", "/opt/spoolwarden-vision",
        ],
        [
            "--uninstall",
            "--purge",
            "--prefix", "/opt/spoolwarden-vision",
        ],
    ]
    for argv in cases:
        res = _run_installer_args(argv)
        assert res.returncode == 0, f"failed for {argv}: {res.stderr}"
