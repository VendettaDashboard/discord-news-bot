"""Woechentliche Red-Folder-News fuer Discord, Montag 06:00 Europe/Berlin."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from calendar_core import (READY_SOURCE_STATUSES, load_calendar, monday, next_monday,
                           publication_lock, render_week, weekly_due)
from news_core import NewsError, atomic_json_write, payload_digest, read_state
from calendar_sources import refresh_calendar

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("discord_news_bot")


def positive_id(key: str, *, required: bool = True) -> int | None:
    raw = os.getenv(key, "").strip()
    if not raw and not required:
        return None
    if not raw.isdecimal() or int(raw) <= 0:
        raise SystemExit(f"{key} muss eine Discord-ID aus Zahlen sein.")
    return int(raw)


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN or TOKEN == "HIER_DEIN_BOT_TOKEN":
    raise SystemExit("DISCORD_TOKEN fehlt in der lokalen .env.")
CHANNEL_ID = positive_id("CHANNEL_ID")
GUILD_ID = positive_id("GUILD_ID", required=False)
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
if TIMEZONE.key != "Europe/Berlin":
    raise SystemExit("Dieser Wochenplan verwendet Europe/Berlin.")
POST_TIME = os.getenv("POST_TIME", "06:00").strip()
LOCAL_SCHEDULE_ENABLED = os.getenv("LOCAL_SCHEDULE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
try:
    POST_HOUR, POST_MINUTE = map(int, POST_TIME.split(":"))
    time(POST_HOUR, POST_MINUTE)
except (TypeError, ValueError) as exc:
    raise SystemExit("POST_TIME muss HH:MM enthalten.") from exc


def file_path(key: str, default: str) -> Path:
    path = Path(os.getenv(key, default))
    return path if path.is_absolute() else ROOT / path


NEWS_FILE = file_path("NEWS_FILE", "data/news.json")
PREVIEW_FILE = file_path("PREVIEW_FILE", "data/preview.json")
STATE_FILE = file_path("STATE_FILE", "data/state.json")
PREVIEW_STATE_FILE = ROOT / "data" / "preview_state.json"
SOURCE_REPORT_FILE = ROOT / "data" / "source_status.json"


class NewsClient(discord.Client):
    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
        else:
            synced = await tree.sync()
        logger.info("%d Discord-Befehle synchronisiert", len(synced))


client = NewsClient(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)
publish_lock = asyncio.Lock()


async def publish(*, preview: bool = False, refresh_existing: bool = False) -> str:
    async with publish_lock:
        now = datetime.now(TIMEZONE)
        target_week = next_monday(now.date()) if preview else monday(now.date())
        news_path = PREVIEW_FILE if preview else NEWS_FILE
        state_path = PREVIEW_STATE_FILE if preview else STATE_FILE
        try:
            with publication_lock(state_path.with_suffix(".lock")):
                payload = load_calendar(news_path, target_week, now=now, require_crosscheck=not preview)
                digest = payload_digest(payload)
                state = read_state(state_path)
                same_week = state.get("last_sent_week") == target_week.isoformat()
                if same_week and not refresh_existing:
                    return "Diese Woche wurde bereits veroeffentlicht."
                if refresh_existing and not same_week:
                    raise NewsError("Es gibt keinen fertigen Wochenbeitrag zum Aktualisieren.")
                if state.get("pending_week") == target_week.isoformat() and not same_week:
                    raise NewsError("Ein alter Teilversand muss zuerst geprueft werden.")
                channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
                if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD_ID:
                    raise NewsError("Zielkanal oder Server stimmt nicht mit der Konfiguration ueberein.")
                design = render_week(payload)
                embed = discord.Embed(color=design["color"])
                for field in design["fields"]:
                    embed.add_field(name=field["name"], value=field["value"], inline=False)
                legacy = [state.get("messages", {}).get(field["date"]) for field in design["fields"]]
                legacy = [value for value in legacy if isinstance(value, int) and value > 0]
                previous_id = state.get("message_id") or (legacy[0] if legacy else None)
                if refresh_existing:
                    if not isinstance(previous_id, int) or previous_id <= 0:
                        raise NewsError("Die gespeicherte Nachrichten-ID fehlt.")
                    message = await channel.get_partial_message(previous_id).edit(
                        content=None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
                else:
                    message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                cleanup = list(dict.fromkeys(state.get("cleanup_message_ids", []) +
                                              [value for value in legacy if value != message.id]))
                record = {
                    "last_sent_week": target_week.isoformat(),
                    "last_sent_at": state.get("last_sent_at", now.isoformat(timespec="seconds"))
                        if refresh_existing else now.isoformat(timespec="seconds"),
                    "digest": digest, "message_id": message.id, "format_version": 2,
                    "preview": preview, "events": len(payload["events"]),
                    "cleanup_message_ids": cleanup,
                    "superseded_message_ids": list(dict.fromkeys(
                        state.get("superseded_message_ids", []) + [value for value in legacy if value != message.id])),
                }
                if refresh_existing:
                    record["last_edited_at"] = now.isoformat(timespec="seconds")
                atomic_json_write(state_path, record)
                # Only the bot's exact, saved legacy IDs are removed after the replacement is confirmed.
                for old_id in cleanup.copy():
                    if not isinstance(old_id, int) or old_id <= 0 or old_id == message.id:
                        raise NewsError("Ungueltige alte Nachrichten-ID; Bereinigung abgebrochen.")
                    try:
                        await channel.get_partial_message(old_id).delete()
                    except discord.NotFound:
                        pass
                    cleanup.remove(old_id)
                    record["cleanup_message_ids"] = cleanup.copy()
                    atomic_json_write(state_path, record)
                logger.info("Wochenkarte %s bestaetigt; Probe=%s; Nachricht=%s",
                            target_week, preview, message.id)
                action = "aktualisiert" if refresh_existing else "versendet"
                return f"1 Wochenkarte fuer {target_week.isoformat()} {action}."
        except NewsError as exc:
            return f"Kein Versand: {exc}"
        except (discord.HTTPException, OSError) as exc:
            logger.exception("Wochenversand fehlgeschlagen")
            return f"Versand fehlgeschlagen: {type(exc).__name__}"


@tasks.loop(seconds=30)
async def weekly_check() -> None:
    if not LOCAL_SCHEDULE_ENABLED:
        return
    now = datetime.now(TIMEZONE)
    if now.weekday() != 0 or (now.hour, now.minute) < (5, 40):
        return
    state = read_state(STATE_FILE)
    if state.get("last_sent_week") == now.date().isoformat():
        return
    report = read_state(SOURCE_REPORT_FILE)
    try:
        checked = datetime.fromisoformat(report.get("checked_at", ""))
        if checked.tzinfo is None:
            raise ValueError
        retry_due = (now - checked).total_seconds() >= 900
    except (TypeError, ValueError):
        retry_due = True
    if report.get("week_start") != now.date().isoformat() or retry_due:
        report = await refresh_calendar(NEWS_FILE, SOURCE_REPORT_FILE, now=now)
        if report["status"] not in READY_SOURCE_STATUSES:
            logger.warning("Quellenabgleich nicht bereit: %s", report["status"])
    if (report.get("status") in READY_SOURCE_STATUSES
            and weekly_due(now, state, POST_HOUR, POST_MINUTE)):
        logger.info(await publish())


@weekly_check.before_loop
async def before_weekly_check() -> None:
    await client.wait_until_ready()


@tree.command(name="news_senden", description="Die geprueften Red-Folder-News dieser Woche veroeffentlichen")
@app_commands.checks.has_permissions(manage_guild=True)
async def news_senden(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.followup.send(await publish(), ephemeral=True)


@tree.command(name="news_probe", description="Probelauf fuer die naechste Woche, getrennter Versandstatus")
@app_commands.checks.has_permissions(manage_guild=True)
async def news_probe(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.followup.send(await publish(preview=True), ephemeral=True)


@tree.command(name="news_status", description="Wochenkalender und Versandstatus pruefen")
@app_commands.checks.has_permissions(manage_guild=True)
async def news_status(interaction: discord.Interaction) -> None:
    now = datetime.now(TIMEZONE)
    try:
        payload = load_calendar(NEWS_FILE, monday(now.date()), now=now, require_crosscheck=True)
        source = f"Ready: {len(payload['events'])} matching events for this week"
    except NewsError as exc:
        source = f"Not ready: {exc}"
    state = read_state(STATE_FILE)
    report = read_state(SOURCE_REPORT_FILE)
    statuses = report.get("sources", {})
    checks = " | ".join(f"{name}: " + ("OK" if item.get("ok") else item.get("error", "Unavailable"))
                        for name, item in statuses.items()) or "Not checked yet"
    await interaction.response.send_message(
        f"**News:** {source}\n**Last sent:** {state.get('last_sent_at', 'Never')}"
        f"\n**Schedule:** Monday {POST_TIME} ({TIMEZONE.key})"
        f"\n**Sources:** {checks}\n**Cross-check:** {report.get('status', 'Pending')}", ephemeral=True)


@tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "Du benoetigst die Berechtigung 'Server verwalten'."
    else:
        logger.error("Slash-Command-Fehler: %s", type(error).__name__)
        message = "Der Befehl konnte nicht ausgefuehrt werden."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@client.event
async def on_ready() -> None:
    if LOCAL_SCHEDULE_ENABLED and not weekly_check.is_running():
        weekly_check.start()
    logger.info("Eingeloggt als %s; Zielkanal %s; Montag %s (%s)",
                client.user, CHANNEL_ID, POST_TIME, TIMEZONE.key)


async def main() -> None:
    async with client:
        await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
