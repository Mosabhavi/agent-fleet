# Scheduled agents (generation 2)

Generation 1 of the fleet is the Python runtime in [`../fleet`](../fleet): four Telegram
bots on a VPS. Generation 2 moved most of the agents onto **scheduled Claude agents** —
each one a cron-triggered, unattended run with connectors (Gmail, Google Calendar,
Notion, Todoist, Interactive Brokers, web search) instead of hand-written API clients.
The persona and the rules that were Python config became the prompt.

Why the move: the cloud sandbox those agents run in cannot reach `api.telegram.org`, but
it *can* reach every data source the agents actually need. Trading Telegram for native
connectors removed most of the glue code. Bob stayed local, because he needs real files.

Each file here is the **spec** for one agent, written from its live prompt with
personal data, workspace IDs and account details replaced by `<PLACEHOLDERS>`.

| Agent | Role | Cadence (Asia/Riyadh) | Writes to |
|---|---|---|---|
| [Tony](tony.md) | COO — audits the fleet, proposes new work | 21:00 daily | report only |
| [Berlin](berlin.md) | Chief of staff — morning brief | 08:00 Sun–Thu | report |
| [Warden](warden.md) | Brokerage account monitor | 23:15 Mon–Fri | report (never trades) |
| [Nataly](nataly.md) | Executive assistant — Notion + Calendar | 07:00, 18:00, Fri 09:00 | Notion, Google Calendar |
| [Rocco](rocco.md) | Lead generation for a web-design side business | 08:30 Sun–Thu | lead book, email (capped) |
| [Bob](bob.md) | File organizer on the Mac | hourly (launchd) | the filesystem (never deletes) |

Sub-agents spawned inside a run: Rocco's crew (**Scout**, **Preview builder**,
**Follow-up**, **Ledger**) and Nataly's crew (**Vera** — deadline research, **Cass** —
prep packs).

## Patterns every agent shares

- **Rules outrank voice.** Every prompt has a `HARD RULES` block that is written to
  survive a rewrite of the personality.
- **Never delete.** Archive by status, by toggle, by retitling an event `CANCELLED —`,
  or by moving a file to a review folder.
- **Never invent.** An unknown date, price or figure is reported as unknown, with the
  source that was missing. Two sources that disagree are logged, not resolved.
- **No irreversible action without a human.** Trades, outbound messages to new people,
  and changes to the fleet itself are drafted and wait for explicit approval.
- **Report status to the Fleet Office.** Three writes per run — `working` at the
  start, a few step updates, `done`/`error` with a one-line summary at the end — to a
  small shared database behind a pixel-art "office" page where each agent has a desk.
  No money figures or personal identifiers ever go into a status line.
- **One voice per agent.** Sub-agents research, draft and build; the parent decides,
  sends and speaks.
