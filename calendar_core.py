"""Filter, Wochenvalidierung und Tageskarten fuer Red-Folder-News."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from news_core import NewsError

BERLIN = ZoneInfo("Europe/Berlin")
COUNTRIES = {
    "USD": "United States", "EUR": "Euro Zone", "GBP": "United Kingdom",
    "JPY": "Japan", "CHF": "Switzerland", "CAD": "Canada",
    "AUD": "Australia", "NZD": "New Zealand",
}
HEADERS = ("Time", "Currency", "Event", "Forecast", "Previous", "Impact")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def next_monday(day: date) -> date:
    return monday(day) + timedelta(days=7)


def weekly_due(now: datetime, state: dict, hour: int = 6, minute: int = 0) -> bool:
    local = now.astimezone(BERLIN)
    return (local.weekday() == 0 and (local.hour, local.minute) >= (hour, minute)
            and state.get("last_sent_week") != local.date().isoformat())


def classify(name: str, currency: str) -> str | None:
    text = name.lower()
    if any(word in text for word in (
        "unemployment", "adp", "jobless", "employment change", "earnings", "wage",
        "statement", "minutes", "projection", "conference", "speaks", "speech",
        "ppi", "pce", "pmi", "retail", "gdp", "holiday",
    )):
        return None
    if re.search(r"\bcpi\b|consumer price|inflation rate|\bhicp\b", text):
        if "expectation" not in text:
            return "CPI"
    if "interest rate decision" in text or text.strip() == "interest rate":
        return "Interest Rate"
    if currency == "USD" and re.fullmatch(r"(?:u\.?s\.? |united states )?(?:non.?farm payrolls|nfp)(?: \([^)]*\))*", text.strip()):
        return "NFP"
    return None


def value_text(value) -> str:
    if value is None or value == "":
        return "—"
    if not isinstance(value, str):
        raise NewsError("Forecast und Previous muessen unveraenderte Quelltexte sein.")
    if len(value) > 30 or any(char in value for char in ("\n", "\r", "|", "`")):
        raise NewsError("Ungueltiger Kalenderwert.")
    return value


def normalize_calendar(raw: dict, expected_week: date, *, now: datetime | None = None) -> dict:
    if not isinstance(raw, dict) or raw.get("week_start") != expected_week.isoformat():
        raise NewsError("Kalender gehoert nicht zur benoetigten Woche.")
    if expected_week.weekday() != 0:
        raise NewsError("week_start muss ein Montag sein.")
    if raw.get("coverage_complete") is not True:
        raise NewsError("Kalenderabdeckung ist nicht vollstaendig geprueft.")
    if raw.get("checked_currencies") != list(COUNTRIES):
        raise NewsError("Alle acht Waehrungen muessen geprueft sein.")
    try:
        verified = datetime.fromisoformat(raw["verified_at"])
        if verified.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise NewsError("Ein Pruefzeitpunkt mit Zeitzone ist erforderlich.") from exc
    current = now or datetime.now(BERLIN)
    age = current - verified
    if age > timedelta(hours=24) or age < -timedelta(minutes=5):
        raise NewsError("Kalender muss innerhalb der letzten 24 Stunden geprueft worden sein.")
    if not isinstance(raw.get("events"), list):
        raise NewsError("events muss eine Liste sein.")
    merged = {}
    for entry in raw["events"]:
        if not isinstance(entry, dict):
            raise NewsError("Ungueltiger Kalendereintrag.")
        currency = entry.get("currency")
        if currency not in COUNTRIES or entry.get("country") != COUNTRIES[currency]:
            continue
        name = entry.get("source_event", "")
        if not isinstance(name, str):
            raise NewsError("source_event muss der originale Eventname sein.")
        kind = classify(name, currency)
        if kind is None:
            continue
        source = entry.get("source_url", "")
        if not isinstance(source, str) or not source.startswith("https://"):
            raise NewsError("Jeder uebernommene Termin braucht einen Quellenlink.")
        try:
            at = datetime.fromisoformat(entry["at"])
            if at.tzinfo is None:
                raise ValueError
            at = at.astimezone(BERLIN)
        except (KeyError, TypeError, ValueError) as exc:
            raise NewsError("Jeder Termin braucht Datum, Uhrzeit und Zeitzone.") from exc
        if not expected_week <= at.date() <= expected_week + timedelta(days=4):
            continue
        row = {
            "date": at.date().isoformat(), "time": at.strftime("%H:%M"),
            "currency": currency, "event": kind,
            "forecast": value_text(entry.get("forecast")),
            "previous": value_text(entry.get("previous")), "impact": "🔴 High",
            "source_event": name, "source_url": source,
        }
        # Headline before Core; YoY before MoM. Never combine values from different rows.
        rank = (int(any(word in name.lower() for word in ("core", "trimmed", "median", "excluding", "excl"))), int(not re.search(r"yoy|y/y", name, re.I)), int(bool(re.search(r"mom|m/m", name, re.I))))
        key = (row["date"], currency, kind)
        if key not in merged or rank < merged[key][0]:
            merged[key] = (rank, row)
    events = sorted((item[1] for item in merged.values()),
                    key=lambda row: (row["date"], row["time"], row["currency"], row["event"]))
    return {"week_start": expected_week.isoformat(), "verified_at": raw["verified_at"], "events": events}


def load_calendar(path: Path, expected_week: date, *, now: datetime | None = None, require_crosscheck: bool = False) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NewsError("News-Datei fehlt oder ist nicht lesbar.") from exc
    if require_crosscheck:
        proof = raw.get("crosscheck", {}) if isinstance(raw, dict) else {}
        if (proof.get("status") not in ("matched", "checked_with_differences")
                or proof.get("sources") != ["Investing.com", "Forex Factory"]):
            raise NewsError("Der Abgleich von Investing.com und Forex Factory fehlt.")
    return normalize_calendar(raw, expected_week, now=now)


def render_cards(calendar: dict) -> list[dict]:
    start = date.fromisoformat(calendar["week_start"])
    all_rows = [
        (row["time"], row["currency"], row["event"], row["forecast"], row["previous"], row["impact"])
        for row in calendar["events"]
    ]
    widths = [max([len(header)] + [len(row[i]) for row in all_rows]) for i, header in enumerate(HEADERS)]
    def format_row(row):
        return " ".join(cell.ljust(widths[i]) if i < 5 else cell for i, cell in enumerate(row))
    cards = []
    for offset in range(5):
        day = start + timedelta(days=offset)
        rows = [row for row in calendar["events"] if row["date"] == day.isoformat()]
        title = f"{WEEKDAYS[offset]}, {day.day} {MONTHS[day.month - 1]}"
        if rows:
            body = [format_row(HEADERS)]
            body += [format_row((row["time"], row["currency"], row["event"],
                                 row["forecast"], row["previous"], row["impact"])) for row in rows]
            description = "```\n" + "\n".join(body) + "\n```"
        else:
            description = "No news"
        if len(description) > 4096:
            raise NewsError("Tageskarte ist fuer Discord zu gross; Versand abgebrochen.")
        cards.append({"date": day.isoformat(), "title": title, "description": description})
    return cards



def render_week(calendar: dict) -> dict:
    """Eine Discord-Karte mit fuenf Tagesabschnitten und festen Spalten."""
    cards = render_cards(calendar)
    fields = []
    for card in cards:
        if len(card["description"]) > 1024:
            raise NewsError("Ein Tagesabschnitt ist fuer eine Discord-Karte zu gross.")
        fields.append({"date": card["date"], "name": card["title"], "value": card["description"]})
    if sum(len(field["name"]) + len(field["value"]) for field in fields) > 6000:
        raise NewsError("Die Wochenkarte ueberschreitet das Discord-Zeichenlimit.")
    return {"color": 0xE05260, "fields": fields}


@contextmanager
def publication_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        import os
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise NewsError("Ein Versand laeuft bereits.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
