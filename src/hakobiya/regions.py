"""Keeping the game media RomM downloads Japanese.

RomM asks ScreenScraper for the Japanese version of each image first, but when there is none it takes another
region's (us, wor, eu…), and that fallback can't be switched off. The library holds Japanese releases only, so a
spine or box back from another region shows the wrong packaging. Hakobiya looks at what RomM saved: on "Check now"
(and every six hours when HAKOBIYA_REGION_CHECK=1) the packaging and screen images in AUTO are taken out when they
aren't Japanese (kept in a backup, with undo); the front of the box, the disc/cartridge and the logo are only
listed, for the owner to pick. The region is read from the `media=` tag of the stored ScreenScraper URL; nothing is fetched."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

from .db import DB
from .minerva import utcnow
from .romm import Romm

log = logging.getLogger(__name__)

# ss_metadata key prefix (<kind>_path, <kind>_url) -> what the image is
MEDIA = {"box2d": "front of the box", "box2d_side": "spine", "box2d_back": "back of the box", "box3d": "3D box",
         "physical": "disc / cartridge", "logo": "logo", "marquee": "marquee", "title_screen": "title screen"}
AUTO = {"box2d_side", "box2d_back", "box3d", "marquee", "title_screen"}


def region_of(url: str | None) -> str | None:
    """The region in a ScreenScraper media URL's tag, e.g. `media=box-2D-side(us)` -> us. None when the image has
    no region (fan art) or didn't come from ScreenScraper."""
    m = re.search(r"[?&]media=([^&]+)", url or "")
    r = re.search(r"\(([a-z]{2,4})\)", unquote(m.group(1))) if m else None
    return r.group(1) if r else None


def foreign(rom: dict) -> list[dict]:
    """The saved images of a game that come from a region other than Japan."""
    ss = rom.get("ss_metadata") or {}
    out = []
    for kind, label in MEDIA.items():
        region = region_of(ss.get(f"{kind}_url"))
        if ss.get(f"{kind}_path") and region and region != "jp":
            out.append({"rom_id": rom["id"], "platform_id": rom.get("platform_id"), "name": rom.get("name") or
                        rom.get("fs_name"), "kind": kind, "label": label, "region": region,
                        "path": ss[f"{kind}_path"], "auto": kind in AUTO})
    return out


def inside(root: Path, relative: str) -> Path | None:
    """`relative` under `root`, or None if it would leave it."""
    path = (root / relative).resolve()
    return path if path.is_relative_to(root.resolve()) else None


class RegionKeeper:
    def __init__(self, db: DB, romm: Romm, resources: Path, backups: Path, interval: float = 6 * 3600):
        self.db, self.romm, self.resources, self.backups, self.interval = db, romm, resources, backups, interval

    def put_back(self, item: dict) -> bool:
        """The owner undid this image's removal: leave it alone from then on."""
        return self.db.one("SELECT 1 FROM media_changes WHERE rom_id=? AND kind=? AND path=? AND undone_at IS NOT NULL",
                           (item["rom_id"], item["kind"], item["path"])) is not None

    async def found(self) -> list[dict]:
        """Every image from another region in the library right now."""
        out = []
        for p in await self.romm.platforms():
            for rom in await self.romm.roms(p["id"]):
                for item in foreign(rom):
                    out.append({**item, "platform_id": p["id"], "platform": p.get("display_name") or p["name"],
                                "put_back": self.put_back(item)})
        return out

    async def remove(self, rom_id: int, kinds: set[str], by: str | None) -> int:
        """Take these images of one game out of RomM: the file moves to the backup folder, its path is cleared."""
        rom = await self.romm.rom(rom_id)           # fresh, so a scan in between isn't overwritten
        items = [i for i in foreign(rom) if i["kind"] in kinds]
        if not items:
            return 0
        ss = dict(rom.get("ss_metadata") or {})
        stamp = utcnow().strftime("%Y%m%dT%H%M%S")
        moved = []
        for i in items:
            src = inside(self.resources, i["path"])
            backup = None
            if src and src.is_file():
                self.backups.mkdir(parents=True, exist_ok=True)
                backup = self.backups / f"{rom_id}-{i['kind']}-{stamp}{src.suffix}"
                shutil.copy2(src, backup)
            ss[f"{i['kind']}_path"] = None
            moved.append((i, src, backup))
        await self.romm.set_ss_metadata(rom_id, ss)
        for i, src, backup in moved:
            if backup:
                src.unlink(missing_ok=True)
            self.db.execute("INSERT INTO media_changes(rom_id, platform_id, name, kind, region, path, backup_path,"
                            " removed_at, removed_by) VALUES (?,?,?,?,?,?,?,?,?)",
                            (rom_id, rom.get("platform_id") or 0, i["name"], i["kind"], i["region"], i["path"],
                             str(backup) if backup else None, utcnow().isoformat(), by))
        return len(moved)

    async def undo(self, change) -> None:
        """Put a removed image back where it was."""
        target = inside(self.resources, change["path"])
        if target is None or not change["backup_path"] or not Path(change["backup_path"]).is_file():
            raise FileNotFoundError("no backup of this image")
        rom = await self.romm.rom(change["rom_id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(change["backup_path"], target)
        await self.romm.set_ss_metadata(change["rom_id"], {**(rom.get("ss_metadata") or {}),
                                                           f"{change['kind']}_path": change["path"]})
        self.db.execute("UPDATE media_changes SET undone_at=? WHERE id=?", (utcnow().isoformat(), change["id"]))

    async def sweep(self) -> int:
        """Remove what is removed automatically; returns how many images went."""
        todo: dict[int, set[str]] = {}
        for item in await self.found():
            if item["auto"] and not item["put_back"]:
                todo.setdefault(item["rom_id"], set()).add(item["kind"])
        n = 0
        for rom_id, kinds in todo.items():
            n += await self.remove(rom_id, kinds, None)
        if n:
            log.info("removed %d image(s) from other regions", n)
        return n

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception as e:
                log.warning("region check failed: %s", e)
            await asyncio.sleep(self.interval)
