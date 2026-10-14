"""The running fleet: one poller, four voices, a scheduler and a command surface."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx
from croniter import croniter

from . import router
from .agent import Agent
from .audit import audit
from .config import AGENTS, settings
from .llm import LLM
from .store import Store
from .telegram import Bot, TelegramError

log = logging.getLogger("fleet.runtime")

HELP = """\
*Fleet commands*
`/status` — who is running, what fired last
`/schedule` — every agent's cron
`/run <agent>` — make an agent produce its brief now
`/pause <agent>` · `/resume <agent>` — stop or restart scheduled runs
`/tasks` — open and proposed work
`/approve <id>` · `/reject <id>` — decide on Tony's proposals
`/audit` — recent audit verdicts
`/help` — this

Talk to one agent by naming them (`Warden, what moved today?`) or by replying to their
message. Anything else goes to Tony, who decides who owns it."""


class Fleet:
    def __init__(self) -> None:
        self.store = Store(settings.db_path)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
        self.llm = LLM(settings.anthropic_key, settings.model, client=self.http)
        self.bots: dict[str, Bot] = {}
        self.agents: dict[str, Agent] = {}
        self.chat_id = str(settings.chat_id)
        self._stop = asyncio.Event()

    # ---------------- lifecycle ----------------
    async def setup(self) -> None:
        missing = [s.token_env for s in AGENTS.values()
                   if not s.token and not s.local_only]
        if missing:
            raise SystemExit("Missing bot tokens in .env: " + ", ".join(missing))
        if not self.chat_id:
            raise SystemExit("FLEET_CHAT_ID is not set. Run `python -m fleet whereami`.")

        shared = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=15.0))
        for key, spec in AGENTS.items():
            if not spec.token:
                log.warning("%s has no token; it will not be able to post.", spec.name)
                continue
            bot = Bot(spec.token, spec.name, client=shared)
            await bot.get_me()
            self.bots[key] = bot
            log.info("%s connected as @%s", spec.name, bot.username)

        for key, spec in AGENTS.items():
            self.agents[key] = Agent(spec, self.llm, self.store, runner=self)
            if spec.schedule:
                self.store.seed_schedule(key, spec.schedule)

        try:
            log.info("model: %s", await self.llm.resolve_model())
        except Exception as exc:
            # A blip while listing models must not stop the fleet from starting;
            # the first request that needs a model will resolve it then.
            log.warning("model not resolved at startup (%s) — will retry on first use", exc)

    @property
    def usernames(self) -> dict[str, str]:
        return {k: (b.username or "") for k, b in self.bots.items()}

    # ---------------- posting ----------------
    async def post(self, agent_key: str, text: str, reply_to: int | None = None) -> None:
        bot = self.bots.get(agent_key)
        spec = AGENTS[agent_key]
        if bot is None:
            log.error("%s has no bot token; dropping message", spec.name)
            return
        try:
            sent = await bot.send(self.chat_id, text, reply_to=reply_to)
        except TelegramError as exc:
            log.error("%s failed to post: %s", spec.name, exc)
            return
        self.store.add_message(self.chat_id, spec.name, text,
                               agent_key=agent_key,
                               tg_message_id=sent[0]["message_id"] if sent else None)

    # ---------------- incoming ----------------
    async def handle(self, message: dict) -> None:
        chat = str(message.get("chat", {}).get("id", ""))
        if chat != self.chat_id:
            return
        text = (message.get("text") or message.get("caption") or "").strip()
        if not text:
            return
        user = message.get("from", {})
        if user.get("is_bot"):
            return                      # our own posts are recorded when we send them
        speaker = user.get("first_name") or user.get("username") or "someone"
        is_owner = (not settings.owner_id) or str(user.get("id")) == str(settings.owner_id)

        self.store.add_message(chat, speaker, text, is_owner=is_owner,
                               tg_message_id=message.get("message_id"))

        if text.startswith("/"):
            await self.command(text, message, is_owner)
            return

        reply_key = self._replied_agent(message)
        route = await router.choose(
            text, speaker,
            reply_agent_key=reply_key,
            usernames=self.usernames,
            llm=self.llm,
            tony_system=self.agents["tony"].system_prompt(),
        )
        if route.agent_key is None:
            return
        await self.dispatch(route.agent_key, message.get("message_id"))

    def _replied_agent(self, message: dict) -> str | None:
        reply = message.get("reply_to_message")
        if not reply:
            return None
        uname = (reply.get("from") or {}).get("username", "")
        for key, bot in self.bots.items():
            if bot.username and bot.username.lower() == uname.lower():
                return key
        return None

    async def dispatch(self, agent_key: str, reply_to: int | None = None) -> None:
        agent = self.agents[agent_key]
        spec = AGENTS[agent_key]
        if spec.local_only and agent_key not in self.bots:
            await self.post("tony", f"{spec.name} runs on the MacBook and is not reachable "
                                    f"from the server right now.")
            return
        try:
            answer = await agent.respond(self.chat_id)
        except Exception as exc:
            log.exception("%s failed to answer", spec.name)
            await self.post("tony", f"{spec.name} failed to answer: `{exc}`")
            return
        if answer:
            await self.post(agent_key, answer, reply_to=reply_to)

    # ---------------- scheduled runs ----------------
    async def run_scheduled(self, agent_key: str, reason: str = "") -> None:
        spec = AGENTS[agent_key]
        agent = self.agents[agent_key]
        try:
            text = await agent.brief(self.chat_id, extra=reason)
        except Exception as exc:
            log.exception("%s scheduled run failed", spec.name)
            self.store.mark_run(agent_key, f"failed: {exc}")
            await self.post("tony",
                            f"{spec.name}'s scheduled run failed: `{str(exc)[:300]}`")
            return
        await self.post(agent_key, text)
        self.store.mark_run(agent_key, "ok")

        if settings.audit_enabled and agent_key != "tony":
            clean, detail = await audit(self.llm, spec, text)
            self.store.add_audit(agent_key, "clear" if clean else "violation", detail)
            if not clean:
                await self.post("tony", f"Audit — {spec.name}: {detail}")

    async def scheduler(self) -> None:
        """Minute-resolution cron over the fleet timezone."""
        while not self._stop.is_set():
            now = datetime.now(settings.tz)
            for row in self.store.schedule_rows():
                key = row["agent_key"]
                if key not in AGENTS or not row["enabled"]:
                    continue
                if AGENTS[key].local_only:
                    # Bob does real work on the Mac via `python -m fleet bob` (launchd).
                    # Running him from here would narrate an hour of tidying that never
                    # happened, so the scheduler leaves him alone.
                    continue
                if not croniter.is_valid(row["cron"]):
                    continue
                last = row["last_run"] or 0
                prev_fire = croniter(row["cron"], now).get_prev(datetime)
                if prev_fire.timestamp() > last and (now - prev_fire).total_seconds() < 3600:
                    log.info("scheduled run: %s", key)
                    self.store.mark_run(key, "started")
                    asyncio.create_task(self.run_scheduled(key))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    # ---------------- commands ----------------
    async def command(self, text: str, message: dict, is_owner: bool) -> None:
        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        arg = parts[1].lower() if len(parts) > 1 else ""
        reply_to = message.get("message_id")

        def agent_arg() -> str | None:
            if arg in AGENTS:
                return arg
            for key, spec in AGENTS.items():
                if spec.name.lower() == arg:
                    return key
            return None

        if cmd == "help":
            await self.post("tony", HELP, reply_to)
        elif cmd == "status":
            lines = ["*Fleet status*"]
            for key, spec in AGENTS.items():
                row = self.store.schedule_for(key)
                last = (datetime.fromtimestamp(row["last_run"], settings.tz)
                        .strftime("%d %b %H:%M") if row and row["last_run"] else "never")
                state = "paused" if row and not row["enabled"] else "active"
                where = "MacBook" if spec.local_only else "server"
                online = "connected" if key in self.bots else "no token"
                lines.append(f"{spec.name} — {state}, {where}, {online}; last run {last}")
            await self.post("tony", "\n".join(lines), reply_to)
        elif cmd == "schedule":
            await self.post("tony", await self.agents["tony"].tools.list_schedule({}), reply_to)
        elif cmd in ("pause", "resume"):
            key = agent_arg()
            if not key:
                await self.post("tony", f"Which agent? {', '.join(AGENTS)}", reply_to)
            elif not is_owner:
                await self.post("tony", "Only Mosab changes the schedule.", reply_to)
            else:
                self.store.set_enabled(key, cmd == "resume")
                await self.post("tony", f"{AGENTS[key].name} {cmd}d.", reply_to)
        elif cmd == "run":
            key = agent_arg()
            if not key:
                await self.post("tony", f"Which agent? {', '.join(AGENTS)}", reply_to)
            else:
                await self.post("tony", f"{AGENTS[key].name}, run your brief now.", reply_to)
                asyncio.create_task(self.run_scheduled(key, reason="Manual run, out of schedule."))
        elif cmd == "tasks":
            await self.post("tony", await self.agents["tony"].tools.list_tasks({}), reply_to)
        elif cmd in ("approve", "reject"):
            if not is_owner:
                await self.post("tony", "That decision is Mosab's.", reply_to)
            elif not arg.isdigit():
                await self.post("tony", f"Usage: /{cmd} <task id>", reply_to)
            else:
                status = "approved" if cmd == "approve" else "rejected"
                ok = self.store.set_task_status(int(arg), status)
                await self.post("tony", f"Task #{arg} {status}." if ok else "No such task.",
                                reply_to)
        elif cmd == "audit":
            await self.post("tony", await self.agents["tony"].tools.audit_log({}), reply_to)
        elif cmd == "whoami":
            u = message.get("from", {})
            await self.post("tony", f"user id `{u.get('id')}`, chat id `{self.chat_id}`",
                            reply_to)
        else:
            await self.dispatch("tony", reply_to)

    # ---------------- poll loop ----------------
    async def poller(self) -> None:
        bot = self.bots["tony"]
        offset = self.store.get("tg_offset")
        backoff = 1
        while not self._stop.is_set():
            try:
                updates = await bot.poll(offset, timeout=45)
                backoff = 1
            except Exception as exc:
                log.warning("poll failed: %s", exc)
                await asyncio.sleep(min(backoff, 60))
                backoff *= 2
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.store.set("tg_offset", offset)
                msg = update.get("message") or update.get("edited_message")
                if msg:
                    try:
                        await self.handle(msg)
                    except Exception:
                        log.exception("handler failed")

    async def run(self) -> None:
        await self.setup()
        await self.post("tony", "Fleet online. `/help` for the command list.")
        await asyncio.gather(self.poller(), self.scheduler())

    async def aclose(self) -> None:
        self._stop.set()
        for agent in self.agents.values():
            await agent.aclose()
        await self.http.aclose()
        self.store.close()
