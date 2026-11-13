"""Verschluesselter, dauerhafter Versandstatus im GitHub-Repository."""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

import aiohttp
from cryptography.fernet import Fernet, InvalidToken


class StateError(RuntimeError):
    pass


class GitHubState:
    def __init__(self, session, repository: str, token: str, key: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise StateError("GitHub-Repository oder Laufberechtigung fehlt.")
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise StateError("STATE_KEY fehlt oder ist ungueltig.") from exc
        self.session, self.repository, self.token = session, repository, token
        self.path = ".bot-state/delivery.json"
        self.sha = None

    @property
    def url(self):
        return f"https://api.github.com/repos/{self.repository}/contents/{quote(self.path, safe='/')}"

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Discord-News-Bot"}

    def encode(self, state: dict) -> bytes:
        clear = json.dumps({"context": self.repository + "/" + self.path, "state": state},
                           ensure_ascii=False, sort_keys=True).encode("utf-8")
        return json.dumps({"version": 1, "encrypted": self.cipher.encrypt(clear).decode("ascii")}).encode()

    def decode(self, blob: bytes) -> dict:
        try:
            envelope = json.loads(blob)
            if envelope["version"] != 1:
                raise ValueError
            clear = json.loads(self.cipher.decrypt(envelope["encrypted"].encode("ascii")))
            if clear["context"] != self.repository + "/" + self.path or not isinstance(clear["state"], dict):
                raise ValueError
            return clear["state"]
        except (ValueError, KeyError, TypeError, InvalidToken, UnicodeError) as exc:
            raise StateError("Cloud-Versandstatus kann nicht entschluesselt oder geprueft werden.") from exc

    async def load(self) -> dict:
        async with self.session.get(self.url, headers=self.headers) as response:
            if response.status == 404:
                self.sha = None
                return {}
            if response.status != 200:
                raise StateError(f"Cloud-Versandstatus nicht lesbar (HTTP {response.status}).")
            data = await response.json()
        try:
            self.sha = data["sha"]
            return self.decode(base64.b64decode(data["content"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise StateError("Cloud-Versandstatus hat ein unbekanntes Format.") from exc

    async def save(self, state: dict) -> None:
        body = {"message": "Update encrypted delivery state",
                "content": base64.b64encode(self.encode(state)).decode("ascii")}
        if self.sha:
            body["sha"] = self.sha
        try:
            async with self.session.put(self.url, headers=self.headers, json=body) as response:
                if response.status in (200, 201):
                    data = await response.json()
                    self.sha = data["content"]["sha"]
                    return
                if response.status not in (409, 422):
                    raise StateError(f"Cloud-Versandstatus nicht speicherbar (HTTP {response.status}).")
        except (aiohttp.ClientError, TimeoutError):
            # A confirmed identical write is safe to accept after an uncertain HTTP response.
            if await self.load() == state:
                return
            raise StateError("Speichern des Cloud-Status wurde nicht bestaetigt.")
        if await self.load() != state:
            raise StateError("Cloud-Status wurde gleichzeitig geaendert; Versand abgebrochen.")
