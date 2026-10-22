#!/usr/bin/env bash
# Seat wake for Throttle Logic / ThunderMax. Safe, read-only on .tbw.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AGENT_BRIDGE_WHOAMI="${AGENT_BRIDGE_WHOAMI:-grok}"
echo "========================================"
echo " Throttle Logic teach · whoami=${AGENT_BRIDGE_WHOAMI}"
echo " $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "========================================"
echo
"$ROOT/tmax" train brief
echo
echo "=== drill ==="
"$ROOT/tmax" train drill
echo
echo "=== quiz (oral) ==="
"$ROOT/tmax" train quiz
echo
echo "Docs: $ROOT/train/CURRICULUM.md"
echo "Skills: reading-tbw-8bit · throttle-logic-train · thundermax-tune-loop"
echo "========================================"
