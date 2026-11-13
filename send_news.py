"""Geprueften Wochenkalender einmal senden oder seinen bestehenden Beitrag neu gestalten."""
import argparse
import asyncio
import json
from datetime import datetime
import bot
import discord
from calendar_core import monday, next_monday
from news_core import read_state


async def run(preview: bool, refresh_existing: bool = False) -> int:
    sender = discord.Client(intents=discord.Intents.none())
    async with sender:
        await sender.login(bot.TOKEN)
        bot.client = sender
        result = await bot.publish(preview=preview, refresh_existing=refresh_existing)
        state = read_state(bot.PREVIEW_STATE_FILE if preview else bot.STATE_FILE)
        print(result)
        now = datetime.now(bot.TIMEZONE)
        expected = (next_monday(now.date()) if preview else monday(now.date())).isoformat()
        if (state.get("last_sent_week") != expected or not state.get("message_id")
                or state.get("format_version") != 2 or state.get("cleanup_message_ids")):
            return 1
        if result.startswith(("Kein Versand", "Versand fehlgeschlagen")):
            return 1
        print(json.dumps({"preview": preview, "week": expected,
                          "confirmed_message_id": state["message_id"]}, ensure_ascii=True))
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--refresh", action="store_true",
                        help="Vorhandenen Beitrag aktualisieren; keinen neuen erstellen")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args.preview, args.refresh)))
    except Exception as exc:
        print("Versand fehlgeschlagen:", type(exc).__name__)
        raise SystemExit(1)
