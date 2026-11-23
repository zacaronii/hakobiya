"""Manuals out of PS one Classics packages: the reader (against packages built here, with keys made up here), the
PDF, and the page that fetches a package, reads it, removes it again and puts its manual into RomM."""
import asyncio
import hashlib
import io
import os
import random
import struct
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from hakobiya import minerva as mv
from hakobiya import packages, torrent
from hakobiya.app import Settings, create_app
from hakobiya.db import DB
from hakobiya.qbit import QBit

HEAD = {"origin": "http://testserver", "x-hakobiya-key": "k", "x-authentik-username": "someone"}
KEYS = packages.Keys((("pkg_psp", os.urandom(16)), ("pkg_ps3", os.urandom(16))), os.urandom(8), os.urandom(8))
OLD = b"%PDF-1.4\nthe manual RomM had\n%%EOF"


# ------------------------------------------------------------------ a package, built the way one is read
def png(width=48, height=64, seed=1, mode="RGB") -> bytes:
    rnd = random.Random(seed)
    im = Image.frombytes(mode, (width, height), bytes(rnd.randrange(256) for _ in range(width * height * len(mode))))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def des(data: bytes, keys=KEYS) -> bytes:
    e = Cipher(packages.TripleDES(keys.document_key * 3), modes.CBC(keys.document_iv)).encryptor()
    return e.update(bytes(data)) + e.finalize()


def document(pages: list[bytes | None], keys=KEYS, exponent=0) -> bytes:
    """A DOCUMENT.DAT with these PNG pages; None leaves a page's slot empty."""
    size = packages.INFO_SIZE[exponent]
    head, info = bytearray(0x60), bytearray(size)
    head[:4] = b"DOC "
    struct.pack_into("<I", head, 0x1c, exponent)
    info[:4] = b"\xff" * 4
    struct.pack_into("<I", info, 4, len(pages))
    out = bytearray(packages.PGD_EMPTY + des(head, keys) + bytes(0x20) + bytes(size))
    for i, page in enumerate(pages):
        if page is None:
            continue
        body = bytearray(page + bytes(-len(page) % 8 + 16))
        parts = [(0, 0x40), (0x80, 0x28), (len(body) - 16, 16)]        # only parts of a page are encrypted
        for start, length in parts:
            body[start:start + length] = des(body[start:start + length], keys)
        first = bytearray(0x20)
        struct.pack_into("<I", first, 8, len(parts))
        whole = des(first, keys) + des(b"".join(struct.pack("<II", *p) for p in parts), keys) + body + bytes(0x20)
        struct.pack_into("<I", info, 8 + i * 0x80, len(out))
        struct.pack_into("<I", info, 8 + i * 0x80 + 12, len(whole))
        out += whole
    out[16 + 0x80:16 + 0x80 + size] = des(info, keys)
    return bytes(out)


def package(items: list[tuple[str, bytes, str]], keys=KEYS, table_key="pkg_ps3") -> bytes:
    """A .pkg with these (name, content, key name) items: the table under one key, each item under its own."""
    by_name, iv = dict(keys.pkg), os.urandom(16)
    plain = bytearray(len(items) * 32)
    spans = [(0, len(plain), table_key)]                                 # (start, end, key) of what is encrypted how
    names = []
    for name, _, key in items:
        names.append((len(plain), len(name)))
        spans.append((len(plain), len(plain) + len(name), key))
        plain += name.encode()
    plain += bytes(-len(plain) % 16)
    for i, (name, content, key) in enumerate(items):
        struct.pack_into(">IIQQII", plain, i * 32, *names[i], len(plain), len(content), (0x90 if key == "pkg_psp" else 0x80) << 24, 0)
        spans.append((len(plain), len(plain) + len(content), key))
        plain += content + bytes(-len(content) % 16)
    under = {n: packages.ctr(k, iv, 0, bytes(plain)) for n, k in by_name.items()}
    data = bytearray(len(plain))
    for start, end, key in spans:
        data[start:end] = under[key][start:end]
    head = bytearray(0x180)                                              # the data doesn't start right after the header
    head[:4] = packages.PKG_MAGIC
    struct.pack_into(">IQQQ", head, 0x14, len(items), len(head) + len(data), len(head), len(data))
    head[0x70:0x80] = iv
    return bytes(head) + bytes(data)


