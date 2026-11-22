"""Box art: where a RomM cover came from, its shape, and Japanese alternatives from libretro-thumbnails.

Nothing here changes RomM by itself; the Art pages show current vs proposed and upload only what the user picks
(user's rule, 2026-11-20). libretro-thumbnails names covers after the No-Intro/Redump file names, which is how
this library is named, so a lookup by the game's own file name usually finds its Japanese box."""

from __future__ import annotations

import asyncio
import hashlib
import re
import struct
import time
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

from .db import DB

LIBRETRO = "https://raw.githubusercontent.com/libretro-thumbnails/{repo}/master/Named_Boxarts/"
REPOS = {
    "fds": "Nintendo_-_Family_Computer_Disk_System", "nes": "Nintendo_-_Nintendo_Entertainment_System",
    "snes": "Nintendo_-_Super_Nintendo_Entertainment_System", "gb": "Nintendo_-_Game_Boy",
    "gbc": "Nintendo_-_Game_Boy_Color", "gba": "Nintendo_-_Game_Boy_Advance", "n64": "Nintendo_-_Nintendo_64",
    "nds": "Nintendo_-_Nintendo_DS", "3ds": "Nintendo_-_Nintendo_3DS", "genesis": "Sega_-_Mega_Drive_-_Genesis",
    "gamegear": "Sega_-_Game_Gear", "saturn": "Sega_-_Saturn", "dc": "Sega_-_Dreamcast",
    "segacd": "Sega_-_Mega-CD_-_Sega_CD", "psx": "Sony_-_PlayStation", "ps2": "Sony_-_PlayStation_2",
    "psp": "Sony_-_PlayStation_Portable", "tg16": "NEC_-_PC_Engine_-_TurboGrafx_16",
    "turbografx-cd": "NEC_-_PC_Engine_CD_-_TurboGrafx-CD",
}
MISS_TTL = 7 * 86400              # a cover libretro didn't have is looked up again after a week
ODD_SHAPE = 0.08                  # aspect more than 8% off the platform's usual shape


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG, JPEG or WebP header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + length
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8 " and len(data) >= 30:
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if data[12:16] == b"VP8L" and len(data) >= 25:
            b = data[21:25]
            return 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
    return None


def cover_source(rom: dict) -> tuple[str, bool]:
    """(label, is_japanese) for the current cover, from RomM's url_cover."""
    if not rom.get("path_cover_large"):
        return "no cover", False
    url = unquote(rom.get("url_cover") or "")
    if not url:
        return "uploaded", True                  # set by hand (e.g. the libretro JP covers); trusted as chosen
    if (m := re.search(r"media=box-2D\((\w+)\)", url)):
        return f"ScreenScraper {m.group(1)}", m.group(1) == "jp"
    if "igdb" in url:
        return "IGDB", False
    return "other", False


def lookup_names(rom: dict) -> list[str]:
    """File-name stems to try on libretro: disc 1 for multi-disc folders, then the game's own name."""
    names = []
    if rom.get("has_multiple_files"):
        discs = sorted(f["file_name"] for f in rom.get("files", []) if re.search(r"\.(chd|cue|iso)$", f["file_name"]))
        if discs:
            names.append(re.sub(r"\.\w+$", "", discs[0]))
    names.append(rom.get("fs_name_no_ext") or re.sub(r"\.\w+$", "", rom.get("fs_name", "")))
    return list(dict.fromkeys(n for n in names if n))


def libretro_name(name: str) -> str:
    return re.sub(r'[&*/:`<>?\\|"]', "_", name)


class Libretro:
    """Japanese box candidates from libretro-thumbnails, cached on disk (hits) and in the DB (misses)."""

    def __init__(self, db: DB, cache_dir: Path, client: httpx.AsyncClient | None = None):
        self.db, self.cache_dir = db, cache_dir
        self.http = client or httpx.AsyncClient(timeout=30, follow_redirects=True,
                                                headers={"User-Agent": "hakobiya (personal RomM art)"})
        self.limit = asyncio.Semaphore(4)       # be gentle with GitHub's raw host

    def _path(self, slug: str, name: str) -> Path:
        return self.cache_dir / slug / (hashlib.sha1(name.encode()).hexdigest()[:24] + ".png")

    async def find(self, slug: str, names: list[str]) -> tuple[str, bytes] | None:
        """(matched name, PNG) for the first name libretro has, else None."""
        repo = REPOS.get(slug)
        if not repo:
            return None
        for name in names:
            path = self._path(slug, name)
            if path.exists():
                return name, path.read_bytes()
            miss = self.db.one("SELECT fetched_at FROM art_misses WHERE slug=? AND name=?", (slug, name))
            if miss and time.time() - miss["fetched_at"] < MISS_TTL:
                continue
            data = await self._fetch(LIBRETRO.format(repo=repo), libretro_name(name) + ".png")
            if data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self.db.execute("DELETE FROM art_misses WHERE slug=? AND name=?", (slug, name))
                return name, data
            self.db.execute("INSERT OR REPLACE INTO art_misses(slug, name, fetched_at) VALUES (?,?,?)",
                            (slug, name, time.time()))
        return None

    async def _fetch(self, base: str, filename: str) -> bytes | None:
        async with self.limit:
            for _ in range(3):                  # libretro keeps shared covers as symlinks: the body is the target
                r = await self.http.get(base + quote(filename))
                if r.status_code != 200:
                    return None
                if r.content[:8] == b"\x89PNG\r\n\x1a\n":
                    return r.content
                target = r.text.strip()
                if len(target) > 300 or "\n" in target or not target.endswith(".png"):
                    return None
                filename = target.rsplit("/", 1)[-1]
        return None
