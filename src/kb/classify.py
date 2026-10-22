"""File-type detection. Sniff contents; never assume a name is enough."""

from pathlib import Path

TEXT_EXT = {".md", ".txt", ".rst"}
TABLE_EXT = {".csv", ".tsv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
DOC_EXT = {".pdf", ".docx", ".doc"}
EXPORT_EXT = {".json", ".xml"}
SKIP_PREFIX = ("._", ".ds_store")

DATALOG_HINTS = ("rpm", "tps", "afr", "cht", "map", "iat", "trim", "throttle")


def _head_text(path, n=4096):
    try:
        return path.read_bytes()[:n].decode("utf-8", errors="replace")
    except OSError:
        return ""


def classify_file(path):
    """Return (file_type, mime_guess, notes)."""
    path = Path(path)
    name = path.name
    lower = name.lower()
    if name.startswith("._") or lower == ".ds_store":
        return "skip", "application/x-appledouble", "sidecar"
    ext = path.suffix.lower()
    if ext == ".tbw":
        return "tbw", "application/octet-stream", "thundermax-binary"
    if ext in IMAGE_EXT:
        return "screenshot", f"image/{ext.lstrip('.')}", "image"
    if ext == ".pdf":
        return "manual", "application/pdf", "pdf"
    if ext in TABLE_EXT:
        head = _head_text(path, 2000).lower()
        if any(h in head for h in DATALOG_HINTS):
            return "datalog", "text/csv", "datalog-csv"
        return "map", "text/csv", "table-csv"
    if ext in TEXT_EXT:
        head = _head_text(path, 1500).lower()
        if "type: learned" in head or "historical note" in head:
            return "notes", "text/markdown", "learned-note"
        if any(w in lower for w in ("manual", "guide", "reference", "protocol")):
            return "manual", "text/markdown", "manual-md"
        return "document", "text/markdown", "markdown"
    if ext == ".json":
        return "official_export", "application/json", "json-export"
    if ext in {".bin", ".dat"}:
        return "other", "application/octet-stream", "unknown-binary"
    if not ext:
        return "other", "application/octet-stream", "no-extension"
    return "other", "application/octet-stream", ext.lstrip(".")


def should_skip(path):
    path = Path(path)
    name = path.name.lower()
    if name.startswith("._") or name in {".ds_store", ".keep"}:
        return True
    if path.is_dir():
        return True
    return False
