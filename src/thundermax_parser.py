#!/usr/bin/env python3
"""ThunderMax .tbw tune-file parser.

Parses the proprietary ThunderMax TBW binary format (M8 TBW module family)
far enough to be useful in a tuning workflow:

  * integrity / signature validation
  * base-map ID extraction (length-prefixed ASCII at offset 0x10)
  * header word decode
  * region-level comparison of two tunes (what changed, where, by how much)
  * decode-report generation (markdown, same layout as the shop's existing
    FINALALLAROUNDGOODTUNEVatlV2_decode_report.md)
  * batch scan of a directory of .tbw files into an index table

Format knowledge (reverse-engineered from real tunes off the shop NAS):

  offset 0x00  u32 LE   unknown (0x87 in all observed files)
  offset 0x04  u32 LE   0x4000 in all observed files
  offset 0x08  u32 LE   1 in all observed files
  offset 0x0c  u32 LE   0x147 in all observed files (possibly section count)
  offset 0x10  u8       length of base-map ID (0x10 = 16 in all observed)
  offset 0x11  ASCII    base-map ID, e.g. ZZSSQXETDN100720, HYSSPVCAHN051320
  0x21..~0x360          per-section metadata / checksums (changes whenever
                        any table changes)
  beyond                table data; observed 16-byte-stride rows of LE u16
                        (value, flag) pairs in timing regions

All observed files are exactly 214470 bytes. Values in tables are raw device
units; scaling to engineering units (degrees, AFR) is NOT yet confirmed, so
compare output reports raw deltas alongside a few candidate scalings.

Numeric grid extraction to engineering units still requires TMax Tuner
export - see the capture checklist in generated reports.
"""

import argparse
import datetime
import string
import struct
import sys
from pathlib import Path

try:  # optional: label changed regions with named tables (tables.json)
    import table_map
    _TABLES = table_map.load_map()
except Exception:  # module or json missing / malformed -> degrade gracefully
    table_map = None
    _TABLES = None

EXPECTED_SIZE = 214470
MAP_ID_OFFSET = 0x10
HEADER_WORDS = 4
# Region before this offset changes whenever anything changes (per-section
# metadata / checksums); regions after it are actual table data.
METADATA_BOUNDARY = 0x3C0


class TbwError(Exception):
    pass


class TbwFile:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if len(self.data) < 64:
            raise TbwError(f"{self.path.name}: too small to be a TBW file")
        self.header = struct.unpack_from("<4I", self.data, 0)
        id_len = self.data[MAP_ID_OFFSET]
        raw_id = self.data[MAP_ID_OFFSET + 1 : MAP_ID_OFFSET + 1 + id_len]
        self.base_map_id = raw_id.decode("ascii", errors="replace")

    @property
    def size_ok(self):
        return len(self.data) == EXPECTED_SIZE

    @property
    def id_ok(self):
        return (
            0 < self.data[MAP_ID_OFFSET] <= 32
            and all(c in string.printable for c in self.base_map_id)
            and self.base_map_id.isupper()
        )

    @property
    def valid(self):
        return self.size_ok and self.id_ok

    def integrity_lines(self):
        lines = []
        status = "OK" if self.id_ok else "FAILED"
        lines.append(
            f"- **Header check:** {status} — ASCII signature at 0x0010 → `{self.base_map_id}`"
        )
        if self.size_ok:
            lines.append(f"- **File status:** Valid ThunderMax TBW binary ({len(self.data)} bytes)")
        else:
            lines.append(
                f"- **File status:** UNEXPECTED SIZE {len(self.data)} bytes "
                f"(expected {EXPECTED_SIZE}) — verify this file"
            )
        lines.append("- **Module family:** TBW (M8 compatible)")
        lines.append(
            "- **Header words:** "
            + ", ".join(f"0x{w:X}" for w in self.header)
        )
        return lines


def diff_regions(a, b, gap=48):
    """Contiguous changed-byte regions between two same-size buffers.

    Changes closer together than `gap` bytes are merged into one region.
    Returns list of (start, end_exclusive).
    """
    if len(a.data) != len(b.data):
        raise TbwError("files are different sizes; cannot compare")
    regions = []
    start = None
    last = None
    for i, (x, y) in enumerate(zip(a.data, b.data)):
        if x != y:
            if start is None:
                start = i
            elif i - last > gap:
                regions.append((start, last + 1))
                start = i
            last = i
    if start is not None:
        regions.append((start, last + 1))
    return regions


