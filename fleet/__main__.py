"""CLI: run | doctor | whereami | bob"""
from __future__ import annotations

import asyncio
import logging
import sys

from .config import AGENTS, settings


def _log() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


async def cmd_run() -> int:
    from .runtime import Fleet
    fleet = Fleet()
    try:
        await fleet.run()
    except KeyboardInterrupt:
        pass
    finally:
        await fleet.aclose()
    return 0


async def cmd_whereami() -> int:
    """Print the chat id of any group Tony can see, so you can fill FLEET_CHAT_ID."""
    from .telegram import Bot
    spec = AGENTS["tony"]
    if not spec.token:
        print("TONY_BOT_TOKEN is not set.")
        return 1
    async with Bot(spec.token, "Tony") as bot:
        me = await bot.get_me()
        print(f"Tony is @{me.get('username')}")
        print("Send any message in the group now. Listening for 60s...\n")
        offset = None
        for _ in range(3):
            updates = await bot.poll(offset, timeout=20)
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                chat = msg.get("chat", {})
                if chat:
                    print(f"chat: {chat.get('title') or chat.get('first_name')} "
                          f"type={chat.get('type')} id={chat.get('id')}")
                frm = msg.get("from", {})
                if frm:
                    print(f"  from: {frm.get('first_name')} id={frm.get('id')}")
            if offset:
                print("\nPut the group id in FLEET_CHAT_ID and your own id in FLEET_OWNER_ID.")
                return 0
        print("No messages seen. Is Tony in the group?")
        return 1


async def cmd_doctor() -> int:
    from .telegram import Bot, TelegramError
    from .llm import LLM
    ok = True

    print("== configuration ==")
    for label, value in (("FLEET_CHAT_ID", settings.chat_id),
                         ("FLEET_OWNER_ID", settings.owner_id),
                         ("ANTHROPIC_API_KEY", "set" if settings.anthropic_key else ""),
                         ("FLEET_TZ", str(settings.tz))):
        state = "ok" if value else "MISSING"
        ok &= bool(value) or label == "FLEET_OWNER_ID"
        print(f"  {label:20} {state} {value if label.startswith('FLEET_T') else ''}")

    print("\n== telegram ==")
    for key, spec in AGENTS.items():
        if not spec.token:
            print(f"  {spec.name:8} no token ({spec.token_env})"
                  + ("  [optional: runs on the Mac]" if spec.local_only else "  MISSING"))
            ok &= spec.local_only
            continue
        try:
            async with Bot(spec.token, spec.name) as bot:
                me = await bot.get_me()
                note = ""
                if key == "tony":
                    reads_all = me.get("can_read_all_group_messages")
                    note = ("  privacy OFF (correct)" if reads_all
                            else "  PRIVACY IS ON — Tony will only see @mentions. "
                                 "Fix with /setprivacy in BotFather.")
                    ok &= bool(reads_all)
                print(f"  {spec.name:8} @{me.get('username')}{note}")
                if settings.chat_id:
                    try:
                        chat = await bot.call("getChat", chat_id=settings.chat_id)
                        print(f"           sees chat '{chat.get('title')}'")
                    except TelegramError as exc:
                        print(f"           CANNOT SEE THE GROUP: {exc}")
                        ok = False
        except Exception as exc:
            print(f"  {spec.name:8} FAILED: {exc}")
            ok = False

    print("\n== anthropic ==")
    try:
        llm = LLM(settings.anthropic_key, settings.model)
        model = await llm.resolve_model()
        print(f"  model: {model}")
        await llm.aclose()
    except Exception as exc:
        print(f"  FAILED: {exc}")
        ok = False

    print("\n== schedule ==")
    from croniter import croniter
    from datetime import datetime
    now = datetime.now(settings.tz)
    for key, spec in AGENTS.items():
        if not spec.schedule:
            continue
        if croniter.is_valid(spec.schedule):
            nxt = croniter(spec.schedule, now).get_next(datetime)
            print(f"  {spec.name:8} {spec.schedule:15} next {nxt:%a %d %b %H:%M}")
        else:
            print(f"  {spec.name:8} INVALID CRON: {spec.schedule}")
            ok = False

    print("\n" + ("All checks passed." if ok else "Some checks failed — see above."))
    return 0 if ok else 1


async def cmd_bob() -> int:
    from .bob_local import run_once
    return await run_once()


def main() -> int:
    _log()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    table = {"run": cmd_run, "doctor": cmd_doctor, "whereami": cmd_whereami, "bob": cmd_bob}
    if cmd not in table:
        print(f"usage: python -m fleet [{'|'.join(table)}]")
        return 2
    return asyncio.run(table[cmd]())


if __name__ == "__main__":
    raise SystemExit(main())