def zipped(path: Path, pkg: bytes | None, name="UP0000-TEST00000_00-0000000000000000.pkg") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("readme.txt", "not a package")
        if pkg is not None:
            z.writestr(name, pkg)
    return path


PAGES = [png(48, 64, 1), png(64, 48, 2), png(40, 40, 3)]
GAME = [("USRDIR/CONTENT/EBOOT.PBP", os.urandom(5000), "pkg_psp"), ("USRDIR/CONTENT/ICON0.PNG", png(8, 8, 9), "pkg_ps3")]


def with_manual(pages=PAGES, keys=KEYS) -> bytes:
    return package([GAME[1], GAME[0], ("USRDIR/CONTENT/DOCUMENT.DAT", document(pages, keys), "pkg_psp")], keys)


# ------------------------------------------------------------------ the key file
def test_the_keys_come_from_a_file_in_the_config_folder(tmp_path):
    f = tmp_path / "psn-keys.txt"
    assert packages.load_keys(f) is None                                 # no file: the feature is off
    f.write_text("# made up for this test\npkg_psp = " + "0a" * 16 + "\ndocument_key=" + "0b" * 8 +
                 "   # eight bytes\n\ndocument_iv = " + "0C" * 8 + "\n")
    keys = packages.load_keys(f)
    assert keys == packages.Keys((("pkg_psp", b"\x0a" * 16),), b"\x0b" * 8, b"\x0c" * 8)
    for bad, says in (("pkg_psp = " + "0a" * 15, "wrong length"), ("pkg_vita = " + "0a" * 16, "isn't a key"),
                      ("pkg_psp = zz", "wrong length"), ("document_key = " + "0b" * 8, "needs pkg_psp")):
        f.write_text(bad + "\n")
        with pytest.raises(packages.PackageError, match=says) as e:
            packages.load_keys(f)
        assert "0a0a" not in str(e.value)                                # a key is never repeated in a message


# ------------------------------------------------------------------ the reader and the PDF
def test_a_package_s_manual_becomes_a_pdf_with_the_same_pixels(tmp_path):
    z = zipped(tmp_path / "in/Moon Garden (Japan).zip", with_manual(PAGES[:2] + [None] + PAGES[2:]))
    out = tmp_path / "out/manual.pdf"
    assert packages.manual_pdf(z, KEYS, out) == 3
    pdf = PdfReader(out)
    assert len(pdf.pages) == 3 and not list(out.parent.glob("*.part"))
    for page, source in zip(pdf.pages, PAGES):
        want = Image.open(io.BytesIO(source))
        assert (page.mediabox.width, page.mediabox.height) == want.size      # a point a pixel
        got = page.images[0].image
        assert got.size == want.size and got.convert("RGB").tobytes() == want.tobytes()


def test_the_larger_page_table_and_see_through_pages_are_read_too(tmp_path):
    clear = png(16, 16, 4, "RGBA")
    z = zipped(tmp_path / "a.zip", package([("DOCUMENT.DAT", document([clear, PAGES[0]], exponent=1), "pkg_ps3")]))
    assert packages.manual_pdf(z, KEYS, tmp_path / "a.pdf") == 2
    got = PdfReader(tmp_path / "a.pdf").pages[0].images[0].image.convert("RGB")
    white = Image.new("RGBA", (16, 16), "white")
    assert got.tobytes() == Image.alpha_composite(white, Image.open(io.BytesIO(clear))).convert("RGB").tobytes()


