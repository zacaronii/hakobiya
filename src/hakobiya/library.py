"""The RomM library on disk: which platform folder a set belongs in, and which games are already there."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Set platform (the part after "No-Intro - " / "Redump - ") -> RomM platform folder. Variants such as
# "(Aftermarket)" or "(Private)" map like their base set. Unknown sets get no folder until a rule exists.
PLATFORMS = {
    "Nintendo - Family Computer Disk System (FDS)": "fds",
    "Nintendo - Nintendo Entertainment System (Headered)": "nes",
    "Nintendo - Nintendo Entertainment System (Headerless)": "nes",
    "Nintendo - Super Nintendo Entertainment System": "snes",
    "Nintendo - Game Boy": "gb",
    "Nintendo - Game Boy Color": "gbc",
    "Nintendo - Game Boy Advance": "gba",
    "Nintendo - Nintendo 64 (BigEndian)": "n64",
    "Nintendo - Nintendo DS (Decrypted)": "nds",
    "Nintendo - Nintendo 3DS (Decrypted)": "3ds",
    "Nintendo - New Nintendo 3DS (Decrypted)": "3ds",
    "Sega - Mega Drive - Genesis": "genesis",
    "Sega - Game Gear": "gamegear",
    "Sega - Saturn": "saturn",
    "Sega - Dreamcast": "dc",
    "Sega - Dreamcast - GDI Files": "dc",
    "Sega - Mega CD & Sega CD": "segacd",
    "NEC - PC Engine - TurboGrafx-16": "tg16",
    "NEC - PC Engine CD & TurboGrafx CD": "turbografx-cd",
    "Sony - PlayStation": "psx",
    "Sony - PlayStation 2": "ps2",
    "Sony - PlayStation Portable": "psp",
}
VARIANT_RE = re.compile(r"\s*\((Aftermarket|Private)\)$")


def platform_slug(source: str, platform: str) -> str | None:
    if source not in ("No-Intro", "Redump"):
        return None
    return PLATFORMS.get(VARIANT_RE.sub("", platform))


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold().strip()


class Library:
    """Names present in roms/<slug>/: files by stem, and folders (multi-disc games live in a folder named after the
    game). Re-read when the folder changes."""

    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[str, tuple[float, set[str]]] = {}

    def names(self, slug: str) -> set[str]:
        folder = self.root / slug
        try:
            mtime = folder.stat().st_mtime
        except OSError:
            return set()
        hit = self._cache.get(slug)
        if hit and hit[0] == mtime:
            return hit[1]
        found: set[str] = set()
        for p in folder.iterdir():
            stem = p.name if p.is_dir() else p.name.rsplit(".", 1)[0]
            found.add(norm(stem))
        self._cache[slug] = (mtime, found)
        return found

    def has(self, slug: str | None, stem: str, game: str) -> bool:
        if not slug:
            return False
        present = self.names(slug)
        return norm(stem) in present or norm(game) in present
