#!/usr/bin/env python3
"""ThunderMax tuning assistant — local LLM Q&A over the shop's tuning docs.

Answers tuning questions using the brain_vault ThunderMax documentation as
grounding context, via a local Ollama server. Can also analyze a .tbw tune
(or a pair of tunes) with thundermax_parser and explain the changes.

Usage:
  tune_assistant.py ask "why do I get decel pops above 4k?"
  tune_assistant.py chat
  tune_assistant.py analyze new.tbw --baseline old.tbw

Requires: ollama serve running on 127.0.0.1:11434 (stdlib only, no pip deps).
"""

import argparse
import datetime
import io
import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

import thundermax_parser as tmx

OLLAMA_URL = "http://127.0.0.1:11434"
# Two-tier local models (see joshua's tower setup):
#   FAST — qwen2.5-coder:14b: fits entirely on the RTX 5060 Ti, ~46 tok/s.
#   BIG  — hermes3:70b: smarter reasoning, but ~0.7 tok/s (mostly CPU).
# By default the assistant auto-routes per question (classify() below);
# --fast / --deep / --model override.
FAST_MODEL = "qwen2.5-coder:14b"
BIG_MODEL = "hermes3:70b"
MODEL = FAST_MODEL  # default model when one is passed explicitly
LOCAL_CORPUS = Path(__file__).resolve().parent.parent / "docs" / "corpus"
NAS_CORPUS = Path("/mnt/nas/ADMIN/brain_vault")
DOCS_DIR = LOCAL_CORPUS if any(LOCAL_CORPUS.glob("*.md")) else NAS_CORPUS
DOC_GLOB = ["*thundermax*", "*thunder_max*"]
MAX_CONTEXT_CHARS = 24000

# Bike-setup profile: what "my setup" means, and which tune base-map IDs belong
# to it. `learn`/`sync` grow the knowledge base and this profile so the
# assistant stays current with new tunes for the same bike.
PROFILE_PATH = Path(__file__).resolve().parent / "bike_profile.json"


def load_profile():
    try:
        return json.loads(PROFILE_PATH.read_text())
    except (OSError, ValueError):
        return {"setup_key": "default", "label": "unknown setup", "base_map_ids": []}


def save_profile(profile):
    PROFILE_PATH.write_text(json.dumps(profile, indent=2) + "\n")


PROFILE = load_profile()

SYSTEM_PROMPT = """\
You are the Throttle Logic ThunderMax tuning assistant for a 2023
Harley-Davidson Low Rider ST with a 131ci build running a ThunderMax TBW
(throttle-by-wire) ECM. You help with AFR targets, VE/fuel-flow tables,
ignition timing, AutoTune gating/zone locking, decel pop fixes, and
validation-ride protocol.

House rules learned from this shop's tuning history:
- AutoTune learning gates: enable above 200F, disable above 280F; cold-start
  and heat-soak trims are garbage and must be blocked.
- Decel pop protocol: closed-throttle decels 4000->2000 rpm in 3rd/4th.
  Pops above 4k -> add +2% VE @ 0-2% TPS, 3840-4608 rpm. Broad-range pops ->
  subtract 1 deg spark @ 0-2% TPS, 2048-2816 rpm.
- Always log TPS, RPM, AFR target vs actual, CHT, trims.
- Heat management: progressive timing retard past 226F to prevent ping
  during heat soak.
- Ping at WOT (belly = midrange TPS): if ping disappears at WOT, lower the
  belly only; if it persists, lower both; if it gets worse, lower WOT more.
- Idle/tip-in smoothness: equalize spark 768-1280 rpm; keep low-TPS
  gradients smooth.
- Alternate decel-pop fix from the Oct 2025 sessions: disable decel fuel
  cut and add +4-6% fuel at 0-2% TPS in the 1792-3328 rpm band.

Tune-diff analyses label each changed region with a named table band and a
confidence tier (from the project's reverse-engineered table map):
- Categories: TIMING (ignition), AFR (targets), AFR_VE / FUEL (fuel & VE
  pages), AUTOTUNE (learned trims - churns on every ride, usually not a
  deliberate edit), METADATA/SHARED (checksums - ignore), UNMAPPED.
- Confidence: high = trust the label; medium = right category, cell-level
  detail unproven; low = located only. Timing scale is ~49 raw units per
  degree (medium confidence). Raw cell values are NOT yet in engineering
  units - never quote AFR/degree numbers from raw deltas; describe the
  direction and location of changes instead.

CRITICAL - this is a FUEL-INJECTED bike: the ThunderMax TBW ECM controls
electronic fuel injection and a throttle-by-wire body. There is NO carburetor,
NO mixture/idle screws, NO choke, NO accelerator pump, NO jets. NEVER suggest
adjusting any of those - they do not exist on this motorcycle. All fueling and
timing changes are made in the ThunderMax maps (VE/fuel-flow pages, AFR
targets, ignition timing, AutoTune) using the TMax software. The engine is
air/oil-cooled - there is NO coolant or radiator; temperature means CHT
(cylinder-head temp), never "coolant temp".

CRITICAL - ThunderMax is the ONLY tuner on this bike, tuned solely with
ThunderMax's own software (TMax / TMaxII). NEVER mention or give steps for
Power Vision, Dynojet, Power Commander, or any other tuner/flashing device -
they are competitors and do NOT apply here; ignore any such references that
appear in the reference excerpts. When asked for "the tune" to fix something,
you ADVISE the specific map changes to make in the ThunderMax software (which
table/cells, the direction of change, the TPS/RPM location) - you do NOT
produce, generate, or flash a tune file. Only the ThunderMax software writes
the .tbw; say so plainly if asked to create or flash one.

Ground answers in the provided reference excerpts when given. Be specific about
table cells (TPS/RPM ranges) and say when a dyno or validation ride is
required. Never guess numbers you cannot support. If the reference excerpts do
not contain what is needed to answer, say so plainly and state what log or data
you would need - do NOT fill the gap with generic carbureted-engine advice.
"""


