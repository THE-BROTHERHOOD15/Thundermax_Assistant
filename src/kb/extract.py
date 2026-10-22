"""Safe extraction. Never fabricate decoded values from unreadable binaries."""

import csv
import hashlib
import io
import json
import subprocess
from pathlib import Path

DATALOG_SEGMENTS = (
    ("idle", lambda r, t: r is not None and r < 1200 and (t is None or t < 3)),
    ("high-load", lambda r, t: t is not None and t >= 80),
    ("acceleration", None),
    ("cruise", lambda r, t: t is not None and 3 <= t <= 30 and r is not None and 1800 <= r <= 4000),
    ("warm-up", None),
)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def entropy(data, sample=4096):
    if not data:
        return 0.0
    chunk = data[:sample]
    counts = [0] * 256
    for b in chunk:
        counts[b] += 1
    import math
    n = len(chunk)
    ent = 0.0
    for c in counts:
        if c:
            p = c / n
            ent -= p * math.log2(p)
    return ent


def _col(row, names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            try:
                return float(row[n])
            except (TypeError, ValueError):
                return None
    return None


def extract_text_file(path):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"status": "corrupt", "reason": str(e), "text": None}
    if not text.strip():
        return {"status": "unsupported", "reason": "empty text", "text": ""}
    return {"status": "parsed", "text": text, "reason": None}


def extract_pdf(path):
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"status": "unsupported", "reason": f"pdftotext failed: {e}", "text": None}
    text = proc.stdout or ""
    if len(text.strip()) < 100:
        return {
            "status": "unsupported",
            "reason": "pdf has almost no extractable text (likely scan/image-only); OCR tool required",
            "text": text,
        }
    return {"status": "parsed", "text": text[:200000], "reason": None}


def extract_csv(path, as_datalog=False):
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"status": "corrupt", "reason": str(e), "text": None}
    try:
        rows = list(csv.DictReader(io.StringIO(raw)))
    except csv.Error as e:
        return {"status": "corrupt", "reason": f"csv parse: {e}", "text": raw[:4000]}
    if not rows:
        return {"status": "unsupported", "reason": "csv has no data rows", "text": raw[:4000]}
    if not as_datalog:
        keys = list(rows[0].keys())
        buf = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
        for r in rows[:200]:
            buf.append("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |")
        return {"status": "parsed", "text": "\n".join(buf), "rows": len(rows), "reason": None}

    segs = []
    rpm_names = ("rpm", "RPM", "engine_rpm")
    tps_names = ("tps", "TPS", "throttle", "throttle_pct")
    afr_names = ("afr", "AFR", "afr_actual", "lambda")
    buckets = {name: [] for name, _ in DATALOG_SEGMENTS}
    buckets["anomaly"] = []
    prev_tps = None
    for i, r in enumerate(rows, start=2):
        rpm = _col(r, rpm_names)
        tps = _col(r, tps_names)
        afr = _col(r, afr_names)
        placed = False
        if prev_tps is not None and tps is not None and tps - prev_tps >= 8:
            buckets["acceleration"].append((i, rpm, tps, afr))
            placed = True
        if not placed:
            for name, pred in DATALOG_SEGMENTS:
                if pred and pred(rpm, tps):
                    buckets[name].append((i, rpm, tps, afr))
                    placed = True
                    break
        if afr is not None and (afr < 11.5 or afr > 16.5):
            buckets["anomaly"].append((i, rpm, tps, afr))
        prev_tps = tps if tps is not None else prev_tps

    summaries = []
    for name, pts in buckets.items():
        if not pts:
            continue
        afrs = [p[3] for p in pts if p[3] is not None]
        rpms = [p[1] for p in pts if p[1] is not None]
        summary = (
            f"Segment {name}: rows {pts[0][0]}-{pts[-1][0]}, n={len(pts)}. "
            f"RPM {min(rpms):.0f}-{max(rpms):.0f}. " if rpms else
            f"Segment {name}: rows {pts[0][0]}-{pts[-1][0]}, n={len(pts)}. "
        )
        if afrs:
            summary += f"AFR {min(afrs):.2f}-{max(afrs):.2f} mean {sum(afrs)/len(afrs):.2f}."
        else:
            summary += "No AFR column in this authorized export."
        summaries.append({
            "segment": name,
            "row_start": pts[0][0],
            "row_end": pts[-1][0],
            "summary": summary,
        })
    text = "Datalog segment summaries (raw CSV stored separately; rows are not embedded).\n\n"
    text += "\n".join(s["summary"] for s in summaries) or "No meaningful segments detected."
    return {
        "status": "parsed",
        "text": text,
        "segments": summaries,
        "rows": len(rows),
        "reason": None,
    }


