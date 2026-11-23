"""qBittorrent WebAPI, server side: one login per action and a logout after (qBittorrent 5.3 caps WebUI sessions)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

STOPPED = {"stoppedUP", "stoppedDL", "pausedUP", "pausedDL"}
PRIO_BATCH = 2000                               # file ids per filePrio request


class QBitError(RuntimeError):
    pass


class QBit:
    def __init__(self, url: str, username: str, password: str, client: httpx.AsyncClient | None = None):
        self.url = url.rstrip("/")
        self.username, self.password = username, password
        self._client = client

    async def __aenter__(self) -> "QBit":
        self.http = self._client or httpx.AsyncClient(timeout=60, headers={"Referer": self.url})
        r = await self.http.post(f"{self.url}/api/v2/auth/login",
                                 data={"username": self.username, "password": self.password})
        # 5.x answers 204 with no body on success, older versions 200 "Ok."; a wrong password is 200 "Fails."
        if r.status_code not in (200, 204) or r.text.strip() == "Fails.":
            raise QBitError(f"qBittorrent login failed (HTTP {r.status_code})")
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            await self.http.post(f"{self.url}/api/v2/auth/logout")
        except httpx.HTTPError:
            pass
        if self._client is None:
            await self.http.aclose()

    async def get(self, path: str, **params) -> Any:
        r = await self.http.get(f"{self.url}/api/v2/{path}", params=params)
        if r.status_code != 200:
            raise QBitError(f"qBittorrent {path}: HTTP {r.status_code}")
        return r.json()

    async def post(self, path: str, data: dict | None = None, files: dict | None = None) -> str:
        r = await self.http.post(f"{self.url}/api/v2/{path}", data=data or {}, files=files)
        if r.status_code not in (200, 204):
            raise QBitError(f"qBittorrent {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.text

    # ------------------------------------------------------------- writes
    async def set_priority(self, infohash: str, ids: list[int], priority: int) -> None:
        for i in range(0, len(ids), PRIO_BATCH):
            await self.post("torrents/filePrio", {"hash": infohash, "id": "|".join(map(str, ids[i:i + PRIO_BATCH])),
                                                  "priority": str(priority)})

    async def start(self, infohash: str) -> None:
        await self.post("torrents/start", {"hashes": infohash})

    async def delete(self, infohash: str) -> None:
        """Remove a torrent and the files it downloaded."""
        await self.post("torrents/delete", {"hashes": infohash, "deleteFiles": "true"})

    async def add_stopped(self, torrent_bytes: bytes, filename: str, tags: str) -> None:
        # `stopped` is WebAPI 2.11+ (qBittorrent 5), `paused` the older name; both are sent.
        text = await self.post("torrents/add", {"stopped": "true", "paused": "true", "tags": tags,
                                                "contentLayout": "Original"},
                               files={"torrents": (filename, torrent_bytes, "application/x-bittorrent")})
        if text.strip() == "Fails.":
            raise QBitError("qBittorrent refused the torrent")

    async def wait_for(self, infohash: str, n_files: int, timeout: float = 60) -> list[dict]:
        """The new torrent's file list once qBittorrent has loaded it."""
        deadline = time.monotonic() + timeout
        while True:
            if await self.get("torrents/info", hashes=infohash):
                files = await self.get("torrents/files", hash=infohash)
                if len(files) >= n_files:
                    return files
            if time.monotonic() > deadline:
                raise QBitError("qBittorrent didn't list the new torrent's files in time")
            await asyncio.sleep(1)


class QBitView:
    """Read-only lookups for the browser pages, cached for a short while so paging through a set doesn't log in on
    every click."""

    TTL = 60

    def __init__(self, make):
        self.make = make                       # () -> QBit
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _cached(self, key: str, fetch) -> Any:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.TTL:
            return hit[1]
        value = await fetch()
        self._cache[key] = (time.monotonic(), value)
        return value

    async def torrents(self) -> list[dict]:
        async def fetch():
            async with self.make() as q:
                return await q.get("torrents/info")
        return await self._cached("torrents", fetch)

    async def files(self, infohash: str) -> list[dict]:
        async def fetch():
            async with self.make() as q:
                return await q.get("torrents/files", hash=infohash)
        return await self._cached("files:" + infohash, fetch)

    def forget(self) -> None:
        self._cache.clear()
