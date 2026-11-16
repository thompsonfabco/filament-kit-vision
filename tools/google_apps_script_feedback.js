/**
 * Write-only Google Apps Script Web App for spoolwarden-vision user feedback bundles.
 *
 * Setup:
 *   1. Create a private quarantine folder in Google Drive (shared with NOBODY). Copy its folder ID.
 *   2. Go to https://script.google.com -> New Project -> paste this file.
 *   3. Set QUARANTINE_FOLDER_ID below to your private quarantine folder ID.
 *   4. Deploy -> New deployment -> Web app:
 *      - Execute as: Me
 *      - Who has access: Anyone
 *
 * Security:
 *   - Only doPost(e) is exposed; there is no doGet() listing or reading files.
 *   - Enforces a 25 MB decoded ZIP cap and requires a valid ZIP header (PK\x03\x04).
 *   - Validates ZIP shape: must contain pending_feedback.xml and only flat .jpg/.jpeg images.
 *   - Verifies SHA-256 package digest and 20-bit Proof-of-Work over package_sha256:public_key_b64:nonce.
 *   - Enforces a public-key blocklist and daily upload count/byte caps in ScriptProperties.
 *   - Writes both the .zip and a .meta.json sidecar into the quarantine folder for offline verification.
 */

const QUARANTINE_FOLDER_ID = "REPLACE_WITH_PRIVATE_DRIVE_FOLDER_ID";
const MAX_ZIP_BYTES = 25 * 1024 * 1024;
const POW_DIFFICULTY = 20;
const MAX_DAILY_COUNT = 50;
const MAX_DAILY_BYTES = 200 * 1024 * 1024;
const BLOCKED_PUBLIC_KEYS = [
  // Add base64 Ed25519 public keys to block here, or set ScriptProperty BLOCKED_PUBLIC_KEYS (JSON array)
];

function bytesToHex(bytes) {
  return bytes
    .map(function (b) {
      const v = b < 0 ? b + 256 : b;
      return (v < 16 ? "0" : "") + v.toString(16);
    })
    .join("");
}

function hasLeadingZeroBits(bytes, bits) {
  if (bits <= 0) return true;
  const fullBytes = Math.floor(bits / 8);
  const remBits = bits % 8;
  for (let i = 0; i < fullBytes; i++) {
    const v = bytes[i] < 0 ? bytes[i] + 256 : bytes[i];
    if (v !== 0) return false;
  }
  if (remBits > 0) {
    const v = bytes[fullBytes] < 0 ? bytes[fullBytes] + 256 : bytes[fullBytes];
    const mask = (0xff << (8 - remBits)) & 0xff;
    if ((v & mask) !== 0) return false;
  }
  return true;
}

function getBlockedKeySet() {
  const blocked = {};
  for (let i = 0; i < BLOCKED_PUBLIC_KEYS.length; i++) {
    blocked[String(BLOCKED_PUBLIC_KEYS[i]).trim()] = true;
  }
  try {
    const prop = PropertiesService.getScriptProperties().getProperty("BLOCKED_PUBLIC_KEYS");
    if (prop) {
      const arr = JSON.parse(prop);
      if (Array.isArray(arr)) {
        for (let i = 0; i < arr.length; i++) {
          blocked[String(arr[i]).trim()] = true;
        }
      }
    }
  } catch (err) {
    // Ignore malformed ScriptProperty
  }
  return blocked;
}

function checkAndUpdateDailyQuota(incomingBytes) {
  const props = PropertiesService.getScriptProperties();
  const today = new Date().toISOString().slice(0, 10);
  const storedDate = props.getProperty("QUOTA_DATE") || "";
  let count = storedDate === today ? parseInt(props.getProperty("QUOTA_COUNT") || "0", 10) : 0;
  let totalBytes = storedDate === today ? parseInt(props.getProperty("QUOTA_BYTES") || "0", 10) : 0;

  if (count + 1 > MAX_DAILY_COUNT) {
    return { ok: false, error: "Daily submission count cap reached" };
  }
  if (totalBytes + incomingBytes > MAX_DAILY_BYTES) {
    return { ok: false, error: "Daily submission byte cap reached" };
  }
  props.setProperties({
    QUOTA_DATE: today,
    QUOTA_COUNT: String(count + 1),
    QUOTA_BYTES: String(totalBytes + incomingBytes),
  });
  return { ok: true };
}