def extract_tbw(path):
    """Use the shop's existing authorized parser for header/base-map ID only.

    Does not invent AFR, timing, or cell values. Unexpected size or a failed
    parse goes to quarantine, not a fake map table.
    """
    try:
        import thundermax_parser as tmx
    except ImportError:
        return {
            "status": "unsupported",
            "reason": "thundermax_parser not importable",
            "text": None,
        }
    try:
        t = tmx.TbwFile(path)
    except Exception as e:
        return {"status": "corrupt", "reason": f"tbw parse failed: {e}", "text": None}
    if not t.size_ok:
        return {
            "status": "unsupported",
            "reason": (
                f"unexpected TBW size {len(t.data)} bytes "
                f"(known {sorted(tmx.KNOWN_SIZES)}); official TMax export required"
            ),
            "text": None,
            "map_id": t.base_map_id,
            "size": len(t.data),
        }
    lines = [
        "ThunderMax TBW original (immutable archive copy).",
        "Numeric map cells are NOT decoded to engineering units.",
        "Do not treat this summary as AFR or timing values.",
        f"Base map ID: {t.base_map_id}",
        f"Valid: {t.valid}",
        f"Size: {len(t.data)} bytes",
        f"Header: {', '.join(f'0x{w:X}' for w in t.header)}",
    ]
    lines.extend(t.integrity_lines())
    return {
        "status": "parsed",
        "text": "\n".join(lines),
        "map_id": t.base_map_id,
        "size": len(t.data),
        "valid": t.valid,
        "reason": None,
    }


def extract_image(path):
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "status": "needs_review",
            "reason": "tesseract not installed; image archived, OCR skipped",
            "text": f"Image {Path(path).name} stored as original. No OCR available.",
        }
    text = (proc.stdout or "").strip()
    if len(text) < 20:
        return {
            "status": "needs_review",
            "reason": "OCR produced almost no text",
            "text": text or f"Image {Path(path).name}: OCR empty.",
        }
    return {"status": "parsed", "text": text, "reason": None}


def extract_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as e:
        return {"status": "corrupt", "reason": f"json: {e}", "text": None}
    text = json.dumps(data, indent=2, ensure_ascii=False)[:80000]
    return {"status": "parsed", "text": text, "reason": None}


def extract_unknown_binary(path):
    data = Path(path).read_bytes()[:8192]
    ent = entropy(data)
    if ent >= 7.5:
        return {
            "status": "unsupported",
            "reason": (
                f"high-entropy binary (entropy {ent:.2f}); likely encrypted or "
                "proprietary. Official ThunderMax export required. Contents unknown."
            ),
            "text": None,
        }
    return {
        "status": "unsupported",
        "reason": "unknown binary format; no authorized parser",
        "text": None,
    }


def extract(path, file_type):
    path = Path(path)
    if file_type == "tbw":
        return extract_tbw(path)
    if file_type == "datalog":
        return extract_csv(path, as_datalog=True)
    if file_type == "map" and path.suffix.lower() in {".csv", ".tsv"}:
        return extract_csv(path, as_datalog=False)
    if file_type in {"screenshot", "image"}:
        return extract_image(path)
    if file_type == "official_export" and path.suffix.lower() == ".json":
        return extract_json(path)
    if path.suffix.lower() == ".pdf":
        return extract_pdf(path)
    if path.suffix.lower() in {".md", ".txt", ".rst", ".csv"}:
        if file_type == "datalog":
            return extract_csv(path, as_datalog=True)
        return extract_text_file(path)
    return extract_unknown_binary(path)
