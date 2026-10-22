"""Predictable, sortable archive names.

YYYY-MM-DD__project__vehicle__engine__event__type__version.ext
"""

import re
from datetime import datetime, timezone

NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})__"
    r"(?P<project>[^_]+)__"
    r"(?P<vehicle>[^_]+)__"
    r"(?P<engine>[^_]+)__"
    r"(?P<event>[^_]+)__"
    r"(?P<type>[^_]+)__"
    r"(?P<version>v\d+)\."
    r"(?P<ext>[a-z0-9.]+)$"
)


def slug_field(value, fallback="unk"):
    s = (value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80] or fallback


def archive_name(date, project, vehicle, engine, event, file_type, version, ext):
    if not date:
        date = datetime.now(timezone.utc).date().isoformat()
    ver = version if str(version or "").lower().startswith("v") else f"v{int(version or 1):02d}"
    ext = (ext or "bin").lstrip(".").lower()
    return (
        f"{date}__{slug_field(project)}__{slug_field(vehicle)}__"
        f"{slug_field(engine)}__{slug_field(event)}__{slug_field(file_type)}__"
        f"{ver.lower()}.{ext}"
    )


def parse_archive_name(name):
    m = NAME_RE.match(name.lower())
    return m.groupdict() if m else None


def mtime_date(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date().isoformat()
    except OSError:
        return datetime.now(timezone.utc).date().isoformat()