def find_docs():
    docs = []
    for pat in DOC_GLOB:
        docs.extend(DOCS_DIR.glob(pat))
    return sorted(set(docs))


STOPWORDS = frozenset("""
the and for was are with that this what when where how why our your you did
does not can could should would will about say said from have has had get
its than then them they there their
""".split())


def _passages(text, size=1200):
    """Split a doc into passages on blank-line / heading boundaries, so
    retrieval can surface the most on-point excerpt instead of a doc's head."""
    passages, cur = [], ""
    for b in re.split(r"\n\s*\n", text):
        b = b.strip()
        if not b:
            continue
        if len(cur) + len(b) + 2 <= size:
            cur = f"{cur}\n\n{b}" if cur else b
        elif len(b) <= size:
            if cur:
                passages.append(cur)
            cur = b
        else:
            if cur:
                passages.append(cur)
                cur = ""
            for i in range(0, len(b), size):
                passages.append(b[i:i + size])
    if cur:
        passages.append(cur)
    return passages


# Competing tuners/flashers that do NOT apply to this ThunderMax bike — passages
# mentioning them are down-ranked so they never ground a ThunderMax answer.
# (A real-world test had the assistant walking Joshua through a Power Vision
# flashing workflow, which is the wrong tuner for this bike entirely.)
COMPETITOR_TERMS = ("power vision", "powervision", "power-vision", "dynojet",
                    "power commander", "powercommander", "pv1", "pv-1", "pv3", "pv-3")
COMPETITOR_PENALTY = 0.15


def scored_passages(question, profile=None):
    """Score corpus passages against a question; return [(score, docname,
    passage)] best-first. Extracted from relevant_context so callers that merge
    multiple retrievers (the web UI) get per-passage results with scores.
    `profile` defaults to the module-level PROFILE (CLI behavior unchanged);
    long-running callers pass a fresh load_profile() so setup boosting tracks
    the on-disk profile."""
    words = {w for w in re.findall(r"[a-z]{3,}", question.lower())
             if w not in STOPWORDS}
    if not words:
        return []
    scored = []
    setup_key = (profile if profile is not None else PROFILE).get("setup_key", "")
    for doc in find_docs():
        try:
            text = doc.read_text(errors="replace")
        except OSError:
            continue
        # boost knowledge learned for THIS setup so newly-synced tunes and notes
        # for my bike surface ahead of generic manual pages
        learned = "_learned_" in doc.name
        mine = learned and bool(setup_key) and setup_key in doc.name
        boost = 1.6 if mine else (1.3 if learned else 1.0)
        for passage in _passages(text):
            body = passage.lower()
            # singular fallback so "soaks" still hits a passage that says "soak"
            hits = sum(body.count(w) + (body.count(w[:-1]) if w.endswith("s") else 0)
                       for w in words)
            if not hits:
                continue
            distinct = sum(1 for w in words if w in body)
            # breadth dominates raw frequency: a passage covering many of the
            # question's terms beats one spamming a single common term
            score = (distinct * 10 + hits) * boost
            # down-rank competing-tuner content (Power Vision / Dynojet / Power
            # Commander) so it never becomes the grounding for a ThunderMax
            # answer. Applied here rather than in relevant_context because this
            # is now the single scoring path — the web UI's keyword leg reads
            # it too, and the tainted corpus docs would otherwise leak straight
            # into the co-pilot's citations.
            if any(term in body for term in COMPETITOR_TERMS):
                score *= COMPETITOR_PENALTY
            scored.append((score, doc.name, passage.strip()))
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored


def relevant_context(question, budget=MAX_CONTEXT_CHARS):
    """Chunk-level retrieval: score individual passages by keyword overlap and
    pack the best excerpts first. Rewarding breadth of match (how many distinct
    query terms a passage touches) keeps an on-topic paragraph ahead of a doc
    that merely repeats one common word (e.g. 'rpm') many times - the failure
    that let a surge question pull generic timing pages instead of the answer."""
    scored = scored_passages(question)
    parts, used, seen = [], 0, set()

    # A symptom question gets the DETERMINISTIC remedy first, above anything
    # retrieval found. Keyword scoring cannot be trusted with this class of
    # question: asked "how do I fix pinging" it returns a generic manual
    # chapter, because a chapter that says "timing" forty times outscores the
    # one paragraph that says what to change. Guessing wrong here costs an
    # engine, not an afternoon.
    #
    # advisor.py is guardrail-filtered and offline, so this block is the same
    # answer `tmax fix` gives, and it is prepended rather than merged so the
    # model cannot rank it away.
    try:
        import advisor
        keys = advisor.match(question)
        if keys:
            blocks = []
            for k in keys[:2]:
                a = advisor.advise(k)
                blocks.append(advisor.render(a))
            head = ("=== HOUSE REMEDY (deterministic, guardrail-checked — "
                    "prefer this over anything below) ===\n\n"
                    + "\n\n---\n\n".join(blocks))
            parts.append(head)
            used += len(head)
    except Exception:
        pass    # the assistant must still answer if the advisor is unavailable

    for _, name, passage in scored:
        key = (name, passage[:80])
        if key in seen:
            continue
        seen.add(key)
        block = f"--- {name} ---\n{passage}"
        if used + len(block) > budget:
            continue  # skip; a smaller later passage may still fit
        parts.append(block)
        used += len(block)
        if used > budget * 0.92:
            break
    return "\n\n".join(parts)


# Ollama on this tower runs with OLLAMA_CONTEXT_LENGTH=4096 (see
# `systemctl show ollama -p Environment`). Anything past that window is dropped
# SERVER-SIDE, silently, with no error: we pack up to MAX_CONTEXT_CHARS (24 000
# chars ~ 6 900 tokens) of retrieved material, so most of it — often including
# the one excerpt that actually answers the question — never reached the model.
# Symptom was correct retrieval and vague or invented answers. So size num_ctx
# to the prompt we are actually sending.
CHARS_PER_TOKEN = 3.5      # conservative for English prose + markdown
CTX_ANSWER_TOKENS = 1024   # headroom the reply itself needs
CTX_MIN = 4096
CTX_MAX_BIG = 8192         # the 70b is already ~0.7 tok/s; a huge window hurts
CTX_MAX = 16384


def ctx_for(messages, model=None):
    """Pick num_ctx for this prompt, clamped by what the model can afford."""
    chars = sum(len(m.get("content", "")) for m in messages)
    need = int(chars / CHARS_PER_TOKEN) + CTX_ANSWER_TOKENS
    cap = CTX_MAX_BIG if model and model == BIG_MODEL else CTX_MAX
    n = CTX_MIN
    while n < need and n < cap:
        n *= 2
    return min(max(n, CTX_MIN), cap)