def test_one_package_key_is_enough_when_it_is_the_package_s(tmp_path):
    psp = packages.Keys(KEYS.pkg[:1], KEYS.document_key, KEYS.document_iv)
    z = zipped(tmp_path / "a.zip", package([("DOCUMENT.DAT", document(PAGES[:1]), "pkg_psp")], table_key="pkg_psp"))
    assert packages.manual_pdf(z, psp, tmp_path / "a.pdf") == 1
    ps3 = packages.Keys(KEYS.pkg[1:], KEYS.document_key, KEYS.document_iv)
    with pytest.raises(packages.PackageError, match="package keys don't open"):
        packages.manual_pdf(z, ps3, tmp_path / "b.pdf")


def test_a_package_without_a_manual_gives_none(tmp_path):
    z = zipped(tmp_path / "a.zip", package(GAME))
    assert packages.manual_pdf(z, KEYS, tmp_path / "a.pdf") == 0 and not (tmp_path / "a.pdf").exists()


def test_what_can_t_be_read_says_why_and_writes_nothing(tmp_path):
    out = tmp_path / "a.pdf"
    other = packages.Keys(KEYS.pkg, os.urandom(8), KEYS.document_iv)
    cut = with_manual()[:-0x2000]
    not_png = package([("DOCUMENT.DAT", document([b"GIF89a" + bytes(300)]), "pkg_psp")])
    huge = package([("DOCUMENT.DAT", document([png(packages.MAX_SIDE + 1, 1, 5)]), "pkg_psp")])
    cases = [(zipped(tmp_path / "1.zip", with_manual()), other, "document key doesn't open"),
             (zipped(tmp_path / "2.zip", None), KEYS, "no .pkg in the zip"),
             (zipped(tmp_path / "3.zip", b"\0" * 0x400), KEYS, "doesn't start like a package"),
             (zipped(tmp_path / "4.zip", cut), KEYS, "shorter than its header says|lies outside"),
             (zipped(tmp_path / "5.zip", not_png), KEYS, "isn't a picture"),
             (zipped(tmp_path / "6.zip", huge), KEYS, "larger than a manual's page"),
             (zipped(tmp_path / "7.zip", package([("DOCUMENT.DAT", b"\0" * 0x200, "pkg_psp")])), KEYS, "doesn't start like")]
    (tmp_path / "8.zip").write_bytes(b"not a zip at all")
    cases.append((tmp_path / "8.zip", KEYS, "can't be read"))
    for z, keys, says in cases:
        with pytest.raises(packages.PackageError, match=says):
            packages.manual_pdf(z, keys, out)
        assert not out.exists()


# ------------------------------------------------------------------ the page: fetch, read, remove, apply
def bencode(x) -> bytes:
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(map(bencode, x)) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(x.items())) + b"e"


FILES = ["Moon Garden (Japan).zip", "Moon Garden 2 (Japan).zip", "Moon Garden (USA).zip", "Star Road (Japan).zip",
         "Kaze no Machi (Japan).zip"]
TORRENT = bencode({b"info": {b"name": b"PS one Classics", b"piece length": 16384, b"pieces": b"\0" * 20,
                             b"files": [{b"length": 600_000_000 + i, b"path": [f.encode()]} for i, f in enumerate(FILES)]}})
T = torrent.parse(TORRENT)
INDEX = ('<table><tr><td class="indexcolname"><a href="Minerva_Myrient%20-%20No-Intro%20-%20Sony%20-%20PlayStation%20'
         '(PS%20one%20Classics)%20(PSN).torrent">the set</a></td><td class="indexcollastmod">2026-01-01 00:00  </td>'
         '<td class="indexcolsize"> 1M</td></tr></table>')
ROMS = [
    {"id": 1, "name": "Moon Garden", "fs_name": "Moon Garden (Japan).chd", "fs_name_no_tags": "Moon Garden",
     "alternative_names": ["つきのにわ"], "platform_id": 9, "platform_fs_slug": "psx",
     "path_manual": "roms/9/1/manual/1.pdf", "url_manual": "https://x/mediaJeu.php?devid=d&media=manuel(us)&gameid=1"},
    {"id": 2, "name": "Moon Garden", "fs_name": "Moon Garden (Japan).gba", "fs_name_no_tags": "Moon Garden",
     "alternative_names": [], "platform_id": 4, "platform_fs_slug": "gba", "path_manual": "", "url_manual": ""},
    {"id": 3, "name": "Kaze no Machi", "fs_name": "Kaze no Machi (Japan).chd", "fs_name_no_tags": "Kaze no Machi",
     "alternative_names": [], "platform_id": 9, "platform_fs_slug": "psx", "path_manual": "", "url_manual": ""},
]


