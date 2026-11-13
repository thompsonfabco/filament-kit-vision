"""End-to-end CPU Hybrid Inference Pipeline (ONNX Detector + RapidOCR + Unwarping + ONNX MultiHead Visual + Catalog Fusion)."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import cv2
import joblib
import numpy as np
import onnxruntime as ort
from rapidocr_onnxruntime import RapidOCR

log = logging.getLogger(__name__)

_ACCEL_PRIORITY = [
    "CUDAExecutionProvider",
    "ROCMExecutionProvider",
    "OpenVINOExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
]


def select_onnx_providers() -> list[str]:
    """Discover available ONNX Runtime hardware accelerators with CPUExecutionProvider fallback."""
    force_cpu = os.environ.get("FILAMENT_KIT_VISION_CPU_ONLY", "").strip().lower() in ("1", "true", "yes")
    if force_cpu:
        return ["CPUExecutionProvider"]
    available = set(ort.get_available_providers())
    chosen = [p for p in _ACCEL_PRIORITY if p in available]
    if "CPUExecutionProvider" not in chosen:
        chosen.append("CPUExecutionProvider")
    return chosen


def _create_ort_session(model_path: str | Path, sess_opts: ort.SessionOptions, providers: list[str]) -> ort.InferenceSession:
    """Create an ORT InferenceSession using best available providers, falling back to CPU on init failure."""
    try:
        return ort.InferenceSession(str(model_path), sess_options=sess_opts, providers=providers)
    except Exception as exc:
        if providers != ["CPUExecutionProvider"]:
            log.warning("Accelerated ORT providers %s failed (%s); falling back to CPUExecutionProvider", providers, exc)
            return ort.InferenceSession(str(model_path), sess_options=sess_opts, providers=["CPUExecutionProvider"])
        raise

from ..catalog.matcher import CatalogMatcher
from ..data_engine.auto_labeler import extract_multi_angle_ocr_and_boxes, parse_ocr_heuristic
from ..xml_io import PROJECT_ROOT, BoundingBox, FilamentItem, IntakeReadResult
from .color_lab import extract_filament_color
from .train_detector import DETECTOR_CLASSES
from .train_visual import COLOR_FAMILIES, SEALED_STATES, VISUAL_FINISHES, VISUAL_VENDORS
from .unwarp import unwarp_circular_spool_label


def _softmax(x: np.ndarray) -> np.ndarray:
    e_x = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e_x / np.sum(e_x, axis=-1, keepdims=True)


class CPUHybridPipeline:
    """Fast CPU-only inference pipeline using ONNX Runtime, RapidOCR, and CatalogMatcher."""

    def __init__(
        self,
        detector_onnx: str | Path = PROJECT_ROOT / "models" / "cpu" / "detector_rfdetr_n.onnx",
        visual_onnx: str | Path = PROJECT_ROOT / "models" / "cpu" / "visual_multihead.onnx",
        text_parser_joblib: str | Path = PROJECT_ROOT / "models" / "cpu" / "text_parser.joblib",
    ):
        self.matcher = CatalogMatcher()
        self.providers = select_onnx_providers()

        ocr_kwargs: dict[str, Any] = {}
        if "CUDAExecutionProvider" in self.providers:
            ocr_kwargs = {"det_use_cuda": True, "cls_use_cuda": True, "rec_use_cuda": True}
        elif "DmlExecutionProvider" in self.providers:
            ocr_kwargs = {"det_use_dml": True, "cls_use_dml": True, "rec_use_dml": True}
        try:
            self.ocr = RapidOCR(**ocr_kwargs) if ocr_kwargs else RapidOCR()
        except Exception as exc:
            log.warning("Accelerated RapidOCR init failed (%s); falling back to CPU", exc)
            self.ocr = RapidOCR()

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = min(8, max(2, os.cpu_count() or 4))
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.detector_session: ort.InferenceSession | None = None
        if Path(detector_onnx).is_file():
            self.detector_session = _create_ort_session(detector_onnx, sess_opts, self.providers)

        self.visual_session: ort.InferenceSession | None = None
        if Path(visual_onnx).is_file():
            self.visual_session = _create_ort_session(visual_onnx, sess_opts, self.providers)

        self.active_providers: list[str] = (
            self.visual_session.get_providers()
            if self.visual_session is not None
            else (self.detector_session.get_providers() if self.detector_session is not None else ["CPUExecutionProvider"])
        )

        self.text_parser: dict[str, Any] | None = None
        if Path(text_parser_joblib).is_file():
            self.text_parser = joblib.load(text_parser_joblib)

    def _detect_regions(self, img_bgr: np.ndarray, conf_thresh: float = 0.35) -> list[BoundingBox]:
        """Run RF-DETR-N ONNX detector to locate box_label, spool_label, spool_windings, and spool_flange."""
        if self.detector_session is None:
            return []
        resized = cv2.resize(img_bgr, (384, 384), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        norm = ((rgb - mean) / std).transpose(2, 0, 1)[np.newaxis, ...]

        inp_name = self.detector_session.get_inputs()[0].name
        dets, logits = self.detector_session.run(None, {inp_name: norm})
        probs = 1.0 / (1.0 + np.exp(-np.clip(logits[0], -20.0, 20.0)))

        results: list[BoundingBox] = []
        num_cls = len(DETECTOR_CLASSES)
        for i in range(dets.shape[1]):
            cls_probs = probs[i, :num_cls]
            cid = int(np.argmax(cls_probs))
            score = float(cls_probs[cid])
            if score < conf_thresh:
                continue
            cx, cy, bw, bh = [float(v) for v in dets[0, i]]
            x1 = max(0.0, min(1.0, cx - bw / 2.0))
            y1 = max(0.0, min(1.0, cy - bh / 2.0))
            x2 = max(0.0, min(1.0, cx + bw / 2.0))
            y2 = max(0.0, min(1.0, cy + bh / 2.0))
            if (x2 - x1) > 0.02 and (y2 - y1) > 0.02:
                results.append(
                    BoundingBox(
                        label=DETECTOR_CLASSES[cid],
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        confidence=score,
                    )
                )
        results.sort(key=lambda b: b.confidence, reverse=True)
        return results

    def _run_visual_onnx(self, img_bgr: np.ndarray) -> dict[str, tuple[str, float]]:
        if self.visual_session is None:
            return {}
        resized = cv2.resize(img_bgr, (224, 224), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        norm = ((rgb - mean) / std).transpose(2, 0, 1)[np.newaxis, ...]

        c_log, f_log, v_log, s_log = self.visual_session.run(None, {"image": norm})
        c_prob = _softmax(c_log[0])
        f_prob = _softmax(f_log[0])
        v_prob = _softmax(v_log[0])
        s_prob = _softmax(s_log[0])

        c_idx = int(np.argmax(c_prob))
        f_idx = int(np.argmax(f_prob))
        v_idx = int(np.argmax(v_prob))
        s_idx = int(np.argmax(s_prob))

        return {
            "color_family": (COLOR_FAMILIES[c_idx], float(c_prob[c_idx])),
            "visual_finish": (VISUAL_FINISHES[f_idx], float(f_prob[f_idx])),
            "visual_vendor": (VISUAL_VENDORS[v_idx], float(v_prob[v_idx])),
            "sealed_state": (SEALED_STATES[s_idx], float(s_prob[s_idx])),
        }

    def predict_image(self, image_input: str | Path | bytes | np.ndarray, photo_index: int = 0) -> FilamentItem:
        if isinstance(image_input, (str, Path)):
            img_bgr = cv2.imread(str(image_input))
        elif isinstance(image_input, bytes):
            arr = np.frombuffer(image_input, dtype=np.uint8)
            img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            img_bgr = image_input

        if img_bgr is None or img_bgr.size == 0:
            raise ValueError("Invalid or empty image passed to CPUHybridPipeline")

        # Keep up to 1600px side so small barcode/address text remains legible
        h, w = img_bgr.shape[:2]
        if max(h, w) > 1600:
            scale = 1600.0 / max(h, w)
            img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

        det_boxes = self._detect_regions(img_bgr)
        ocr_lines, boxes = extract_multi_angle_ocr_and_boxes(img_bgr, self.ocr)
        if det_boxes:
            boxes = det_boxes + boxes

        # Crop detected label regions for sharper OCR on small/angled stickers
        seen_ocr = set(ocr_lines)
        for b in det_boxes[:3]:
            if b.label in ("box_label", "spool_label"):
                pad = 0.06
                px1 = max(0, int((b.x1 - pad) * img_bgr.shape[1]))
                py1 = max(0, int((b.y1 - pad) * img_bgr.shape[0]))
                px2 = min(img_bgr.shape[1], int((b.x2 + pad) * img_bgr.shape[1]))
                py2 = min(img_bgr.shape[0], int((b.y2 + pad) * img_bgr.shape[0]))
                crop = img_bgr[py1:py2, px1:px2]
                if crop.size > 0 and min(crop.shape[:2]) >= 24:
                    if max(crop.shape[:2]) < 480:
                        s_up = 480.0 / max(crop.shape[:2])
                        crop = cv2.resize(crop, (int(crop.shape[1] * s_up), int(crop.shape[0] * s_up)), interpolation=cv2.INTER_CUBIC)
                    res_c, _ = self.ocr(crop)
                    if res_c:
                        for _, text, score in res_c:
                            t = str(text).strip()
                            if float(score) >= 0.45 and t and t not in seen_ocr:
                                ocr_lines.append(t)
                                seen_ocr.add(t)

        is_spool = any(b.label in ("spool_flange", "spool_windings", "spool_label") for b in boxes)

        # If circular spool with minimal upright OCR text, try polar unwarping
        if is_spool and len(" ".join(ocr_lines)) < 16:
            strips = unwarp_circular_spool_label(img_bgr)
            for strip in strips[:2]:
                res_s, _ = self.ocr(strip)
                if res_s:
                    for _, text, score in res_s:
                        if float(score) >= 0.48 and str(text).strip():
                            ocr_lines.append(str(text).strip())

        item, vis_finish = parse_ocr_heuristic(ocr_lines, img_bgr, self.matcher, is_spool=is_spool)
        item.photos = [photo_index]

        # Refine visually estimated filament color from detected spool_windings crop
        if item.color_hex_source == "estimate":
            windings = next((b for b in det_boxes if b.label == "spool_windings"), None)
            if windings is not None:
                px1 = max(0, int(windings.x1 * img_bgr.shape[1]))
                py1 = max(0, int(windings.y1 * img_bgr.shape[0]))
                px2 = min(img_bgr.shape[1], int(windings.x2 * img_bgr.shape[1]))
                py2 = min(img_bgr.shape[0], int(windings.y2 * img_bgr.shape[0]))
                w_crop = img_bgr[py1:py2, px1:px2]
                if w_crop.size > 0 and min(w_crop.shape[:2]) >= 16:
                    w_hex, w_fam, _ = extract_filament_color(w_crop)
                    item.color_hex = w_hex
                    item.color_family = w_fam
                    if item.color_name in COLOR_FAMILIES:
                        item.color_name = "unknown"

        # Fuse trained Text Parser predictions when OCR text is present
        if self.text_parser is not None and ocr_lines:
            joined_ocr = " | ".join(ocr_lines)
            if item.vendor == "Unknown":
                v_probs = self.text_parser["vendor_pipe"].predict_proba([joined_ocr])[0]
                v_best_idx = int(np.argmax(v_probs))
                if float(v_probs[v_best_idx]) >= 0.35:
                    item.vendor = str(self.text_parser["vendor_pipe"].classes_[v_best_idx])

            if item.line in ("PLA", "PETG", "ABS", "ASA"):
                l_probs = self.text_parser["line_pipe"].predict_proba([joined_ocr])[0]
                l_best_idx = int(np.argmax(l_probs))
                if float(l_probs[l_best_idx]) >= 0.42:
                    item.line = str(self.text_parser["line_pipe"].classes_[l_best_idx])

        # Fuse trained Visual Multi-Head ONNX classifier predictions
        vis_preds = self._run_visual_onnx(img_bgr)
        if vis_preds:
            v_col, c_conf = vis_preds["color_family"]
            v_fin, f_conf = vis_preds["visual_finish"]
            v_ven, v_conf = vis_preds["visual_vendor"]
            v_seal, s_conf = vis_preds["sealed_state"]

            # If no label text gave a vendor, use visual flange vendor
            if item.vendor == "Unknown" and v_ven != "Other" and v_conf >= 0.45:
                item.vendor = v_ven
                if "vendor" not in item.estimates:
                    item.estimates.append("vendor")

            # If color was only visually estimated (no explicit label color text), use ONNX color family if confident
            if item.color_hex_source == "estimate" and c_conf >= 0.50:
                item.color_family = v_col
                if item.color_name in COLOR_FAMILIES:
                    item.color_name = v_col

            # If no label text at all, infer subtype from visual finish
            if not ocr_lines:
                item.source_type = "exposed_spool"
                if v_fin in ("Matte", "Silk", "Galaxy", "Translucent", "Wood", "Glow") and f_conf >= 0.45:
                    item.line = f"{item.material} {v_fin}"
                elif v_fin == "CF" and f_conf >= 0.50:
                    item.material = "PLA-CF"
                    item.line = "PLA-CF"

        # Robust sealed state determination
        joined_ocr_lower = " ".join(ocr_lines).lower()
        has_bag_text = any(
            w in joined_ocr_lower
            for w in (
                "desiccant", "silica", "silica gel", "do not eat", "throw away",
                "vacuum sealed", "vacuum bag", "keep away from children",
            )
        )

        v_state, s_conf = vis_preds.get("sealed_state", ("bare_spool", 0.0))

        if has_bag_text:
            item.sealed = True
            item.sealed_reason = "in vacuum bag"
        elif v_state == "bare_spool" and s_conf >= 0.50:
            item.sealed = False
            item.sealed_reason = "bare spool"
            item.source_type = "spool_label"
        elif v_state == "in_box" and s_conf >= 0.70 and not is_spool:
            item.sealed = True
            item.sealed_reason = "in box"
            item.source_type = "box_label"
        elif v_state == "in_vacuum_bag" and s_conf >= 0.85:
            item.sealed = True
            item.sealed_reason = "in vacuum bag"
        elif is_spool or any(w in joined_ocr_lower for w in ("spool", "reusable spool", "tare", "flange")):
            # Spool images without explicit vacuum bag text or high-confidence bag packaging are opened spools
            item.sealed = False
            item.sealed_reason = "bare spool"
            item.source_type = "spool_label"
        else:
            item.sealed = True
            item.sealed_reason = "in box"

        item = self.matcher.enrich_item(item)
        return item

    def predict(self, images: list[str | Path | bytes | np.ndarray]) -> IntakeReadResult:
        t0 = time.perf_counter()
        items: list[FilamentItem] = []
        questions: list[str] = []

        for idx, img in enumerate(images):
            it = self.predict_image(img, photo_index=idx)
            # Group photos of identical vendor + line + color into one item with increased count/photos
            merged = False
            for existing in items:
                if (
                    existing.vendor == it.vendor
                    and existing.line == it.line
                    and existing.color_name == it.color_name
                    and existing.sealed == it.sealed
                ):
                    existing.photos.append(idx)
                    merged = True
                    break
            if not merged:
                items.append(it)
            if it.vendor == "Unknown":
                questions.append(f"Photo {idx}: Please confirm filament brand (detected {it.line} {it.color_name}).")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return IntakeReadResult(
            items=items,
            questions=questions,
            tier="cpu-hybrid",
            latency_ms=elapsed_ms,
            model_name="filament-kit-vision-cpu-v1",
        )
