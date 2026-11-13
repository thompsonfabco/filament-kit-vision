# filament-kit-vision HTTP API contract, v1 (DRAFT)

Status: draft for agreement with the filament-kit server builder. Nothing here is released.
Owner: the `filament-kit-vision` repo. The kit pins a compatible range; the module never changes v1 in a
breaking way.

## Principles

- **The module reads photos; the kit decides.** The module returns label readings in the kit's item shape. The
  kit owns all post-processing in ONE function shared with its cloud reader: existing-filament match and the
  values that win from it, `match` text, colour hex validation, `count` floor, product lines, the NFC tag rule,
  scale weight, order matching, the daily cap (cloud only). The module never sets `tag`, `scale`, `gross`,
  `match` or order fields.
- **JSON only.** Request and response bodies are `application/json; charset=utf-8`. (The daemon's older XML body,
  `?format=` switch and `image_paths` are not part of the contract; the kit never uses them. `image_paths` lets a
  caller make the daemon open a local file by path and is removed from the network API: see "Module changes".)
- **Unknown fields are ignored** by both sides. Additions never break the other side.
- **Default bind is `127.0.0.1:7915`.** The kit's `local_intake.url` may point elsewhere (another machine with a
  GPU). Protecting that link is the user's choice: an optional bearer token, off by default.

## Versioning

- `api_version` is `"MAJOR.MINOR"`, currently `"1.0"`.
- MINOR bumps add optional request fields, response fields, error codes or tiers. MAJOR bumps change or remove
  something; the path prefix becomes `/v2/` and the module serves `/v1/` alongside it for at least one module
  release.
- The kit stores `min_api = "1.0"` and accepts any `1.x` with `x >= 0`. A `2.x`-only module is refused at install
  and at health check with a clear message ("local reader too new for this kit; update the kit").

## Authentication (optional)

- Off by default. When the module is installed with a token (install option `--token-file`), every route except
  `GET /v1/health` requires `Authorization: Bearer <token>`. Missing or wrong token: `401 unauthorized`.
- The kit stores the token in its secrets file, never in logs, backups of settings or status output.
- `GET /v1/health` stays open so the kit can tell "reader down" from "token wrong"; it reveals no owner data.

## GET /v1/health

Response `200`:

```json
{
  "service": "filament-kit-vision",
  "api_version": "1.0",
  "module_version": "0.1.0",
  "status": "ok",
  "default_tier": "auto",
  "tiers": {
    "cpu": {"usable": true,  "reason": ""},
    "gpu": {"usable": false, "reason": "no CUDA device", "loaded": false}
  },
  "auth_required": false,
  "revise": true,
  "limits": {"max_photos": 8, "max_photo_bytes": 20971520, "max_body_bytes": 67108864},
  "started_at": "2026-11-12T16:40:00Z",
  "last_read_at": null
}
```

- `status`: `ok` (default tier usable) or `degraded` (default tier not usable, e.g. `gpu` chosen but the adapter or
  base model is missing; reads still work on the CPU tier where allowed). The kit shows `degraded` with `reason`.
- `tiers.<name>.usable`: true when a read on that tier would run now (models present, device present).
  `reason`: one short sentence when not usable (`"no CUDA device"`, `"GPU adapter not installed"`,
  `"base model not downloaded"`, `"CUDA out of memory at load"`), else empty string `""`.
- `tiers.gpu.loaded`: the VLM is in VRAM now (it unloads after the idle timeout).
- `auth_required`: whether reads need the bearer token.
- `revise`: boolean, true if the `/v1/revise` endpoint is available.
- `last_read_at`: UTC time of the last successful read, or `null`. Used by the kit's `status` / `doctor`.
- Must answer within 1 s and never load a model.

## POST /v1/read

Request:

