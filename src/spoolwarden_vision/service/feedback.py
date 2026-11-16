"""Opt-in user correction feedback queue, EXIF-stripping bundler, Ed25519/PoW signing, and Drive upload."""
from __future__ import annotations

import base64
import datetime
import hashlib
import io
import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as StdET
import zipfile
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as DefusedET
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from PIL import Image, ImageOps

from ..xml_io import (
    DATA_DIR,
    AnnotatedSample,
    FilamentItem,
    get_state_dir,
    parse_xml_file,
    write_xml_atomic,
)

MAX_FEEDBACK_PHOTOS = 8
MAX_FEEDBACK_IMAGE_SIDE = 1024
MAX_FEEDBACK_ZIP_BYTES = 25 * 1024 * 1024  # 25 MB cap for Apps Script doPost
DEFAULT_POW_DIFFICULTY = 20
DEFAULT_FEEDBACK_DIR = get_state_dir() / "feedback_queue"

_ITEM_FIELDS = (
    "vendor",
    "name",
    "material",
    "line",
    "color_name",
    "color_family",
    "color_hex",
    "net",
    "tare",
    "density",
    "diameter",
    "nozzle",
    "bed",
    "nozzle_range",
    "bed_range",
    "sealed",
    "existing_filament_id",
)


def _strip_data_uri(b64_str: str) -> bytes:
    s = b64_str.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    return base64.b64decode(s)


def sanitize_image_b64(b64_str: str, max_side: int = MAX_FEEDBACK_IMAGE_SIDE) -> bytes:
    """Decode base64 image, bake EXIF orientation, resize, and re-encode as metadata-free JPEG."""
    raw = _strip_data_uri(b64_str)
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        # Re-create from raw pixel buffer so zero EXIF/ICC/APP markers survive
        clean = Image.frombytes("RGB", img.size, img.tobytes())
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue()


def _item_to_el(tag: str, data: dict[str, Any]) -> StdET.Element:
    el = StdET.Element(tag)
    for key in _ITEM_FIELDS:
        if key in data and data[key] is not None:
            child = StdET.SubElement(el, key.replace("_", "-"))
            val = data[key]
            if isinstance(val, bool):
                child.text = "true" if val else "false"
            else:
                child.text = str(val)
    return el


def _el_to_item_dict(el: StdET.Element | None) -> dict[str, Any]:
    if el is None:
        return {}
    out: dict[str, Any] = {}
    for child in el:
        key = child.tag.replace("-", "_")
        txt = (child.text or "").strip()
        if key in ("net", "tare", "nozzle", "bed", "existing_filament_id"):
            try:
                out[key] = int(txt)
                continue
            except ValueError:
                pass
        elif key in ("density", "diameter"):
            try:
                out[key] = float(txt)
                continue
            except ValueError:
                pass
        elif key == "sealed":
            out[key] = txt.lower() == "true"
            continue
        out[key] = txt
    return out