class FakeQBit:
    """Just enough of qBittorrent's WebAPI for one set's torrent."""

    def __init__(self, tags: str | None = None, on=()):
        self.torrent, self.calls, self.deleted = None, [], []
        if tags is not None:
            self.load(tags, on)

    def load(self, tags, on=()):
        self.torrent = {"hash": T.infohash, "name": T.name, "state": "downloading", "tags": tags,
                        "files": [{"index": f.index, "name": f.path, "priority": 1 if f.index in on else 0,
                                   "progress": 0.0} for f in T.files]}

    def file(self, idx: int) -> dict:
        return self.torrent["files"][idx]

    def on(self) -> list[int]:
        return [f["index"] for f in self.torrent["files"] if f["priority"] > 0]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v2/")
        multipart = b"form-data" in request.headers.get("content-type", "").encode()
        form = parse_qs(request.content.decode("latin-1")) if request.method == "POST" and not multipart else {}
        self.calls.append(path)
        if path in ("auth/login", "auth/logout"):
            return httpx.Response(204)
        if path == "torrents/info":
            mine = self.torrent and request.url.params.get("hashes", T.infohash) == T.infohash
            return httpx.Response(200, json=[{k: v for k, v in self.torrent.items() if k != "files"}] if mine else [])
        if path == "torrents/files":
            if not self.torrent or request.url.params["hash"] != T.infohash:
                return httpx.Response(404)
            return httpx.Response(200, json=self.torrent["files"])
        if path == "torrents/add":
            tags = request.content.split(b'name="tags"\r\n\r\n', 1)[1].split(b"\r\n", 1)[0].decode()
            self.load(tags, on=range(len(T.files)))                      # qBittorrent adds with every file on
            return httpx.Response(200, text="Ok.")
        if path == "torrents/filePrio":
            for i in form["id"][0].split("|"):
                self.file(int(i))["priority"] = int(form["priority"][0])
            return httpx.Response(200)
        if path == "torrents/start":
            return httpx.Response(200)
        if path == "torrents/delete":
            assert form["hashes"] == [T.infohash] and form["deleteFiles"] == ["true"]
            self.deleted.append(T.infohash)
            self.torrent = None
            return httpx.Response(200)
        return httpx.Response(404)


class Env:
    def __init__(self, tmp_path, fake):
        self.tmp, self.fake, self.uploads = tmp_path, fake, []
        self.res, self.config, self.data = tmp_path / "res", tmp_path / "config", tmp_path / "data"
        (self.res / "roms/9/1/manual").mkdir(parents=True)
        (self.res / "roms/9/1/manual/1.pdf").write_bytes(OLD)
        self.db = DB(":memory:")
        s = Settings(self.config, tmp_path / "roms", "http://qbit", "u", "p", proxy_key="k", data_root=self.data,
                     romm_url="http://romm", romm_token="tok", romm_resources=self.res, archive_pause=0)
        self.c = TestClient(create_app(s, mv.Minerva(self.db, tmp_path / "t"), None, self.db), headers=HEAD)
        # the app's own follower runs in the container; here its rounds are made by hand, on the same database
        self.packages = packages.Packages(self.db, self.config / "psn-keys.txt", self.config / "package-manuals",
                                          self.data, lambda: QBit("http://qbit", "u", "p"))

    def keys(self):
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / "psn-keys.txt").write_text(
            "".join(f"{n} = {k.hex()}\n" for n, k in KEYS.pkg) +
            f"document_key = {KEYS.document_key.hex()}\ndocument_iv = {KEYS.document_iv.hex()}\n")

    def ready(self):
        """The key file is there and the set's list is loaded."""
        self.keys()
        assert self.post("open", rom="1").status_code == 303
        return self

    def post(self, what, **data):
        return self.c.post(f"/manuals/psn/{what}", data=data, follow_redirects=False)

    def said(self, r) -> str:
        return unquote(r.headers["location"])

    def arrive(self, idx: int, pkg: bytes | None):
        """qBittorrent finished the file."""
        zipped(self.data / "torrents" / T.files[idx].path, pkg)
        self.fake.file(idx)["progress"] = 1.0

    def round(self):
        asyncio.run(self.packages.tick())

    def row(self, idx=0):
        return self.db.one("SELECT * FROM package_manuals WHERE idx=?", (idx,))

    def page(self, rom=1) -> str:
        return self.c.get(f"/manuals/g/{rom}").text