def stream_chat(messages, model=MODEL):
    """Yield assistant content tokens from Ollama /api/chat (streaming).
    Shared by the CLI printer (ollama_chat) and the HTTP API (api_server)."""
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({"model": model, "messages": messages, "stream": True,
                         "options": {"num_ctx": ctx_for(messages, model)}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            part = json.loads(line)
            token = part.get("message", {}).get("content", "")
            if token:
                yield token
            if part.get("done"):
                break


def ollama_chat(messages, model=MODEL, stream=True):
    reply = []
    for token in stream_chat(messages, model):
        reply.append(token)
        if stream:
            sys.stdout.write(token)
            sys.stdout.flush()
    if stream:
        sys.stdout.write("\n")
    return "".join(reply)


def classify(question):
    """Route a question: EASY grounded lookups -> FAST_MODEL,
    HARD open-ended tuning strategy / deep reasoning -> BIG_MODEL.
    Falls back to FAST_MODEL if the classifier is unavailable."""
    sysmsg = (
        "You are a routing classifier for a motorcycle tuning assistant. "
        "Reply with EXACTLY one word: EASY or HARD. "
        "HARD = open-ended tuning strategy, multi-step diagnosis, or deep "
        "reasoning beyond a direct lookup. EASY = quick factual lookups, "
        "definitions, or straightforward questions answerable from docs."
    )
    body = {
        "model": FAST_MODEL,
        "system": sysmsg,
        "prompt": "Classify this request:\n\n" + question,
        "stream": False,
        "options": {"num_predict": 3, "temperature": 0},
    }
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read()).get("response", "").upper()
        return BIG_MODEL if "HARD" in out else FAST_MODEL
    except Exception:
        return FAST_MODEL


def resolve_model(args, question, prefer_big=False):
    """Decide which model to use: explicit flags win, else auto-route."""
    if getattr(args, "deep", False):
        return BIG_MODEL
    if getattr(args, "fast", False):
        return FAST_MODEL
    if getattr(args, "model", None):
        return args.model
    if prefer_big:
        return BIG_MODEL
    return classify(question)


def pick_model(mode="auto", question=""):
    """Model selection for API callers. mode: 'auto' (classify), 'fast', 'deep'."""
    if mode == "fast":
        return FAST_MODEL
    if mode == "deep":
        return BIG_MODEL
    return classify(question)


def announce(model):
    """Tell the user which model answered, and warn when it's the slow one."""
    if model == BIG_MODEL:
        sys.stderr.write(
            "\033[35m🧠 Hermes 70B\033[0m (deep-think — slow on this box, be "
            "patient; use --fast to force the quick model)\n")
    else:
        sys.stderr.write(f"\033[36m⚡ {model}\033[0m (fast, on GPU)\n")
    sys.stderr.flush()


def build_messages(question, extra_context=""):
    context = relevant_context(question)
    user = question
    blocks = [b for b in (extra_context, context) if b]
    if blocks:
        user = (
            "Reference material:\n\n" + "\n\n".join(blocks) +
            f"\n\nQuestion: {question}"
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def cmd_ask(args):
    model = resolve_model(args, args.question)
    announce(model)
    ollama_chat(build_messages(args.question), model=model)


def cmd_chat(args):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    print("ThunderMax tuning assistant. Ctrl-D or 'quit' to exit.")
    first = True
    while True:
        try:
            q = input("\nyou> ").strip()
        except EOFError:
            break
        if not q or q.lower() in ("quit", "exit"):
            break
        content = q
        if first:
            ctx = relevant_context(q)
            if ctx:
                content = f"Reference material:\n\n{ctx}\n\nQuestion: {q}"
            first = False
        messages.append({"role": "user", "content": content})
        model = resolve_model(args, q)
        announce(model)
        print("assistant> ", end="", flush=True)
        answer = ollama_chat(messages, model=model)
        messages.append({"role": "assistant", "content": answer})


def _diff_facts(baseline, new):
    """Compact, classified fact block for the LLM: categories, not raw hex."""
    import table_map
    rows = table_map.classify_diff(baseline, new)
    lines = [f"Baseline: {Path(baseline).name}", f"New tune: {Path(new).name}"]
    if not rows:
        lines.append("The two tunes are byte-identical.")
        return "\n".join(lines)
    interesting = [r for r in rows
                   if r["category"] not in ("METADATA", "SHARED")]
    lines.append("\nChanged table bands (from the reverse-engineered map):")
    for cat, v in sorted(table_map.summarize(interesting).items(),
                         key=lambda kv: -kv[1]["bytes"]):
        conf = ",".join(sorted(c for c in v["confidences"] if c != "-"))
        lines.append(f"- {cat}: {v['bytes']} changed bytes in "
                     f"{v['regions']} regions (confidence: {conf or 'unmapped'})")
    named = {}
    for r in interesting:
        if r["band"]:
            # first sentence only: the rest describes how the band was
            # discovered, which the LLM tends to misread as part of THIS diff
            short = r["desc"].split(". ")[0]
            named.setdefault(r["band"], [0, short, r["confidence"]])[0] += r["changed_bytes"]
    lines.append("\nBands touched:")
    for band, (n, desc, conf) in sorted(named.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"- {band} ({conf}, {n} bytes): {desc}")
    skipped = sum(r["changed_bytes"] for r in rows) - sum(
        r["changed_bytes"] for r in interesting)
    if skipped:
        lines.append(f"\n({skipped} changed bytes of checksum/metadata churn "
                     "omitted - not tuning content.)")
    return "\n".join(lines)


def cmd_analyze(args):
    tbw = tmx.TbwFile(args.file)
    buf = io.StringIO()
    buf.write(f"Tune file: {tbw.path.name}\n")
    for line in tbw.integrity_lines():
        buf.write(line + "\n")
    if args.baseline:
        buf.write("\n" + _diff_facts(args.baseline, args.file) + "\n")
    facts = buf.getvalue()
    print(facts)
    print("\n--- assistant read ---\n")
    question = (
        "Explain what this tune analysis means for the rider and what to "
        "validate on the next ride."
        if not args.baseline
        else "Describe ONLY what the tune-diff facts above show was changed - "
        "do not invent features or modes that are not listed. Explain what "
        "the rider should feel from those specific changes and what to "
        "validate on the next ride per the house protocol."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"Tune-diff facts (authoritative - describe only these):\n\n"
            f"{facts}\n\n{question}"},
    ]
    # Tune-diff interpretation is high-stakes and infrequent -> default to the
    # 70B deep-thinker unless the user overrides with --fast/--model.
    model = resolve_model(args, question, prefer_big=True)
    announce(model)
    ollama_chat(messages, model=model)


# ----------------------------------------------------------------------------
# Knowledge base: teach the assistant + keep it current with new tunes.
# ----------------------------------------------------------------------------

def _slug(s, n=48):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s[:n].strip("-") or "note")


