"""Manuals out of PlayStation Store packages.

Sony's PS one Classics release of a game carries the game's manual: `DOCUMENT.DAT` inside the package, a list of
PNG pages (480 pixels wide). A PSP game's package can carry one the same way. Minerva's sets of these packages are
torrents like any other, so a package is fetched through qBittorrent (only that file switched on), read, and the
manual saved as a PDF for the Manuals page, where it is looked at and ticked like one from the Internet Archive.
Never automatic: a package is fetched only when asked for on a game's page.

The packages and the manuals are encrypted with fixed keys. They are not in this repository: Hakobiya reads them
from `psn-keys.txt` in its config folder, and the feature stays off without that file. The PSP set is offered
when the file also has the names only PSP packages need.

Layout, as read here:
- the zip holds one `.pkg`: a 0x100-byte header (magic, item count, where the data starts, the IV), then the data,
  AES-128-CTR from its start: the item table (32 bytes an item), the names, the files;
- `DOCUMENT.DAT`: 16 bytes of an empty PGD header, then DES-CBC parts, each from the same IV: a 0x60-byte header
  (`DOC `, version, game code, page-limit exponent), the page table (0x80 bytes a page: offset, size), and per
  page a 0x20-byte head, a table of the page's encrypted parts, and the PNG with only those parts encrypted.
  Checksums follow the header, the table and each page: 0x20 bytes in a PS one Classics package, 0x30 in a PSP
  one. They are skipped, not checked;
- a PS one Classics manual is under one fixed DES key. A PSP one is under its own, XORed with a fixed constant,
  and that key is what `DOCINFO.EDAT` next to it holds: 0x80 bytes of an EDAT header, then a PGD (the PSP's wrap
  for encrypted, checked data) of one 16-byte block. The PGD's own key is taken back out of its header check; the
  data's key and description (0x30 to 0x60) and the data (0x90) are under the fixed AES keys 0x38, 0x39 and 0x63
  of the PSP's key engine; the check of the data's table (0x60) tells whether the keys fit."""

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
from cryptography.hazmat.primitives.cmac import CMAC
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


@dataclass(frozen=True)
class PackageSet:
    kind: str                           # how its manuals are encrypted; a page's form names the set by it too
    name: str                           # Minerva's set
    slugs: frozenset[str]               # RomM platform folders whose games the set's packages are
    source: str                         # stands where an Internet Archive item's name does; never a valid one
    label: str


PS1 = PackageSet("ps1", "No-Intro - Sony - PlayStation (PS one Classics) (PSN)", frozenset({"psx"}), ":psn",
                 "PS one Classics")
PSP = PackageSet("psp", "No-Intro - Sony - PlayStation Portable (PSN) (Encrypted)", frozenset({"psp"}), ":psn-psp",
                 "PSP PlayStation Store")
SETS = (PS1, PSP)
TAG = "hakobiya-manual"                 # on a torrent Hakobiya added only to read manuals out of it
KEY_NAMES = {"pkg_psp": 16, "pkg_ps3": 16, "document_key": 8, "document_iv": 8,
             "kirk_38": 16, "kirk_39": 16, "kirk_63": 16, "pgd_mac_xor": 16, "pgd_seed_xor": 16, "pgd_stream_xor": 16,
             "document_xor": 8}
NEEDS = {"ps1": (),                     # by kind: the names its packages need on top of the file's first four
         "psp": ("pkg_psp", "kirk_38", "kirk_39", "kirk_63", "pgd_mac_xor", "pgd_seed_xor", "pgd_stream_xor",
                 "document_xor")}
TAIL = {"ps1": 0x20, "psp": 0x30}       # by kind: the checksums after each part of a manual
PKG_MAGIC = b"\x7fPKG"
PGD_EMPTY = b"\0PGD\1\0\0\0\1\0\0\0\0\0\0\0"
PNG = b"\x89PNG\r\n\x1a\n"
PNG_END = b"IEND\xae\x42\x60\x82"
INFO_SIZE = {0: 0x31e8, 1: 0x1f3e8}     # by the header's page-limit exponent: 99 or 999 pages
MAX_ITEMS = 4096
MAX_NAMES = 4 * 1024 * 1024
MAX_DOCUMENT = 256 * 1024 * 1024
MAX_EDAT = 4096
MAX_SIDE = 4096


