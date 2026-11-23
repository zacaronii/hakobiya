"""Manuals out of PS one Classics packages.

Sony's PS one Classics release of a game carries the game's manual: `DOCUMENT.DAT` inside the package, a list of
PNG pages (480 pixels wide). Minerva's set of these packages is a torrent like any other, so a package is fetched
through qBittorrent (only that file switched on), read, and the manual saved as a PDF for the Manuals page, where
it is looked at and ticked like one from the Internet Archive. Never automatic: a package is fetched only when
asked for on a game's page.

The package and the manual are encrypted with fixed keys. They are not in this repository: Hakobiya reads them
from `psn-keys.txt` in its config folder, and the feature stays off without that file.

Layout, as read here:
- the zip holds one `.pkg`: a 0x100-byte header (magic, item count, where the data starts, the IV), then the data,
  AES-128-CTR from its start: the item table (32 bytes an item), the names, the files;
- `DOCUMENT.DAT`: 16 bytes of an empty PGD header, then DES-CBC parts, each from the same IV: a 0x60-byte header
  (`DOC `, version, game code, page-limit exponent), the page table (0x80 bytes a page: offset, size), and per
  page a 0x20-byte head, a table of the page's encrypted parts, and the PNG with only those parts encrypted."""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

try:                                              # cryptography 43 moved the old ciphers
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:                               # pragma: no cover
    TripleDES = algorithms.TripleDES

from .db import DB
from .manuals import name_match
from .minerva import utcnow
from .qbit import QBitError
from .sender import TAG as SENT_TAG, send

log = logging.getLogger(__name__)
SET = "No-Intro - Sony - PlayStation (PS one Classics) (PSN)"
SLUGS = {"psx"}                         # RomM platform folders whose games the set's packages are
SOURCE = ":psn"                         # stands where an Internet Archive item's name does; never a valid one
TAG = "hakobiya-manual"                 # on a torrent Hakobiya added only to read manuals out of it
KEY_NAMES = {"pkg_psp": 16, "pkg_ps3": 16, "document_key": 8, "document_iv": 8}
PKG_MAGIC = b"\x7fPKG"
PGD_EMPTY = b"\0PGD\1\0\0\0\1\0\0\0\0\0\0\0"
PNG = b"\x89PNG\r\n\x1a\n"
PNG_END = b"IEND\xae\x42\x60\x82"
INFO_SIZE = {0: 0x31e8, 1: 0x1f3e8}     # by the header's page-limit exponent: 99 or 999 pages
MAX_ITEMS = 4096
MAX_NAMES = 4 * 1024 * 1024
MAX_DOCUMENT = 256 * 1024 * 1024
MAX_SIDE = 4096


class PackageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Keys:
    pkg: tuple[tuple[str, bytes], ...]   # (name, AES key) of the package keys the file has
    document_key: bytes
    document_iv: bytes


def load_keys(path: Path) -> Keys | None:
    """The keys from `name = hex` lines, or None when there is no key file."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    found: dict[str, bytes] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, value = (part.strip() for part in line.partition("="))
        try:
            key = bytes.fromhex(value)
        except ValueError:
            key = b""
        if name not in KEY_NAMES or len(key) != KEY_NAMES[name]:
            raise PackageError(f"{path.name}: `{name}` isn't a key Hakobiya knows, or it has the wrong length")
        found[name] = key
    pkg = tuple((n, found[n]) for n in ("pkg_psp", "pkg_ps3") if n in found)
    if not pkg or "document_key" not in found or "document_iv" not in found:
        raise PackageError(f"{path.name} needs pkg_psp (or pkg_ps3), document_key and document_iv")
    return Keys(pkg, found["document_key"], found["document_iv"])


# ------------------------------------------------------------------ the package
def ctr(key: bytes, iv: bytes, offset: int, data: bytes) -> bytes:
    """AES-128-CTR of `data`, which starts `offset` bytes (a multiple of 16) into the encrypted part."""
    counter = ((int.from_bytes(iv, "big") + offset // 16) % (1 << 128)).to_bytes(16, "big")
    return Cipher(algorithms.AES(key), modes.CTR(counter)).decryptor().update(data)


class Forward:
    """Reads a zip member front to back: a deflated member can't be sought in."""

    def __init__(self, f):
        self.f, self.pos = f, 0

    def at(self, offset: int, n: int) -> bytes:
        if offset < self.pos:
            raise PackageError("the package's parts aren't in the order Hakobiya reads them in")
        while self.pos < offset:
            step = len(self.f.read(min(1 << 20, offset - self.pos)))
            if not step:
                raise PackageError("the package is shorter than its header says")
            self.pos += step
        data = self.f.read(n)
        self.pos += len(data)
        if len(data) != n:
            raise PackageError("the package is shorter than its header says")
        return data