def _today():
    return datetime.date.today().isoformat()


def learn_write(title, content, source="manual", setup=None, profile=None):
    """Write a new knowledge doc into the corpus, tagged to a setup, and return
    its path. The `thundermax_learned_` name matches DOC_GLOB so find_docs()
    picks it up, and the `_learned_` marker earns a retrieval boost."""
    setup = setup or (profile if profile is not None else PROFILE).get("setup_key", "default")
    path = DOCS_DIR / f"thundermax_learned_{setup}_{_today()}_{_slug(title)}.md"
    path.write_text(
        f"---\ntype: learned\nsetup: {setup}\ntitle: {title}\n"
        f"source: {source}\ndate: {_today()}\n---\n\n{content.strip()}\n"
    )
    return path


def cmd_learn(args):
    setup = args.setup or PROFILE.get("setup_key")
    if args.file:
        content = Path(args.file).read_text(errors="replace")
        title = args.title or Path(args.file).stem
        source = args.file
    else:
        content = args.text if args.text else sys.stdin.read()
        title = args.title or " ".join(content.split()[:8])
        source = "manual"
    if not content.strip():
        print("learn: nothing to add (give text, --file, or pipe stdin)", file=sys.stderr)
        return 1
    path = learn_write(title, content, source=source, setup=setup)
    print(f"learned -> {path.name}  (setup: {setup})")
    return 0


def _tune_doc(t, path, baseline=None, profile=None):
    """Summarize a matching tune into a knowledge doc (decoded facts, no raw
    dump). Includes a classified diff vs a baseline when one is given."""
    lines = [f"Tune file: {path.name}", f"Base map ID: {t.base_map_id}",
             f"Valid: {t.valid}"]
    lines += list(t.integrity_lines())
    if baseline:
        try:
            lines.append("\n" + _diff_facts(baseline, str(path)))
        except Exception as e:
            lines.append(f"\n(diff vs baseline failed: {e})")
    body = ("This tune matches MY setup (its base-map ID is in the bike profile), "
            "so it is folded into the knowledge base to keep tuning advice current "
            "for this bike.\n\n" + "\n".join(lines))
    return learn_write(f"tune {path.stem} ({t.base_map_id})", body,
                       source=str(path),
                       setup=(profile if profile is not None else PROFILE).get("setup_key"))


