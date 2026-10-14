"""Integration smoke test of the runtime with fake Telegram and fake model."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["FLEET_CHAT_ID"] = "-1001"
os.environ["FLEET_OWNER_ID"] = "777"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
for var in ("TONY_BOT_TOKEN", "BERLIN_BOT_TOKEN", "WARDEN_BOT_TOKEN", "BOB_BOT_TOKEN"):
    os.environ[var] = "111:fake"

from fleet.config import AGENTS, settings          # noqa: E402
from fleet.runtime import Fleet                    # noqa: E402
from fleet.store import Store                      # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'pass' if cond else 'FAIL'}  {name} {'' if cond else detail}")
    if not cond:
        FAILURES.append(name)


class FakeBot:
    def __init__(self, name: str):
        self.name = name
        self.username = f"{name.lower()}_bot"
        self.outbox: list[str] = []

    async def send(self, chat_id, text, reply_to=None):
        self.outbox.append(text)
        return [{"message_id": 1000 + len(self.outbox)}]

    async def call(self, method, **kw):
        return {"title": "Fleet"}


class FakeLLM:
    """Router calls come back as one word; agent turns echo who was asked."""
    def __init__(self):
        self.route_answer = "tony"
        self.calls: list[str] = []

    async def complete(self, *, system, messages, **kw):
        if kw.get("max_tokens", 999) <= 12:              # the router's one-word call
            return self.route_answer
        who = system.split("**")[1] if "**" in system else "agent"
        self.calls.append(who)
        return f"{who} answering."

    async def resolve_model(self):
        return "fake-model"


def build() -> tuple[Fleet, FakeLLM, str]:
    tmp = tempfile.mkdtemp()
    settings.db_path = Path(tmp) / "t.db"
    fleet = Fleet()
    fleet.store = Store(settings.db_path)
    llm = FakeLLM()
    fleet.llm = llm
    fleet.chat_id = "-1001"
    from fleet.agent import Agent
    for key, spec in AGENTS.items():
        fleet.bots[key] = FakeBot(spec.name)
        fleet.agents[key] = Agent(spec, llm, fleet.store, runner=fleet)
        if spec.schedule:
            fleet.store.seed_schedule(key, spec.schedule)
    return fleet, llm, tmp


def msg(text: str, uid: int = 777, mid: int = 1, reply_to_bot: str | None = None) -> dict:
    m = {"message_id": mid, "chat": {"id": -1001}, "text": text,
         "from": {"id": uid, "first_name": "Mosab", "is_bot": False}}
    if reply_to_bot:
        m["reply_to_message"] = {"from": {"username": reply_to_bot, "is_bot": True}}
    return m


async def main() -> int:
    print("runtime")
    fleet, llm, _ = build()

    await fleet.handle(msg("Warden, what moved today?"))
    check("named agent answers", fleet.bots["warden"].outbox, "warden silent")
    check("others stay quiet", not fleet.bots["berlin"].outbox)

    llm.route_answer = "berlin"
    await fleet.handle(msg("any quizzes this week?", mid=2))
    check("router sends it to Berlin", len(fleet.bots["berlin"].outbox) == 1)

    await fleet.handle(msg("and the one after?", mid=3, reply_to_bot="berlin_bot"))
    check("reply stays with Berlin", len(fleet.bots["berlin"].outbox) == 2)

    llm.route_answer = "none"
    before = sum(len(b.outbox) for b in fleet.bots.values())
    await fleet.handle(msg("ok", mid=4))
    check("'none' produces silence",
          sum(len(b.outbox) for b in fleet.bots.values()) == before)

    await fleet.handle(msg("/help", mid=5))
    check("/help answered by Tony", any("Fleet commands" in t for t in fleet.bots["tony"].outbox))

    await fleet.handle(msg("/status", mid=6))
    check("/status lists every agent",
          all(a.name in fleet.bots["tony"].outbox[-1] for a in AGENTS.values()))

    await fleet.handle(msg("/pause warden", mid=7))
    check("owner can pause", fleet.store.schedule_for("warden")["enabled"] == 0)

    await fleet.handle(msg("/resume warden", mid=8, uid=999))
    check("a stranger cannot resume", fleet.store.schedule_for("warden")["enabled"] == 0)
    check("stranger is told no", "Mosab" in fleet.bots["tony"].outbox[-1])

    await fleet.handle(msg("/resume warden", mid=9))
    check("owner can resume", fleet.store.schedule_for("warden")["enabled"] == 1)

    tid = fleet.store.add_task("berlin", "Track DAP deadlines")
    await fleet.handle(msg(f"/approve {tid}", mid=10))
    check("approval moves the task",
          fleet.store.list_tasks("approved")[0]["id"] == tid)

    await fleet.run_scheduled("berlin")
    check("scheduled run posts", len(fleet.bots["berlin"].outbox) == 3)
    check("run is recorded", fleet.store.schedule_for("berlin")["last_status"] == "ok")

    rows = fleet.store.transcript("-1001", limit=100)
    check("transcript captures both sides",
          any(r["agent_key"] == "berlin" for r in rows) and any(r["is_owner"] for r in rows))

    fleet.bots["tony"].outbox.clear()
    await fleet.handle({"message_id": 11, "chat": {"id": -1001}, "text": "hi",
                        "from": {"id": 5, "first_name": "Berlin", "is_bot": True}})
    check("bot chatter is ignored", not fleet.bots["tony"].outbox)

    await fleet.handle({"message_id": 12, "chat": {"id": -9999}, "text": "hello",
                        "from": {"id": 777, "first_name": "Mosab", "is_bot": False}})
    check("other chats are ignored", not fleet.bots["tony"].outbox)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
