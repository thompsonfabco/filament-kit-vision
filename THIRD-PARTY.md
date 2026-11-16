# Third-Party Licenses and Components

`spoolwarden-vision` is licensed under the **GNU General Public License v3.0 (GPL-3.0)**, matching `filament-kit`.

All bundled model weights, upstream base models, inference runtimes, and dependencies are permissively licensed as documented below.

---

## 1. Machine Learning Models & Weights

| Component | Architecture / Base Model | Upstream Publisher & Link | Upstream License | Distribution Mode |
|---|---|---|---|---|
| **Spool & Label Region Detector** | RF-DETR Nano (`rf-detr-n`) | Roboflow (`github.com/roboflow/rf-detr`) | **Apache-2.0** (`https://www.apache.org/licenses/LICENSE-2.0`) | Bundled ONNX export in release asset |
| **Multi-Head Visual Classifier** | MobileNetV3-Small | PyTorch / torchvision (`github.com/pytorch/vision`) | **Apache-2.0** / **BSD-3-Clause** | Bundled ONNX export in release asset |
| **Spool & Box OCR** | RapidOCR (`rapidocr`) | RapidAI (`github.com/RapidAI/RapidOCR`) | **Apache-2.0** (`https://www.apache.org/licenses/LICENSE-2.0`) | Bundled ONNX models via pip dependency |
| **Text-to-Spoolman Parser** | TF-IDF + Log-Loss Linear | Scikit-Learn (`scikit-learn.org`) / NumPy `.npz` | **BSD-3-Clause** | Pickle-free weights bundled in release asset |
| **GPU Vision-Language Model** | `Qwen/Qwen2.5-VL-7B-Instruct` | Alibaba Cloud Qwen Team (`huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct`) | **Apache-2.0** (`https://www.apache.org/licenses/LICENSE-2.0`) | Base model downloaded directly from Hugging Face at install time; fine-tuned LoRA adapter bundled in optional release asset |

*(Note: No AGPL-3.0 or non-commercial research weights are shipped or required.)*

---

## 2. Software Dependencies & Libraries

| Library | License | Usage in Module |
|---|---|---|
| `onnxruntime` | **MIT** | CPU inference for region detector, visual classifier, and RapidOCR |
| `rapidocr` | **Apache-2.0** | Multi-angle text recognition on box labels and spool stickers |
| `scikit-learn` | **BSD-3-Clause** | Text parser training and feature extraction |
| `rapidfuzz` | **MIT** | Fast C++ fuzzy matching against catalog vendors and lines |
| `defusedxml` | **Python Software Foundation (PSF)** | XML parsing and serialization |
| `pillow` | **Historical Permission Notice and Disclaimer (HPND)** | Image decoding and color space transforms |
| `pillow-heif` | **Apache-2.0 / LGPL-3.0** | Decoder for mobile `.HEIC` photos |
| `numpy` | **BSD-3-Clause** | Array manipulation and polar unwarping |
| `torch` | **BSD-3-Clause** | GPU tier VLM LoRA training and inference |
| `transformers` | **Apache-2.0** | Hugging Face vision-language model pipeline |
| `peft` | **Apache-2.0** | Parameter-efficient fine-tuning (LoRA) |
| `accelerate` | **Apache-2.0** | Device placement and bf16 execution |
| `bitsandbytes` | **MIT** | 4-bit NF4 quantization for 6 GB GPU VRAM compatibility |

---

## 3. Dataset & Data Provenance

1. **Catalog Metadata (`data/catalog/filament_catalog.xml`):**
   - Derived directly from `filament-library/library` (MIT License) and `configs/taxonomy.xml`.
   - Contains no invented color names. Only colors committed to `filament-library` are indexed; all other visual detections report `unknown` with measured LAB hex and family.
2. **Seed Photos (`seed images/`):**
   - Private user-supplied mobile photos. Excluded from git and release packages (`.gitignore`).
3. **Web / Community Harvested Data (`data/harvested/`):**
   - Reference photos harvested for training research. Excluded from git and release packages (`.gitignore`).
4. **Synthetic Domain-Randomized Generator (`data/synthetic/`):**
   - Generated algorithmically by `spoolwarden-vision` data engine. Excluded from release binaries; regenerated on demand.