def sync_folder(path, baseline=None, profile=None):
    """Fold tunes matching MY setup into the KB. Returns structured results
    (shared by the CLI `sync` command and the HTTP API)."""
    prof = profile if profile is not None else PROFILE
    root = Path(path)
    tbws = [root] if root.suffix.lower() == ".tbw" else sorted(root.rglob("*.tbw"))
    if not tbws:
        return {"ok": False, "error": f"no .tbw files under {root}",
                "matched": [], "skipped": []}
    known = set(prof.get("base_map_ids", []))
    adopted = None
    if baseline:
        try:
            known.add(tmx.TbwFile(baseline).base_map_id)
        except Exception:
            pass
    if not known:  # bootstrap: adopt the most common id present as "my setup"
        ids = []
        for f in tbws:
            try:
                ids.append(tmx.TbwFile(f).base_map_id)
            except Exception:
                pass
        if ids:
            adopted = Counter(ids).most_common(1)[0][0]
            known.add(adopted)
    matched, skipped, seen_ids = [], [], set()
    for f in tbws:
        try:
            t = tmx.TbwFile(f)
        except Exception as e:
            skipped.append({"file": f.name, "reason": f"unreadable: {e}"})
            continue
        if t.base_map_id in known:
            doc = _tune_doc(t, f, baseline=baseline, profile=prof)
            matched.append({"file": f.name, "base_map_id": t.base_map_id, "doc": doc.name})
            seen_ids.add(t.base_map_id)
        else:
            skipped.append({"file": f.name, "base_map_id": t.base_map_id})
    # Read-modify-write against the FRESH on-disk profile: a stale in-memory
    # copy (long-running server vs a concurrent CLI sync) must never clobber
    # base-map ids the other process just added.
    current = load_profile()
    all_ids = sorted(set(current.get("base_map_ids", [])) | known | seen_ids)
    if all_ids != sorted(current.get("base_map_ids", [])):
        current["base_map_ids"] = all_ids
        save_profile(current)
    prof["base_map_ids"] = all_ids
    return {"ok": True, "adopted": adopted, "matched": matched,
            "skipped": skipped, "base_map_ids": all_ids}


def cmd_sync(args):
    res = sync_folder(args.path, baseline=args.baseline)
    if not res["ok"]:
        print(f"sync: {res['error']}", file=sys.stderr)
        return 1
    if res.get("adopted"):
        print(f"sync: no setup base-map on record; adopted most common id as MY setup: {res['adopted']}")
    print(f"\nsync: {len(res['matched'])} tune(s) matched MY setup -> added to the knowledge base.")
    for m in res["matched"]:
        print(f"  + {m['file']}  ->  {m['doc']}")
    if res["skipped"]:
        print(f"sync: {len(res['skipped'])} tune(s) skipped (different setup / other base-map id):")
        for s in res["skipped"][:10]:
            print(f"  - {s['file']}  ({s.get('base_map_id', s.get('reason', ''))})")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="ThunderMax tuning assistant (Ollama-backed)")
    p.add_argument("--model", default=None,
                   help="force a specific Ollama model (overrides auto-routing)")
    p.add_argument("--fast", action="store_true",
                   help=f"force the fast GPU model ({FAST_MODEL})")
    p.add_argument("--deep", action="store_true",
                   help=f"force the 70B deep-think model ({BIG_MODEL})")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("ask", help="one-shot question")
    pa.add_argument("question")
    pa.set_defaults(func=cmd_ask)

    pc = sub.add_parser("chat", help="interactive session")
    pc.set_defaults(func=cmd_chat)

    pz = sub.add_parser("analyze", help="analyze a .tbw (optionally vs a baseline)")
    pz.add_argument("file")
    pz.add_argument("--baseline")
    pz.set_defaults(func=cmd_analyze)

    pl = sub.add_parser("learn", help="add knowledge to the KB, tagged to your setup")
    pl.add_argument("text", nargs="?", help="the note to remember (or use --file / stdin)")
    pl.add_argument("--file", help="ingest a file's contents as knowledge")
    pl.add_argument("--title", help="title for the knowledge doc")
    pl.add_argument("--setup", help="setup key to tag (default: your bike profile)")
    pl.set_defaults(func=cmd_learn)

    py = sub.add_parser("sync", help="scan a tunes folder; fold tunes matching your setup into the KB")
    py.add_argument("path", help="folder (or a single .tbw) to scan")
    py.add_argument("--baseline", help="a known .tbw of your setup to match/diff against")
    py.set_defaults(func=cmd_sync)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