def document_of(zip_path: Path, keys: Keys) -> bytes | None:
    """The package's DOCUMENT.DAT, decrypted out of the package; None when the package has none."""
    with zipfile.ZipFile(zip_path) as z:
        member = next((i for i in z.infolist() if i.filename.lower().endswith(".pkg")), None)
        if member is None:
            raise PackageError("no .pkg in the zip")
        with z.open(member) as f:
            src = Forward(f)
            head = src.at(0, 0x100)
            if head[:4] != PKG_MAGIC:
                raise PackageError("the .pkg doesn't start like a package")
            items, _, data_off, _ = struct.unpack(">IQQQ", head[0x14:0x30])
            iv = head[0x70:0x80]
            if not 0 < items <= MAX_ITEMS:
                raise PackageError("the package lists no files, or too many")
            raw = src.at(data_off, items * 32)
            entries = None
            for _, key in keys.pkg:
                table = ctr(key, iv, 0, raw)
                if struct.unpack(">I", table[:4])[0] == items * 32:       # the first name follows the table
                    entries = [struct.unpack(">IIQQII", table[i * 32:i * 32 + 32]) for i in range(items)]
                    break
            if entries is None:
                raise PackageError("the package keys don't open this package")
            end = max(name_off + name_size for name_off, name_size, *_ in entries)
            if not items * 32 <= end <= MAX_NAMES:
                raise PackageError("the package's file names are out of place")
            raw += src.at(data_off + items * 32, end + (-end) % 16 - items * 32)
            plain = {name: ctr(key, iv, 0, raw) for name, key in keys.pkg}
            for name_off, name_size, off, size, flags, _ in entries:
                # each item says which key it is under; the other one is tried too
                order = sorted(plain, key=lambda n: (n == "pkg_psp") != (flags >> 24 == 0x90))
                names = [plain[n][name_off:name_off + name_size] for n in order]
                which = next((n for n, text in zip(order, names) if text and all(32 <= b < 127 for b in text)), None)
                if which is None or plain[which][name_off:name_off + name_size].decode().rsplit("/", 1)[-1].upper() \
                        != "DOCUMENT.DAT":
                    continue
                if not 0 < size <= MAX_DOCUMENT or off % 16:
                    raise PackageError("the package's manual has a size or place Hakobiya doesn't read")
                body = src.at(data_off + off, size + (-size) % 16)
                return ctr(dict(keys.pkg)[which], iv, off, body)[:size]
    return None


