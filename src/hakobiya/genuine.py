"""Keeping generated disc/cart art in its place until a genuine Japanese scan exists.

RomM can't lock disc art (only covers and manuals), and a ScreenScraper metadata refresh rewrites it with whatever
ScreenScraper has, any region, or drops it. So Hakobiya manages it after saving: it keeps its own copy (Restore on
the Make page when RomM replaced or dropped it), and once a week asks ScreenScraper, with RomM's own login, whether
a Japanese scan now exists; if so, that genuine scan takes the generated image's place."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .db import DB
from .minerva import utcnow
from .romm import Romm

log = logging.getLogger(__name__)
SS_API = "https://api.screenscraper.fr/api2/jeuInfos.php"
SENSITIVE = {"devid", "devpassword", "ssid", "sspassword"}
RECHECK = timedelta(days=7)


def strip_secrets(url: str) -> str:
    """The media URL without login parameters, the way RomM stores ScreenScraper URLs."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in SENSITIVE]
    return urlunsplit(parts._replace(query=urlencode(query)))


def japanese_scan(game: dict) -> str | None:
    """The URL of ScreenScraper's Japanese disc/cart scan for disc 1, if it has one."""
    for m in game.get("medias", []):
        if m.get("type") == "support-2D" and m.get("region") == "jp" and str(m.get("support") or "1") in ("1", "0"):
            return m.get("url")
    return None


class ScreenScraper:
    def __init__(self, creds: dict[str, str], client: httpx.AsyncClient | None = None):
        self.creds = creds
        self.http = client or httpx.AsyncClient(timeout=60)

    async def game(self, ss_id: int) -> dict | None:
        q = {"devid": self.creds["dev_id"], "devpassword": self.creds["dev_password"], "softname": "romm",
             "ssid": self.creds["user"], "sspassword": self.creds["password"], "output": "json", "gameid": ss_id}
        r = await self.http.get(SS_API, params=q)
        if r.status_code != 200:
            return None
        return (r.json().get("response") or {}).get("jeu")

    async def download(self, url: str) -> bytes | None:
        r = await self.http.get(url)
        return r.content if r.status_code == 200 and r.content[:4] == b"\x89PNG" else None


def status_of(change, rom: dict, resources: Path) -> str:
    """What happened to a saved generated image: kept | genuine | overwritten | dropped. Judged by comparing RomM's
    file with Hakobiya's copy (RomM's API doesn't return a marker kept in its metadata)."""
    ss = rom.get("ss_metadata") or {}
    path = resources / ss["physical_path"] if ss.get("physical_path") else None
    if path is None or not path.is_file():
        return "dropped"
    copy = Path(change["generated_path"]) if change["generated_path"] else None
    if copy and copy.is_file() and path.read_bytes() == copy.read_bytes():
        return "kept"
    if re.search(r"support-2D(%28|\()jp", ss.get("physical_url") or ""):
        return "genuine"
    return "overwritten"


class Keeper:
    def __init__(self, db: DB, romm: Romm, ss: ScreenScraper | None, resources: Path, interval: float = 6 * 3600):
        self.db, self.romm, self.ss, self.resources, self.interval = db, romm, ss, resources, interval

    def active(self):
        return self.db.all("SELECT * FROM make_changes WHERE undone_at IS NULL AND superseded_at IS NULL ORDER BY id")

    async def restore(self, change) -> None:
        """Put Hakobiya's copy back and mark it generated again (after a RomM refresh replaced or dropped it)."""
        rom = await self.romm.rom(change["rom_id"])
        target = self.resources / change["physical_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(change["generated_path"]).read_bytes())
        ss = dict(rom.get("ss_metadata") or {})
        await self.romm.set_ss_metadata(change["rom_id"], {**ss, "physical_path": change["physical_path"],
                                                           "physical_url": None, "physical_disc": "1",
                                                           "physical_extra_discs": [], "hakobiya_generated": True})

    async def check(self, change) -> str:
        """One generated image: is there a genuine Japanese scan now? Swap it in if so."""
        rom = await self.romm.rom(change["rom_id"])
        state = status_of(change, rom, self.resources)
        now = utcnow().isoformat()
        if state == "genuine":                   # RomM already brought a Japanese scan in
            self.db.execute("UPDATE make_changes SET superseded_at=?, superseded_by='RomM refresh' WHERE id=?",
                            (now, change["id"]))
            return "genuine"
        if self.ss is None or not rom.get("ss_id"):
            return state
        game = await self.ss.game(rom["ss_id"])
        url = japanese_scan(game or {})
        self.db.execute("UPDATE make_changes SET checked_at=? WHERE id=?", (now, change["id"]))
        if not url:
            return state
        data = await self.ss.download(url)
        if not data:
            return state
        target = self.resources / change["physical_path"]
        target.write_bytes(data)
        ss = {k: v for k, v in (rom.get("ss_metadata") or {}).items() if k != "hakobiya_generated"}
        await self.romm.set_ss_metadata(change["rom_id"], {**ss, "physical_path": change["physical_path"],
                                                           "physical_url": strip_secrets(url), "physical_disc": "1"})
        self.db.execute("UPDATE make_changes SET superseded_at=?, superseded_by='ScreenScraper jp scan' WHERE id=?",
                        (now, change["id"]))
        log.info("genuine Japanese scan replaced the generated art of %s", change["name"])
        return "genuine"

    async def due(self) -> list[str]:
        out = []
        for c in self.active():
            if c["checked_at"] and utcnow() - datetime.fromisoformat(c["checked_at"]) < RECHECK:
                continue
            out.append(await self.check(c))
            await asyncio.sleep(2)               # gentle with ScreenScraper
        return out

    async def run(self) -> None:
        while True:
            try:
                await self.due()
            except Exception as e:
                log.warning("genuine-scan check failed: %s", e)
            await asyncio.sleep(self.interval)
