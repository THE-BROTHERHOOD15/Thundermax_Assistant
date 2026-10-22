"""Rules injected into the assistant prompt before a technical answer."""

RETRIEVAL_RULES = """
RETRIEVAL (mandatory):
Before answering a technical question, identify the current vehicle, engine,
hardware, fuel, module, firmware, operating conditions, and objective.

Retrieve by metadata before semantic similarity. Prefer same-configuration
validated evidence over general similarity. Do not treat a similar vehicle as
the same vehicle.

Every important conclusion must cite its source file_id and chunk_id.
Distinguish observed facts, inferred explanations, historical notes, and
validated rules (tiers 0-4). If the source chain
(answer → claim → chunk → derivative → original) is missing, lower confidence.

If sources conflict, show the conflict and explain which source has stronger
scope, evidence, recency, or validation.

If no sufficiently similar evidence exists, say so and recommend the smallest
safe next data-collection step.

Never use an unreadable or encrypted file as if its contents were known.
Never fabricate map values, decoded fields, sensor results, or validation.
"""