# ------------------------------------------------------------------ the manual
def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def pages_of(document: bytes, keys: Keys) -> list[bytes]:
    """The manual's pages as PNG files, in order."""
    def des(data: bytes) -> bytes:
        if len(data) % 8:
            raise PackageError("a part of the manual has a length that can't be decrypted")
        d = Cipher(TripleDES(keys.document_key * 3), modes.CBC(keys.document_iv)).decryptor()   # single DES
        return d.update(bytes(data)) + d.finalize()

    if document[:16] != PGD_EMPTY or len(document) < 16 + 0x80:
        raise PackageError("the manual doesn't start like a PS one Classics manual")
    b = document[16:]
    head = des(b[:0x60])
    if head[:4] != b"DOC ":
        raise PackageError("the document key doesn't open this manual")
    size = INFO_SIZE.get(u32(head, 0x1c))
    if size is None or len(b) < 0x80 + size:
        raise PackageError("the manual's page table is of a kind Hakobiya doesn't read")
    info = des(b[0x80:0x80 + size])
    count = u32(info, 4)
    if info[:4] != b"\xff\xff\xff\xff" or 8 + count * 0x80 > size:
        raise PackageError("the manual's page table can't be read")
    pages = []
    for i in range(count):
        entry = info[8 + i * 0x80:8 + (i + 1) * 0x80]
        off, n = u32(entry, 0), u32(entry, 12)
        if not off:
            continue
        page = document[off:off + n]                 # ends with 0x20 bytes of checksums
        if len(page) != n or n < 0x60:
            raise PackageError(f"page {i + 1} lies outside the manual")
        page = page[:-0x20]
        parts = u32(des(page[:0x20]), 8)
        if 0x20 + parts * 8 > len(page):
            raise PackageError(f"page {i + 1} can't be read")
        table = des(page[0x20:0x20 + parts * 8])
        body = bytearray(page[0x20 + parts * 8:])
        for j in range(parts):
            start, length = u32(table, j * 8), u32(table, j * 8 + 4)
            if start + length > len(body):
                raise PackageError(f"page {i + 1} can't be read")
            body[start:start + length] = des(body[start:start + length])
        end = body.rfind(PNG_END)
        if body[:8] != PNG or end < 0:
            raise PackageError(f"page {i + 1} isn't a picture")
        pages.append(bytes(body[:end + 8]))
    return pages


def write_pdf(pages: list[bytes], out: Path) -> None:
    """One PDF page per PNG, a point a pixel, the pixels kept as they are (deflated, not JPEG): the scans are
    small and their text is fine."""
    objects: list[bytes] = [b"", b""]                # 1: the catalog, 2: the page list
    kids = []
    for png in pages:
        try:
            with Image.open(io.BytesIO(png)) as im:
                if max(im.size) > MAX_SIDE:
                    raise PackageError("a page is larger than a manual's page would be")
                if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
                    im = im.convert("RGBA")
                    white = Image.new("RGBA", im.size, "white")
                    im = Image.alpha_composite(white, im)
                (w, h), pixels = im.size, zlib.compress(im.convert("RGB").tobytes(), 9)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
            raise PackageError(f"a page can't be read as a picture ({e})") from None
        n = len(objects) + 1                         # this page's object number; its picture and drawing follow
        kids.append(n)
        draw = f"q {w} 0 0 {h} 0 0 cm /P Do Q".encode()
        objects += [
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Resources << /XObject << /P {n + 1} 0 R >> >>"
            f" /Contents {n + 2} 0 R >>".encode(),
            f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8"
            f" /Filter /FlateDecode /Length {len(pixels)} >>\nstream\n".encode() + pixels + b"\nendstream",
            f"<< /Length {len(draw)} >>\nstream\n".encode() + draw + b"\nendstream"]
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode()
    body, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    body += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".part")
    part.write_bytes(body)
    part.replace(out)


def manual_pdf(zip_path: Path, keys: Keys, out: Path) -> int:
    """Write the package's manual to `out`; the number of pages, 0 when the package has no manual."""
    try:
        document = document_of(zip_path, keys)
    except (zipfile.BadZipFile, struct.error, zlib.error, EOFError) as e:
        raise PackageError(f"the package can't be read ({e})") from None
    pages = pages_of(document, keys) if document else []
    if pages:
        write_pdf(pages, out)
    return len(pages)


