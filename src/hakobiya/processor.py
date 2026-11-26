"""Filing finished downloads into the RomM library.

The seeding originals in /data/torrents are only ever read. Per platform folder:
- cartridge sets: the zip is hardlinked into roms/<slug>/ (no extra space; RomM hashes zip contents);
- CD sets: unzip to a temp folder on the array, `chdman createcd` (cue or gdi) -> .chd;
- DVD/UMD sets: `chdman createdvd` (PSP with -hs 2048) -> .chd;
- 3DS: the .3ds is extracted (Azahar can't read it zipped);
- GameCube and Wii (the NKit RVZ sets): the .rvz is extracted (Dolphin reads RVZ, not a zip of it);
- anything else: left alone until a rule exists.
Discs of one game go into a folder named after the game with an .m3u listing them, as in the existing library.
Nothing in the library is ever overwritten."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import names

KINDS = {
    **{s: "link" for s in ("fds", "nes", "snes", "gb", "gbc", "gba", "n64", "nds", "genesis", "gamegear", "tg16")},
    **{s: "chd_cd" for s in ("psx", "saturn", "segacd", "turbografx-cd", "dc")},
    **{s: "chd_dvd" for s in ("psp", "ps2")},
    "3ds": "extract",
    "ngc": "extract",
    "wii": "extract",
}
# What "extract" takes out of the zip, and the extension it is filed under.
EXTRACT = {"3ds": ((".3ds", ".cci"), ".3ds"), "ngc": ((".rvz",), ".rvz"), "wii": ((".rvz",), ".rvz")}
DISC_RE = re.compile(r"\(Disc (\d+)\)")


@dataclass
class Outcome:
    state: str          # done | exists | failed | needs_rule | waiting_space
    note: str
    target: str | None = None


class Processor:
    def __init__(self, data_root: Path, library_root: Path, min_free_tb: float = 0.75,
                 chdman: str = "chdman", sevenzip: str = "7z"):
        self.data_root, self.library_root = data_root, library_root
        self.tmp_root = data_root / "torrents" / ".hakobiya-tmp"
        self.min_free_tb, self.chdman, self.sevenzip = min_free_tb, chdman, sevenzip

    def free_tb(self) -> float:
        st = os.statvfs(self.data_root)
        return st.f_bavail * st.f_frsize / 1024 ** 4

    def run(self, job_id: int, path: str, slug: str | None) -> Outcome:
        kind = KINDS.get(slug or "")
        if kind is None:
            return Outcome("needs_rule", f"no RomM folder rule for this set ({slug or 'unmapped'})")
        src = self.data_root / "torrents" / path
        if not src.is_file():
            return Outcome("failed", f"downloaded file not found at {src}")
        n = names.parse(src.name)
        folder = self.library_root / slug
        if n.disc is not None and kind != "link":
            folder = folder / n.game
        stem = names.strip_ext(src.name)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if kind == "link":
                target = folder / src.name
                if target.exists():
                    return Outcome("exists", "already in the library; left as it is", str(target))
                os.link(src, target)
                return Outcome("done", "hardlinked (no extra space)", str(target))
            if self.free_tb() < self.min_free_tb:
                return Outcome("waiting_space", f"less than {self.min_free_tb} TB free; will retry")
            if kind == "extract":
                out = self._extract(job_id, src, folder, slug)
                if n.disc is not None and out.state == "done":
                    self._write_m3u(folder, n.game, EXTRACT[slug][1])
                return out
            target = folder / f"{stem}.chd"
            if target.exists():
                return Outcome("exists", "already in the library; left as it is", str(target))
            out = self._convert(job_id, src, target, kind, slug)
            if n.disc is not None:
                self._write_m3u(folder, n.game)
            return out
        except (OSError, subprocess.SubprocessError, zipfile.BadZipFile, ValueError) as e:
            return Outcome("failed", f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------- steps
    def _unpack(self, job_id: int, src: Path) -> Path:
        tmp = self.tmp_root / str(job_id)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as z:
                z.extractall(tmp)                    # zipfile drops absolute paths and ".." parts
        elif src.suffix.lower() == ".7z":
            subprocess.run([self.sevenzip, "x", "-y", f"-o{tmp}", str(src)], check=True, capture_output=True,
                           timeout=3600)
        else:
            os.link(src, tmp / src.name)
        return tmp

    def _find(self, tmp: Path, *exts: str) -> Path:
        hits = sorted(p for p in tmp.rglob("*") if p.suffix.lower() in exts)
        if not hits:
            raise ValueError(f"no {'/'.join(exts)} file inside the download")
        return hits[0]

    def _convert(self, job_id: int, src: Path, target: Path, kind: str, slug: str) -> Outcome:
        tmp = self._unpack(job_id, src)
        try:
            if kind == "chd_cd":
                image = self._find(tmp, ".gdi") if slug == "dc" and any(tmp.rglob("*.gdi")) else self._find(tmp, ".cue")
                cmd = [self.chdman, "createcd", "-i", str(image)]
            else:
                image = self._find(tmp, ".iso")
                cmd = [self.chdman, "createdvd", "-i", str(image)] + (["-hs", "2048"] if slug == "psp" else [])
            work = tmp / "out.chd"
            proc = subprocess.run(cmd + ["-o", str(work)], capture_output=True, text=True, timeout=6 * 3600)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
                return Outcome("failed", "chdman: " + " / ".join(tail))
            if target.exists():                      # appeared meanwhile: never overwrite
                return Outcome("exists", "already in the library; left as it is", str(target))
            shutil.move(str(work), str(target))
            mb = target.stat().st_size / 1024 ** 2
            return Outcome("done", f"converted to CHD ({mb:,.0f} MB)", str(target))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _extract(self, job_id: int, src: Path, folder: Path, slug: str) -> Outcome:
        exts, ext = EXTRACT[slug]
        tmp = self._unpack(job_id, src)
        try:
            image = self._find(tmp, *exts)
            target = folder / f"{names.strip_ext(src.name)}{ext}"
            if target.exists():
                return Outcome("exists", "already in the library; left as it is", str(target))
            shutil.move(str(image), str(target))
            return Outcome("done", f"extracted ({target.stat().st_size / 1024 ** 3:.1f} GB)", str(target))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _write_m3u(folder: Path, game: str, ext: str = ".chd") -> None:
        discs = sorted(folder.glob(f"*{ext}"), key=lambda p: int(m.group(1)) if (m := DISC_RE.search(p.name)) else 0)
        tmp = folder / f".{game}.m3u.tmp"
        tmp.write_text("".join(p.name + "\n" for p in discs))
        tmp.replace(folder / f"{game}.m3u")