def ensure_install_keypair(state_dir: str | Path) -> tuple[str, Path]:
    """Ensure a per-installation Ed25519 keypair exists in state_dir; return (public_key_b64, priv_path)."""
    sdir = Path(state_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    priv_path = sdir / "install_ed25519.pem"
    pub_path = sdir / "install_ed25519.pub"

    if priv_path.is_file():
        priv = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
        if not isinstance(priv, Ed25519PrivateKey):
            raise ValueError("Existing install_ed25519.pem is not an Ed25519 private key")
    else:
        priv = Ed25519PrivateKey.generate()
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        priv_path.write_bytes(pem)
        try:
            os.chmod(priv_path, 0o600)
        except OSError:
            pass

    raw_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = base64.b64encode(raw_pub).decode("ascii")
    if not pub_path.is_file() or pub_path.read_text(encoding="utf-8").strip() != pub_b64:
        pub_path.write_text(pub_b64 + "\n", encoding="utf-8")
    return pub_b64, priv_path


def sign_package_sha256(package_sha256: str, state_dir: str | Path) -> tuple[str, str]:
    """Sign a package SHA-256 hex digest with the installation's Ed25519 key."""
    pub_b64, priv_path = ensure_install_keypair(state_dir)
    priv = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise ValueError("Invalid Ed25519 private key")
    sig = priv.sign(package_sha256.encode("utf-8"))
    return base64.b64encode(sig).decode("ascii"), pub_b64


def verify_package_signature(package_sha256: str, signature_b64: str, public_key_b64: str) -> bool:
    """Verify Ed25519 signature over package SHA-256 hex digest."""
    try:
        raw_pub = base64.b64decode(public_key_b64.strip())
        raw_sig = base64.b64decode(signature_b64.strip())
        pub = Ed25519PublicKey.from_public_bytes(raw_pub)
        pub.verify(raw_sig, package_sha256.encode("utf-8"))
        return True
    except Exception:
        return False


def _has_leading_zero_bits(digest: bytes, bits: int) -> bool:
    if bits <= 0:
        return True
    full_bytes, rem_bits = divmod(bits, 8)
    for b in digest[:full_bytes]:
        if b != 0:
            return False
    if rem_bits:
        mask = 0xFF << (8 - rem_bits) & 0xFF
        if digest[full_bytes] & mask:
            return False
    return True


def verify_pow(
    package_sha256: str,
    public_key_b64: str,
    nonce: int | str,
    difficulty: int = DEFAULT_POW_DIFFICULTY,
) -> bool:
    """Verify SHA-256 proof-of-work over package_sha256:public_key_b64:nonce."""
    if difficulty < 0 or difficulty > 64:
        return False
    msg = f"{package_sha256}:{public_key_b64}:{nonce}".encode("utf-8")
    digest = hashlib.sha256(msg).digest()
    return _has_leading_zero_bits(digest, difficulty)


def compute_pow(
    package_sha256: str,
    public_key_b64: str,
    difficulty: int = DEFAULT_POW_DIFFICULTY,
) -> tuple[int, str]:
    """Compute an integer nonce meeting the leading-zero-bit SHA-256 PoW difficulty."""
    nonce = 0
    prefix = f"{package_sha256}:{public_key_b64}:".encode("utf-8")
    while True:
        digest = hashlib.sha256(prefix + str(nonce).encode("ascii")).digest()
        if _has_leading_zero_bits(digest, difficulty):
            return nonce, digest.hex()
        nonce += 1


def validate_feedback_zip_bytes(zip_bytes: bytes) -> dict[str, Any]:
    """Validate feedback ZIP size, magic header, safe filenames, and XML/JPEG-only shape."""
    if not zip_bytes or len(zip_bytes) > MAX_FEEDBACK_ZIP_BYTES:
        raise ValueError(f"ZIP size {len(zip_bytes)} outside allowed range (1..{MAX_FEEDBACK_ZIP_BYTES})")
    if zip_bytes[:4] != b"PK\x03\x04":
        raise ValueError("Payload is not a valid ZIP archive")

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = zf.namelist()
        if "pending_feedback.xml" not in names:
            raise ValueError("ZIP missing pending_feedback.xml")
        for name in names:
            if name.startswith("/") or "\\" in name or ".." in name or "/" in name:
                raise ValueError(f"Unsafe or nested path in ZIP: {name}")
            if name != "pending_feedback.xml" and not name.lower().endswith((".jpg", ".jpeg")):
                raise ValueError(f"Unexpected non-JPEG file in ZIP: {name}")
        fb_root = DefusedET.fromstring(zf.read("pending_feedback.xml"))
        if fb_root.tag != "feedback-queue":
            raise ValueError(f"Unexpected root XML tag: {fb_root.tag}")
        entry_count = len(fb_root.findall("entry"))
    return {"entry_count": entry_count, "files": names}


def _configured_pow_difficulty(override: int | None = None) -> int:
    if override is not None:
        return int(override)
    env_val = os.environ.get("SPOOLWARDEN_VISION_POW_DIFFICULTY", "").strip()
    if env_val:
        return max(0, int(env_val))
    return DEFAULT_POW_DIFFICULTY


class FeedbackQueue:
    """Manages locally queued user corrections in state_dir/feedback_queue/pending_feedback.xml."""

    def __init__(
        self,
        queue_dir: str | Path = DEFAULT_FEEDBACK_DIR,
        state_dir: str | Path | None = None,
    ) -> None:
        self.queue_dir = Path(queue_dir)
        self.state_dir = Path(state_dir) if state_dir is not None else self.queue_dir.parent
        self.xml_path = self.queue_dir / "pending_feedback.xml"
        self._lock = threading.RLock()
        self._submit_lock = threading.Lock()
        self._submit_state: dict[str, Any] = {
            "state": "idle",
            "phase": "idle",
            "started_at": None,
            "finished_at": None,
            "submitted_count": 0,
            "bundle_bytes": 0,
            "error": "",
        }

    def _load_root(self) -> StdET.Element:
        if self.xml_path.is_file():
            return parse_xml_file(self.xml_path)
        return StdET.Element("feedback-queue", attrib={"version": "1"})

    def _save_root(self, root: StdET.Element) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        write_xml_atomic(self.xml_path, root)

    def _remove_entry_files(self, entry_el: StdET.Element) -> None:
        for im in entry_el.findall("./images/image"):
            fname = (im.text or "").strip()
            if fname:
                (self.queue_dir / fname).unlink(missing_ok=True)

    def enqueue(
        self,
        images_b64: list[str],
        predicted: dict[str, Any],
        corrected: dict[str, Any],
        note: str = "",
        source: str = "local",
        record_id: str = "",
    ) -> dict[str, Any]:
        if not images_b64:
            raise ValueError("At least one image is required in images_b64")
        if len(images_b64) > MAX_FEEDBACK_PHOTOS:
            raise ValueError(f"Too many images ({len(images_b64)} > {MAX_FEEDBACK_PHOTOS})")
        if not isinstance(predicted, dict) or not isinstance(corrected, dict):
            raise ValueError("'predicted' and 'corrected' must be JSON objects")

        clean_source = str(source or "local").strip().lower()
        if clean_source not in ("local", "cloud"):
            raise ValueError("source must be 'local' or 'cloud'")
        clean_record_id = str(record_id or "").strip()

        with self._lock:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            entry_id = f"fb_{uuid.uuid4().hex[:12]}"
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

            saved_files: list[str] = []
            try:
                for idx, b64 in enumerate(images_b64):
                    jpg_bytes = sanitize_image_b64(b64)
                    fname = f"{entry_id}_{idx}.jpg"
                    (self.queue_dir / fname).write_bytes(jpg_bytes)
                    saved_files.append(fname)
            except Exception:
                for fname in saved_files:
                    (self.queue_dir / fname).unlink(missing_ok=True)
                raise

            root = self._load_root()
            if clean_record_id:
                for old_el in list(root.findall("entry")):
                    if old_el.attrib.get("record-id", "") == clean_record_id:
                        self._remove_entry_files(old_el)
                        root.remove(old_el)

            attrs = {
                "id": entry_id,
                "created-at": created_at,
                "source": clean_source,
            }
            if clean_record_id:
                attrs["record-id"] = clean_record_id

            entry_el = StdET.SubElement(root, "entry", attrib=attrs)
            if note.strip():
                note_el = StdET.SubElement(entry_el, "note")
                note_el.text = note.strip()[:500]
            imgs_el = StdET.SubElement(entry_el, "images")
            for fname in saved_files:
                img_el = StdET.SubElement(imgs_el, "image")
                img_el.text = fname
            entry_el.append(_item_to_el("predicted", predicted))
            entry_el.append(_item_to_el("corrected", corrected))
            self._save_root(root)

            st = self.status()
            return {
                "ok": True,
                "id": entry_id,
                "source": clean_source,
                "record_id": clean_record_id,
                "queued_count": st["count"],
                "bundle_mb": st["bundle_mb"],
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            root = self._load_root()
            entries: list[dict[str, Any]] = []
            total_bytes = self.xml_path.stat().st_size if self.xml_path.is_file() else 0

            for entry_el in root.findall("entry"):
                eid = entry_el.attrib.get("id", "")
                imgs = [
                    (im.text or "").strip()
                    for im in entry_el.findall("./images/image")
                    if (im.text or "").strip()
                ]
                for fname in imgs:
                    fpath = self.queue_dir / fname
                    if fpath.is_file():
                        total_bytes += fpath.stat().st_size
                note_el = entry_el.find("note")
                entries.append(
                    {
                        "id": eid,
                        "created_at": entry_el.attrib.get("created-at", ""),
                        "source": entry_el.attrib.get("source", "local"),
                        "record_id": entry_el.attrib.get("record-id", ""),
                        "note": (note_el.text or "").strip() if note_el is not None else "",
                        "images": imgs,
                        "image_url": f"/v1/feedback/{eid}/image" if imgs else "",
                        "predicted": _el_to_item_dict(entry_el.find("predicted")),
                        "corrected": _el_to_item_dict(entry_el.find("corrected")),
                    }
                )

            return {
                "count": len(entries),
                "bundle_mb": round(total_bytes / 1_000_000, 3),
                "entries": entries,
            }

    def get_entry_image(self, entry_id: str, index: int = 0) -> bytes | None:
        with self._lock:
            if not self.xml_path.is_file():
                return None
            root = self._load_root()
            for entry_el in root.findall("entry"):
                if entry_el.attrib.get("id") == entry_id:
                    imgs = [
                        (im.text or "").strip()
                        for im in entry_el.findall("./images/image")
                        if (im.text or "").strip()
                    ]
                    if index < 0 or index >= len(imgs):
                        return None
                    fpath = self.queue_dir / Path(imgs[index]).name
                    if fpath.is_file():
                        return fpath.read_bytes()
                    return None
            return None

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            if not self.xml_path.is_file():
                return False
            root = self._load_root()
            removed = False
            for entry_el in list(root.findall("entry")):
                if entry_el.attrib.get("id") == entry_id:
                    self._remove_entry_files(entry_el)
                    root.remove(entry_el)
                    removed = True
            if removed:
                self._save_root(root)
            return removed

    def clear(self) -> None:
        with self._lock:
            if self.xml_path.is_file():
                root = self._load_root()
                for entry_el in root.findall("entry"):
                    self._remove_entry_files(entry_el)
                self.xml_path.unlink(missing_ok=True)

    def build_bundle_zip(self) -> bytes:
        with self._lock:
            st = self.status()
            if st["count"] == 0:
                raise ValueError("Feedback queue is empty")

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(self.xml_path, arcname="pending_feedback.xml")
                for entry in st["entries"]:
                    for fname in entry["images"]:
                        fpath = self.queue_dir / fname
                        if fpath.is_file():
                            zf.write(fpath, arcname=fname)

            raw = buf.getvalue()
            if len(raw) > MAX_FEEDBACK_ZIP_BYTES:
                raise ValueError(
                    f"Feedback bundle ({len(raw)} bytes) exceeds {MAX_FEEDBACK_ZIP_BYTES} byte limit; remove some entries"
                )
            return raw

    def _validate_endpoint(self, endpoint_url: str) -> str:
        url = endpoint_url.strip()
        if not url or not (
            url.startswith("https://")
            or url.startswith("http://127.0.0.1")
            or url.startswith("http://localhost")
        ):
            raise ValueError("A valid HTTPS feedback upload endpoint must be configured")
        return url

    def _build_signed_payload(
        self,
        pow_difficulty: int | None = None,
        phase_cb: Any = None,
    ) -> tuple[bytes, int, int, str, str]:
        with self._lock:
            st = self.status()
            zip_bytes = self.build_bundle_zip()
        pkg_sha256 = hashlib.sha256(zip_bytes).hexdigest()
        sig_b64, pub_b64 = sign_package_sha256(pkg_sha256, self.state_dir)
        difficulty = _configured_pow_difficulty(pow_difficulty)
        if phase_cb:
            phase_cb("pow")
        nonce, _ = compute_pow(pkg_sha256, pub_b64, difficulty=difficulty)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = json.dumps(
            {
                "filename": f"filament-feedback-{stamp}.zip",
                "mime_type": "application/zip",
                "entry_count": st["count"],
                "zip_b64": base64.b64encode(zip_bytes).decode("ascii"),
                "package_sha256": pkg_sha256,
                "public_key_b64": pub_b64,
                "signature_b64": sig_b64,
                "pow_nonce": nonce,
                "pow_difficulty": difficulty,
            }
        ).encode("utf-8")
        return payload, st["count"], len(zip_bytes), pkg_sha256, pub_b64

    def _post_payload(self, url: str, payload: bytes, timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                status_code = resp.status
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Upload failed with HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Upload failed: {exc.reason}") from exc

        if status_code >= 400:
            raise RuntimeError(f"Upload failed with HTTP {status_code}: {resp_body}")

        try:
            parsed = json.loads(resp_body) if resp_body.strip() else {"ok": True}
        except json.JSONDecodeError:
            parsed = {"ok": True}

        if isinstance(parsed, dict) and parsed.get("ok") is False:
            raise RuntimeError(f"Upload rejected by server: {parsed.get('error', resp_body)}")
        return parsed

    def submit(
        self,
        endpoint_url: str,
        timeout: float = 45.0,
        pow_difficulty: int | None = None,
    ) -> dict[str, Any]:
        url = self._validate_endpoint(endpoint_url)
        payload, submitted_count, bundle_bytes, pkg_sha256, pub_b64 = self._build_signed_payload(
            pow_difficulty=pow_difficulty
        )
        parsed = self._post_payload(url, payload, timeout=timeout)
        self.clear()
        return {
            "ok": True,
            "submitted_count": submitted_count,
            "bundle_bytes": bundle_bytes,
            "package_sha256": pkg_sha256,
            "public_key_b64": pub_b64,
            "server_response": parsed,
        }

    def submit_status(self) -> dict[str, Any]:
        with self._submit_lock:
            return dict(self._submit_state)

    def submit_async(
        self,
        endpoint_url: str,
        timeout: float = 45.0,
        pow_difficulty: int | None = None,
    ) -> dict[str, Any]:
        url = self._validate_endpoint(endpoint_url)
        with self._lock:
            st = self.status()
            if st["count"] == 0:
                raise ValueError("Feedback queue is empty")

        with self._submit_lock:
            if self._submit_state["state"] == "running":
                return dict(self._submit_state)
            self._submit_state = {
                "state": "running",
                "phase": "packaging",
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "finished_at": None,
                "submitted_count": st["count"],
                "bundle_bytes": 0,
                "error": "",
            }

        def _set_phase(phase: str) -> None:
            with self._submit_lock:
                self._submit_state["phase"] = phase

        def _worker() -> None:
            try:
                payload, submitted_count, bundle_bytes, pkg_sha256, pub_b64 = self._build_signed_payload(
                    pow_difficulty=pow_difficulty,
                    phase_cb=_set_phase,
                )
                _set_phase("uploading")
                parsed = self._post_payload(url, payload, timeout=timeout)
                self.clear()
                with self._submit_lock:
                    self._submit_state.update(
                        {
                            "state": "completed",
                            "phase": "done",
                            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "submitted_count": submitted_count,
                            "bundle_bytes": bundle_bytes,
                            "package_sha256": pkg_sha256,
                            "public_key_b64": pub_b64,
                            "server_response": parsed,
                            "error": "",
                        }
                    )
            except Exception as exc:
                with self._submit_lock:
                    self._submit_state.update(
                        {
                            "state": "failed",
                            "phase": "failed",
                            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "error": str(exc),
                        }
                    )

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return self.submit_status()


def _load_bundle_meta(zpath: Path) -> dict[str, Any] | None:
    for candidate in (Path(str(zpath) + ".meta.json"), zpath.with_suffix(".meta.json")):
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def ingest_feedback_bundles(
    source: str | Path,
    dest_xml: str | Path = DATA_DIR / "annotations" / "feedback_verified.xml",
    dest_images_dir: str | Path = DATA_DIR / "feedback_verified",
    drop_keys: list[str] | set[str] | None = None,
    require_signature: bool = True,
    pow_difficulty: int = DEFAULT_POW_DIFFICULTY,
) -> dict[str, Any]:
    """Import verified feedback .zip bundles into feedback_verified.xml (checking signature, PoW, shape)."""
    src = Path(source)
    zips = [src] if src.is_file() and src.suffix.lower() == ".zip" else sorted(src.glob("*.zip"))
    if not zips:
        raise FileNotFoundError(f"No .zip feedback bundles found at {src}")

    drop_set = {k.strip() for k in (drop_keys or []) if k and k.strip()}
    dest_img_root = Path(dest_images_dir)
    dest_img_root.mkdir(parents=True, exist_ok=True)
    dest_xml_path = Path(dest_xml)

    if dest_xml_path.is_file():
        out_root = parse_xml_file(dest_xml_path)
    else:
        out_root = StdET.Element("dataset", attrib={"version": "1", "source": "user_feedback"})

    dropped_samples = 0
    if drop_set:
        for sample_el in list(out_root.findall("sample")):
            if sample_el.attrib.get("public-key", "") in drop_set:
                out_root.remove(sample_el)
                dropped_samples += 1

    existing_ids = {s.attrib.get("id", "") for s in out_root.findall("sample")}
    imported_entries = 0
    imported_images = 0
    by_key: dict[str, int] = {}
    rejected: list[dict[str, str]] = []

    ocr_engine: Any = None
    cv2_mod: Any = None
    np_mod: Any = None
    extract_fn: Any = None
    try:
        import cv2 as cv2_mod  # type: ignore[no-redef]
        import numpy as np_mod  # type: ignore[no-redef]
        from ..cpu_tier.ocr_helper import create_rapidocr
        from ..data_engine.auto_labeler import extract_multi_angle_ocr_and_boxes as extract_fn

        ocr_engine = create_rapidocr()
    except Exception:
        ocr_engine = None

    for zpath in zips:
        zip_bytes = zpath.read_bytes()
        try:
            validate_feedback_zip_bytes(zip_bytes)
        except Exception as exc:
            rejected.append({"zip": zpath.name, "reason": f"invalid_zip_shape: {exc}"})
            continue

        meta = _load_bundle_meta(zpath)
        pub_key = ""
        if require_signature:
            if not meta:
                rejected.append({"zip": zpath.name, "reason": "missing_meta_json"})
                continue
            pkg_sha = hashlib.sha256(zip_bytes).hexdigest()
            pub_key = str(meta.get("public_key_b64") or "").strip()
            sig_b64 = str(meta.get("signature_b64") or "").strip()
            nonce = meta.get("pow_nonce")
            diff = int(meta.get("pow_difficulty", pow_difficulty))
            if meta.get("package_sha256") and meta.get("package_sha256") != pkg_sha:
                rejected.append({"zip": zpath.name, "reason": "sha256_mismatch"})
                continue
            if not verify_package_signature(pkg_sha, sig_b64, pub_key):
                rejected.append({"zip": zpath.name, "reason": "invalid_signature"})
                continue
            if nonce is None or not verify_pow(pkg_sha, pub_key, nonce, difficulty=max(diff, pow_difficulty)):
                rejected.append({"zip": zpath.name, "reason": "invalid_pow"})
                continue
            if pub_key in drop_set:
                rejected.append({"zip": zpath.name, "reason": "dropped_key"})
                continue
        elif meta:
            pub_key = str(meta.get("public_key_b64") or "").strip()
            if pub_key in drop_set:
                rejected.append({"zip": zpath.name, "reason": "dropped_key"})
                continue

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = set(zf.namelist())
            fb_root = DefusedET.fromstring(zf.read("pending_feedback.xml"))

            for entry_el in fb_root.findall("entry"):
                eid = entry_el.attrib.get("id", "") or uuid.uuid4().hex[:12]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", eid) or eid in existing_ids:
                    continue

                corr = _el_to_item_dict(entry_el.find("corrected"))
                if not corr.get("vendor") or not corr.get("material"):
                    continue

                saved_paths: list[tuple[str, int, int, list[str], list[Any]]] = []
                for im in entry_el.findall("./images/image"):
                    fname = Path((im.text or "").strip()).name
                    if not fname or fname not in names:
                        continue
                    img_bytes = zf.read(fname)
                    try:
                        with Image.open(io.BytesIO(img_bytes)) as pil_im:
                            pil_im.verify()
                        with Image.open(io.BytesIO(img_bytes)) as pil_im2:
                            w, h = pil_im2.size
                    except Exception:
                        continue
                    out_img = dest_img_root / fname
                    out_img.write_bytes(img_bytes)
                    rel_or_abs = out_img.relative_to(DATA_DIR.parent) if out_img.is_relative_to(DATA_DIR.parent) else out_img

                    ocr_lines: list[str] = []
                    boxes: list[Any] = []
                    if ocr_engine is not None and cv2_mod is not None and np_mod is not None and extract_fn is not None:
                        arr = np_mod.frombuffer(img_bytes, dtype=np_mod.uint8)
                        img_bgr = cv2_mod.imdecode(arr, cv2_mod.IMREAD_COLOR)
                        if img_bgr is not None and img_bgr.size > 0:
                            ocr_lines, boxes = extract_fn(img_bgr, ocr_engine)

                    saved_paths.append((str(rel_or_abs).replace("\\", "/"), w, h, ocr_lines, boxes))
                    imported_images += 1

                if not saved_paths:
                    continue

                item = FilamentItem(
                    vendor=str(corr.get("vendor", "Unknown")),
                    material=str(corr.get("material", "PLA")),
                    line=str(corr.get("line", "")),
                    color_name=str(corr.get("color_name", "")),
                    color_family=str(corr.get("color_family", "")),
                    color_hex=str(corr.get("color_hex", "")),
                    net_grams=int(corr.get("net_grams", corr.get("net", 1000))),
                    tare_grams=int(corr.get("tare_grams", corr.get("tare", 170))),
                    density=float(corr.get("density", 1.24)),
                    diameter=float(corr.get("diameter", 1.75)),
                    nozzle=int(corr.get("nozzle", 210)),
                    bed=int(corr.get("bed", 60)),
                    nozzle_range=str(corr.get("nozzle_range", "190-220")),
                    bed_range=str(corr.get("bed_range", "50-60")),
                    sealed=bool(corr.get("sealed", False)),
                )
                synth_ocr = f"{item.vendor} {item.line} {item.material} {item.color_name}".strip()
                for idx_p, (spath, w, h, ocr_lines, boxes) in enumerate(saved_paths):
                    sid = eid if idx_p == 0 else f"{eid}_{idx_p}"
                    if sid in existing_ids:
                        continue
                    sample = AnnotatedSample(
                        sample_id=sid,
                        image_path=spath,
                        width=w,
                        height=h,
                        split="train",
                        item=item,
                        boxes=boxes,
                        ocr_lines=ocr_lines if ocr_lines else ([synth_ocr] if synth_ocr else []),
                    )
                    sample_el = sample.to_element()
                    sample_el.attrib["source"] = "user_feedback"
                    sample_el.attrib["weight"] = "2.0"
                    if pub_key:
                        sample_el.attrib["public-key"] = pub_key
                    out_root.append(sample_el)
                    existing_ids.add(sid)
                imported_entries += 1
                key_label = pub_key or "unsigned"
                by_key[key_label] = by_key.get(key_label, 0) + 1

    write_xml_atomic(dest_xml_path, out_root)
    return {
        "zips_processed": len(zips),
        "imported_entries": imported_entries,
        "imported_images": imported_images,
        "dropped_samples": dropped_samples,
        "by_key": by_key,
        "rejected": rejected,
        "dest_xml": str(dest_xml_path),
    }