# ------------------------------------------------------------------ fetching and following
class Packages:
    def __init__(self, db: DB, key_file: Path, out_dir: Path, data_root: Path, make_qbit, interval: float = 60):
        self.db, self.key_file, self.out_dir, self.data_root = db, key_file, out_dir, data_root
        self.make_qbit, self.interval = make_qbit, interval       # () -> QBit
        self.wake = asyncio.Event()

    def keys(self) -> Keys | None:
        return load_keys(self.key_file)

    def loaded(self) -> bool:
        """Whether the set's file list is here (someone opened the set, here or on its own page)."""
        row = self.db.one("SELECT file_count FROM sets WHERE name=?", (SET,))
        return bool(row and row["file_count"])

    def pdf(self, row_id: int) -> Path | None:
        """The manual read out of a package, while Hakobiya has it."""
        row = self.db.one("SELECT pdf_path FROM package_manuals WHERE id=? AND state='ready'", (row_id,))
        path = Path(row["pdf_path"]) if row and row["pdf_path"] else None
        return path if path and path.is_file() else None

    def name_of(self, row_id: int) -> str:
        row = self.db.one("SELECT path FROM package_manuals WHERE id=?", (row_id,))
        return row["path"].rsplit("/", 1)[-1] if row else ""

    def matches(self, names: list[str]) -> list[dict]:
        """The set's Japanese packages whose title is a game's, each with where its manual stands."""
        out = []
        for f in self.db.all("SELECT idx, path, filename, size FROM files WHERE set_name=? AND regions LIKE '%\"Japan\"%'"
                             " ORDER BY filename", (SET,)):
            match = name_match(names, f["filename"])
            if not match:
                continue
            row = self.db.one("SELECT * FROM package_manuals WHERE set_name=? AND idx=? AND path=?",
                              (SET, f["idx"], f["path"]))
            if row and row["state"] == "ready" and self.pdf(row["id"]) is None:
                row = None                           # its PDF is gone: as if never fetched
            out.append({**dict(f), "match": match, "row": dict(row) if row else None})
        return sorted(out, key=lambda f: f["match"] != "same")

    async def ask(self, minerva, idx: int, user: str | None) -> None:
        """Have qBittorrent fetch one package of the set, to be read when it is there."""
        if self.keys() is None:
            raise PackageError(f"{self.key_file.name} isn't in Hakobiya's config folder")
        f = self.db.one("SELECT * FROM files WHERE set_name=? AND idx=?", (SET, idx))
        if f is None or not self.loaded():
            raise PackageError("no such package in the set")
        t = await minerva.open_set(SET)              # the kept .torrent, or a new one when Minerva changed the set
        if not any(x.index == idx and x.path == f["path"] for x in t.files):
            raise PackageError("Minerva changed the set since this page was made. Reload it and pick again.")
        data = Path(self.db.one("SELECT torrent_path FROM sets WHERE name=?", (SET,))["torrent_path"]).read_bytes()
        async with self.make_qbit() as q:
            await send(q, t, data, f"Minerva_Myrient - {SET}.torrent", [idx], tags=f"{SENT_TAG},{TAG}")
        now = utcnow().isoformat()
        self.db.execute("INSERT INTO package_manuals(set_name, idx, path, size, infohash, state, asked_by, created_at,"
                        " updated_at) VALUES (?,?,?,?,?, 'downloading', ?,?,?) ON CONFLICT(set_name, idx) DO UPDATE SET"
                        " path=excluded.path, size=excluded.size, infohash=excluded.infohash, state='downloading',"
                        " note=NULL, pages=NULL, pdf_path=NULL, asked_by=excluded.asked_by, updated_at=excluded.updated_at",
                        (SET, idx, f["path"], f["size"], t.infohash, user, now, now))
        self.wake.set()

    def _set(self, row_id: int, state: str, note: str | None, pages: int | None = None, pdf: Path | None = None) -> None:
        self.db.execute("UPDATE package_manuals SET state=?, note=?, pages=?, pdf_path=?, updated_at=? WHERE id=?",
                        (state, note, pages, str(pdf) if pdf else None, utcnow().isoformat(), row_id))

    async def _read(self, row) -> bool:
        """Read an arrived package; False when it couldn't be read (it then stays in qBittorrent, for another go)."""
        out, name = self.out_dir / f"psn-{row['id']}.pdf", row["path"].rsplit("/", 1)[-1]
        try:
            keys = self.keys()
            if keys is None:
                raise PackageError(f"{self.key_file.name} isn't in Hakobiya's config folder any more")
            pages = await asyncio.to_thread(manual_pdf, self.data_root / "torrents" / row["path"], keys, out)
        except (PackageError, OSError) as e:
            self._set(row["id"], "failed", f"{str(e)[:300]}. The package is still in qBittorrent.")
            log.warning("package %s couldn't be read: %s", name, e)
            return False
        self._set(row["id"], "ready" if pages else "none", None, pages or None, out if pages else None)
        log.info("package %s: %s", name, f"{pages} pages" if pages else "no manual in it")
        return True

    async def _tidy(self, q, infohash: str) -> str:
        """Remove a torrent again, with its files, when Hakobiya added it only for manuals and nothing else uses
        it: no package still on its way, nothing sent from the set's own page, and no other file switched on or
        downloaded. `removed`, `waiting` (for another package), `kept` (it's used for other things) or `gone`."""
        info = await q.get("torrents/info", hashes=infohash)
        if not info:
            return "gone"
        if self.db.one("SELECT 1 FROM package_manuals WHERE infohash=? AND state='downloading'", (infohash,)):
            return "waiting"
        if TAG not in [t.strip() for t in (info[0].get("tags") or "").split(",")] or \
                self.db.one("SELECT 1 FROM jobs WHERE infohash=?", (infohash,)):
            return "kept"
        ours = {r["path"] for r in self.db.all("SELECT path FROM package_manuals WHERE infohash=?", (infohash,))}
        files = await q.get("torrents/files", hash=infohash)
        if any((f["priority"] > 0 or f["progress"] >= 1) and f["name"] not in ours for f in files):
            return "kept"
        await q.delete(infohash)
        log.info("removed the manual-only torrent %s from qBittorrent", infohash)
        return "removed"

    async def stop(self, row_id: int) -> None:
        """Give up on a package that is still on its way; its torrent goes if nothing else uses it."""
        row = self.db.one("SELECT * FROM package_manuals WHERE id=? AND state='downloading'", (row_id,))
        if row is None:
            return
        self._set(row_id, "failed", "stopped")
        async with self.make_qbit() as q:
            if await self._tidy(q, row["infohash"]) == "kept":
                self._set(row_id, "failed", "stopped; the package stays switched on in qBittorrent, where the set is "
                                            "used for other things")

    async def tick(self) -> None:
        """Read the packages that have arrived, then remove what was fetched only for them."""
        rows = self.db.all("SELECT * FROM package_manuals WHERE state='downloading' ORDER BY id")
        if not rows:
            return
        async with self.make_qbit() as q:
            for infohash in sorted({r["infohash"] for r in rows}):
                there = await q.get("torrents/info", hashes=infohash)    # asked first: an unknown hash's files are a 404
                files = {f["name"]: f for f in await q.get("torrents/files", hash=infohash)} if there else {}
                read = []
                for r in (r for r in rows if r["infohash"] == infohash):
                    f = files.get(r["path"])
                    if f is None:
                        self._set(r["id"], "failed", "not in qBittorrent any more")
                    elif f["progress"] >= 1:
                        if await self._read(r):
                            read.append(r["id"])
                    elif f["priority"] == 0:
                        self._set(r["id"], "failed", "switched off in qBittorrent")
                if read and await self._tidy(q, infohash) == "kept":
                    self.db.execute(f"UPDATE package_manuals SET note=? WHERE id IN ({','.join('?' * len(read))})",
                                    ("The package stays in qBittorrent, where the set is used for other things.", *read))

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except (QBitError, OSError, PackageError) as e:      # qBittorrent down, a broken key file: next round
                log.warning("package round failed: %s", e)
            except Exception:
                log.exception("package round failed")
            try:
                await asyncio.wait_for(self.wake.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
