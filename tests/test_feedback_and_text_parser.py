"""Tests for v0.2.0 features: pickle-free text_parser.npz and opt-in feedback queue/upload."""
from __future__ import annotations

import base64
import io
import json
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

from filament_kit_vision.catalog.matcher import CatalogMatcher
from filament_kit_vision.cpu_tier.text_parser_io import load_text_parser_npz
from filament_kit_vision.service.daemon import start_daemon
from filament_kit_vision.service.feedback import ingest_feedback_bundles
from filament_kit_vision.service.updater import run_one_click_update
from filament_kit_vision.xml_io import load_training_samples_with_feedback


def _make_jpeg_with_exif_b64() -> str:
    img = Image.new("RGB", (1600, 1200), color=(25, 118, 210))
    exif = Image.Exif()
    exif[0x010F] = "FakeCameraMaker"
    exif[0x0110] = "SecretModel123"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_pickle_free_text_parser_npz() -> None:
    npz_path = Path(__file__).resolve().parents[1] / "models" / "cpu" / "text_parser.npz"
    if not npz_path.is_file():
        return
    pipes = load_text_parser_npz(npz_path)
    assert set(pipes.keys()) == {"vendor_pipe", "material_pipe", "line_pipe"}
    probs = pipes["vendor_pipe"].predict_proba(["Bambu Lab PLA Basic Bambu Green 1kg"])
    assert probs.shape == (1, len(pipes["vendor_pipe"].classes_))
    top_vendor = str(pipes["vendor_pipe"].classes_[probs[0].argmax()])
    assert "Bambu" in top_vendor
    assert CatalogMatcher().normalize_material("PLA", "Rapid PETG") == "PETG"


