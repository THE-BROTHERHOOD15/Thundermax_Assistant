# ThunderMax AI retrieval filesystem

A folder structure alone will not make retrieval fast. This project uses a
**three-layer knowledge base**:

1. **Immutable file archive** — every original map, log, document, and image.
2. **Structured metadata database** — SQLite filters by motorcycle, engine,
   hardware, fuel, firmware, date, and result.
3. **Hybrid retrieval index** — exact keyword (FTS5), metadata filtering, and
   optional `nomic-embed-text` embeddings.

Live data root: `~/thundermax-kb` (override with `TMAX_KB_ROOT`).
Code: `src/kb/` in this repo. The git repo does not store originals.

## Commands

```bash
./tmax kb init
./tmax kb ingest ~/tmax-exchange/tunes-from-usb-2026-08-19 --project-key usb-2026-08-19 --vehicle-key 2023-fxlrst --engine-key 131ci --hardware-key ss550-63inj-2into1
./tmax kb ingest docs/corpus --source corpus_mirror --project-key corpus
./tmax kb retrieve "Can I flash 17AUG onto 6.3 injectors?"
./tmax kb views v_quarantine
./tmax kb eval --no-embed
./tmax kb backup
```

`tmax kb ingest` **copies**. It never writes, patches, or overwrites a `.tbw`.

## Source chain

Every important conclusion must point:

```
answer → knowledge claim → source chunk → normalized derivative → original file
```

If that chain is missing, confidence drops. Unreadable or encrypted files go
to `08_quarantine/` with a reason; they are never treated as known contents.

## Knowledge tiers

| Tier | Meaning |
|------|---------|
| 0 | Raw original |
| 1 | Extracted text/tables/OCR |
| 2 | Annotated metadata |
| 3 | Evidence-backed claims |
| 4 | Validated shop rules |

The assistant may use Tier 4 most confidently. Tier 0 and 1 are evidence, not
conclusions.

## Retrieval order

1. Metadata filter (vehicle, engine, hardware, fuel, firmware, date, validation)
2. SQLite FTS5 (map IDs, fault codes, sensor names, symptoms)
3. Semantic search (`nomic-embed-text`) when embeddings exist
4. Rerank: same vehicle+hardware, then same engine, then validated symptom, then general docs, then unvalidated notes
5. Compact evidence bundle, not whole documents

## Absolute rules

- Never edit `01_originals/`.
- Never silently replace a wrong note — supersede it.
- Separate observation, interpretation, and conclusion.
- Keep unsupported files visible.
- Never fabricate map values, decoded fields, or validation.
