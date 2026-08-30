#!/usr/bin/env bash
# verify.sh — self-verification entry point for thundermax-assistant.
#
# Checks only what is genuinely checkable without hardware, NAS access,
# or a running Ollama server:
#   1. Required project files exist
#   2. Python sources byte-compile (syntax check)
#   3. thundermax_parser imports cleanly and its CLI responds to --help
#   4. docs/corpus contains at least one markdown doc
#
# Read-only: never creates or modifies any .tbw file (or anything else).
# Exit code 0 = all checks passed.

set -u
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

FAIL=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAIL=1; }

echo "== thundermax-assistant verify =="
echo "project root: $PROJECT_ROOT"
echo

# 1. Required files
for f in README.md COLLABORATION.md tmax src/thundermax_parser.py \
         src/tune_assistant.py src/table_map.py src/tables.json \
         src/ecm_state.py data/ecm_state.json tests/test_ecm_state.py \
         tests/test_thundermax.py; do
  if [ -f "$f" ]; then pass "exists: $f"; else fail "missing: $f"; fi
done
if [ -x tmax ]; then pass "executable: tmax"; else fail "tmax not executable (chmod +x tmax)"; fi

# 2. Python syntax check (compile only, nothing executed)
for py in src/*.py; do
  if python3 -m py_compile "$py" 2>/dev/null; then
    pass "syntax ok: $py"
  else
    fail "syntax error: $py"
    python3 -m py_compile "$py" 2>&1 | sed 's/^/      /'
  fi
done

# 3. Parser imports and CLI answers --help (stdlib only, no network/NAS)
if python3 -c "import sys; sys.path.insert(0, 'src'); import thundermax_parser" 2>/dev/null; then
  pass "import ok: thundermax_parser"
else
  fail "import failed: thundermax_parser"
  python3 -c "import sys; sys.path.insert(0, 'src'); import thundermax_parser" 2>&1 | sed 's/^/      /'
fi

if python3 src/thundermax_parser.py --help >/dev/null 2>&1; then
  pass "cli ok: thundermax_parser.py --help"
else
  fail "cli error: thundermax_parser.py --help exited non-zero"
fi

# 4. tables.json parses and table_map answers `bands`
if python3 -c "import json; json.load(open('src/tables.json'))" 2>/dev/null; then
  pass "json ok: src/tables.json"
else
  fail "json invalid: src/tables.json"
fi
if python3 src/table_map.py bands >/dev/null 2>&1; then
  pass "cli ok: table_map.py bands"
else
  fail "cli error: table_map.py bands exited non-zero"
fi

# 5. Unit tests (self-contained synthetic tunes; no NAS/Ollama needed)
if python3 -m unittest discover -s tests >/dev/null 2>&1; then
  pass "unit tests: all passing"
else
  fail "unit tests FAILED (run: python3 -m unittest discover -s tests -v)"
fi

# 6. Local corpus present (tune_assistant falls back to NAS otherwise)
md_count=$(find docs/corpus -maxdepth 1 -name '*.md' 2>/dev/null | wc -l)
if [ "$md_count" -ge 1 ]; then
  pass "corpus: $md_count markdown doc(s) in docs/corpus"
else
  fail "corpus: no markdown docs found in docs/corpus"
fi

# Informational only — not a failure: the assistant needs Ollama at runtime,
# but its absence doesn't mean the repo is broken.
if curl -sf -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  echo "INFO  ollama server reachable on 127.0.0.1:11434"
else
  echo "INFO  ollama server not reachable (only needed for tune_assistant.py)"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: all checks passed"
else
  echo "RESULT: one or more checks FAILED"
fi
exit "$FAIL"