def test_feedback_http_roundtrip_and_ingest(tmp_path: Path) -> None:
    # 1. Start a mock write-only Apps Script receiver
    received: list[dict] = []

    class MockAppsScript(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received.append(body)
            resp = json.dumps({"ok": True, "file_id": "mock_123"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format: str, *args: object) -> None:
            return

    mock_srv = HTTPServer(("127.0.0.1", 0), MockAppsScript)
    mock_port = mock_srv.server_address[1]
    threading.Thread(target=mock_srv.serve_forever, daemon=True).start()

    # 2. Verify opt_in=False rejects POST /v1/feedback with 403
    srv_off = start_daemon(host="127.0.0.1", port=0, tier="cpu", feedback_opt_in=False, feedback_dir=tmp_path / "q_off")
    off_port = srv_off.server_address[1]
    threading.Thread(target=srv_off.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{off_port}/v1/feedback",
            data=json.dumps({"images_b64": [_make_jpeg_with_exif_b64()], "predicted": {}, "corrected": {}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("Expected 403 when feedback_opt_in=False")
        except urllib.error.HTTPError as err:
            assert err.code == 403
    finally:
        srv_off.shutdown()
        srv_off.server_close()

    # 3. Verify full opt_in=True lifecycle: enqueue -> GET -> bundle.zip -> submit -> ingest
    q_dir = tmp_path / "q_on"
    srv = start_daemon(
        host="127.0.0.1",
        port=0,
        tier="cpu",
        feedback_opt_in=True,
        feedback_endpoint=f"http://127.0.0.1:{mock_port}/exec",
        feedback_dir=q_dir,
    )
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        b64 = _make_jpeg_with_exif_b64()
        post_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/feedback",
            data=json.dumps(
                {
                    "images_b64": [b64],
                    "predicted": {"vendor": "Unknown", "material": "PLA", "color_family": "Blue"},
                    "corrected": {"vendor": "Bambu Lab", "material": "PLA", "line": "PLA Basic", "color_name": "Cobalt Blue"},
                    "note": "Misread spool label",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(post_req, timeout=15) as r:
            enq = json.loads(r.read().decode("utf-8"))
        assert enq["ok"] is True
        assert enq["queued_count"] == 1
        entry_id = enq["id"]

        # Verify EXIF stripped and resized <= 1024
        jpg_files = list(q_dir.glob("fb_*.jpg"))
        assert len(jpg_files) == 1
        with Image.open(jpg_files[0]) as saved_im:
            assert max(saved_im.size) <= 1024
            assert len(saved_im.getexif()) == 0

        # Download bundle.zip
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/feedback/bundle.zip", timeout=10) as r:
            zip_bytes = r.read()
        assert zip_bytes[:2] == b"PK"

        # Submit to mock Apps Script endpoint (202 async + poll)
        sub_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/feedback/submit",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(sub_req, timeout=15) as r:
            assert r.status == 202
            sub_res = json.loads(r.read().decode("utf-8"))
        assert sub_res["state"] in ("running", "completed")

        deadline = time.time() + 30.0
        poll_res = sub_res
        while time.time() < deadline and poll_res.get("state") == "running":
            time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/feedback/submit", timeout=10) as r:
                poll_res = json.loads(r.read().decode("utf-8"))
        assert poll_res["state"] == "completed", poll_res
        assert poll_res["submitted_count"] == 1
        assert len(received) == 1

        # Queue is cleared after submit
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/feedback", timeout=10) as r:
            st = json.loads(r.read().decode("utf-8"))
        assert st["count"] == 0

        # Save received zip + .meta.json and test ingest_feedback_bundles
        rx_zip = tmp_path / "rx.zip"
        rx_zip.write_bytes(base64.b64decode(received[0]["zip_b64"]))
        rx_meta = Path(str(rx_zip) + ".meta.json")
        rx_meta.write_text(
            json.dumps(
                {
                    "package_sha256": received[0]["package_sha256"],
                    "public_key_b64": received[0]["public_key_b64"],
                    "signature_b64": received[0]["signature_b64"],
                    "pow_nonce": received[0]["pow_nonce"],
                    "pow_difficulty": received[0]["pow_difficulty"],
                }
            ),
            encoding="utf-8",
        )
        with zipfile.ZipFile(rx_zip, "r") as zf:
            assert "pending_feedback.xml" in zf.namelist()
            assert f"{entry_id}_0.jpg" in zf.namelist()

        ingest_res = ingest_feedback_bundles(
            rx_zip,
            dest_xml=tmp_path / "feedback_verified.xml",
            dest_images_dir=tmp_path / "feedback_verified",
        )
        assert ingest_res["imported_entries"] == 1
        assert ingest_res["imported_images"] == 1
        assert (tmp_path / "feedback_verified.xml").is_file()
        loaded = load_training_samples_with_feedback(
            tmp_path / "missing_train.xml",
            feedback_xml=tmp_path / "feedback_verified.xml",
            feedback_weight=2,
        )
        assert len(loaded) == 2
        assert loaded[0].item.vendor == "Bambu Lab"
        assert len(loaded[0].ocr_lines) >= 1
        assert len(loaded[0].boxes) >= 1

        # Verify POST /v1/read applies optional 'hint'
        hint_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/read",
            data=json.dumps(
                {
                    "api_version": "1.0",
                    "images_b64": [b64],
                    "tier": "cpu",
                    "hint": "Bambu Lab PETG-CF bare spool",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(hint_req, timeout=20) as r:
            hint_res = json.loads(r.read().decode("utf-8"))
        assert hint_res["items"][0]["vendor"] == "Bambu Lab"
        assert hint_res["items"][0]["material"] == "PETG-CF"
        assert hint_res["items"][0]["sealed"] is False

        # Test one-click updater: ingest from incoming dir, archive zip, respect min_new_samples gate
        inc_dir = tmp_path / "incoming"
        inc_dir.mkdir()
        (inc_dir / "filament-feedback-batch1.zip").write_bytes(rx_zip.read_bytes())
        (inc_dir / "filament-feedback-batch1.zip.meta.json").write_text(rx_meta.read_text(encoding="utf-8"), encoding="utf-8")
        upd = run_one_click_update(
            source_dirs=[inc_dir],
            min_new_samples=10,
            force=False,
            skip_train=True,
            skip_benchmark=True,
            archive_dir=tmp_path / "archived",
            feedback_xml=tmp_path / "upd_fb.xml",
            feedback_images_dir=tmp_path / "upd_imgs",
        )
        assert upd["zips_found"] == 1
        assert upd["new_feedback_samples"] == 1
        assert upd["trained"] is False
        assert "skipped_reason" in upd
        assert (tmp_path / "archived" / "filament-feedback-batch1.zip").is_file()
        assert not (inc_dir / "filament-feedback-batch1.zip").exists()
    finally:
        srv.shutdown()
        srv.server_close()
        mock_srv.shutdown()
        mock_srv.server_close()


def test_section14_feedback_security_and_contract(tmp_path: Path, monkeypatch) -> None:
    """Contract tests for Section 14: health capabilities, bearer auth, record_id dedup, non-loopback 403, Ed25519+PoW, drop-key."""
    import urllib.error
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from filament_kit_vision.service.feedback import verify_package_signature, verify_pow

    monkeypatch.setenv("FILAMENT_KIT_VISION_POW_DIFFICULTY", "8")
    state_dir = tmp_path / "ext_state"
    monkeypatch.setenv("FILAMENT_KIT_VISION_STATE_DIR", str(state_dir))

    received: list[dict] = []

    class FakeEndpoint(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length") or "0")
            received.append(json.loads(self.rfile.read(n).decode("utf-8")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "file_id": "quarantine-1"}')

        def log_message(self, format: str, *args) -> None:
            return

    mock_srv = HTTPServer(("127.0.0.1", 0), FakeEndpoint)
    mock_port = mock_srv.server_address[1]
    threading.Thread(target=mock_srv.serve_forever, daemon=True).start()

    token = "sec14-test-token"
    srv = start_daemon(
        host="127.0.0.1",
        port=0,
        tier="cpu",
        auth_token=token,
        feedback_opt_in=True,
        feedback_endpoint=f"http://127.0.0.1:{mock_port}/exec",
        state_dir=state_dir,
    )
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    auth_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    try:
        # 1. Per-install keypair created in external state_dir on opt-in
        assert (state_dir / "install_ed25519.pem").is_file()
        assert (state_dir / "install_ed25519.pub").is_file()

        # 2. Health reports api_version 1.1, capabilities, feedback summary, and accelerator
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=10) as r:
            health = json.loads(r.read().decode("utf-8"))
        assert health["api_version"] == "1.1"
        assert health["capabilities"]["feedback"] is True
        assert health["feedback"]["opt_in"] is True
        assert health["feedback"]["endpoint_configured"] is True
        assert health["tiers"]["cpu"]["accelerator"] == "cpu"

        # 3. Feedback endpoints reject missing bearer token
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/feedback", timeout=5)
            raise AssertionError("Expected 401 without bearer token")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        # 4. record_id deduplication & source tracking
        b64 = _make_jpeg_with_exif_b64()
        for idx, mat in enumerate(("PLA", "PETG")):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/feedback",
                data=json.dumps(
                    {
                        "api_version": "1.1",
                        "images_b64": [b64],
                        "predicted": {"vendor": "Unknown", "material": "PLA"},
                        "corrected": {"vendor": "Prusa", "material": mat},
                        "source": "cloud",
                        "record_id": "rec-42",
                        "note": f"attempt {idx}",
                    }
                ).encode("utf-8"),
                headers=auth_headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode("utf-8"))
            assert res["queued_count"] == 1
            entry_id = res["id"]

        with urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/v1/feedback", headers=auth_headers),
            timeout=10,
        ) as r:
            fb_list = json.loads(r.read().decode("utf-8"))
        assert fb_list["count"] == 1
        ent = fb_list["entries"][0]
        assert ent["source"] == "cloud"
        assert ent["record_id"] == "rec-42"
        assert ent["corrected"]["material"] == "PETG"
        assert ent["image_url"] == f"/v1/feedback/{entry_id}/image"

        # 5. Image endpoint returns JPEG bytes
        with urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}{ent['image_url']}", headers=auth_headers),
            timeout=10,
        ) as r:
            img_bytes = r.read()
        assert img_bytes[:2] == b"\xff\xd8"

        # 6. Non-loopback caller cannot override endpoint in POST /v1/feedback/submit (simulated via handler check)
        handler_cls = srv.RequestHandlerClass
        assert handler_cls is not None
        fake_non_loopback = type("Dummy", (), {"client_address": ("10.0.0.5", 12345)})()
        assert handler_cls._is_loopback_client(fake_non_loopback) is False
        fake_loopback = type("Dummy", (), {"client_address": ("127.0.0.1", 12345)})()
        assert handler_cls._is_loopback_client(fake_loopback) is True

        # 7. Background submit (202) + poll + Ed25519 + PoW verification + drop-key
        sub_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/feedback/submit",
            data=b"{}",
            headers=auth_headers,
            method="POST",
        )
        with urllib.request.urlopen(sub_req, timeout=15) as r:
            assert r.status == 202
        deadline = time.time() + 15.0
        while time.time() < deadline:
            with urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/v1/feedback/submit", headers=auth_headers),
                timeout=10,
            ) as r:
                st = json.loads(r.read().decode("utf-8"))
            if st["state"] == "completed":
                break
            time.sleep(0.05)
        assert st["state"] == "completed"
        assert len(received) == 1
        pkt = received[0]
        assert verify_package_signature(pkt["package_sha256"], pkt["signature_b64"], pkt["public_key_b64"])
        assert verify_pow(pkt["package_sha256"], pkt["public_key_b64"], pkt["pow_nonce"], difficulty=8)

        # Bad/unsigned zip is refused by ingest_feedback_bundles
        bad_zip = tmp_path / "unsigned.zip"
        bad_zip.write_bytes(base64.b64decode(pkt["zip_b64"]))
        bad_res = ingest_feedback_bundles(
            bad_zip,
            dest_xml=tmp_path / "v_bad.xml",
            dest_images_dir=tmp_path / "v_bad_imgs",
            pow_difficulty=8,
        )
        assert bad_res["imported_entries"] == 0
        assert len(bad_res["rejected"]) == 1

        # Signed zip succeeds, then --drop-key purges it
        good_zip = tmp_path / "signed.zip"
        good_zip.write_bytes(base64.b64decode(pkt["zip_b64"]))
        Path(str(good_zip) + ".meta.json").write_text(json.dumps(pkt), encoding="utf-8")
        good_res = ingest_feedback_bundles(
            good_zip,
            dest_xml=tmp_path / "v_good.xml",
            dest_images_dir=tmp_path / "v_good_imgs",
            pow_difficulty=8,
        )
        assert good_res["imported_entries"] == 1
        assert good_res["by_key"][pkt["public_key_b64"]] == 1

        drop_res = ingest_feedback_bundles(
            good_zip,
            dest_xml=tmp_path / "v_good.xml",
            dest_images_dir=tmp_path / "v_good_imgs",
            drop_keys=[pkt["public_key_b64"]],
            pow_difficulty=8,
        )
        assert drop_res["dropped_samples"] == 1
        assert drop_res["imported_entries"] == 0
    finally:
        srv.shutdown()
        srv.server_close()
        mock_srv.shutdown()
        mock_srv.server_close()