```json
{
  "api_version": "1.0",
  "tier": "auto",
  "images_b64": ["<base64 JPEG/PNG/HEIC>", "..."],
  "hint": "user's note or the revise note, may be empty",
  "filaments": [ { "...": "Spoolman /filament objects, unchanged" } ],
  "vendors":   [ { "...": "Spoolman /vendor objects, unchanged" } ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `api_version` | no | The version the kit speaks. Absent = `"1.0"`. A higher MAJOR than the module knows: `400 unsupported_api_version`. |
| `tier` | no | `auto` (default: CPU first, GPU when unsure and usable), `cpu`, or `gpu`. Absent = the module's configured default tier. |
| `images_b64` | yes | 1 to `limits.max_photos` photos, standard base64, in the order the user took them. Photo index `i` in an item's `photos` refers to this list. |
| `hint` | no | Free text from the user (intake note, or the intake-history revise note). The GPU tier puts it in its prompt; the CPU tier MAY use it only to break ties between catalog candidates. Never echoed back. |
| `filaments`, `vendors` | no | The kit's current Spoolman lists, used only to propose `existing_filament_id` and canonical vendor names. The module does not store them. |

Fields the kit must NOT send (the kit handles them itself): `tag`, `scale`, `orders`, `station_state`. If sent they
are ignored.

Response `200`:

```json
{
  "api_version": "1.0",
  "items": [
    {
      "photos": [0, 1],
      "existing_filament_id": 42,
      "vendor": "SUNLU",
      "name": "PLA Matte Charcoal",
      "material": "PLA",
      "line": "PLA Matte",
      "color_hex": "333333",
      "net": 1000,
      "tare": 135,
      "density": 1.24,
      "count": 1,
      "nozzle": 215,
      "bed": 55,
      "nozzle_range": "200-230",
      "bed_range": "50-60",
      "lot": null,
      "note": "box label; SKU read; colour hex estimated",
      "estimates": ["color_hex", "tare"],
      "sealed": true,
      "sealed_reason": "in box",
      "confidence": 0.86,
      "field_confidence": {"vendor": 0.97, "material": 0.99, "line": 0.81, "color": 0.74, "sealed": 0.93}
    }
  ],
  "questions": ["Is the colour Charcoal or Black?"],
  "reader": {
    "service": "filament-kit-vision",
    "module_version": "0.1.0",
    "tier_used": "cpu",
    "escalated": false,
    "fallback_reason": null,
    "model": "cpu-hybrid rf-detr-n+mnv3s+rapidocr",
    "latency_ms": 312.4
  },
  "usage": {"model": "filament-kit-vision/cpu", "input_tokens": 0, "cached": 0, "output_tokens": 0}
}
```

Item fields: exactly the kit's `ITEM_PROPS` (`server/filament_kit/intake.py`), all present, `null` where the kit's
schema allows it, plus:

- `confidence` (0..1): the module's overall confidence for the item. The kit shows it on the result screen and,
  with reader setting `local-then-cloud`, escalates to the cloud reader when any item is below its threshold
  (kit setting, default 0.78) or `vendor` is `"Unknown"`.
- `field_confidence` (optional object, keys among `vendor`, `material`, `line`, `color`, `sealed`, `net`): per-field
  confidence for the edit screen's highlighting. Absent when the tier cannot tell.

Rules the module follows:
- `vendor` is `"Unknown"` and `note` starts with `"BRAND UNKNOWN:"` when the brand cannot be read or inferred.
- `color_hex`: 6 uppercase hex digits without `#`, or `null`.
- `existing_filament_id` is a suggestion from the passed `filaments` list (exact vendor + name or line + colour),
  else `null`. The kit re-checks it and its values win, as for the cloud reader.
- `material` is the base polymer per the kit's rule (every PLA variant is `PLA`, composites keep their code).
- `count >= 1`.
- Items are never empty on success: when nothing could be read, one item with `vendor` `"Unknown"`, low
  `confidence` and a question.
- `usage` keeps the kit's existing shape (token counts 0) so history and logs need no special case.
- `reader.tier_used` is the tier that produced the items; `escalated` is true when `auto` ran the GPU after the CPU;
  `fallback_reason` is set when a requested tier could not run and `auto` fell back (never for an explicit tier).

## POST /v1/revise

