"""RomM API (bearer token): platforms, games and their covers, and uploading a cover (RomM locks an uploaded one)."""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx


class RommError(RuntimeError):
    pass


class Romm:
    def __init__(self, url: str, token: str, client: httpx.AsyncClient | None = None):
        self.url = url.rstrip("/")
        self.http = client or httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {token}"})

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, **params):
        r = await self.http.get(f"{self.url}/api{path}", params=params)
        if r.status_code != 200:
            raise RommError(f"RomM {path}: HTTP {r.status_code}")
        return r.json()

    async def platforms(self) -> list[dict]:
        return [p for p in await self._get("/platforms") if p.get("rom_count")]

    async def roms(self, platform_id: int) -> list[dict]:
        out, offset = [], 0
        while True:
            page = await self._get("/roms", platform_ids=platform_id, limit=250, offset=offset)
            items = page["items"] if isinstance(page, dict) else page
            out += items
            if not isinstance(page, dict) or len(out) >= page.get("total", 0) or not items:
                return out
            offset += len(items)

    async def rom(self, rom_id: int) -> dict:
        return await self._get(f"/roms/{rom_id}")

    async def asset(self, path: str) -> bytes | None:
        """A cover image by its RomM path (`/assets/romm/resources/...`, with RomM's `?ts=` cache buster)."""
        if not path or not path.startswith("/assets/romm/resources/"):
            return None
        base, _, query = path.partition("?")
        r = await self.http.get(self.url + quote(base) + ("?" + quote(query, safe="=&") if query else ""))
        return r.content if r.status_code == 200 else None

    async def set_ss_metadata(self, rom_id: int, ss_metadata: dict) -> dict:
        """Replace the game's ScreenScraper metadata (its match, ss_id, stays as it is)."""
        r = await self.http.put(f"{self.url}/api/roms/{rom_id}",
                                files={"raw_ss_metadata": (None, json.dumps(ss_metadata, ensure_ascii=False))})
        if r.status_code != 200:
            raise RommError(f"RomM metadata update for #{rom_id}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    async def upload_cover(self, rom_id: int, image: bytes, filename: str = "cover.png") -> dict:
        ctype = "image/png" if image[:4] == b"\x89PNG" else "image/jpeg"
        r = await self.http.put(f"{self.url}/api/roms/{rom_id}", files={"artwork": (filename, image, ctype)})
        if r.status_code != 200:
            raise RommError(f"RomM cover upload for #{rom_id}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()
