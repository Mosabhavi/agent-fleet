"""One agent: persona + tools + the group transcript it can see."""
from __future__ import annotations

import logging
from datetime import datetime

from .config import AgentSpec, roster, settings
from .llm import LLM
from .store import Store
from .tools import Toolbox

log = logging.getLogger("fleet.agent")

FLEET_CONTEXT = """
# Where you are
You are one of four agents in a private Telegram group with {owner}. The group is the
whole workplace: he talks to you here, you publish here, and the others read what you
write. Everyone can see everything.

Roster:
{roster}

Each agent posts through its own Telegram account, so your name is already on your
message. Do not prefix your text with your own name.

# House rules
- Telegram Markdown only, and sparingly: *bold*, _italic_, `code`. No headings, no
  tables drawn with pipes — Telegram will not render them, so use short labelled lines
  instead. Keep messages under about 3500 characters.
- Answer in the language {owner} used. If he writes Arabic, reply in Saudi dialect,
  never Egyptian.
- No emoji. No flattery. No "great question". No apologising for existing.
- Never invent a figure, a headline, or a source. If a tool failed or you have no
  access, say exactly that.
- You are not the other agents. Do not answer in their voice or on their behalf.

# Your standing rules
These are yours specifically, and Tony audits you against them in public:
{rules}

Current time: {clock}
""".strip()


class Agent:
    def __init__(self, spec: AgentSpec, llm: LLM, store: Store, runner=None):
        self.spec = spec
        self.llm = llm
        self.store = store
        self.tools = Toolbox(spec.key, store, runner=runner)

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def name(self) -> str:
        return self.spec.name

    def system_prompt(self) -> str:
        rules = "\n".join(f"- {r}" for r in self.spec.rules) or "- (none beyond the house rules)"
        if self.key == "warden" and settings.hold_list:
            rules += f"\n- Current hold list: {', '.join(settings.hold_list)}."
        context = FLEET_CONTEXT.format(
            owner=settings.owner_name,
            roster=roster(),
            rules=rules,
            clock=datetime.now(settings.tz).strftime("%A %d %B %Y, %H:%M %Z"),
        )
        return f"{self.spec.persona}\n\n---\n\n{context}"

    def _history(self, chat_id: str, limit: int | None = None) -> list[dict]:
        """Render the group transcript as an alternating conversation.

        Everything that is not this agent's own message is shown as user-side input,
        labelled with who said it, so the agent can see the whole room.
        """
        rows = self.store.transcript(chat_id, limit or settings.transcript_window)
        messages: list[dict] = []
        pending: list[str] = []
        for row in rows:
            if row["agent_key"] == self.key:
                if pending:
                    messages.append({"role": "user", "content": "\n".join(pending)})
                    pending = []
                messages.append({"role": "assistant", "content": row["text"]})
            else:
                pending.append(f"[{row['speaker']}]: {row['text']}")
        if pending:
            messages.append({"role": "user", "content": "\n".join(pending)})
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "[system]: (start of session)"})
        if messages[-1]["role"] == "assistant":
            messages.append({"role": "user", "content": "[system]: (continue)"})
        return messages

    async def respond(self, chat_id: str, instruction: str | None = None,
                      temperature: float = 0.6, max_tokens: int = 2500) -> str:
        schema, impls = self.tools.build(tony=(self.key == "tony"))
        messages = self._history(chat_id)
        if instruction:
            messages.append({"role": "user", "content": f"[system]: {instruction}"})
        text = await self.llm.complete(
            system=self.system_prompt(),
            messages=messages,
            tools=schema,
            tool_impls=impls,
            max_turns=settings.max_tool_turns,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return text.strip()

    async def brief(self, chat_id: str, extra: str = "") -> str:
        """A scheduled run: produce this agent's standing deliverable."""
        prompt = self.spec.brief_prompt or "Report your status."
        if extra:
            prompt = f"{prompt}\n\n{extra}"
        return await self.respond(
            chat_id,
            instruction=f"Scheduled run. {prompt}",
            temperature=0.4,
            max_tokens=3200,
        )

    async def aclose(self) -> None:
        await self.tools.aclose()