def region_deltas(a, b, start, end, stride=2, width=2):
    """LE deltas across a changed region, read on the region's OWN grid.

    Alignment matters more than it looks. This used to snap the start back to
    an even FILE offset (`start - start % 2`), which silently corrupted every
    band that begins on an odd offset — and the located bands in tables.json
    mostly do (`0x01989`, `0x01FC9`, `0x0C3C9`). Reading a 4-byte value one
    byte early puts the real low byte in the high position, so the delta comes
    back multiplied by 256.

    Measured on the labelled "-1 degree globally" tune pair: the timing-limit
    array at 0x0C3C9 moved -49 raw in 127 of 128 records (49 raw/degree, which
    is where that scale was derived). The old even-aligned read reported
    -12544 for all 128 — exactly 256x the truth. That wrong number reached
    both `tmax compare` and the web tune-diff.

    `stride`/`width` let a caller read a band on its real record grid
    (e.g. stride=8, width=4 for the timing-limit array). The defaults keep the
    plain u16 sweep for unmapped regions, but now anchored at `start`.
    """
    fmt = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}.get(width)
    if fmt is None:
        raise ValueError(f"unsupported width {width}")
    deltas = []
    for off in range(start, min(end + 1, len(a.data)) - (width - 1), stride):
        va = struct.unpack_from(fmt, a.data, off)[0]
        vb = struct.unpack_from(fmt, b.data, off)[0]
        if va != vb:
            deltas.append(vb - va)
    return deltas


def _grid_for(band, start, end):
    """(start, end, stride, width) to read a changed region on.

    A located band knows its own record layout, so read it on that grid and
    anchor to the band's start — the changed region often begins mid-record.
    An unmapped region falls back to a plain u16 sweep from its own start.
    `stride` in tables.json is the record pitch; the value inside it is 4 bytes
    wide on an 8-byte stride, else the stride itself.
    """
    if not band:
        return start, end, 2, 2
    lo = _band_offset(band, 0)
    stride = int(band.get("stride") or 2)
    width = 4 if stride == 8 else min(stride, 4)
    # step back to the record boundary this region starts inside
    if lo is not None and start >= lo and stride > 1:
        start -= (start - lo) % stride
    return start, end, stride, width


def deltas_for_region(a, b, band, start, end):
    """Deltas for a changed region, read on its band's grid where that works.

    Falls back to the plain u16 sweep when the grid read finds nothing: a
    change can sit inside a record without landing on its boundary (or the
    band's stride in tables.json can simply be wrong), and reporting a changed
    region as having NO deltas would hide it from the diff entirely — a worse
    failure than the misaligned magnitude this grid read exists to fix.
    """
    d = region_deltas(a, b, *_grid_for(band, start, end))
    if d:
        return d
    return region_deltas(a, b, start, end)


def _band_offset(band, idx):
    try:
        return int(band["range"][idx], 16)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def compare(a, b, out=sys.stdout):
    w = out.write
    w(f"# TBW Compare — {a.path.name} → {b.path.name}\n\n")
    w(f"- Base map A: `{a.base_map_id}`\n- Base map B: `{b.base_map_id}`\n")
    if a.base_map_id != b.base_map_id:
        w("- **Note:** different base maps — table-level comparison may not align.\n")
    regions = diff_regions(a, b)
    meta = [r for r in regions if r[0] < METADATA_BOUNDARY]
    tables = [r for r in regions if r[0] >= METADATA_BOUNDARY]
    total = sum(e - s for s, e in regions)
    w(f"- Changed bytes: {total} across {len(regions)} regions "
      f"({len(meta)} metadata/checksum, {len(tables)} table-data)\n\n")
    if not tables:
        w("No table-data changes — files differ only in metadata/checksums.\n")
        return
    w("## Table-data regions\n\n")
    w("| offset | table (confidence) | length | u16 changes | delta min | delta max | delta mode |\n")
    w("|---|---|---|---|---|---|---|\n")
    cat_bytes = {}
    for s, e in tables:
        band = table_map.band_for_offset(s, _TABLES) if _TABLES else None
        d = deltas_for_region(a, b, band, s, e)
        if not d:
            continue
        mode = max(set(d), key=d.count)
        if band:
            label = f"{band['name']} ({band['confidence']})"
            cat = band["category"]
        else:
            label = "unmapped"
            cat = "UNMAPPED"
        cat_bytes[cat] = cat_bytes.get(cat, 0) + (e - s)
        w(f"| 0x{s:05X} | {label} | {e - s} | {len(d)} | {min(d)} | {max(d)} | {mode} |\n")
    if _TABLES and cat_bytes:
        w("\n**Changed bytes by table category:** "
          + ", ".join(f"{c} {n}" for c, n in
                      sorted(cat_bytes.items(), key=lambda kv: -kv[1]))
          + "\n")
    all_d = [x for s, e in tables
             for x in deltas_for_region(
                 a, b, table_map.band_for_offset(s, _TABLES) if _TABLES else None, s, e)]
    if all_d:
        modes = sorted(set(all_d), key=all_d.count, reverse=True)[:3]
        w("\n## Interpretation hints (raw device units)\n\n")
        for m in modes:
            w(f"- delta {m:+d}: {all_d.count(m)}× "
              f"(if ×1/256 scale → {m/256:+.2f}; if ×1/1024 → {m/1024:+.3f})\n")
        w("\nUniform deltas across a whole region usually mean a global "
          "adjustment (e.g. \"+2° timing everywhere\" or a flat VE/fuel "
          "percentage change).\n")