@pytest.fixture
def env(tmp_path):
    made = []
    with respx.mock(assert_all_called=False) as router:
        def make(fake=None) -> Env:
            e = Env(tmp_path, fake or FakeQBit())
            roms = {r["id"]: dict(r) for r in ROMS}

            def upload(request, rom_id):
                data = request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                e.uploads.append((int(rom_id), data))
                (e.res / f"roms/9/{rom_id}/manual/{rom_id}.pdf").write_bytes(data)
                return httpx.Response(201)

            router.get("http://romm/api/platforms").mock(return_value=httpx.Response(200, json=[
                {"id": 9, "name": "PlayStation", "display_name": "PlayStation", "fs_slug": "psx", "rom_count": 2}]))
            router.get("http://romm/api/roms").mock(return_value=httpx.Response(200, json={
                "items": [r for r in ROMS if r["platform_id"] == 9], "total": 2}))
            router.get(url__regex=r"http://romm/api/roms/(?P<rom_id>\d+)$").mock(
                side_effect=lambda request, rom_id: httpx.Response(200, json=roms[int(rom_id)]))
            router.post(url__regex=r"http://romm/api/roms/(?P<rom_id>\d+)/manuals$").mock(side_effect=upload)
            router.get("https://archive.org/advancedsearch.php").mock(
                return_value=httpx.Response(200, json={"response": {"docs": []}}))
            router.get(url__regex=r"https://archive\.org/metadata/.*").mock(return_value=httpx.Response(200, json={
                "metadata": {"title": "Made-up manuals"}, "files": [{"name": "Other Game.pdf", "source": "original", "size": "9"}]}))
            e.index = router.get(mv.CDN).mock(return_value=httpx.Response(200, text=INDEX))
            e.set_file = router.get(url__regex=r".*\(PSN\)\.torrent$").mock(return_value=httpx.Response(200, content=TORRENT))
            router.route(host="qbit").mock(side_effect=e.fake.handle)
            made.append(e)
            return e
        yield make


def test_the_section_is_for_playstation_games_and_needs_the_key_file(env):
    e = env()
    page = e.page()
    assert "In PS one Classics packages" in page and "psn-keys.txt" in page and "/manuals/psn/" not in page
    assert "In PS one Classics packages" not in e.page(2)                 # another platform's game
    assert "isn't in Hakobiya's config folder" in e.said(e.post("fetch", rom="1", idx="0"))
    e.keys()
    page = e.page()
    assert 'formaction="/manuals/psn/open"' in page and "Fetch the package" not in page
    assert not e.set_file.called                                          # looking at a page asks Minerva for nothing
    assert "no such package" in e.said(e.post("fetch", rom="1", idx="0"))
    assert e.said(e.post("open", rom="1")) == "/manuals/g/1"
    assert e.set_file.call_count == 1 and e.index.call_count == 1
    page = e.page()
    assert "Moon Garden (Japan).zip" in page and "same title" in page
    assert "Moon Garden 2 (Japan).zip" in page and "similar title" in page
    assert "(USA)" not in page and "Star Road" not in page
    assert page.count('formaction="/manuals/psn/fetch"') == 2 and "formnovalidate" in page
    e.post("open", rom="1")
    assert e.set_file.call_count == 1                                     # the set's .torrent is kept
    (e.config / "psn-keys.txt").write_text("pkg_psp = 12\n")
    page = e.page()
    assert "can't be used" in page and "wrong length" in page and "Fetch the package" not in page
    assert not e.fake.calls and e.row() is None                           # qBittorrent wasn't asked for anything


