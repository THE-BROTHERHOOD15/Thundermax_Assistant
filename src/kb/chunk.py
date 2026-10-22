"""Type-aware chunking. Do not chunk every file the same way."""

import re

TOKEN_CHARS = 4
MIN_TOKENS = 400
MAX_TOKENS = 900
OVERLAP_TOKENS = 80


def estimate_tokens(text):
    return max(1, len(text) // TOKEN_CHARS)


def _split_heading_blocks(text):
    parts = re.split(r"(?m)(?=^#{1,6}\s)", text)
    return [p.strip() for p in parts if p.strip()]


def _pack(blocks, max_chars=MAX_TOKENS * TOKEN_CHARS, min_chars=MIN_TOKENS * TOKEN_CHARS):
    packed, cur, heading = [], "", ""
    for block in blocks:
        hm = re.match(r"^(#{1,6}\s+.+)", block)
        block_heading = hm.group(1).strip() if hm else heading
        if not cur:
            cur, heading = block, block_heading
            continue
        if len(cur) + 2 + len(block) <= max_chars:
            cur = cur + "\n\n" + block
            continue
        if len(cur) >= min_chars // 2:
            packed.append((heading, cur))
            overlap = cur[-OVERLAP_TOKENS * TOKEN_CHARS:]
            cur = (overlap + "\n\n" + block) if overlap else block
            heading = block_heading
        else:
            cur = cur + "\n\n" + block
    if cur:
        packed.append((heading, cur))
    return packed


def _hard_wrap(text, heading=""):
    max_chars = MAX_TOKENS * TOKEN_CHARS
    if len(text) <= max_chars:
        return [(heading, text)]
    out = []
    step = max_chars - OVERLAP_TOKENS * TOKEN_CHARS
    i = 0
    while i < len(text):
        out.append((heading, text[i:i + max_chars]))
        i += step
    return out


def chunk_document(text):
    blocks = _split_heading_blocks(text)
    packed = _pack(blocks) if blocks else _hard_wrap(text)
    chunks = []
    for heading, body in packed:
        if estimate_tokens(body) > MAX_TOKENS:
            chunks.extend(
                {"kind": "heading", "heading": h, "text": t}
                for h, t in _hard_wrap(body, heading)
            )
        else:
            chunks.append({"kind": "heading", "heading": heading, "text": body})
    return chunks


def chunk_notes(text, date=None, vehicle=None, project=None):
    blocks = re.split(r"(?m)(?=^#{1,3}\s)|(?=\n\n(?:Symptom|Decision|Result|Lesson)\b)", text)
    blocks = [b.strip() for b in blocks if b.strip()]
    if not blocks:
        blocks = [text.strip()] if text.strip() else []
    packed = _pack(blocks, max_chars=700 * TOKEN_CHARS, min_chars=80)
    out = []
    for heading, body in packed:
        kind = "note"
        low = (heading + " " + body[:80]).lower()
        if "symptom" in low:
            kind = "symptom"
        elif "decision" in low:
            kind = "decision"
        elif "result" in low:
            kind = "result"
        elif "lesson" in low:
            kind = "lesson"
        elif "session" in low:
            kind = "session"
        out.append({
            "kind": kind,
            "heading": heading,
            "text": body,
            "date": date,
            "vehicle": vehicle,
            "project": project,
        })
    return out


def chunk_datalog_segments(segments):
    """segments: list of {segment, row_start, row_end, summary}."""
    out = []
    for seg in segments:
        text = seg.get("summary") or ""
        if not text.strip():
            continue
        out.append({
            "kind": "segment",
            "heading": f"datalog {seg.get('segment', 'window')}",
            "text": text,
            "row_start": seg.get("row_start"),
            "row_end": seg.get("row_end"),
        })
    return out


def chunk_map_summary(summary, table_name=None):
    if not (summary or "").strip():
        return []
    return [{
        "kind": "table",
        "heading": table_name or "map summary",
        "text": summary.strip(),
    }]


def chunk_for_type(file_type, text, **meta):
    text = text or ""
    if file_type in {"notes"}:
        return chunk_notes(text, date=meta.get("date"), vehicle=meta.get("vehicle"),
                           project=meta.get("project"))
    if file_type == "datalog":
        return chunk_datalog_segments(meta.get("segments") or [])
    if file_type in {"tbw", "map", "official_export"}:
        return chunk_map_summary(text, table_name=meta.get("table_name"))
    return chunk_document(text)