CAPTURE_CHECKLIST = """\
## 2) Capture Checklist (Use TMax Tuner on Windows)
Perform in this exact order so page indices align:
1. **Map Editing → Read Module Maps and Settings** (ensure values shown come from the module/file)
2. **Tuning Maps → AFR Targets vs TPS/RPM** — capture the full grid
3. **Tuning Maps → Front Fuel Flow vs TPS/RPM** — capture grid
4. **Tuning Maps → Rear Fuel Flow vs TPS/RPM** — capture grid
5. **Tuning Maps → Ignition Timing → Timing vs TPS @ RPM** — capture all RPM pages
6. **Tuning Maps → Ignition Timing → Rear Cylinder Timing Offset vs TPS** — capture full TPS axis
7. **Tuning Maps → Ignition Timing → Timing vs Engine Temp** — capture full curve
8. **Tuning Maps → AFR vs Engine Temp** — capture full curve
9. **Module Configuration → Basic Settings** — capture rev limit, speedo/VSS, decel fuel cut, compression releases, idle behavior
10. **Module Configuration → Idle RPM vs Engine Temp** — capture full curve

Tip: If copy/export is unavailable, take clear screenshots at 125–150% zoom so the entire table is readable.
"""

TPS_COLS = ["0%", "2%", "5%", "10%", "15%", "20%", "25%", "30%", "40%", "50%", "60%", "75%", "100%"]
RPM_ROWS = [768, 1024, 1280, 1536, 1792, 2048, 2304, 2560, 2816,
            3072, 3328, 3584, 3840, 4096, 4352, 4608]
TIMING_RPM_PAGES = [1024, 1536, 1792, 2048, 2304, 2560, 2816, 3072,
                    3328, 3584, 3840, 4096, 4352, 4608]


def _grid(cols, rows):
    out = ["| RPM \\ TPS | " + " | ".join(cols) + " |"]
    out.append("|---:|" + ":--:|" * len(cols))
    for r in rows:
        out.append(f"| {r} |" + "  |" * len(cols))
    return "\n".join(out)


def decode_report(tbw, bike="2023 Harley-Davidson Low Rider ST — 131ci, 2-into-1",
                  out=sys.stdout):
    w = out.write
    today = datetime.date.today().isoformat()
    w(f"# ThunderMax Map Decode Report — {tbw.path.name}\n")
    w(f"**Date:** {today}  \n**Author:** Throttle Logic (thundermax-assistant)  \n")
    w(f"**Bike:** {bike}  \n**File:** `{tbw.path.name}`  \n")
    w(f"**Detected Base Map ID (from header):** `{tbw.base_map_id}`\n\n")
    w("> This report captures all the pages and parameters we need from your "
      "tune. Your `.tbw` is a proprietary binary; we verified integrity and "
      "extracted the base-map ID. Numeric table values (AFR, Timing, Fuel) "
      "require in-app export/screenshot. Use the capture steps below and "
      "populate the values into this report.\n\n---\n\n")
    w("## 1) Integrity & Metadata\n")
    for line in tbw.integrity_lines():
        w(line + "\n")
    w("\n---\n\n")
    w(CAPTURE_CHECKLIST)
    w("\n---\n\n## 3) AFR Targets vs TPS/RPM\n_Grid capture goes here._\n\n")
    w(_grid(TPS_COLS, RPM_ROWS))
    w("\n\n---\n\n## 4) Front Fuel Flow vs TPS/RPM\n_Grid capture goes here._\n\n(Same axis as AFR grid.)\n")
    w("\n---\n\n## 5) Rear Fuel Flow vs TPS/RPM\n_Grid capture goes here._\n\n(Same axis as AFR grid.)\n")
    w("\n---\n\n## 6) Ignition Timing — Timing vs TPS @ RPM\n")
    w("_Enter degrees for each RPM page. Use the same TPS headers as above._\n\n")
    for rpm in TIMING_RPM_PAGES:
        w(f"### {rpm} RPM\n")
        w("| TPS% | " + " | ".join(c.rstrip("%") for c in TPS_COLS) + " |\n")
        w("|:--|" + ":--:|" * len(TPS_COLS) + "\n")
        w("| deg |" + "  |" * len(TPS_COLS) + "\n\n")
    w("---\n\n## 7) Rear Cylinder Timing Offset vs TPS\n_Full TPS axis capture goes here._\n")
    w("\n---\n\n## 8) Timing vs Engine Temp\n_Full curve capture goes here._\n")
    w("\n---\n\n## 9) AFR vs Engine Temp\n_Full curve capture goes here._\n")
    w("\n---\n\n## 10) Module Configuration — Basic Settings\n")
    w("- Rev limit: \n- Speedo/VSS: \n- Decel fuel cut: \n- Compression releases: \n- Idle behavior: \n")
    w("\n---\n\n## 11) Idle RPM vs Engine Temp\n_Full curve capture goes here._\n")


