"""Minerva's CDN index (https://cdn.minerva-archive.org/): the list of set torrents, and each set's .torrent.

House rules (agreed with the user, see PLAN.md): only the CDN index and the .torrent files on it, never the main
site's /browse/, /api/ or /rom/. The index is fetched when it's over a day old (conditional GET) or on Refresh; a
set's .torrent only when someone opens that set, and then kept. One request at a time, with an honest User-Agent."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

from . import __version__, names, torrent
from .db import DB

log = logging.getLogger(__name__)
CDN = "https://cdn.minerva-archive.org/"
USER_AGENT = f"hakobiya/{__version__} (personal; one user, cached)"
PREFIX = "Minerva_Myrient - "
STALE_AFTER = timedelta(hours=24)
ROW_RE = re.compile(
    r'<td class="indexcolname"><a href="([^"]+\.torrent)">.*?</a></td>'
    r'<td class="indexcollastmod">([^<]*)</td><td class="indexcolsize">([^<]*)</td>', re.S)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def split_name(name: str) -> tuple[str, str]:
    """`No-Intro - Nintendo - Family Computer Disk System (FDS)` -> (`No-Intro`, `Nintendo - Family ...`)."""
    source, _, platform = name.partition(" - ")
    return (source, platform) if platform else ("Other", name)


def parse_index(text: str) -> list[dict]:
    rows = []
    for href, modified, size in ROW_RE.findall(text):
        href = html.unescape(href)                 # `&amp;` in set names
        name = unquote(href)
        if not name.endswith(".torrent"):
            continue
        name = name[: -len(".torrent")]
        if name.startswith(PREFIX):
            name = name[len(PREFIX):]
        source, platform = split_name(name)
        rows.append({"name": name, "href": href, "source": source, "platform": platform,
                     "modified": modified.strip(), "size_text": size.strip()})
    return rows


class Minerva:
    def __init__(self, db: DB, cache_dir: Path, client: httpx.AsyncClient | None = None):
        self.db, self.cache_dir = db, cache_dir
        self.client = client or httpx.AsyncClient(timeout=60, follow_redirects=True,
                                                   headers={"User-Agent": USER_AGENT})
        self.lock = asyncio.Lock()             # one request to Minerva at a time

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------- index
    def index_age(self) -> timedelta | None:
        at = self.db.get("index_fetched_at")
        return utcnow() - datetime.fromisoformat(at) if at else None

    async def refresh_index(self, force: bool = False) -> str:
        """Fetch the index if it's stale (or forced). Returns what happened, for the page."""
        age = self.index_age()
        if not force and age is not None and age < STALE_AFTER:
            return "fresh"
        headers = {}
        if self.db.get("index_etag"):
            headers["If-None-Match"] = self.db.get("index_etag")
        if self.db.get("index_last_modified"):
            headers["If-Modified-Since"] = self.db.get("index_last_modified")
        async with self.lock:
            r = await self.client.get(CDN, headers=headers)
        self.db.put("index_fetched_at", utcnow().isoformat())
        if r.status_code == 304:
            return "unchanged"
        r.raise_for_status()
        rows = parse_index(r.text)
        if not rows:
            raise RuntimeError("the Minerva index had no torrents in it (did its layout change?)")
        self.db.put("index_etag", r.headers.get("etag"))
        self.db.put("index_last_modified", r.headers.get("last-modified"))
        self.db.execute("BEGIN")
        for row in rows:
            self.db.execute(
                "INSERT INTO sets(name, href, source, platform, modified, size_text) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET href=excluded.href, source=excluded.source,"
                " platform=excluded.platform, size_text=excluded.size_text,"
                # a set whose torrent changed upstream is fetched again when next opened
                " torrent_path=CASE WHEN sets.modified IS excluded.modified THEN sets.torrent_path END,"
                " modified=excluded.modified", tuple(row.values()))
        self.db.execute("COMMIT")
        log.info("index: %d sets", len(rows))
        return f"{len(rows)} sets"

    # ------------------------------------------------------------- one set
    async def open_set(self, name: str) -> torrent.Torrent:
        """The set's torrent (downloaded once, then from the cache) with its files loaded for browsing."""
        row = self.db.one("SELECT * FROM sets WHERE name=?", (name,))
        if row is None:
            raise KeyError(name)
        path = Path(row["torrent_path"]) if row["torrent_path"] else None
        if path is None or not path.exists():
            async with self.lock:
                r = await self.client.get(CDN + row["href"])
            r.raise_for_status()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.cache_dir / (hashlib.sha1(name.encode()).hexdigest()[:20] + ".torrent")
            t = torrent.parse(r.content)                     # check it before keeping it
            path.write_bytes(r.content)
            self._load_files(name, t, path)
            return t
        t = torrent.parse(path.read_bytes())
        if not row["file_count"]:
            self._load_files(name, t, path)
        return t

    def _load_files(self, name: str, t: torrent.Torrent, path: Path) -> None:
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM files WHERE set_name=?", (name,))
        for f in t.files:
            fn = f.path.rsplit("/", 1)[-1]
            n = names.parse(fn)
            self.db.execute(
                "INSERT INTO files(set_name, idx, path, filename, size, title, game, regions, languages, tags, disc, rev)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, f.index, f.path, fn, f.size, n.title, n.game, json.dumps(n.regions), json.dumps(n.languages),
                 json.dumps(sorted(n.tags)), n.disc, n.rev))
        self.db.execute("UPDATE sets SET torrent_path=?, infohash=?, file_count=?, total_size=?, opened_at=? WHERE name=?",
                        (str(path), t.infohash, len(t.files), t.size, utcnow().isoformat(), name))
        self.db.execute("COMMIT")
