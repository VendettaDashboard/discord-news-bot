"""Gepruefte Wochenkalender importieren, ohne deren Pruefzeitpunkt zu veraendern."""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from calendar_core import monday, next_monday, normalize_calendar
from news_core import NewsError, atomic_json_write

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    week = next_monday(now.date()) if args.preview else monday(now.date())
    key = "PREVIEW_FILE" if args.preview else "NEWS_FILE"
    default = "data/preview.json" if args.preview else "data/news.json"
    target = args.output or Path(os.getenv(key, default))
    if not target.is_absolute():
        target = ROOT / target
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
        checked = normalize_calendar(raw, week, now=now)
        atomic_json_write(target, raw)
    except (OSError, ValueError, NewsError) as exc:
        print("Import fehlgeschlagen:", str(exc))
        return 1
    print(f"Kalender fuer {week} importiert: {len(checked['events'])} passende Termine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
