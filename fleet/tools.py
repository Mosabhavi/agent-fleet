"""Tools the agents can call. Everything here runs on the server, not in the group."""
from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable
from xml.etree import ElementTree

import httpx

from .config import AGENTS, settings
from .store import Store

UA = {"User-Agent": "fleet-agents/1.0 (+telegram ops bot)"}

DEFAULT_FEEDS = {
    "saudi": [
        "https://www.spa.gov.sa/rss.xml",
        "https://www.arabnews.com/rss.xml",
    ],
    "world": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.reuters.com/reuters/topNews",
    ],
    "us": [
        "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    ],
    "tech": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://hnrss.org/frontpage?points=150",
        "https://techcrunch.com/feed/",
    ],
    "markets": [
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "https://finance.yahoo.com/news/rssindex",
    ],
}


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


class Toolbox:
    """Builds the tool schema + implementations for one agent."""

    def __init__(self, agent_key: str, store: Store,
                 runner: Any | None = None,
                 client: httpx.AsyncClient | None = None):
        self.agent_key = agent_key
        self.store = store
        self.runner = runner          # set for Tony: gives him schedule/dispatch control
        self.http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True, headers=UA)

    # ---------------- implementations ----------------
    async def web_get(self, args: dict) -> str:
        url = args["url"]
        r = await self.http.get(url)
        r.raise_for_status()
        body = _strip_html(r.text)
        return body[: int(args.get("max_chars", 6000))]

    async def news(self, args: dict) -> str:
        topic = args.get("topic", "world")
        limit = int(args.get("limit", 8))
        feeds = json.loads(os.environ.get("FLEET_FEEDS", "{}")).get(topic) \
            or DEFAULT_FEEDS.get(topic, DEFAULT_FEEDS["world"])
        items: list[str] = []
        for feed in feeds:
            try:
                r = await self.http.get(feed)
                r.raise_for_status()
                root = ElementTree.fromstring(r.content)
            except Exception as exc:
                items.append(f"[feed unavailable: {feed} — {exc}]")
                continue
            entries = root.iter("item")
            for entry in list(entries)[:limit]:
                title = (entry.findtext("title") or "").strip()
                link = (entry.findtext("link") or "").strip()
                date = (entry.findtext("pubDate") or "").strip()
                desc = _strip_html(entry.findtext("description") or "")[:220]
                if title:
                    items.append(f"- {title} ({date})\n  {desc}\n  {link}")
        return "\n".join(items[: limit * 2]) or "No items retrieved."

    async def search(self, args: dict) -> str:
        key = os.environ.get("BRAVE_API_KEY", "")
        if not key:
            return ("Web search is not configured (no BRAVE_API_KEY). "
                    "Use the news tool or web_get on a known URL instead.")
        r = await self.http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": args["query"], "count": int(args.get("count", 6))},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        r.raise_for_status()
        out = []
        for hit in r.json().get("web", {}).get("results", []):
            out.append(f"- {hit.get('title')}\n  {_strip_html(hit.get('description',''))[:220]}"
                       f"\n  {hit.get('url')}")
        return "\n".join(out) or "No results."

    async def now(self, args: dict) -> str:
        t = datetime.now(settings.tz)
        return t.strftime("%A %d %B %Y, %H:%M ") + str(settings.tz)

    async def remember(self, args: dict) -> str:
        self.store.remember(self.agent_key, args["key"], args["value"])
        return f"Stored under '{args['key']}'."

    async def recall(self, args: dict) -> str:
        notes = self.store.recall(self.agent_key)
        return json.dumps(notes, ensure_ascii=False, indent=1) if notes else "Nothing stored yet."

    async def list_tasks(self, args: dict) -> str:
        rows = self.store.list_tasks(args.get("status"))
        if not rows:
            return "No tasks."
        return "\n".join(
            f"#{r['id']} [{r['status']}] ({r['owner_agent']}) {r['title']}"
            + (f" — {r['detail']}" if r["detail"] else "")
            for r in rows)

    async def propose_task(self, args: dict) -> str:
        owner = args.get("owner", "tony").lower()
        if owner not in AGENTS:
            return f"Unknown agent '{owner}'. Known: {', '.join(AGENTS)}"
        tid = self.store.add_task(owner, args["title"], args.get("detail", ""), "proposed")
        return (f"Proposed as task #{tid} for {AGENTS[owner].name}. "
                f"It stays inactive until Mosab replies /approve {tid}.")

    async def set_task_status(self, args: dict) -> str:
        ok = self.store.set_task_status(int(args["task_id"]), args["status"])
        return "Updated." if ok else "No task with that id."

    # ---- Tony-only ----
    async def list_schedule(self, args: dict) -> str:
        rows = self.store.schedule_rows()
        out = []
        for r in rows:
            spec = AGENTS.get(r["agent_key"])
            name = spec.name if spec else r["agent_key"]
            last = (datetime.fromtimestamp(r["last_run"], settings.tz).strftime("%d %b %H:%M")
                    if r["last_run"] else "never")
            state = "on" if r["enabled"] else "PAUSED"
            out.append(f"{name}: `{r['cron']}` [{state}] last run {last} "
                       f"({r['last_status'] or '-'})")
        return "\n".join(out) or "Nothing scheduled."

    async def set_schedule(self, args: dict) -> str:
        agent = args["agent"].lower()
        if agent not in AGENTS:
            return f"Unknown agent '{agent}'."
        cron = args["cron"].strip()
        from croniter import croniter
        if not croniter.is_valid(cron):
            return f"'{cron}' is not a valid cron expression."
        self.store.set_cron(agent, cron)
        return f"{AGENTS[agent].name} now runs on `{cron}` ({settings.tz})."

    async def pause_agent(self, args: dict) -> str:
        agent = args["agent"].lower()
        if agent not in AGENTS:
            return f"Unknown agent '{agent}'."
        self.store.set_enabled(agent, False)
        return f"{AGENTS[agent].name} paused. Scheduled runs will not fire until resumed."

    async def resume_agent(self, args: dict) -> str:
        agent = args["agent"].lower()
        if agent not in AGENTS:
            return f"Unknown agent '{agent}'."
        self.store.set_enabled(agent, True)
        return f"{AGENTS[agent].name} resumed."

    async def run_agent(self, args: dict) -> str:
        agent = args["agent"].lower()
        if agent not in AGENTS:
            return f"Unknown agent '{agent}'."
        if self.runner is None:
            return "No runner attached; cannot trigger a run."
        await self.runner.run_scheduled(agent, reason="Tony triggered this run")
        return f"{AGENTS[agent].name} was told to run now; his post follows in the group."

    async def audit_log(self, args: dict) -> str:
        rows = self.store.recent_audits(int(args.get("limit", 10)))
        if not rows:
            return "No audits recorded."
        return "\n".join(
            f"{datetime.fromtimestamp(r['ts'], settings.tz):%d %b %H:%M} "
            f"{r['agent_key']}: {r['verdict']} {r['detail']}" for r in rows)

    # ---------------- schema ----------------
    def build(self, tony: bool = False) -> tuple[list[dict], dict[str, Callable[[dict], Awaitable[str]]]]:
        schema: list[dict] = [
            {"name": "now", "description": "Current date and time in the fleet timezone.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "news",
             "description": "Fetch recent headlines from curated RSS feeds.",
             "input_schema": {"type": "object", "properties": {
                 "topic": {"type": "string",
                           "enum": ["saudi", "world", "us", "tech", "markets"]},
                 "limit": {"type": "integer"}}, "required": ["topic"]}},
            {"name": "web_get",
             "description": "Fetch a URL and return its text content, tags stripped.",
             "input_schema": {"type": "object", "properties": {
                 "url": {"type": "string"}, "max_chars": {"type": "integer"}},
                 "required": ["url"]}},
            {"name": "search",
             "description": "Web search, if a search key is configured on the server.",
             "input_schema": {"type": "object", "properties": {
                 "query": {"type": "string"}, "count": {"type": "integer"}},
                 "required": ["query"]}},
            {"name": "remember",
             "description": "Store a durable note for yourself, keyed by a short name.",
             "input_schema": {"type": "object", "properties": {
                 "key": {"type": "string"}, "value": {"type": "string"}},
                 "required": ["key", "value"]}},
            {"name": "recall", "description": "Read back your stored notes.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "list_tasks", "description": "List fleet tasks and their status.",
             "input_schema": {"type": "object", "properties": {
                 "status": {"type": "string",
                            "enum": ["proposed", "approved", "done", "rejected"]}}}},
        ]
        impls: dict[str, Callable[[dict], Awaitable[str]]] = {
            "now": self.now, "news": self.news, "web_get": self.web_get,
            "search": self.search, "remember": self.remember, "recall": self.recall,
            "list_tasks": self.list_tasks,
        }

        if tony:
            schema += [
                {"name": "propose_task",
                 "description": "Propose new work for an agent. It stays inactive until Mosab "
                                "approves it with /approve <id>.",
                 "input_schema": {"type": "object", "properties": {
                     "title": {"type": "string"}, "detail": {"type": "string"},
                     "owner": {"type": "string",
                               "enum": list(AGENTS.keys())}},
                     "required": ["title", "owner"]}},
                {"name": "set_task_status",
                 "description": "Move a task to approved, done or rejected.",
                 "input_schema": {"type": "object", "properties": {
                     "task_id": {"type": "integer"},
                     "status": {"type": "string",
                                "enum": ["proposed", "approved", "done", "rejected"]}},
                     "required": ["task_id", "status"]}},
                {"name": "list_schedule", "description": "Show every agent's cron and last run.",
                 "input_schema": {"type": "object", "properties": {}}},
                {"name": "set_schedule",
                 "description": "Change an agent's cron schedule (fleet timezone).",
                 "input_schema": {"type": "object", "properties": {
                     "agent": {"type": "string", "enum": list(AGENTS.keys())},
                     "cron": {"type": "string"}}, "required": ["agent", "cron"]}},
                {"name": "pause_agent", "description": "Stop an agent's scheduled runs.",
                 "input_schema": {"type": "object", "properties": {
                     "agent": {"type": "string", "enum": list(AGENTS.keys())}},
                     "required": ["agent"]}},
                {"name": "resume_agent", "description": "Resume an agent's scheduled runs.",
                 "input_schema": {"type": "object", "properties": {
                     "agent": {"type": "string", "enum": list(AGENTS.keys())}},
                     "required": ["agent"]}},
                {"name": "run_agent",
                 "description": "Make an agent run its brief now, out of schedule.",
                 "input_schema": {"type": "object", "properties": {
                     "agent": {"type": "string", "enum": list(AGENTS.keys())}},
                     "required": ["agent"]}},
                {"name": "audit_log", "description": "Recent audit verdicts on the other agents.",
                 "input_schema": {"type": "object", "properties": {
                     "limit": {"type": "integer"}}}},
            ]
            impls.update({
                "propose_task": self.propose_task,
                "set_task_status": self.set_task_status,
                "list_schedule": self.list_schedule,
                "set_schedule": self.set_schedule,
                "pause_agent": self.pause_agent,
                "resume_agent": self.resume_agent,
                "run_agent": self.run_agent,
                "audit_log": self.audit_log,
            })
        return schema, impls

    async def aclose(self) -> None:
        await self.http.aclose()