def test_a_fetched_package_is_read_and_removed_and_its_manual_goes_into_romm(env):
    e = env().ready()
    r = e.post("fetch", rom="1", idx="0")
    assert "qBittorrent is fetching the package" in e.said(r)
    assert e.fake.torrent["tags"] == "hakobiya,hakobiya-manual" and e.fake.on() == [0]
    row = e.row()
    assert (row["state"], row["path"], row["asked_by"]) == ("downloading", "PS one Classics/Moon Garden (Japan).zip", "someone")
    e.fake.file(0)["progress"] = 0.42
    page = e.page()
    assert "on its way: 42%" in page and 'formaction="/manuals/psn/stop"' in page and 'name="pick"' not in page
    e.round()
    assert e.row()["state"] == "downloading" and not e.fake.deleted       # not there yet

    e.arrive(0, with_manual())
    e.round()
    row = e.row()
    assert (row["state"], row["pages"], row["note"]) == ("ready", 3, None)
    assert e.fake.deleted == [T.infohash]                                 # the package is gone from qBittorrent again
    page = e.page()
    assert f'name="pick" value=":psn/{row["id"]}"' in page and "3 pages" in page
    look = e.c.get(f"/manuals/psn/{row['id']}")
    assert look.headers["content-type"] == "application/pdf" and len(PdfReader(io.BytesIO(look.content)).pages) == 3
    assert not e.uploads                                                  # nothing went into RomM unasked

    r = e.c.post("/manuals/apply", data={"rom": "1", "pick": f":psn/{row['id']}"}, follow_redirects=False)
    assert "is in RomM" in e.said(r) and e.uploads == [(1, look.content)]
    change = e.db.one("SELECT * FROM manual_changes")
    assert (change["identifier"], change["file"]) == (":psn", "Moon Garden (Japan).zip")
    assert change["sha256"] == hashlib.sha256(look.content).hexdigest()
    assert Path(change["backup_path"]).read_bytes() == OLD
    page = e.page()
    assert "PS one Classics package" in page and "Internet Archive: :psn" not in page and "Put back" in page
    assert "PS one Classics package" in e.c.get("/manuals").text
    assert e.c.get(f"/manuals/psn/{row['id']}").status_code == 200       # kept: it can be put in again
    r = e.c.post(f"/manuals/undo/{change['id']}", follow_redirects=False)
    assert "had before is back" in e.said(r) and e.uploads[-1] == (1, OLD)

    Path(row["pdf_path"]).unlink()                                        # its PDF gone: as if never fetched
    assert e.c.get(f"/manuals/psn/{row['id']}").status_code == 404
    assert 'name="pick"' not in e.page() and e.page().count("Fetch the package") == 2
    r = e.c.post("/manuals/apply", data={"rom": "1", "pick": f":psn/{row['id']}"}, follow_redirects=False)
    assert "Fetch the package again" in e.said(r) and len(e.uploads) == 2


def test_a_set_that_is_in_qbittorrent_for_other_things_is_never_removed(env):
    e = env(FakeQBit(tags="", on=[3])).ready()                            # the owner's own torrent of the set
    e.post("fetch", rom="1", idx="0")
    assert e.fake.torrent["tags"] == "" and e.fake.on() == [0, 3]
    e.arrive(0, with_manual())
    e.round()
    row = e.row()
    assert row["state"] == "ready" and "stays in qBittorrent" in row["note"] and "stays in qBittorrent" in e.page()
    assert not e.fake.deleted and e.fake.on() == [0, 3]                   # nothing removed, nothing switched off


