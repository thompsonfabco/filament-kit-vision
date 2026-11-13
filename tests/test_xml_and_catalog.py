"""Unit tests for XML serialization, Spoolman normalization, and CatalogMatcher."""
from __future__ import annotations

from pathlib import Path

from filament_kit_vision.catalog.matcher import CatalogMatcher
from filament_kit_vision.xml_io import (
    AnnotatedSample,
    BoundingBox,
    FilamentItem,
    IntakeReadResult,
    load_dataset_xml,
    save_dataset_xml,
)


def test_intake_read_xml_roundtrip() -> None:
    item = FilamentItem(
        vendor="Bambu Lab",
        material="PLA",
        line="PLA Matte",
        color_name="Charcoal",
        color_family="Black",
        color_hex="1E1E1E",
        color_hex_source="catalog",
        net_grams=1000,
        tare_grams=250,
        density=1.24,
        diameter=1.75,
        nozzle_range="190-230",
        bed_range="35-45",
        sealed=True,
        sealed_reason="in box",
        estimates=["tare"],
    )
    res = IntakeReadResult(
        items=[item],
        questions=["Confirm spool count?"],
        tier="cpu-hybrid",
        latency_ms=84.5,
        model_name="filament-kit-vision-cpu-v1",
    )
    xml_str = res.to_xml()
    assert '<?xml version="1.0" encoding="UTF-8"?>' in xml_str
    assert "<vendor>Bambu Lab</vendor>" in xml_str
    assert "<name>PLA Matte Charcoal</name>" in xml_str

    parsed = IntakeReadResult.from_xml(xml_str)
    assert len(parsed.items) == 1
    p_it = parsed.items[0]
    assert p_it.vendor == "Bambu Lab"
    assert p_it.material == "PLA"
    assert p_it.line == "PLA Matte"
    assert p_it.color_name == "Charcoal"
    assert p_it.name == "PLA Matte Charcoal"
    assert p_it.color_hex == "1E1E1E"
    assert p_it.nozzle == 210
    assert p_it.bed == 40
    assert p_it.sealed is True
    assert parsed.questions == ["Confirm spool count?"]

    fk = parsed.to_filament_kit_dict()
    assert fk["items"][0]["vendor"] == "Bambu Lab"
    assert fk["items"][0]["name"] == "PLA Matte Charcoal"
    assert fk["items"][0]["line"] == "PLA Matte"
    assert fk["items"][0]["material"] == "PLA"
    assert fk["items"][0]["color_hex"] == "1E1E1E"


def test_dataset_xml_roundtrip(tmp_path: Path) -> None:
    sample = AnnotatedSample(
        sample_id="test_01",
        image_path="data/seed_converted/4853.jpg",
        original_path="seed images/4853.HEIC",
        width=1600,
        height=1200,
        split="gold_test",
        item=FilamentItem(vendor="ELEGOO", material="PLA", line="Rapid PLA+", color_name="Brown", color_family="Brown/Beige"),
        boxes=[BoundingBox(label="box_label", x1=0.1, y1=0.2, x2=0.8, y2=0.9, confidence=0.95)],
        ocr_lines=["ELEGOO", "RAPID PLA+", "Brown"],
        visual_finish="Basic",
    )
    xml_file = tmp_path / "test_split.xml"
    save_dataset_xml(xml_file, [sample], split_name="gold_test")
    loaded = load_dataset_xml(xml_file)
    assert len(loaded) == 1
    assert loaded[0].sample_id == "test_01"
    assert loaded[0].item.vendor == "ELEGOO"
    assert loaded[0].item.line == "Rapid PLA+"
    assert loaded[0].boxes[0].label == "box_label"
    assert loaded[0].ocr_lines == ["ELEGOO", "RAPID PLA+", "Brown"]


def test_catalog_matcher_spoolman_rules() -> None:
    matcher = CatalogMatcher()
    # Every PLA variant normalizes to base polymer 'PLA', while CF variants stay 'PLA-CF'
    assert matcher.normalize_material("PLA+ 2.0", "High Speed PLA+ 2.0") == "PLA"
    assert matcher.normalize_material("Rapid PLA+", "") == "PLA"
    assert matcher.normalize_material("Silk PLA", "") == "PLA"
    assert matcher.normalize_material("PETG HF", "") == "PETG"
    assert matcher.normalize_material("PETG-CF", "") == "PETG-CF"
    assert matcher.normalize_material("PAHT-CF", "") == "PAHT-CF"

    v_bambu, _ = matcher.resolve_vendor("BBL PLA Basic Jade White")
    assert v_bambu == "Bambu Lab"

    v_sunlu, _ = matcher.resolve_vendor("SUNLU High Speed PLA+ 2.0")
    assert v_sunlu == "SUNLU"

    v_elegoo, _ = matcher.resolve_vendor("ELEGOO Rapid PETG")
    assert v_elegoo == "ELEGOO"

    draft = FilamentItem(
        vendor="Bambu",
        material="PLA Matte",
        line="PLA Matte",
        color_name="Charcoal",
    )
    enriched = matcher.enrich_item(draft)
    assert enriched.vendor == "Bambu Lab"
    assert enriched.material == "PLA"
    assert enriched.line == "PLA Matte"
    assert enriched.color_name == "Charcoal"
    assert enriched.color_hex == "1E1E1E"
    assert enriched.tare_grams == 250