function validateZipShape(blob) {
  const entries = Utilities.unzip(blob);
  let hasXml = false;
  for (let i = 0; i < entries.length; i++) {
    const name = String(entries[i].getName() || "");
    if (!name || name.indexOf("/") !== -1 || name.indexOf("\\") !== -1 || name.indexOf("..") !== -1) {
      return { ok: false, error: "Unsafe or nested path in ZIP: " + name };
    }
    if (name === "pending_feedback.xml") {
      hasXml = true;
    } else if (!/\.jpe?g$/i.test(name)) {
      return { ok: false, error: "Unexpected non-JPEG file in ZIP: " + name };
    }
  }
  if (!hasXml) {
    return { ok: false, error: "ZIP missing pending_feedback.xml" };
  }
  return { ok: true, entryCount: entries.length };
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return jsonResp({ ok: false, error: "Missing POST body" });
    }
    const body = JSON.parse(e.postData.contents);
    const b64 = String(body.zip_b64 || "");
    const pubKey = String(body.public_key_b64 || "").trim();
    const sigB64 = String(body.signature_b64 || "").trim();
    const powNonce = body.pow_nonce;
    if (!b64 || !pubKey || !sigB64 || powNonce === undefined || powNonce === null) {
      return jsonResp({ ok: false, error: "Missing zip_b64, public_key_b64, signature_b64, or pow_nonce" });
    }

    const blocked = getBlockedKeySet();
    if (blocked[pubKey]) {
      return jsonResp({ ok: false, error: "Public key is blocked" });
    }

    const rawBytes = Utilities.base64Decode(b64);
    if (rawBytes.length === 0 || rawBytes.length > MAX_ZIP_BYTES) {
      return jsonResp({ ok: false, error: "Invalid or oversized ZIP payload" });
    }
    if (rawBytes[0] !== 0x50 || rawBytes[1] !== 0x4b || rawBytes[2] !== 0x03 || rawBytes[3] !== 0x04) {
      return jsonResp({ ok: false, error: "Payload is not a valid ZIP archive" });
    }

    const pkgSha256 = bytesToHex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, rawBytes));
    if (body.package_sha256 && String(body.package_sha256).toLowerCase() !== pkgSha256) {
      return jsonResp({ ok: false, error: "package_sha256 mismatch" });
    }

    const powInput = pkgSha256 + ":" + pubKey + ":" + String(powNonce);
    const powDigest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, powInput, Utilities.Charset.UTF_8);
    if (!hasLeadingZeroBits(powDigest, POW_DIFFICULTY)) {
      return jsonResp({ ok: false, error: "Invalid proof of work" });
    }

    const safeName = String(body.filename || "filament-feedback.zip").replace(/[^A-Za-z0-9._-]/g, "_");
    const blob = Utilities.newBlob(rawBytes, "application/zip", safeName);
    const shape = validateZipShape(blob);
    if (!shape.ok) {
      return jsonResp({ ok: false, error: shape.error });
    }

    const quota = checkAndUpdateDailyQuota(rawBytes.length);
    if (!quota.ok) {
      return jsonResp({ ok: false, error: quota.error });
    }

    const folderId = QUARANTINE_FOLDER_ID.replace(/.*\/folders\//, "").split(/[?#/]/)[0];
    const folder = DriveApp.getFolderById(folderId);
    const file = folder.createFile(blob);

    const meta = {
      filename: safeName,
      file_id: file.getId(),
      received_at: new Date().toISOString(),
      bytes: rawBytes.length,
      entry_count: Number(body.entry_count || 0),
      package_sha256: pkgSha256,
      public_key_b64: pubKey,
      signature_b64: sigB64,
      pow_nonce: powNonce,
      pow_difficulty: POW_DIFFICULTY,
    };
    const metaBlob = Utilities.newBlob(JSON.stringify(meta, null, 2), "application/json", safeName + ".meta.json");
    const metaFile = folder.createFile(metaBlob);

    return jsonResp({
      ok: true,
      file_id: file.getId(),
      meta_file_id: metaFile.getId(),
      bytes: rawBytes.length,
      package_sha256: pkgSha256,
    });
  } catch (err) {
    return jsonResp({ ok: false, error: String(err) });
  }
}

function jsonResp(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