def test_a_torrent_added_for_manuals_stays_while_anything_else_uses_it(env):
    e = env().ready()
    e.post("fetch", rom="1", idx="0")
    e.post("fetch", rom="1", idx="1")
    assert e.fake.on() == [0, 1]
    e.arrive(0, with_manual())
    e.round()
    assert e.row(0)["state"] == "ready" and e.row(0)["note"] is None and not e.fake.deleted   # the other is on its way
    e.fake.file(3)["priority"] = 1                                        # switched on by hand in qBittorrent
    e.arrive(1, with_manual(PAGES[:1]))
    e.round()
    assert e.row(1)["state"] == "ready" and "stays in qBittorrent" in e.row(1)["note"] and not e.fake.deleted
    e.fake.file(3)["priority"] = 0

    e.post("fetch", rom="3", idx="4")
    e.db.execute("INSERT INTO jobs(set_name, idx, path, size, infohash, state, created_at, updated_at)"
                 " VALUES (?, 3, 'x', 1, ?, 'sent', 'now', 'now')", (packages.SET, T.infohash))
    e.arrive(4, package(GAME))
    e.round()
    assert e.row(4)["state"] == "none" and not e.fake.deleted             # sent from the set's own page: kept
    assert "no manual in it" in e.page(3)
    e.db.execute("DELETE FROM jobs")
    e.post("fetch", rom="3", idx="4")
    e.round()                                                             # already complete: read at once
    assert e.row(4)["state"] == "none" and e.fake.deleted == [T.infohash]


def test_a_package_that_can_t_be_read_stays_for_another_go(env):
    e = env().ready()
    e.post("fetch", rom="1", idx="0")
    e.arrive(0, b"\0" * 0x400)
    e.round()
    row = e.row()
    assert row["state"] == "failed" and "doesn't start like a package" in row["note"]
    assert "still in qBittorrent" in row["note"] and not e.fake.deleted
    page = e.page()
    assert "didn't work" in page and "Try again" in page and 'name="pick"' not in page
    zipped(e.data / "torrents" / T.files[0].path, with_manual())
    e.post("fetch", rom="1", idx="0")
    assert e.row()["state"] == "downloading" and e.row()["id"] == row["id"]
    e.round()
    assert e.row()["state"] == "ready" and e.fake.deleted == [T.infohash]


def test_stop_gives_a_package_up(env):
    e = env().ready()
    e.post("fetch", rom="1", idx="0")
    r = e.post("stop", rom="1", row=str(e.row()["id"]))
    assert "Stopped" in e.said(r) and e.row()["state"] == "failed" and e.row()["note"] == "stopped"
    assert e.fake.deleted == [T.infohash]
    e.post("stop", rom="1", row=str(e.row()["id"]))                       # nothing on its way: nothing happens
    e.post("stop", rom="1", row="nonsense")
    assert e.fake.deleted == [T.infohash]


def test_stop_leaves_the_owner_s_torrent_as_it_is(env):
    e = env(FakeQBit(tags="", on=[3])).ready()
    e.post("fetch", rom="1", idx="0")
    e.post("stop", rom="1", row=str(e.row()["id"]))
    assert "stays switched on" in e.row()["note"] and not e.fake.deleted and e.fake.on() == [0, 3]


def test_a_package_that_left_qbittorrent_or_was_switched_off_is_marked(env):
    e = env().ready()
    e.post("fetch", rom="1", idx="0")
    e.post("fetch", rom="1", idx="1")
    e.fake.file(1)["priority"] = 0
    e.round()
    assert (e.row(1)["state"], e.row(1)["note"]) == ("failed", "switched off in qBittorrent")
    assert e.row(0)["state"] == "downloading" and not e.fake.deleted
    e.fake.torrent = None
    e.round()
    assert (e.row(0)["state"], e.row(0)["note"]) == ("failed", "not in qBittorrent any more")


def test_a_list_that_changed_since_the_page_was_made_sends_nothing(env):
    e = env().ready()
    e.db.execute("UPDATE files SET path='PS one Classics/Renamed (Japan).zip' WHERE set_name=? AND idx=0", (packages.SET,))
    r = e.post("fetch", rom="1", idx="0")
    assert "Minerva changed the set" in e.said(r) and "torrents/add" not in e.fake.calls and e.row() is None
    assert "wasn't sent" in e.said(e.post("fetch", rom="1", idx="nonsense"))