class PackageError(RuntimeError):
    pass


def set_for(slug: str | None) -> PackageSet | None:
    """The set whose packages are a RomM platform folder's games."""
    return next((s for s in SETS if slug in s.slugs), None)


def set_by(**field: str) -> PackageSet | None:
    """The set with this `kind`, `name` or `source`."""
    (attr, value), = field.items()
    return next((s for s in SETS if getattr(s, attr) == value), None)


@dataclass(frozen=True)
class Keys:
    pkg: tuple[tuple[str, bytes], ...]   # (name, AES key) of the package keys the file has
    document_key: bytes
    document_iv: bytes
    psp: tuple[tuple[str, bytes], ...] = ()   # (name, value) of what the file has of the names PSP packages need

    def missing(self, kind: str) -> list[str]:
        """The names a kind of package needs that the key file doesn't have."""
        have = dict(self.pkg + self.psp)
        return [n for n in NEEDS[kind] if n not in have]


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
    psp = tuple((n, found[n]) for n in NEEDS["psp"] if n in found and n != "pkg_psp")
    return Keys(pkg, found["document_key"], found["document_iv"], psp)


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


def files_of(zip_path: Path, keys: Keys, wanted: dict[str, int]) -> dict[str, bytes]:
    """The package's files with these names (in capitals, without their folders; each with the most bytes it may
    have), decrypted out of the package. A name the package doesn't have is left out."""
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
            found: dict[str, tuple[int, int, str]] = {}
            for name_off, name_size, off, size, flags, _ in entries:
                # each item says which key it is under; the other one is tried too
                order = sorted(plain, key=lambda n: (n == "pkg_psp") != (flags >> 24 == 0x90))
                names = [plain[n][name_off:name_off + name_size] for n in order]
                which = next((n for n, text in zip(order, names) if text and all(32 <= b < 127 for b in text)), None)
                if which is None:
                    continue
                name = plain[which][name_off:name_off + name_size].decode().rsplit("/", 1)[-1].upper()
                if name not in wanted or name in found:
                    continue
                if not 0 < size <= wanted[name] or off % 16:
                    raise PackageError(f"the package's {name} has a size or place Hakobiya doesn't read")
                found[name] = (off, size, which)
            out = {}
            for name, (off, size, which) in sorted(found.items(), key=lambda item: item[1]):   # as they lie
                body = src.at(data_off + off, size + (-size) % 16)
                out[name] = ctr(dict(keys.pkg)[which], iv, off, body)[:size]
    return out


# ------------------------------------------------------------------ the manual
def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def aes(key: bytes, data: bytes, encrypt: bool = False) -> bytes:
    """AES-128-CBC from an IV of zeros, the way the PSP's key engine runs its fixed keys."""
    c = Cipher(algorithms.AES(key), modes.CBC(bytes(16)))
    return (c.encryptor() if encrypt else c.decryptor()).update(bytes(data))


