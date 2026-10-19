"""The adversarial reviewer must never be silently truncated.

This tower runs OLLAMA_CONTEXT_LENGTH=4096. A request that omits
options.num_ctx is cut server-side with no error at all -- the model answers
from whatever survived. The adversarial prompt carries the whole proposal,
every guardrail finding and the citations, which routinely exceeds 4096
tokens.

A refuting model that cannot see the change it is meant to refute is strictly
more likely to return CONCUR, and CONCUR is the verdict that lets a proposal
advance toward `approved`. So the failure mode is a FALSE PASS on the one gate
that exists to catch a bad change -- silently, with the vet report still
looking complete.
"""
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import vetting as v  # noqa: E402
import webui_core as core  # noqa: E402


class _Resp:
    def __iter__(self):
        return iter([json.dumps(
            {"message": {"content": "VERDICT: CONCUR"}, "done": True}
        ).encode()])

    def close(self):
        pass


def _capture(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(v.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_vetting_sends_num_ctx_for_deep_tier(monkeypatch):
    seen = _capture(monkeypatch)
    v._llm_generate("hermes3:70b", [{"role": "user", "content": "hi"}],
                    tier="deep")
    opts = seen["body"].get("options") or {}
    assert "num_ctx" in opts, "no num_ctx => server truncates to 4096 silently"
    assert opts["num_ctx"] == core.TIER_CTX["deep"]


def test_vetting_sends_num_ctx_for_fast_tier(monkeypatch):
    seen = _capture(monkeypatch)
    v._llm_generate("qwen2.5-coder:14b", [{"role": "user", "content": "hi"}],
                    tier="fast")
    assert seen["body"]["options"]["num_ctx"] == core.TIER_CTX["fast"]


def test_unknown_tier_still_sends_an_explicit_window(monkeypatch):
    """Falling back must mean an explicit default, never omitting the field."""
    seen = _capture(monkeypatch)
    v._llm_generate("whatever", [{"role": "user", "content": "hi"}], tier=None)
    assert seen["body"]["options"]["num_ctx"] == core.DEFAULT_CTX


def test_vetting_window_matches_the_chat_path(monkeypatch):
    """Reviewer and assistant must not disagree about what a tier affords."""
    seen = _capture(monkeypatch)
    for tier in ("fast", "smart", "deep"):
        v._llm_generate("m", [{"role": "user", "content": "x"}], tier=tier)
        assert seen["body"]["options"]["num_ctx"] == core.TIER_CTX[tier], tier
