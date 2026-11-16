"""Integration tests for CPU Hybrid Pipeline, polar unwarping, LAB color extractor, and localhost HTTP daemon."""
from __future__ import annotations

import threading
import numpy as np

from spoolwarden_vision.cpu_tier.color_lab import extract_filament_color
from spoolwarden_vision.cpu_tier.pipeline import CPUHybridPipeline
from spoolwarden_vision.cpu_tier.unwarp import unwarp_circular_spool_label
from spoolwarden_vision.service.daemon import LocalIntakeClient, start_daemon
from spoolwarden_vision.xml_io import PROJECT_ROOT


def test_polar_unwarp_and_lab_color() -> None:
    # Create a synthetic blue spool image
    img = np.zeros((320, 320, 3), dtype=np.uint8)
    img[:, :] = (210, 115, 25)  # BGR Blue
    strips = unwarp_circular_spool_label(img)
    assert len(strips) == 4
    assert strips[0].shape == (220, 900, 3)

    hex_code, family, finish = extract_filament_color(img, is_spool_crop=True)
    assert len(hex_code) == 6
    assert family == "Blue"
    assert finish in ("Basic", "Matte", "Silk")


def test_cpu_pipeline_on_real_seed_images() -> None:
    pipe = CPUHybridPipeline()
    img_4853 = PROJECT_ROOT / "data" / "seed_converted" / "4853.jpg"
    img_4910 = PROJECT_ROOT / "data" / "seed_converted" / "4910.jpg"
    assert img_4853.is_file()
    assert img_4910.is_file()

    res = pipe.predict([img_4853, img_4910])
    assert res.tier == "cpu-hybrid"
    assert len(res.items) == 2

    # 4853.jpg is Anycubic PETG-CF
    assert res.items[0].vendor == "Anycubic"
    assert "CF" in res.items[0].material

    # 4910.jpg is rotated 90-deg Phrozen High Speed PLA Black
    assert res.items[1].vendor == "Phrozen"
    assert res.items[1].material == "PLA"
    assert res.items[1].color_family == "Black"


def test_local_http_daemon_roundtrip() -> None:
    server = start_daemon(host="127.0.0.1", port=0, tier="cpu")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        client = LocalIntakeClient(base_url=f"http://127.0.0.1:{port}")
        h = client.health()
        assert h["status"] == "ok"

        img_bytes = (PROJECT_ROOT / "data" / "seed_converted" / "4890.jpg").read_bytes()
        fake_spoolman_filaments = [
            {
                "id": 42,
                "name": "Elite PETG Mango Yellow",
                "material": "PETG",
                "vendor": {"name": "SUNLU"},
                "spool_weight": 213,
                "density": 1.27,
                "color_hex": "FBC02D",
            }
        ]
        fk_out = client.read_labels([img_bytes], tier="cpu", filaments=fake_spoolman_filaments)
        assert "items" in fk_out
        assert len(fk_out["items"]) == 1
        item = fk_out["items"][0]
        assert item["vendor"] in ("SUNLU", "JAYO")
        assert "confidence" in item
        assert "field_confidence" in item
        assert "reader" in fk_out
        assert fk_out["reader"]["tier_used"] == "cpu"
        assert fk_out["api_version"] == "1.0"

        # Test POST /v1/revise
        revised = client.revise(previous=item, note="brand is Bambu Lab; material is PLA-CF")
        assert len(revised["items"]) == 1
        rev_item = revised["items"][0]
        assert rev_item["vendor"] == "Bambu Lab"
        assert "CF" in rev_item["material"]
        assert rev_item["confidence"] >= 0.90
    finally:
        server.shutdown()
        server.server_close()