def document_key(edat: bytes, keys: Keys) -> bytes:
    """The DES key of a PSP package's manual, out of the package's `DOCINFO.EDAT`."""
    k = dict(keys.psp)
    lacks = [n for n in NEEDS["psp"] if n != "pkg_psp" and n not in k]
    if lacks:
        raise PackageError(f"the key file has no {', '.join(lacks)}")
    p = edat[0x80:]
    if len(p) < 0xb0 or edat[:8] != b"\0PSPEDAT" or u32(edat, 0x0c) != 0x80 or p[:4] != b"\0PGD" or \
            (u32(p, 4), u32(p, 8)) != (1, 1):
        raise PackageError("the package's DOCINFO.EDAT is of a kind Hakobiya doesn't read")

    def mac(data: bytes) -> bytes:
        c = CMAC(algorithms.AES(k["kirk_38"]))
        c.update(data)
        return xor(c.finalize(), k["pgd_mac_xor"])

    def unbox(data: bytes, seed: bytes) -> bytes:
        t = xor(aes(k["kirk_39"], xor(xor(seed, own), k["pgd_seed_xor"])), k["pgd_stream_xor"])
        counters = b"".join(t[:12] + struct.pack("<I", 1 + i) for i in range(len(data) // 16))
        return xor(data, aes(k["kirk_63"], counters))

    own = xor(mac(p[:0x70]), aes(k["kirk_38"], p[0x70:0x80]))        # the PGD's own key, out of its header check
    inner = unbox(p[0x30:0x60], p[0x10:0x20])                        # the data's key, then what the data is like
    size, block, off = struct.unpack_from("<III", inner, 0x14)
    # one 8-byte key in one block at 0x90, its table of one check after it; wrong keys give anything here
    if (size, off) != (8, 0x90) or block < 16 or aes(k["kirk_38"], xor(mac(p[0xa0:0xb0]), own), True) != p[0x60:0x70]:
        raise PackageError("the PSP keys don't open the package's DOCINFO.EDAT, or the file is damaged")
    return xor(unbox(p[0x90:0xa0], inner[:16])[:size], k["document_xor"])


def pages_of(document: bytes, key: bytes, iv: bytes, kind: str = "ps1") -> list[bytes]:
    """The manual's pages as PNG files, in order."""
    def des(data: bytes) -> bytes:
        if len(data) % 8:
            raise PackageError("a part of the manual has a length that can't be decrypted")
        d = Cipher(TripleDES(key * 3), modes.CBC(iv)).decryptor()    # single DES
        return d.update(bytes(data)) + d.finalize()

    tail = TAIL[kind]
    if document[:16] != PGD_EMPTY or len(document) < 16 + 0x60 + tail:
        raise PackageError("the manual doesn't start like a package's manual")
    b = document[16:]
    head = des(b[:0x60])
    if head[:4] != b"DOC ":
        raise PackageError("the document key doesn't open this manual" if kind == "ps1" else
                           "the key in the package's DOCINFO.EDAT doesn't open this manual")
    size = INFO_SIZE.get(u32(head, 0x1c))
    if size is None or len(b) < 0x60 + tail + size:
        raise PackageError("the manual's page table is of a kind Hakobiya doesn't read")
    info = des(b[0x60 + tail:0x60 + tail + size])
    count = u32(info, 4)
    if info[:4] != b"\xff\xff\xff\xff" or 8 + count * 0x80 > size:
        raise PackageError("the manual's page table can't be read")
    pages = []
    for i in range(count):
        entry = info[8 + i * 0x80:8 + (i + 1) * 0x80]
        off, n = u32(entry, 0), u32(entry, 12)
        if not off:
            continue
        page = document[off:off + n]                 # ends with its checksums
        if len(page) != n or n < 0x40 + tail:
            raise PackageError(f"page {i + 1} lies outside the manual")
        page = page[:-tail]
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


def manual_pdf(zip_path: Path, keys: Keys, out: Path, kind: str = "ps1") -> int:
    """Write the package's manual to `out`; the number of pages, 0 when the package has no manual."""
    wanted = {"DOCUMENT.DAT": MAX_DOCUMENT} | ({"DOCINFO.EDAT": MAX_EDAT} if kind == "psp" else {})
    try:
        files = files_of(zip_path, keys, wanted)
    except (zipfile.BadZipFile, struct.error, zlib.error, EOFError) as e:
        raise PackageError(f"the package can't be read ({e})") from None
    document, key = files.get("DOCUMENT.DAT"), keys.document_key
    if document and kind == "psp":
        if "DOCINFO.EDAT" not in files:
            raise PackageError("the package has a manual, but not the DOCINFO.EDAT that holds its key")
        key = document_key(files["DOCINFO.EDAT"], keys)
    pages = pages_of(document, key, keys.document_iv, kind) if document else []
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

    def loaded(self, s: PackageSet) -> bool:
        """Whether the set's file list is here (someone opened the set, here or on its own page)."""
        row = self.db.one("SELECT file_count FROM sets WHERE name=?", (s.name,))
        return bool(row and row["file_count"])

    def pdf(self, row_id: int, s: PackageSet | None = None) -> Path | None:
        """The manual read out of a package (one of this set, when a set is given), while Hakobiya has it."""
        row = self.db.one("SELECT set_name, pdf_path FROM package_manuals WHERE id=? AND state='ready'", (row_id,))
        path = Path(row["pdf_path"]) if row and row["pdf_path"] and (s is None or row["set_name"] == s.name) else None
        return path if path and path.is_file() else None

    def name_of(self, row_id: int) -> str:
        row = self.db.one("SELECT path FROM package_manuals WHERE id=?", (row_id,))
        return row["path"].rsplit("/", 1)[-1] if row else ""

    def matches(self, s: PackageSet, names: list[str]) -> list[dict]:
        """The set's Japanese packages whose title is a game's, each with where its manual stands."""
        out = []
        for f in self.db.all("SELECT idx, path, filename, size FROM files WHERE set_name=? AND regions LIKE '%\"Japan\"%'"
                             " ORDER BY filename", (s.name,)):
            match = name_match(names, f["filename"])
            if not match:
                continue
            row = self.db.one("SELECT * FROM package_manuals WHERE set_name=? AND idx=? AND path=?",
                              (s.name, f["idx"], f["path"]))
            if row and row["state"] == "ready" and self.pdf(row["id"]) is None:
                row = None                           # its PDF is gone: as if never fetched
            out.append({**dict(f), "match": match, "row": dict(row) if row else None})
        return sorted(out, key=lambda f: f["match"] != "same")

    async def ask(self, minerva, s: PackageSet, idx: int, user: str | None) -> None:
        """Have qBittorrent fetch one package of the set, to be read when it is there."""
        keys = self.keys()
        if keys is None:
            raise PackageError(f"{self.key_file.name} isn't in Hakobiya's config folder")
        if keys.missing(s.kind):
            raise PackageError(f"{self.key_file.name} has no {', '.join(keys.missing(s.kind))}")
        f = self.db.one("SELECT * FROM files WHERE set_name=? AND idx=?", (s.name, idx))
        if f is None or not self.loaded(s):
            raise PackageError("no such package in the set")
        t = await minerva.open_set(s.name)           # the kept .torrent, or a new one when Minerva changed the set
        if not any(x.index == idx and x.path == f["path"] for x in t.files):
            raise PackageError("Minerva changed the set since this page was made. Reload it and pick again.")
        data = Path(self.db.one("SELECT torrent_path FROM sets WHERE name=?", (s.name,))["torrent_path"]).read_bytes()
        async with self.make_qbit() as q:
            await send(q, t, data, f"Minerva_Myrient - {s.name}.torrent", [idx], tags=f"{SENT_TAG},{TAG}")
        now = utcnow().isoformat()
        self.db.execute("INSERT INTO package_manuals(set_name, idx, path, size, infohash, state, asked_by, created_at,"
                        " updated_at) VALUES (?,?,?,?,?, 'downloading', ?,?,?) ON CONFLICT(set_name, idx) DO UPDATE SET"
                        " path=excluded.path, size=excluded.size, infohash=excluded.infohash, state='downloading',"
                        " note=NULL, pages=NULL, pdf_path=NULL, asked_by=excluded.asked_by, updated_at=excluded.updated_at",
                        (s.name, idx, f["path"], f["size"], t.infohash, user, now, now))
        self.wake.set()

    def _set(self, row_id: int, state: str, note: str | None, pages: int | None = None, pdf: Path | None = None) -> None:
        self.db.execute("UPDATE package_manuals SET state=?, note=?, pages=?, pdf_path=?, updated_at=? WHERE id=?",
                        (state, note, pages, str(pdf) if pdf else None, utcnow().isoformat(), row_id))

    async def _read(self, row) -> bool:
        """Read an arrived package; False when it couldn't be read (it then stays in qBittorrent, for another go)."""
        out, name = self.out_dir / f"psn-{row['id']}.pdf", row["path"].rsplit("/", 1)[-1]
        try:
            keys, s = self.keys(), set_by(name=row["set_name"])
            if keys is None:
                raise PackageError(f"{self.key_file.name} isn't in Hakobiya's config folder any more")
            if s is None:
                raise PackageError("Hakobiya doesn't read this set's packages")
            if keys.missing(s.kind):
                raise PackageError(f"{self.key_file.name} has no {', '.join(keys.missing(s.kind))} any more")
            pages = await asyncio.to_thread(manual_pdf, self.data_root / "torrents" / row["path"], keys, out, s.kind)
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
