"""Original page scans made into a PDF for RomM.

Some Internet Archive items hold a manual as the uploader's page images in a Comic Book ZIP (`.cbz`) or a plain
`.zip`; the only PDF next to them is the Archive's own, heavily compressed copy. Here the pages are read out of
the archive in memory (never unpacked to disk), in the natural order of their names (page2 before page10), and
written into a PDF a point a pixel:
- a JPEG page (greyscale or colour) goes in as it is, the same bytes (DCTDecode), so nothing is lost;
- PNG, WebP, TIFF and JPEG 2000 pages, and JPEGs in other colour models, are decoded and their pixels stored
  deflated, which loses nothing either (transparency is laid on white, as in `packages.write_pdf`).
Entries that aren't page images (text files, `__MACOSX/`, dotfiles) are skipped. An archive with an entry named
outside itself (an absolute path or `..`), an encrypted entry, or more than the limits below is refused."""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from pathlib import Path

from PIL import Image, features

from . import pdf

MAX_ENTRIES = 10_000                    # entries of any kind in the archive
MAX_PAGES = 2_000
MAX_ENTRY = 64 * 1024 * 1024            # one page's file, unpacked
MAX_TOTAL = 1024 * 1024 * 1024          # all pages' files together, unpacked
MAX_PIXELS = 60_000_000                 # one page (a 600 dpi A4 scan is about 35 million)
MAX_PDF = 1024 * 1024 * 1024            # what goes into the PDF, all pages together
IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".jp2", ".j2k", ".jpf", ".jpx")
JUNK = {"__macosx", "thumbs.db", "desktop.ini"}


class ScanError(ValueError):
    pass


def natural(name: str) -> list:
    """A sort key that puts page2 before page10."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def page_entries(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The archive's page images in reading order."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ScanError(f"it has more than {MAX_ENTRIES} entries")
    pages = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        parts = name.split("/")
        if name.startswith("/") or re.match(r"[A-Za-z]:", name) or ".." in parts:
            raise ScanError(f"an entry's name points outside the archive ({info.filename!r})")
        if info.is_dir() or any(p.startswith(".") or p.casefold() in JUNK for p in parts):
            continue
        if not name.lower().endswith(IMAGES):
            continue
        if info.flag_bits & 0x1:
            raise ScanError("its pages are encrypted")
        if info.file_size > MAX_ENTRY:
            raise ScanError(f"a page is larger than {MAX_ENTRY // 2**20} MB")
        pages.append(info)
    if len(pages) > MAX_PAGES:
        raise ScanError(f"it has more than {MAX_PAGES} pages")
    if sum(i.file_size for i in pages) > MAX_TOTAL:
        raise ScanError(f"its pages come to more than {MAX_TOTAL // 2**20} MB")
    return sorted(pages, key=lambda i: natural(i.filename.replace("\\", "/")))


def read(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """An entry's bytes, never more than MAX_ENTRY, whatever its header says."""
    with zf.open(info) as f:
        data = f.read(MAX_ENTRY + 1)
    if len(data) > MAX_ENTRY:
        raise ScanError(f"a page is larger than {MAX_ENTRY // 2**20} MB")
    return data


def picture(data: bytes, name: str) -> pdf.Picture:
    """One page for the PDF: a JPEG's own bytes, or any other picture's pixels deflated."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w < 1 or h < 1 or w * h > MAX_PIXELS:
                raise ScanError(f"{name} is {w}×{h} pixels, more than a page would be")
            if getattr(im, "n_frames", 1) > 1:
                raise ScanError(f"{name} holds more than one picture")
            if im.format == "JPEG" and im.mode in ("L", "RGB"):
                return pdf.Picture(w, h, "/DeviceGray" if im.mode == "L" else "/DeviceRGB", "/DCTDecode", data)
            if im.format == "JPEG2000" and not features.check_codec("jpg_2000"):
                raise ScanError(f"{name} is a JPEG 2000 picture, and this Pillow can't read those")
            if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
                im = Image.alpha_composite(Image.new("RGBA", im.size, "white"), im.convert("RGBA"))
            im = im.convert("L") if im.mode in ("1", "L") else im.convert("RGB")
            pixels = zlib.compress(im.tobytes(), 6)
            return pdf.Picture(w, h, "/DeviceGray" if im.mode == "L" else "/DeviceRGB", "/FlateDecode", pixels)
    except ScanError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError) as e:
        raise ScanError(f"{name} can't be read as a picture ({e})") from None


def make_pdf(archive: Path, out: Path) -> int:
    """Write the pages in `archive` (a .cbz or .zip) to the PDF `out`; the number of pages."""
    try:
        with zipfile.ZipFile(archive) as zf:
            entries = page_entries(zf)
            if not entries:
                raise ScanError("it holds no page images")
            total = 0

            def pictures():
                nonlocal total
                for info in entries:
                    p = picture(read(zf, info), info.filename.replace("\\", "/").rsplit("/", 1)[-1])
                    total += len(p.data)
                    if total > MAX_PDF:
                        raise ScanError(f"the PDF would be larger than {MAX_PDF // 2**20} MB")
                    yield p

            pdf.write(out, len(entries), pictures())
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as e:
        raise ScanError(f"the archive can't be read ({e})") from None
    return len(entries)
