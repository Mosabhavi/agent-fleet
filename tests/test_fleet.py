"""Offline tests: no Telegram, no Anthropic, no network."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FLEET_CHAT_ID", "-1001")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from croniter import croniter                       # noqa: E402
from fleet import router                            # noqa: E402
from fleet.config import AGENTS, settings           # noqa: E402
from fleet.store import Store                       # noqa: E402
from fleet.telegram import _split                   # noqa: E402
from fleet.bob_local import organize, FORBIDDEN     # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  pass  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def test_routing() -> None:
    print("routing")
    unames = {"tony": "example_tony_bot", "warden": "example_warden_bot"}
    check("names an agent by name",
          router.explicit_target("Warden, what moved today?") == "warden")
    check("case insensitive", router.explicit_target("hey berlin any quizzes?") == "berlin")
    check("@username wins",
          router.explicit_target("@example_warden_bot status", unames) == "warden")
    check("no name means no explicit target",
          router.explicit_target("what is the plan for tomorrow") is None)
    check("first name mentioned wins",
          router.explicit_target("Berlin should hand this to Bob") == "berlin")
    check("substring does not false-positive",
          router.explicit_target("the bobsleigh results") is None)
    check("reply routes to the replied agent",
          router.route_by_reply("berlin") == "berlin")
    check("reply to a stranger routes nowhere",
          router.route_by_reply("someone_else") is None)


def test_store() -> None:
    print("store")
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(Path(tmp) / "t.db")
        s.add_message("-1001", "Mosab", "morning", is_owner=True)
        s.add_message("-1001", "Berlin", "brief follows", agent_key="berlin")
        rows = s.transcript("-1001")
        check("transcript keeps order", [r["speaker"] for r in rows] == ["Mosab", "Berlin"])
        check("owner flag stored", bool(rows[0]["is_owner"]))

        tid = s.add_task("warden", "Watch earnings for held names", "date TBC")
        check("task proposed", s.list_tasks("proposed")[0]["id"] == tid)
        s.set_task_status(tid, "approved")
        check("task approved", s.list_tasks("approved")[0]["status"] == "approved")

        s.seed_schedule("berlin", "0 8 * * 0-4")
        s.seed_schedule("berlin", "0 9 * * 0-4")          # must not overwrite
        check("seed is idempotent", s.schedule_for("berlin")["cron"] == "0 8 * * 0-4")
        s.set_cron("berlin", "30 7 * * 0-4")
        check("cron updated", s.schedule_for("berlin")["cron"] == "30 7 * * 0-4")
        s.set_enabled("berlin", False)
        check("pause persists", s.schedule_for("berlin")["enabled"] == 0)

        s.remember("bob", "quirk", "hates unfinished downloads")
        check("notes round-trip", s.recall("bob")["quirk"].startswith("hates"))
        s.set("tg_offset", 42)
        check("kv round-trip", s.get("tg_offset") == 42)
        s.close()


def test_schedules() -> None:
    print("schedules")
    for key, spec in AGENTS.items():
        if spec.schedule:
            check(f"{key} cron valid", croniter.is_valid(spec.schedule), spec.schedule)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=settings.tz)     # a Tuesday
    berlin_next = croniter(AGENTS["berlin"].schedule, now).get_next(datetime)
    check("Berlin fires 08:00 on the Saudi week",
          berlin_next.hour == 8 and berlin_next.weekday() in {6, 0, 1, 2, 3},
          str(berlin_next))
    warden_next = croniter(AGENTS["warden"].schedule, now).get_next(datetime)
    check("Warden fires 23:15 on a weekday",
          warden_next.hour == 23 and warden_next.minute == 15 and warden_next.weekday() < 5,
          str(warden_next))


def test_rules_present() -> None:
    print("standing rules")
    warden_rules = " ".join(AGENTS["warden"].rules).lower()
    check("no-order-without-confirmation rule is loaded",
          "confirm" in warden_rules and "cancel" in warden_rules)
    check("hold-list rule is loaded", "hold list" in warden_rules)
    check("margin disclosure rule is loaded", "margin" in warden_rules)
    bob_rules = " ".join(AGENTS["bob"].rules).lower()
    check("sealed-folder rule is loaded", "sealed" in bob_rules)
    check("no-delete rule is loaded", "never deletes" in bob_rules)
    for key, spec in AGENTS.items():
        check(f"{key} persona file exists", bool(spec.persona.strip()))


def test_message_splitting() -> None:
    print("telegram splitting")
    check("short text is one chunk", _split("hello") == ["hello"])
    long_paras = "\n\n".join(["x" * 1000] * 10)
    chunks = _split(long_paras)
    check("long text is split", len(chunks) > 1)
    check("every chunk fits", all(len(c) <= 4096 for c in chunks),
          str([len(c) for c in chunks]))
    check("nothing is lost",
          sum(c.count("x") for c in chunks) == long_paras.count("x"))
    giant = "y" * 9000
    check("an unbroken block still fits", all(len(c) <= 4096 for c in _split(giant)))


def test_bob_safety() -> None:
    print("bob file safety")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Desktop"
        (root / "_sealed").mkdir(parents=True)
        secret = root / "_sealed" / "private.pdf"
        secret.write_text("do not touch")
        (root / "photo.png").write_text("img")
        (root / "notes.pdf").write_text("doc")
        (root / "weird.zzz").write_text("?")
        (root / "empty.txt").write_text("")
        (root / "half.crdownload").write_text("partial")
        keep_dir = root / "MyProject"
        keep_dir.mkdir()
        old = time.time() - 3600
        for p in root.rglob("*"):
            os.utime(p, (old, old))

        os.environ["BOB_ROOTS"] = str(root)
        os.environ["BOB_MIN_AGE_MIN"] = "10"
        report = organize()

        check("_sealed is named forbidden", "_sealed" in FORBIDDEN)
        check("sealed contents untouched", secret.exists() and secret.read_text() == "do not touch")
        check("sealed folder still there", (root / "_sealed").is_dir())
        check("image sorted", (root / "Images" / "photo.png").exists())
        check("pdf sorted", (root / "Documents" / "notes.pdf").exists())
        check("unknown extension quarantined",
              (root / os.environ.get("BOB_REVIEW_DIR", "Review — Bob") / "weird.zzz").exists())
        check("empty file quarantined",
              (root / os.environ.get("BOB_REVIEW_DIR", "Review — Bob") / "empty.txt").exists())
        check("partial download quarantined",
              (root / os.environ.get("BOB_REVIEW_DIR", "Review — Bob") / "half.crdownload").exists())
        check("existing folder left alone", keep_dir.is_dir())
        check("nothing was deleted",
              len(list(root.rglob("*.png"))) == 1 and len(list(root.rglob("*.pdf"))) == 2)
        check("report mentions what moved", "photo.png" in report.as_text())

        # a second pass must be a no-op, not a shuffle
        before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        organize()
        after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        check("second pass is stable", before == after)


def test_collision_safety() -> None:
    print("bob collisions")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Downloads"
        (root / "Images").mkdir(parents=True)
        (root / "Images" / "a.png").write_text("original")
        (root / "a.png").write_text("newcomer")
        old = time.time() - 3600
        for p in root.rglob("*"):
            os.utime(p, (old, old))
        os.environ["BOB_ROOTS"] = str(root)
        organize()
        check("original survives a name clash",
              (root / "Images" / "a.png").read_text() == "original")
        check("newcomer kept under a new name",
              (root / "Images" / "a (2).png").read_text() == "newcomer")


async def test_router_llm_fallback() -> None:
    print("router fallback")

    class FakeLLM:
        def __init__(self, answer): self.answer = answer
        async def complete(self, **kw): return self.answer

    r = await router.choose("what is on for tomorrow", "Mosab", reply_agent_key=None,
                            usernames={}, llm=FakeLLM("berlin"))
    check("llm verdict is honoured", r.agent_key == "berlin" and not r.explicit)
    r = await router.choose("ok", "Mosab", reply_agent_key=None,
                            usernames={}, llm=FakeLLM("none"))
    check("'none' means silence", r.agent_key is None)
    r = await router.choose("hmm", "Mosab", reply_agent_key=None,
                            usernames={}, llm=FakeLLM("nonsense-agent"))
    check("unknown verdict falls back to Tony", r.agent_key == "tony")
    r = await router.choose("Warden, status", "Mosab", reply_agent_key=None,
                            usernames={}, llm=FakeLLM("berlin"))
    check("explicit mention beats the llm", r.agent_key == "warden" and r.explicit)


def main() -> int:
    test_routing()
    test_store()
    test_schedules()
    test_rules_present()
    test_message_splitting()
    test_bob_safety()
    test_collision_safety()
    asyncio.run(test_router_llm_fallback())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