def scan(directory, out=sys.stdout):
    w = out.write
    try:
        files = sorted(f for f in Path(directory).glob("*.tbw")
                       if not f.name.startswith("._"))  # skip AppleDouble sidecars
    except OSError as e:
        raise TbwError(f"cannot read {directory}: {e}")
    if not files:
        # refuse to emit an empty index: a stale NAS mount ("Host is down")
        # would otherwise silently clobber a good report written earlier
        raise TbwError(f"no .tbw files found in {directory} - "
                       "is the NAS mounted? refusing to write an empty index")
    w(f"# TBW Index — {directory}\n\n")
    w("| file | base map ID | size | valid |\n|---|---|---|---|\n")
    for f in files:
        try:
            t = TbwFile(f)
            w(f"| {f.name} | `{t.base_map_id}` | {len(t.data)} | "
              f"{'yes' if t.valid else 'CHECK'} |\n")
        except (TbwError, OSError) as e:
            w(f"| {f.name} | — | — | ERROR: {e} |\n")
    w(f"\n{len(files)} files scanned.\n")


def main(argv=None):
    p = argparse.ArgumentParser(description="ThunderMax .tbw tune-file parser")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("info", help="validate a tune and print metadata")
    pi.add_argument("file")

    pr = sub.add_parser("report", help="generate a markdown decode report")
    pr.add_argument("file")
    pr.add_argument("-o", "--output", help="write report to this path")
    pr.add_argument("--bike", default="2023 Harley-Davidson Low Rider ST — 131ci, 2-into-1")

    pc = sub.add_parser("compare", help="diff two tunes region by region")
    pc.add_argument("file_a")
    pc.add_argument("file_b")
    pc.add_argument("-o", "--output", help="write comparison to this path")

    ps = sub.add_parser("scan", help="index every .tbw in a directory")
    ps.add_argument("directory")
    ps.add_argument("-o", "--output", help="write index to this path")

    args = p.parse_args(argv)

    def _out(path):
        return open(path, "w") if path else sys.stdout

    if args.cmd == "info":
        t = TbwFile(args.file)
        print(f"file:        {t.path.name}")
        print(f"base map ID: {t.base_map_id}")
        print(f"size:        {len(t.data)} bytes ({'expected' if t.size_ok else 'UNEXPECTED'})")
        print(f"header:      {' '.join(f'0x{w_:X}' for w_ in t.header)}")
        print(f"valid:       {t.valid}")
        return 0 if t.valid else 1
    if args.cmd == "report":
        t = TbwFile(args.file)
        with _out(args.output) as f:
            decode_report(t, bike=args.bike, out=f)
    elif args.cmd == "compare":
        a, b = TbwFile(args.file_a), TbwFile(args.file_b)
        with _out(args.output) as f:
            compare(a, b, out=f)
    elif args.cmd == "scan":
        # build in memory first: never truncate an existing index file and
        # then fail halfway (e.g. NAS drops mid-scan)
        import io
        buf = io.StringIO()
        scan(args.directory, out=buf)
        with _out(args.output) as f:
            f.write(buf.getvalue())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except (TbwError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
