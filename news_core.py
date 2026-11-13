"""Validierung und Aufbereitung der von Codex/anderen Filtern gelieferten News."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_ITEMS = 20


class NewsError(ValueError):
    pass


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result[: limit - 1] + "…" if len(result) > limit else result


def _web_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 1900:
        return None
    try:
        url = urlparse(value)
        if url.scheme in {"https", "http"} and url.netloc and not url.username and not url.password:
            return value
    except ValueError:
        return None
    return None


def normalize_payload(raw: Any, *, default_date: date | None = None) -> dict:
    """Akzeptiert entweder ein JSON-Array mit Meldungen oder ein News-Objekt."""
    if isinstance(raw, list):
        raw = {"date": (default_date or date.today()).isoformat(), "items": raw}
    if not isinstance(raw, dict):
        raise NewsError("Erwartet wird ein JSON-Objekt oder ein Array mit Meldungen.")

    raw_date = raw.get("date")
    if raw_date is None and default_date is not None:
        raw_date = default_date.isoformat()
    if not isinstance(raw_date, str):
        raise NewsError("Feld 'date' fehlt (Format: JJJJ-MM-TT).")
    try:
        parsed_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise NewsError("Feld 'date' ist kein gültiges ISO-Datum.") from exc

    items = raw.get("items")
    if not isinstance(items, list):
        raise NewsError("Feld 'items' muss ein Array sein.")

    cleaned = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        headline = _text(item.get("headline") or item.get("title"), 240)
        summary = _text(item.get("summary") or item.get("text"), 1750)
        if not headline or not summary:
            continue
        cleaned.append({
            "headline": headline,
            "summary": summary,
            "category": _text(item.get("category"), 100),
            "source": _text(item.get("source"), 200),
            "url": _web_url(item.get("url")),
        })

    if not cleaned:
        raise NewsError("Keine gültigen Meldungen gefunden: 'headline' und 'summary' sind Pflicht.")
    return {
        "date": parsed_date.isoformat(),
        "title": _text(raw.get("title"), 200) or "Gefilterte News",
        "items": cleaned,
    }


def load_news(path: Path, *, expected_date: date | None = None) -> dict:
    if not path.is_file():
        raise NewsError(f"News-Datei nicht gefunden: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NewsError(f"News-Datei kann nicht gelesen werden: {exc}") from exc
    payload = normalize_payload(raw)
    if expected_date is not None and payload["date"] != expected_date.isoformat():
        raise NewsError(
            f"News sind vom {payload['date']}, benötigt wird {expected_date.isoformat()}. "
            "Es wird nichts Veraltetes gepostet."
        )
    return payload


def payload_digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".news_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
