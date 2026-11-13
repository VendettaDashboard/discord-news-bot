"""Direkte Kalenderabrufe und dokumentierter Abgleich ohne Codex."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import aiohttp

from calendar_core import BERLIN, COUNTRIES, classify, monday, normalize_calendar
from news_core import NewsError, atomic_json_write

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
INVESTING_URL = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
COUNTRY_IDS = (5, 72, 4, 35, 12, 6, 25, 43)
RATE_TITLES = {"cash rate", "official cash rate", "official bank rate",
               "overnight rate", "main refinancing rate", "federal funds rate",
               "snb policy rate", "boj policy rate", "boJ interest rate".lower()}
EUR_NAMES = re.compile(r"^(?:(?:core|final|flash|prelim)\s+)*(?:cpi|consumer price|hicp)\b", re.I)
MAX_BYTES = 5_000_000


class SourceError(NewsError):
    pass


def source_name(title: str, currency: str) -> str:
    name = title.strip()
    if currency == "USD" and name.lower() == "non-farm employment change":
        return "Nonfarm Payrolls"
    if name.lower() in RATE_TITLES:
        return "Interest Rate Decision"
    return name


def base_payload(week: date, now: datetime, events: list) -> dict:
    return {
        "week_start": week.isoformat(), "verified_at": now.isoformat(timespec="seconds"),
        "coverage_complete": True, "checked_currencies": list(COUNTRIES), "events": events,
    }


def parse_ff(rows, week: date, now: datetime) -> dict:
    if not isinstance(rows, list) or not rows or len(rows) > 5000:
        raise SourceError("Forex Factory: Kalenderformat ungueltig oder leer.")
    events, weeks = [], []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            raise SourceError("Forex Factory: ungueltige Kalenderzeile.")
        try:
            at = datetime.fromisoformat(row["date"])
            if at.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError("Forex Factory: Termin ohne belastbare Zeitzone.") from exc
        local = at.astimezone(BERLIN)
        if local.weekday() < 5:
            weeks.append(monday(local.date()))
        currency = row.get("country")
        if currency not in COUNTRIES:
            continue
        original = row["title"]
        if currency == "EUR" and not EUR_NAMES.match(original):
            if original.lower() not in RATE_TITLES:
                continue
        name = source_name(original, currency)
        if classify(name, currency) is None:
            continue
        events.append({
            "at": at.isoformat(), "currency": currency, "country": COUNTRIES[currency],
            "source_event": name, "original_source_event": original,
            "forecast": row.get("forecast") or None, "previous": row.get("previous") or None,
            "source_url": FF_URL,
        })
    if not weeks or max(set(weeks), key=weeks.count) != week:
        raise SourceError("Forex Factory: Feed ist noch nicht fuer die benoetigte Woche aktualisiert.")
    payload = base_payload(week, now, events)
    normalize_calendar(payload, week, now=now)
    return payload


class InvestingTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.row = {"attrs": attrs, "cells": []}
        elif tag == "td" and self.row is not None:
            self.cell = {"attrs": attrs, "text": "", "country": None, "href": None}
        elif self.cell is not None:
            if tag == "span" and attrs.get("title") in COUNTRIES.values():
                self.cell["country"] = attrs["title"]
            if tag == "a" and attrs.get("href"):
                self.cell["href"] = attrs["href"]

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None and self.row is not None:
            self.row["cells"].append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def parse_investing_html(html: str) -> tuple[list, list]:
    parser = InvestingTable()
    parser.feed(html)
    rows, ids = [], []
    current_day = None
    for row in parser.rows:
        rid = row["attrs"].get("id", "")
        if not rid.startswith("eventRowId_"):
            for cell in row["cells"]:
                day_id = cell["attrs"].get("id", "")
                if day_id.startswith("theDay") and day_id[6:].isdigit():
                    current_day = datetime.fromtimestamp(int(day_id[6:]), timezone.utc).date()
            continue
        ids.append(rid)
        cells = row["cells"]
        def cell_with_class(fragment):
            return next((cell for cell in cells if fragment in cell["attrs"].get("class", "")), None)
        flag = cell_with_class("flagCur")
        event = cell_with_class("event")
        if not flag or not event:
            raise SourceError("Investing: Kalenderstruktur hat sich geaendert.")
        currency = flag["text"].strip()
        country = flag["country"]
        if currency not in COUNTRIES or country != COUNTRIES[currency]:
            continue
        name = event["text"].strip()
        if classify(name, currency) is None:
            continue
        stamp = row["attrs"].get("data-event-datetime")
        try:
            if stamp:
                at = datetime.strptime(stamp, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
            else:
                time_cell = cell_with_class("first")
                if current_day is None or not time_cell:
                    raise ValueError
                at = datetime.fromisoformat(f"{current_day}T{time_cell['text'].strip()}:00+00:00")
        except ValueError as exc:
            raise SourceError("Investing: relevanter Termin ohne auswertbare Uhrzeit.") from exc
        def raw_value(prefix):
            cell = next((cell for cell in cells if cell["attrs"].get("id", "").startswith(prefix)), None)
            if cell is None:
                raise SourceError("Investing: Wertespalten fehlen.")
            value = cell["text"].strip()
            return None if value in ("", "-", "—") else value
        href = event.get("href")
        url = "https://www.investing.com" + href if href and href.startswith("/") else INVESTING_URL
        rows.append({
            "at": at.isoformat(), "currency": currency, "country": country,
            "source_event": name, "forecast": raw_value("eventForecast_"),
            "previous": raw_value("eventPrevious_"), "source_url": url,
        })
    return rows, ids


async def response_json(response, label):
    if response.status != 200:
        raise SourceError(f"{label}: HTTP {response.status}")
    chunks, size = [], 0
    async for chunk in response.content.iter_chunked(65536):
        size += len(chunk)
        if size > MAX_BYTES:
            raise SourceError(f"{label}: Antwort zu gross.")
        chunks.append(chunk)
    blob = b"".join(chunks)
    try:
        return json.loads(blob)
    except (ValueError, UnicodeError) as exc:
        raise SourceError(f"{label}: keine lesbaren Kalenderdaten.") from exc


async def fetch_ff(session, week: date, now: datetime) -> dict:
    async with session.get(FF_URL) as response:
        rows = await response_json(response, "Forex Factory")
    return parse_ff(rows, week, now)


async def fetch_investing(session, week: date, now: datetime) -> dict:
    # Public calendar form fields; timezone 56 is fixed GMT/UTC.
    # No browser impersonation, CAPTCHA solving, cookies, or proxy bypass.
    events, seen = [], set()
    for page in range(20):
        form = [
            ("dateFrom", week.isoformat()), ("dateTo", (week + timedelta(days=4)).isoformat()),
            ("timeZone", "56"), ("timeFilter", "timeOnly"), ("currentTab", "custom"),
            ("submitFilters", "1"), ("limit_from", str(len(seen))),
        ] + [("country[]", str(country_id)) for country_id in COUNTRY_IDS]
        async with session.post(INVESTING_URL, data=form,
                                headers={"X-Requested-With": "XMLHttpRequest"}) as response:
            data = await response_json(response, "Investing.com")
        if not isinstance(data, dict) or not isinstance(data.get("data"), str):
            raise SourceError("Investing: Kalenderantwort hat ein unbekanntes Format.")
        parsed, ids = parse_investing_html(data["data"])
        fresh = [item for item in ids if item not in seen]
        if not fresh:
            if not seen:
                raise SourceError("Investing: keine pruefbaren Kalenderzeilen empfangen.")
            payload = base_payload(week, now, events)
            normalize_calendar(payload, week, now=now)
            return payload
        if len(fresh) != len(ids):
            raise SourceError("Investing: unklare oder ueberlappende Kalenderseiten.")
        seen.update(ids)
        events.extend(parsed)
    raise SourceError("Investing: Kalenderpaginierung nicht vollstaendig.")


def event_variant(name: str) -> str:
    name = name.lower()
    variant = "yoy" if re.search(r"yoy|y/y|year|annual", name) else (
        "mom" if re.search(r"mom|m/m|month", name) else (
        "qoq" if re.search(r"qoq|q/q|quarter", name) else "rate"))
    core = "core" if any(word in name for word in ("core", "trimmed", "median")) else "headline"
    return core + ":" + variant


def compare_sources(primary: dict, secondary: dict, week: date, now: datetime) -> dict:
    left = normalize_calendar(primary, week, now=now)["events"]
    right = normalize_calendar(secondary, week, now=now)["events"]
    def group(rows):
        groups = {}
        for row in rows:
            groups.setdefault((row["currency"], row["event"]), []).append(row)
        return groups
    a, b = group(left), group(right)
    differences = []
    for key in sorted(set(a) | set(b)):
        x, y = a.get(key, []), b.get(key, [])
        if not x or not y or len(x) != len(y):
            differences.append({"currency": key[0], "event": key[1], "kind": "event_coverage",
                                "investing": len(x), "forex_factory": len(y)})
            continue
        for p, q in zip(x, y):
            if (p["date"], p["time"]) != (q["date"], q["time"]):
                differences.append({"currency": key[0], "event": key[1], "kind": "time",
                                    "investing": p["date"] + " " + p["time"],
                                    "forex_factory": q["date"] + " " + q["time"]})
            if event_variant(p["source_event"]) != event_variant(q["source_event"]):
                differences.append({"currency": key[0], "event": key[1], "kind": "variant",
                                    "investing": event_variant(p["source_event"]),
                                    "forex_factory": event_variant(q["source_event"])})
            else:
                for field in ("forecast", "previous"):
                    if p[field] != q[field] and p[field] != "—" and q[field] != "—":
                        differences.append({"currency": key[0], "event": key[1], "kind": field,
                                            "investing": p[field], "forex_factory": q[field]})
    # Missing events or conflicting timestamps need review; no automatic 'No news'.
    unresolved = any(item["kind"] in ("event_coverage", "time") for item in differences)
    result = dict(primary)
    result["coverage_complete"] = not unresolved
    result["crosscheck"] = {
        "status": "review_required" if unresolved else ("checked_with_differences" if differences else "matched"),
        "sources": ["Investing.com", "Forex Factory"], "differences": differences,
        "checked_at": now.isoformat(timespec="seconds"),
    }
    return result


async def refresh_calendar(news_path: Path, report_path: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(BERLIN)
    week = monday(now.date())
    report = {"week_start": week.isoformat(), "checked_at": now.isoformat(timespec="seconds"),
              "status": "blocked", "sources": {}}
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "DiscordRedFolderBot/1.0"}) as session:
        values = await asyncio.gather(fetch_investing(session, week, now), fetch_ff(session, week, now),
                                      return_exceptions=True)
    for name, result in zip(("Investing.com", "Forex Factory"), values):
        report["sources"][name] = (
            {"ok": False, "error": str(result) if isinstance(result, SourceError) else type(result).__name__}
            if isinstance(result, BaseException) else {"ok": True, "raw_events": len(result["events"])})
    if not any(isinstance(result, BaseException) for result in values):
        combined = compare_sources(values[0], values[1], week, now)
        report["crosscheck"] = combined["crosscheck"]
        report["status"] = combined["crosscheck"]["status"]
        if combined["coverage_complete"]:
            normalize_calendar(combined, week, now=now)
            atomic_json_write(news_path, combined)
    atomic_json_write(report_path, report)
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    report = asyncio.run(refresh_calendar(root / "data" / "news.json", root / "data" / "source_status.json"))
    print(json.dumps(report, ensure_ascii=True))
    raise SystemExit(0 if report["status"] in ("matched", "checked_with_differences") else 1)
