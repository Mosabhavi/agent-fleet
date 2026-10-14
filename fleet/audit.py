"""Tony's audit pass over another agent's published output."""
from __future__ import annotations

import logging

from .config import AgentSpec
from .llm import LLM

log = logging.getLogger("fleet.audit")

AUDIT_SYSTEM = """\
You are an operations auditor. You are given an agent's standing rules and one piece of
output it just published. Decide whether the output breaks any rule.

Answer in one of exactly two shapes:
CLEAR
or
VIOLATION: <rule broken, quoted in a few words> — <what in the output breaks it, one sentence>

Be strict about the rules and indifferent to everything else. Tone, length, formatting
and whether you would have written it differently are not your business. An honest
statement that data was unavailable is not a violation. Only judge what is written in
front of you; do not speculate about what the agent might do next."""

AUDIT_USER = """\
Agent: {name}
Standing rules:
{rules}

Output it published:
---
{output}
---"""


async def audit(llm: LLM, spec: AgentSpec, output: str) -> tuple[bool, str]:
    """Return (clean, detail). Never raises — a failed audit must not kill a brief."""
    if not spec.rules:
        return True, ""
    try:
        verdict = await llm.complete(
            system=AUDIT_SYSTEM,
            messages=[{"role": "user", "content": AUDIT_USER.format(
                name=spec.name,
                rules="\n".join(f"- {r}" for r in spec.rules),
                output=output[:8000])}],
            max_turns=1, max_tokens=200, temperature=0.0,
        )
    except Exception as exc:
        log.warning("audit of %s failed: %s", spec.key, exc)
        return True, ""
    verdict = verdict.strip()
    if verdict.upper().startswith("VIOLATION"):
        return False, verdict.split(":", 1)[-1].strip()
    return True, ""
