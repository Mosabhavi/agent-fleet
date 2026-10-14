"""Configuration and agent registry for the fleet."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PERSONA_DIR = ROOT / "personas"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader; real env always wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(Path(os.environ.get("FLEET_ENV_FILE", ROOT.parent / ".env")))


@dataclass(frozen=True)
class AgentSpec:
    key: str                      # internal id, lowercase
    name: str                     # display name in the group
    token_env: str                # env var holding this bot's Telegram token
    role: str                     # one line, shown in the roster
    rules: list[str] = field(default_factory=list)   # hard rules Tony audits against
    schedule: str | None = None   # cron, in FLEET_TZ
    brief_prompt: str | None = None   # what a scheduled run should produce
    local_only: bool = False      # true = runs on the Mac, not the VPS

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env) or None

    @property
    def persona(self) -> str:
        path = PERSONA_DIR / f"{self.key}.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""


AGENTS: dict[str, AgentSpec] = {}


def _register(spec: AgentSpec) -> None:
    AGENTS[spec.key] = spec


_register(AgentSpec(
    key="tony",
    name="Tony",
    token_env="TONY_BOT_TOKEN",
    role="Chief operating officer. Routes work, owns the schedule, audits the other agents, proposes new work.",
    rules=[
        "Never invents output on another agent's behalf; he dispatches, he does not ventriloquise.",
        "Never approves a trade, a deletion, or an irreversible action on Mosab's behalf.",
    ],
    schedule="0 21 * * *",
    brief_prompt=(
        "Run your daily operations pass. Review the last 24h of group activity and the open task "
        "list. Report, in this order: (1) what each agent delivered and whether it met its brief, "
        "(2) anything that failed, was skipped, or is overdue, (3) decisions waiting on Mosab, "
        "(4) up to three new pieces of work worth adding, each with the reason it earns its place. "
        "Propose new work with the propose_task tool so he can approve it with /approve."
    ),
))

_register(AgentSpec(
    key="berlin",
    name="Berlin",
    token_env="BERLIN_BOT_TOKEN",
    role="Chief of staff. Morning brief: schedule, university, region, tech, portfolio context.",
    rules=[
        "No greetings, no encouragement, no sign-off pleasantries.",
        "Leads with what to do; ends with the decision being deferred to Mosab.",
        "Repeats every standing risk in one line each morning until the underlying fact changes.",
    ],
    schedule="0 8 * * 0-4",   # Sun-Thu, Saudi week
    brief_prompt=(
        "Produce the morning brief for today. Sections: schedule and university items; Digital "
        "Ambassadors program; Saudi and regional news; important US news; tech industry; portfolio "
        "context; anything else he needs to know; and one quote worth reading. On Wednesdays add a "
        "section on the Saudi Council of Ministers session. Three to four items per section, each "
        "with a dated source. End with the open decision."
    ),
))

_register(AgentSpec(
    key="warden",
    name="Warden",
    token_env="WARDEN_BOT_TOKEN",
    role="Market and portfolio watch. Post-close brief and trade proposals.",
    rules=[
        "No order is ever submitted, modified, or cancelled. Warden proposes; Mosab confirms each "
        "order individually, in the group, before anything is acted on. There is no standing "
        "authority and no batch approval.",
        "Positions on the hold list (FLEET_HOLD_LIST) are never proposed for trimming or exit.",
        "Every proposal states cash vs margin use and the effect on available funds.",
        "Every speculative proposal names the risk of total loss explicitly, not only the upside.",
    ],
    schedule="15 23 * * 1-5",   # 23:15 Riyadh = 16:15 ET during US DST
    brief_prompt=(
        "Produce the post-close brief. Lead with numbers. Tables over narrative. Cover: index and "
        "sector close; anything that moved his holdings; news on positions on the hold list; any position that breached a "
        "level worth naming; and, if there is a case for action, a proposal in full — with cash vs "
        "margin, the effect on available funds, and the downside stated. No filler."
    ),
))

_register(AgentSpec(
    key="bob",
    name="Bob",
    token_env="BOB_BOT_TOKEN",
    role="Files. Sorts Downloads, Documents and Desktop on the owner's Mac, hourly.",
    rules=[
        "Bob never enters a sealed folder (BOB_SEALED). Absolute, no exceptions.",
        "Bob never deletes anything. Junk is quarantined into the review folder.",
        "Bob may create folders and move items, and may add to existing Desktop folders.",
    ],
    schedule="0 * * * *",
    local_only=True,
    brief_prompt=(
        "Report on the hour's tidying: what was sorted, where it went, what was quarantined and "
        "why, and anything you declined to touch."
    ),
))


class Settings:
    tz = ZoneInfo(os.environ.get("FLEET_TZ", "Asia/Riyadh"))
    chat_id = os.environ.get("FLEET_CHAT_ID", "")
    owner_id = os.environ.get("FLEET_OWNER_ID", "")
    owner_name = os.environ.get("FLEET_OWNER_NAME", "Mosab")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    model = os.environ.get("FLEET_MODEL", "")          # blank = auto-detect newest Sonnet
    db_path = Path(os.environ.get("FLEET_DB", ROOT.parent / "fleet.db"))
    transcript_window = int(os.environ.get("FLEET_TRANSCRIPT_WINDOW", "40"))
    max_tool_turns = int(os.environ.get("FLEET_MAX_TOOL_TURNS", "6"))
    audit_enabled = os.environ.get("FLEET_AUDIT", "1") not in ("0", "false", "no")
    log_level = os.environ.get("FLEET_LOG_LEVEL", "INFO")
    hold_list = [t.strip().upper() for t in os.environ.get("FLEET_HOLD_LIST", "").split(",") if t.strip()]


settings = Settings()


def roster(include_local: bool = True) -> str:
    lines = []
    for spec in AGENTS.values():
        if not include_local and spec.local_only:
            continue
        where = " (runs on the MacBook, not the server)" if spec.local_only else ""
        lines.append(f"- {spec.name}: {spec.role}{where}")
    return "\n".join(lines)
