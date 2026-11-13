"""Ein einzelner geplanter Cloud-Lauf; kein dauerhaft laufender PC erforderlich."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import aiohttp

from calendar_core import BERLIN, load_calendar, monday, next_monday, normalize_calendar, render_week
from calendar_sources import refresh_calendar
from cloud_state import GitHubState, StateError
from news_core import NewsError, atomic_json_write, read_state

ROOT = Path(__file__).resolve().parent
READY = ("matched", "checked_with_differences")


def scheduled_time(now: datetime) -> bool:
    local = now.astimezone(BERLIN)
    return local.weekday() == 0 and (local.hour, local.minute) >= (6, 0)


async def execute(mode: str, store=None, *, now: datetime | None = None, publisher=None, refresher=None) -> dict:
    now = now or datetime.now(BERLIN)
    refresher = refresher or refresh_calendar
    if mode not in ("check", "send", "trial"):
        raise NewsError("Unbekannter Cloud-Modus.")
    if mode == "send" and not scheduled_time(now):
        return {"status": "outside_schedule"}
    slot = "preview" if mode == "trial" else "production"
    week = next_monday(now.date()) if mode == "trial" else monday(now.date())
    remote = await store.load() if mode != "check" else {}
    if mode != "check" and not remote and os.getenv("INITIAL_PREVIEW_STATE"):
        initial = json.loads(os.environ["INITIAL_PREVIEW_STATE"])
        if not isinstance(initial, dict):
            raise StateError("Initialer Probestatus ist ungueltig.")
        remote = {"preview": initial}
    previous = remote.get(slot, {})
    if not isinstance(previous, dict):
        raise StateError("Gespeicherter Versandstatus ist ungueltig.")
    if mode == "send" and previous.get("last_sent_week") == week.isoformat():
        return {"status": "already_sent", "week": week.isoformat()}
    reservation = remote.get("reservation", {})
    if reservation.get("slot") == slot and reservation.get("week") == week.isoformat():
        raise StateError("Ein frueherer Versand ist ungeklaert; keine doppelte Nachricht angelegt.")
    scratch_root = ROOT / "temp"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cloud-", dir=scratch_root) as directory:
        scratch = Path(directory)
        calendar_path, report_path = scratch / "calendar.json", scratch / "sources.json"
        state_path = scratch / "delivery.json"
        if mode == "trial":
            incoming = os.getenv("PREVIEW_PAYLOAD")
            if not incoming:
                raise NewsError("Der getrennte Probekalender fehlt.")
            payload = json.loads(incoming)
            normalize_calendar(payload, week, now=now)
            atomic_json_write(calendar_path, payload)
        else:
            report = await refresher(calendar_path, report_path, now=now)
            if mode == "check" or report["status"] not in READY:
                return {"status": "checked" if report["status"] in READY else "sources_blocked",
                        "sources": report.get("sources", {}), "crosscheck": report["status"]}
        calendar = load_calendar(calendar_path, week, now=now, require_crosscheck=mode != "trial")
        render_week(calendar)  # Validate the entire single-message layout before reserving a send.
        atomic_json_write(state_path, previous)
        remote["reservation"] = {"slot": slot, "week": week.isoformat(),
                                 "created_at": now.isoformat(timespec="seconds")}
        # A confirmed durable reservation is required before Discord is contacted.
        await store.save(remote)
        result = await (publisher or publish_cloud)(
            calendar_path, state_path, preview=mode == "trial",
            refresh_existing=mode == "trial" and previous.get("last_sent_week") == week.isoformat())
        confirmed = read_state(state_path)
        if (confirmed.get("last_sent_week") != week.isoformat()
                or not confirmed.get("message_id") or confirmed.get("cleanup_message_ids")
                or str(result).startswith(("Kein Versand", "Versand fehlgeschlagen"))):
            raise StateError("Versand nicht vollstaendig bestaetigt; Reservierung bleibt zur Pruefung erhalten.")
        remote[slot] = confirmed
        remote.pop("reservation", None)
        await store.save(remote)
        return {"status": "published", "week": week.isoformat(), "preview": mode == "trial",
                "events": len(calendar["events"]), "messages": 1}


async def publish_cloud(calendar_path: Path, state_path: Path, *, preview: bool, refresh_existing: bool):
    # Import only for a real send; public source diagnostics require no Discord credentials.
    import discord
    import bot
    logging.getLogger("discord_news_bot").disabled = True
    if preview:
        bot.PREVIEW_FILE, bot.PREVIEW_STATE_FILE = calendar_path, state_path
    else:
        bot.NEWS_FILE, bot.STATE_FILE = calendar_path, state_path
    async with discord.Client(intents=discord.Intents.none()) as sender:
        await sender.login(bot.TOKEN)
        bot.client = sender
        return await bot.publish(preview=preview, refresh_existing=refresh_existing)


async def main(mode: str) -> int:
    if mode == "check":
        result = await execute(mode)
    else:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            store = GitHubState(session, os.getenv("GITHUB_REPOSITORY", ""),
                                os.getenv("GITHUB_TOKEN", ""), os.getenv("STATE_KEY", ""))
            result = await execute(mode, store)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] in ("published", "already_sent", "outside_schedule", "checked") else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("check", "send", "trial"), default="check")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(args.mode)))
    except (NewsError, StateError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=True))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": type(exc).__name__}))
        raise SystemExit(1)