Revises a previously read item using a user's text correction note (used for the kit's intake-history revision feature).

Request:

```json
{
  "api_version": "1.0",
  "previous": { "...": "previous item dict from /v1/read" },
  "note": "brand is Bambu Lab, matte charcoal",
  "filaments": [ { "...": "Spoolman /filament objects, optional" } ],
  "vendors": [ { "...": "Spoolman /vendor objects, optional" } ]
}
```

Response `200`:

```json
{
  "api_version": "1.0",
  "item": {
    "vendor": "Bambu Lab",
    "material": "PLA",
    "line": "PLA Matte",
    "name": "PLA Matte Charcoal",
    "confidence": 0.95,
    "field_confidence": {"vendor": 0.95, "material": 0.98, "line": 0.90, "color": 0.90, "sealed": 0.93}
  }
}
```

## Errors

All errors: JSON body `{"error": {"code": "<code>", "message": "<one sentence, no owner data>"}}`.

| HTTP | `error` | When | Kit behaviour |
|---|---|---|---|
| 400 | `bad_request` | Body is not JSON, or a field has the wrong type | Bug: show message, no retry |
| 400 | `no_photos` | `images_b64` empty or missing | Same |
| 400 | `too_many_photos` | More than `limits.max_photos` | Same; the kit should split batches first |
| 400 | `unsupported_api_version` | Request MAJOR higher than the module's | "Update the local reader" |
| 401 | `unauthorized` | Token required and missing/wrong | "Local reader token is wrong" |
| 413 | `too_large` | Body or a photo over the limits | Kit downsizes photos and retries once |
| 415 | `bad_image` | A photo cannot be decoded | Show which photo index in `message` |
| 503 | `tier_unavailable` | `tier` was `gpu` explicitly and the GPU tier is not usable | Kit falls back per its reader setting |
| 503 | `busy` | A GPU read is loading the model or the queue is full; `Retry-After` header in seconds | Kit retries once after `Retry-After` if under its timeout, else falls back |
| 500 | `read_failed` | Anything else; details only in the module's log | Kit falls back per its reader setting |

Kit maps transport failures (connection refused, timeout) to its existing `ReadError("unreachable", ...)`.

## Timing

- CPU tier: typically under 1 s per photo on a small server. GPU tier: first read after idle loads the model
  (up to ~60 s); later reads a few seconds.
- Kit timeout for `/v1/read`: 120 s (setting). `/v1/health`: 3 s.
- Concurrency: the module serialises GPU reads; CPU reads may run in parallel. It answers `503 busy` rather than
  queueing beyond 4 waiting reads.

## Privacy

- The module stores no photos, hints or Spoolman data from requests. Logs carry request id, tier, latency, item
  count and error code only.
- No telemetry, no outbound network from the daemon. (Model downloads happen only in the installer.)

## Contract tests

The module ships `tests/contract/` (planned) runnable against any running instance:
`python -m pytest tests/contract --base-url http://127.0.0.1:7915 [--token-file PATH]`, using bundled synthetic
images only (no owner or web photos). The kit's suite uses its own fake reader that satisfies the same tests.

## Module changes needed to meet this draft (current daemon vs contract)

1. `/v1/health`: add `api_version`, `module_version`, `tiers`, `status: degraded`, `auth_required`, `limits`,
   `last_read_at`; keep the old `tier`/`gpu_available` keys for one release.
2. `/v1/read`: JSON-only path for the kit; drop `image_paths` and the XML `photo path=` form from the network
   handler (keep them in the CLI only); stop echoing `tag`/`scale`.
3. Items: rename `_confidence` to `confidence`; move `_color_name`, `_color_family`, `_source_type` under
   `reader_extra` or drop; add `field_confidence` where available.
4. Explicit `tier: gpu` without a usable GPU: return `503 tier_unavailable` instead of silently using the CPU.
5. Error codes and limits as above; optional bearer token; `busy` with `Retry-After`.
6. `LocalIntakeClient`'s silent in-process fallback is module-internal and not used by the kit.
