"""Background loop: follow sent files in qBittorrent, file finished ones into the library (one at a time, since CHD
conversion is CPU-heavy), then ask RomM to scan the platforms that got something new."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .db import DB
from .library import platform_slug
from .minerva import utcnow
from .processor import Processor
from .qbit import QBit

log = logging.getLogger(__name__)
ACTIVE = ("sent", "downloading")


class Worker:
    def __init__(self, db: DB, processor: Processor, make_qbit, romm_url: str = "", romm_token: str = "",
                 interval: float = 60):
        self.db, self.processor, self.make_qbit = db, processor, make_qbit
        self.romm_url, self.romm_token, self.interval = romm_url.rstrip("/"), romm_token, interval
        self.wake = asyncio.Event()
        self.last_error: str | None = None

    def set_state(self, job_id: int, state: str, note: str | None = None, target: str | None = None) -> None:
        self.db.execute("UPDATE jobs SET state=?, note=coalesce(?, note), target=coalesce(?, target), updated_at=?"
                        " WHERE id=?", (state, note, target, utcnow().isoformat(), job_id))

    async def follow(self) -> None:
        """Update download states from qBittorrent."""
        jobs = self.db.all(f"SELECT * FROM jobs WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE)
        if not jobs:
            return
        async with self.make_qbit() as q:
            files = {}
            for h in {j["infohash"] for j in jobs}:
                for f in await q.get("torrents/files", hash=h):
                    files[(h, f["name"])] = f
        for j in jobs:
            f = files.get((j["infohash"], j["path"]))
            if f is None:
                self.set_state(j["id"], "failed", "not in qBittorrent any more")
            elif f["progress"] >= 1:
                self.set_state(j["id"], "ready", "downloaded; waiting to be filed")
            elif f["priority"] == 0:
                self.set_state(j["id"], "failed", "switched off in qBittorrent")
            elif j["state"] != "downloading":
                self.set_state(j["id"], "downloading")

    async def process(self) -> set[str]:
        """File every ready job; returns the platform folders that changed."""
        changed: set[str] = set()
        for j in self.db.all("SELECT j.*, s.source, s.platform FROM jobs j JOIN sets s ON s.name=j.set_name"
                             " WHERE j.state IN ('ready', 'waiting_space') ORDER BY j.id"):
            slug = platform_slug(j["source"], j["platform"])
            self.set_state(j["id"], "processing", "filing into the library…")
            out = await asyncio.to_thread(self.processor.run, j["id"], j["path"], slug)
            self.set_state(j["id"], out.state, out.note, out.target)
            log.info("job %s %s: %s %s", j["id"], j["path"].rsplit("/", 1)[-1], out.state, out.note)
            if out.state == "done":
                changed.add(slug)
            if out.state == "waiting_space":
                break
        return changed

    async def scan(self, slugs: set[str]) -> None:
        if not (slugs and self.romm_url and self.romm_token):
            return
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.romm_url}/api/tasks/scan", json={"type": "quick", "platform_fs_slugs": sorted(slugs)},
                             headers={"Authorization": f"Bearer {self.romm_token}"})
            r.raise_for_status()
        log.info("asked RomM to scan %s", ", ".join(sorted(slugs)))

    async def tick(self) -> None:
        await self.follow()
        await self.scan(await self.process())

    async def run(self) -> None:
        # Jobs left mid-way by a restart start over (temp files are rebuilt; nothing in the library was written).
        self.db.execute("UPDATE jobs SET state='ready' WHERE state='processing'")
        while True:
            try:
                await self.tick()
                self.last_error = None
            except Exception as e:                   # qBittorrent or RomM down: try again next round
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("worker round failed: %s", self.last_error)
            try:
                await asyncio.wait_for(self.wake.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
