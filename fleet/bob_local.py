"""Bob, running on the MacBook.

Sorts loose files at the top level of Downloads, Documents and Desktop into category
folders, quarantines junk into a review folder, and posts a butler's report to the
Telegram group. Never deletes. Never enters a sealed folder (BOB_SEALED).

Run it from launchd once an hour:  python3 -m fleet bob
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from .config import AGENTS, settings

FORBIDDEN = {n.strip() for n in os.environ.get("BOB_SEALED", "_sealed").split(",") if n.strip()}
# ^ sealed folder names: never opened, listed, read or moved, at any depth
REVIEW = os.environ.get("BOB_REVIEW_DIR", "Review — Bob")
MIN_AGE_MIN = int(os.environ.get("BOB_MIN_AGE_MIN", "10"))
DRY_RUN = os.environ.get("BOB_DRY_RUN", "0") not in ("0", "", "false", "no")

CATEGORIES: dict[str, set[str]] = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".svg", ".tiff", ".bmp"},
    "Documents": {".pdf", ".doc", ".docx", ".pages", ".txt", ".rtf", ".md", ".epub"},
    "Spreadsheets": {".xls", ".xlsx", ".csv", ".tsv", ".numbers"},
    "Presentations": {".ppt", ".pptx", ".key"},
    "Archives": {".zip", ".tar", ".gz", ".tgz", ".rar", ".7z", ".bz2", ".xz"},
    "Installers": {".dmg", ".pkg", ".app", ".mpkg"},
    "Media": {".mp4", ".mov", ".mp3", ".m4a", ".wav", ".avi", ".mkv", ".aac", ".flac"},
    "Code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".sh", ".json", ".yml",
             ".yaml", ".ipynb", ".html", ".css", ".sql", ".tf"},
}
JUNK_EXT = {".tmp", ".temp", ".part", ".crdownload", ".download", ".partial"}
NEVER_TOUCH = {".ds_store", ".localized"}


@dataclass
class Report:
    roots: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    quarantined: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        out = [f"Roots: {', '.join(self.roots)}"]
        if self.moved:
            out.append("Sorted:")
            out += [f"  {name} -> {dest}" for name, dest in self.moved[:40]]
            if len(self.moved) > 40:
                out.append(f"  ...and {len(self.moved) - 40} more")
        if self.quarantined:
            out.append("Quarantined for review:")
            out += [f"  {name} ({why})" for name, why in self.quarantined[:20]]
        if self.skipped:
            out.append("Left alone:")
            out += [f"  {name} ({why})" for name, why in self.skipped[:15]]
        if self.errors:
            out.append("Problems:")
            out += [f"  {e}" for e in self.errors[:10]]
        if not (self.moved or self.quarantined):
            out.append("Nothing loose that needed attention.")
        return "\n".join(out)



# ---- Fleet Office reporting ------------------------------------------------
# Side effect only: writes what Bob is doing to a couple of local files so a
# Cowork relay task can post it to the pixel-art office. Must never break the
# real job — every call is wrapped so a write failure here is silent.
OFFICE_DIR = Path(__file__).resolve().parent.parent
OFFICE_STATUS_PATH = OFFICE_DIR / "office_status.json"
OFFICE_RUNS_PATH = OFFICE_DIR / "office_runs.json"
OFFICE_RUNS_MAX = 30


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _office_report(status: str, started: float, summary: str, run_id: str) -> None:
    try:
        now = time.time()
        doc = {
            "name": "Bob",
            "status": status,
            "task": "Hourly tidy",
            "step": "Sorting Downloads, Documents, Desktop" if status == "working" else "",
            "summary": summary,
            "runId": run_id,
            "startedAt": _iso(started),
            "updatedAt": _iso(now),
        }
        if status != "working":
            doc["endedAt"] = _iso(now)
        OFFICE_STATUS_PATH.write_text(json.dumps(doc, indent=2))

        if status != "working":
            runs: list = []
            if OFFICE_RUNS_PATH.exists():
                try:
                    runs = json.loads(OFFICE_RUNS_PATH.read_text())
                except Exception:
                    runs = []
            runs.append({
                "agent": "bob",
                "task": "Hourly tidy",
                "status": status,
                "startedAt": _iso(started),
                "endedAt": _iso(now),
                "summary": summary,
                "runId": run_id,
                "steps": [],
            })
            runs = runs[-OFFICE_RUNS_MAX:]
            OFFICE_RUNS_PATH.write_text(json.dumps(runs, indent=2))
    except Exception:
        pass  # the office is a nice-to-have; Bob's real job never waits on it


def _category(path: Path) -> str | None:
    ext = path.suffix.lower()
    for name, exts in CATEGORIES.items():
        if ext in exts:
            return name
    return None


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix, n = dest.stem, dest.suffix, 2
    while True:
        candidate = dest.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _move(src: Path, dest_dir: Path, report: Report, quarantine: bool, why: str = "") -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique(dest_dir / src.name)
    if DRY_RUN:
        entry = (src.name, f"{dest_dir.name}/ (dry run)")
    else:
        shutil.move(str(src), str(dest))
        entry = (src.name, f"{dest_dir.parent.name}/{dest_dir.name}")
    if quarantine:
        report.quarantined.append((src.name, why))
    else:
        report.moved.append(entry)


def organize() -> Report:
    roots_env = os.environ.get("BOB_ROOTS", "~/Downloads,~/Documents,~/Desktop")
    roots = [Path(p.strip()).expanduser() for p in roots_env.split(",") if p.strip()]
    report = Report(roots=[str(r) for r in roots])
    cutoff = time.time() - MIN_AGE_MIN * 60

    for root in roots:
        if not root.is_dir():
            report.errors.append(f"{root} is not a folder")
            continue
        if root.name in FORBIDDEN:
            continue
        for entry in sorted(root.iterdir()):
            try:
                if entry.name in FORBIDDEN:
                    continue                                   # a sealed folder: invisible
                if entry.is_symlink():
                    report.skipped.append((entry.name, "an alias"))
                    continue
                if entry.is_dir():
                    continue                                   # folders are his business
                if entry.name.lower() in NEVER_TOUCH or entry.name.startswith("."):
                    continue
                if entry.stat().st_mtime > cutoff:
                    report.skipped.append((entry.name, "still warm"))
                    continue
                if entry.name in {REVIEW}:
                    continue

                ext = entry.suffix.lower()
                if ext in JUNK_EXT:
                    _move(entry, root / REVIEW, report, True, "an unfinished download")
                    continue
                if entry.stat().st_size == 0:
                    _move(entry, root / REVIEW, report, True, "entirely empty")
                    continue

                category = _category(entry)
                if category is None:
                    _move(entry, root / REVIEW, report, True,
                          f"an unfamiliar '{ext or 'no'}' extension")
                    continue
                _move(entry, root / category, report, False)
            except Exception as exc:
                report.errors.append(f"{entry.name}: {exc}")
    return report


async def run_once() -> int:
    """Organize, have Bob narrate it, and post to the group."""
    started = time.time()
    run_id = "bob-" + datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y-%m-%dT%H-%M")
    _office_report("working", started, "", run_id)

    try:
        report = organize()
    except Exception as exc:
        _office_report("error", started, f"Run failed: {exc}", run_id)
        raise
    facts = report.as_text()
    print(facts)

    if report.errors:
        summary = f"Sorted {len(report.moved)}, quarantined {len(report.quarantined)}, {len(report.errors)} problem(s) hit."
        _office_report("error", started, summary, run_id)
    elif report.moved or report.quarantined:
        summary = f"Sorted {len(report.moved)} item(s), quarantined {len(report.quarantined)} for review."
        if DRY_RUN:
            summary += " (dry run)"
        _office_report("done", started, summary, run_id)
    else:
        _office_report("done", started, "Nothing loose that needed attention.", run_id)

    spec = AGENTS["bob"]
    if not spec.token or not settings.chat_id:
        print("\n(no BOB_BOT_TOKEN or FLEET_CHAT_ID — report not posted)")
        return 0

    text = facts
    if settings.anthropic_key:
        from .llm import LLM
        llm = LLM(settings.anthropic_key, settings.model)
        try:
            text = await llm.complete(
                system=spec.persona + "\n\nWrite the hourly report for the group. "
                       "Telegram Markdown, sparingly. Under 1200 characters. No emoji.",
                messages=[{"role": "user", "content":
                           "Here is exactly what you did this hour. Report it. Do not "
                           "invent anything that is not in this list.\n\n" + facts}],
                max_turns=1, max_tokens=700, temperature=0.7)
        except Exception as exc:
            text = f"{facts}\n\n(Bob's voice unavailable: {exc})"
        finally:
            await llm.aclose()

    from .telegram import Bot
    async with Bot(spec.token, spec.name) as bot:
        await bot.send(settings.chat_id, text)
    return 0
