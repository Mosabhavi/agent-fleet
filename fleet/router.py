"""Deciding who owns an incoming message."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import AGENTS

# "warden:" / "@warden" / "warden," / "hey warden" / bare "warden" as first word
_NAME_RE = {
    key: re.compile(rf"(?:^|[\s@(\[])({re.escape(spec.name)})\b", re.IGNORECASE)
    for key, spec in AGENTS.items()
}

ROUTER_INSTRUCTION = """\
Route this message. Reply with ONE lowercase word and nothing else — the key of the \
agent who should answer, chosen from: {keys}. Pick 'tony' when the message is about \
the fleet itself (scheduling, status, who does what), when it is a decision for you, \
or when no other agent clearly owns it. Pick 'none' only if the message needs no \
reply at all (a bare acknowledgement, a reaction, chatter between agents).

Message from {speaker}: {text}"""


@dataclass
class Route:
    agent_key: str | None      # None = nobody answers
    explicit: bool             # True when the human named the agent


def explicit_target(text: str, usernames: dict[str, str] | None = None) -> str | None:
    """Find an agent named directly in the text, by display name or @username."""
    usernames = usernames or {}
    for key, uname in usernames.items():
        if uname and re.search(rf"@{re.escape(uname)}\b", text, re.IGNORECASE):
            return key
    hits = [(m.start(), key) for key, rx in _NAME_RE.items()
            if (m := rx.search(text)) is not None]
    if not hits:
        return None
    hits.sort()
    return hits[0][1]


def route_by_reply(reply_agent_key: str | None) -> str | None:
    """A reply to an agent's message belongs to that agent."""
    return reply_agent_key if reply_agent_key in AGENTS else None


async def choose(text: str, speaker: str, *, reply_agent_key: str | None,
                 usernames: dict[str, str], llm=None, tony_system: str = "") -> Route:
    """Explicit mention wins, then reply target, then Tony's judgement."""
    target = explicit_target(text, usernames)
    if target:
        return Route(target, True)

    replied = route_by_reply(reply_agent_key)
    if replied:
        return Route(replied, True)

    if llm is None:
        return Route("tony", False)

    verdict = await llm.complete(
        system=tony_system or "You are a message router. Answer with one lowercase word.",
        messages=[{"role": "user", "content": ROUTER_INSTRUCTION.format(
            keys=", ".join(list(AGENTS) + ["none"]), speaker=speaker, text=text)}],
        max_turns=1, max_tokens=12, temperature=0.0,
    )
    word = re.sub(r"[^a-z]", "", verdict.strip().lower())[:12]
    if word == "none":
        return Route(None, False)
    return Route(word if word in AGENTS else "tony", False)
