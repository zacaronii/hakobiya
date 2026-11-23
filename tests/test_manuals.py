import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from hakobiya import manuals
from hakobiya import minerva as mv
from hakobiya.app import Settings, create_app
from hakobiya.db import DB

HEAD = {"origin": "http://testserver", "x-hakobiya-key": "k"}
SS = "https://x/mediaJeu.php?devid=d&media=manuel({})&gameid=1"
PDF = b"%PDF-1.4\nthe picked manual\n%%EOF"
OLD = b"%PDF-1.4\nthe manual RomM had\n%%EOF"

ROMS = [
    {"id": 1, "name": "Moon Garden", "fs_name": "Moon Garden (Japan).chd", "fs_name_no_tags": "Moon Garden",
     "alternative_names": ["つきのにわ", "Jardin de Lune"], "platform_id": 9, "platform_fs_slug": "psx",
     "path_manual": "roms/9/1/manual/1.pdf", "url_manual": SS.format("us")},
    {"id": 2, "name": "かぜのまち", "fs_name": "Kaze no Machi (Japan).chd", "fs_name_no_tags": "Kaze no Machi",
     "alternative_names": [], "platform_id": 9, "platform_fs_slug": "psx", "path_manual": "", "url_manual": ""},
    {"id": 3, "name": "Star Road", "fs_name": "Star Road (Japan).chd", "fs_name_no_tags": "Star Road",
     "alternative_names": [], "platform_id": 9, "platform_fs_slug": "psx",
     "path_manual": "roms/9/3/manual/3.pdf", "url_manual": SS.format("jp")},
]


def made_up_scans(pages=3) -> bytes:
    """A Comic Book ZIP of made-up pages (one JPEG and PNGs of plain colour), plus the junk such archives carry."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for i in range(1, pages + 1):
            page = io.BytesIO()
            Image.new("RGB", (20 + i, 30), (40 * i, 90, 160)).save(page, "JPEG" if i == 1 else "PNG")
            zf.writestr(f"scan/{i}.{'jpg' if i == 1 else 'png'}", page.getvalue())
        zf.writestr("__MACOSX/scan/._1.jpg", b"junk")
    return out.getvalue()


CBZ = made_up_scans()
PACK = {"metadata": {"title": "Made-up manuals", "language": "jpn"}, "files": [
    {"name": "つきのにわ (Moon Garden).cbz", "source": "original", "size": str(len(CBZ)), "filecount": "4",
     "format": "Comic Book ZIP"},
    {"name": "つきのにわ (Moon Garden)_jp2.zip", "source": "derivative", "size": "900"},
    {"name": "つきのにわ (Moon Garden).pdf", "source": "derivative", "size": str(len(PDF))},
    {"name": "Moon Garden 2 (o).pdf", "source": "original", "size": "900"},
    {"name": "Other Game (o).pdf", "source": "original", "size": "900"},
    {"name": "Other Game (o)_text.pdf", "source": "derivative", "size": "90"},
    {"name": "__ia_thumb.jpg", "source": "original", "size": "5"}]}
ITEM = {"metadata": {"title": ["Moon Garden manual"], "language": ["Japanese"]}, "files": [
    {"name": "Moon Garden.PDF", "source": "original", "size": str(len(PDF))},
    {"name": "Moon Garden.pdf", "source": "derivative", "size": "12"},
    {"name": "notes.txt", "source": "original", "size": "3"}]}
SCANS = {"metadata": {"title": "Moon Garden scans"}, "files": [
    {"name": f"page{i}.jpg", "source": "original", "size": "5"} for i in range(3)]}


def test_what_a_game_has_now():
    assert manuals.manual_state(ROMS[1]) == ("no manual", False)
    assert manuals.manual_state(ROMS[0]) == ("ScreenScraper us", False)
    assert manuals.manual_state(ROMS[2]) == ("ScreenScraper jp", True)
    assert manuals.manual_state({"path_manual": "a.pdf", "url_manual": ""}) == ("uploaded", True)
    assert manuals.manual_state(ROMS[0], {"identifier": "an-item"}) == ("Internet Archive: an-item", True)
    assert manuals.manual_state(ROMS[1], {"identifier": "an-item"}) == ("no manual", False)   # taken out in RomM since


def test_titles_and_the_search_query():
    assert manuals.game_names(ROMS[0]) == ["つきのにわ", "Moon Garden", "Jardin de Lune"]
    assert manuals.search_terms(ROMS[0]) == ["つきのにわ", "Moon Garden"]
    assert manuals.search_terms({"name": "Tale", "fs_name_no_tags": "Tale, The - Part 2"}) == ["Tale"]
    assert manuals.article_first("Tale, The - Part 2") == "The Tale - Part 2"
    q = manuals.search_query(['Moon "Garden": (Best)', "つきのにわ"])
    assert q.startswith('(title:("Moon Garden Best") OR title:("つきのにわ")) AND (title:(manual OR')
    assert manuals.search_query(["Moon Garden"], manuals_only=False) == '(title:("Moon Garden"))'
    assert manuals.search_query(['"', " "]) == ""
    assert manuals.item_link("https://archive.org/details/moon-garden_manual?x=1") == "moon-garden_manual"
    assert manuals.item_link("archive.org/download/moon.garden/file.pdf") == "moon.garden"
    assert manuals.item_link("Moon Garden") is None and manuals.item_link("https://example.org/details/x") is None


def test_collection_files_are_matched_by_title():
    names = ["つきのにわ", "Moon Garden"]
    assert manuals.name_match(names, "つきのにわ (Moon Garden).pdf") == "same"
    assert manuals.name_match(names, "MOON GARDEN (o)_text.pdf") == "same"
    assert manuals.name_match(names, "Moon Garden 2 (o).pdf") == "close"
    assert manuals.name_match(names, "Other Game (o).pdf") is None
    assert manuals.name_match(["Run"], "Runner Up (o).pdf") is None                 # too short to mean anything
    assert manuals.name_match(names, "つきのにわ (Moon Garden).cbz") == "same"        # page images, same names
    assert manuals.name_match(names, "Moon Garden_jp2.zip") == "same"
    assert manuals.name_match(names, "dir/Moon Garden.zip") == "same"


def test_only_archive_org_hosts_are_followed():
    assert manuals.allowed("https://ia800.us.archive.org/1/items/x/y.pdf") and manuals.allowed("https://archive.org/x")
    assert not manuals.allowed("http://archive.org/x") and not manuals.allowed("https://archive.org.example.com/x")
    assert not manuals.allowed("https://notarchive.org/x")


def answer(body):
    return httpx.Response(200, json=body)


@pytest.fixture
def archive(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        yield manuals.Archive(DB(":memory:"), tmp_path / "cache", pause=0), router, tmp_path


async def test_search_and_file_lists_are_cached(archive):
    a, router, _ = archive
    search = router.get("https://archive.org/advancedsearch.php").mock(return_value=answer({"response": {"docs": [
        {"identifier": "moon-garden-manual", "title": "Moon Garden manual", "language": ["Japanese"], "mediatype": "texts"},
        {"identifier": "bad id/..", "title": "x"}]}}))
    item = router.get("https://archive.org/metadata/moon-garden-manual").mock(return_value=answer(ITEM))
    found = await a.search(["Moon Garden"])
    assert found == [{"identifier": "moon-garden-manual", "title": "Moon Garden manual", "japanese": True,
                      "mediatype": "texts"}]
    assert await a.search(["Moon Garden"]) == found and search.call_count == 1
    assert dict(search.calls[0].request.url.params)["q"] == manuals.search_query(["Moon Garden"])
    await a.search(["Moon Garden"], fresh=True)
    assert search.call_count == 2
    assert a.known("moon-garden-manual") is None
    listing = await a.item("moon-garden-manual")
    assert listing["title"] == "Moon Garden manual" and listing["japanese"]
    assert [(f["name"], f["original"]) for f in listing["pdfs"]] == [("Moon Garden.PDF", True), ("Moon Garden.pdf", False)]
    assert await a.item("moon-garden-manual") == a.known("moon-garden-manual") and item.call_count == 1
    with pytest.raises(manuals.ArchiveError):
        await a.item("../etc")
    router.get("https://archive.org/metadata/nothing-here").mock(return_value=answer({}))
    with pytest.raises(manuals.ArchiveError, match="no item"):
        await a.item("nothing-here")


async def test_a_download_is_a_listed_pdf_from_archive_org(archive):
    a, router, tmp = archive
    router.get("https://archive.org/metadata/moon-garden-manual").mock(return_value=answer(
        {**ITEM, "files": ITEM["files"] + [{"name": "fake.pdf", "source": "original", "size": "9"},
                                           {"name": "away.pdf", "source": "original", "size": "9"},
                                           {"name": "huge.pdf", "source": "original", "size": str(manuals.MAX_SIZE + 1)}]}))
    router.get("https://archive.org/download/moon-garden-manual/Moon%20Garden.PDF").mock(
        return_value=httpx.Response(302, headers={"location": "https://ia800.us.archive.org/7/items/m/Moon%20Garden.PDF"}))
    real = router.get("https://ia800.us.archive.org/7/items/m/Moon%20Garden.PDF").mock(
        return_value=httpx.Response(200, content=PDF))
    router.get("https://archive.org/download/moon-garden-manual/fake.pdf").mock(
        return_value=httpx.Response(200, content=b"<html>not a manual</html>"))
    router.get("https://archive.org/download/moon-garden-manual/away.pdf").mock(
        return_value=httpx.Response(302, headers={"location": "https://elsewhere.example/away.pdf"}))
    elsewhere = router.get("https://elsewhere.example/away.pdf").mock(return_value=httpx.Response(200, content=PDF))

    path = await a.fetch("moon-garden-manual", "Moon Garden.PDF")
    assert path.read_bytes() == PDF and path.parent == tmp / "cache"
    assert await a.fetch("moon-garden-manual", "Moon Garden.PDF") == path and real.call_count == 1
    for name, why in (("notes.txt", "no such PDF"), ("missing.pdf", "no such PDF"), ("fake.pdf", "isn't a PDF"),
                      ("away.pdf", "away from archive.org"), ("huge.pdf", "larger than")):
        with pytest.raises(manuals.ArchiveError, match=why):
            await a.fetch("moon-garden-manual", name)
    assert elsewhere.call_count == 0
    assert [p.name for p in (tmp / "cache").iterdir()] == [path.name]             # nothing half-fetched is left
    a.forget("moon-garden-manual", "Moon Garden.PDF")
    assert not path.exists()


SCAN_ITEM = {"metadata": {"title": "Made-up scans"}, "files": [
    {"name": "Moon Garden.cbz", "source": "original", "size": str(len(CBZ)), "filecount": "4"},
    {"name": "Moon Garden.pdf", "source": "derivative", "size": "50", "format": "Text PDF"},
    {"name": "Moon Garden_jp2.zip", "source": "derivative", "size": "50"},
    {"name": "Moon Garden.epub", "source": "derivative", "size": "50"},
    {"name": "Broken.cbz", "source": "original", "size": "30"},
    {"name": "Fake.zip", "source": "original", "size": "30"},
    {"name": "Away.cbz", "source": "original", "size": "30"},
    {"name": "Huge.cbz", "source": "original", "size": str(manuals.MAX_SCANS + 1)}]}


async def test_original_page_images_are_listed_and_made_into_a_pdf(archive):
    from pypdf import PdfReader
    a, router, tmp = archive
    meta = router.get("https://archive.org/metadata/moon-scans").mock(return_value=answer(SCAN_ITEM))
    listing = await a.item("moon-scans")
    assert [(f["name"], f["files"]) for f in listing["scans"]] == [
        ("Away.cbz", None), ("Broken.cbz", None), ("Fake.zip", None), ("Huge.cbz", None), ("Moon Garden.cbz", 4)]
    assert [f["name"] for f in listing["pdfs"]] == ["Moon Garden.pdf"]          # the _jp2.zip isn't an original
    url = "https://archive.org/download/moon-scans/Moon%20Garden.cbz"
    router.get(url).mock(return_value=httpx.Response(
        302, headers={"location": "https://ia601.us.archive.org/3/items/moon-scans/Moon%20Garden.cbz"}))
    real = router.get("https://ia601.us.archive.org/3/items/moon-scans/Moon%20Garden.cbz").mock(
        return_value=httpx.Response(200, content=CBZ))
    assert a.pages("moon-scans", "Moon Garden.cbz") is None
    path = await a.fetch("moon-scans", "Moon Garden.cbz")
    assert path.suffix == ".pdf" and path.read_bytes().startswith(b"%PDF-")
    assert len(PdfReader(path).pages) == 3 and a.pages("moon-scans", "Moon Garden.cbz") == 3
    assert [p.name for p in (tmp / "cache").iterdir()] == [path.name]             # the archive itself isn't kept
    assert await a.fetch("moon-scans", "Moon Garden.cbz") == path and real.call_count == 1

    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as zf:
        zf.writestr("1.jpg", b"not a picture")
    router.get("https://archive.org/download/moon-scans/Broken.cbz").mock(
        return_value=httpx.Response(200, content=broken.getvalue()))
    router.get("https://archive.org/download/moon-scans/Fake.zip").mock(
        return_value=httpx.Response(200, content=b"<html>not an archive</html>"))
    router.get("https://archive.org/download/moon-scans/Away.cbz").mock(
        return_value=httpx.Response(302, headers={"location": "https://elsewhere.example/Away.cbz"}))
    huge = router.get("https://archive.org/download/moon-scans/Huge.cbz")
    for name, why in (("Broken.cbz", "couldn't be made into a PDF: 1.jpg can't be read"),
                      ("Fake.zip", "isn't a zip archive"), ("Away.cbz", "away from archive.org"),
                      ("Huge.cbz", "larger than 200 MB"), ("Moon Garden_jp2.zip", "no such PDF or page images")):
        with pytest.raises(manuals.ArchiveError, match=why):
            await a.fetch("moon-scans", name)
    assert huge.call_count == 0 and meta.call_count == 1
    assert [p.name for p in (tmp / "cache").iterdir()] == [path.name]             # nothing half-made is left
    a.forget("moon-scans", "Moon Garden.cbz")
    assert a.pages("moon-scans", "Moon Garden.cbz") is None


async def test_a_file_list_kept_before_page_images_were_listed_is_read_again(archive):
    a, router, _ = archive
    meta = router.get("https://archive.org/metadata/moon-scans").mock(return_value=answer(SCAN_ITEM))
    a._keep("item:moon-scans", {"identifier": "moon-scans", "title": "Made-up scans", "japanese": False,
                                "pdfs": [], "images": 0})
    assert len((await a.item("moon-scans"))["scans"]) == 5 and meta.call_count == 1
    a._keep("item:moon-scans", {"identifier": "moon-scans", "title": "Made-up scans", "japanese": False,
                                "pdfs": [], "images": 0})
    meta.mock(return_value=httpx.Response(503))
    assert (await a.item("moon-scans"))["scans"] == []                             # the Archive down: as it was


def test_the_cache_drops_what_is_old_and_then_the_oldest_over_its_size(tmp_path, monkeypatch):
    import os
    import time
    a = manuals.Archive(DB(":memory:"), tmp_path / "cache", pause=0)
    a.sweep()                                                                      # no folder yet: nothing to do
    a.cache_dir.mkdir()
    now = time.time()
    for name, age in (("week.pdf", 8 * 86400), ("left.part", 8 * 86400), ("first.pdf", 300), ("second.pdf", 200),
                      ("third.pdf", 100)):
        (a.cache_dir / name).write_bytes(b"x" * 10)
        os.utime(a.cache_dir / name, (now - age, now - age))
    a.sweep()
    assert sorted(p.name for p in a.cache_dir.iterdir()) == ["first.pdf", "second.pdf", "third.pdf"]
    monkeypatch.setattr(manuals, "CACHE_SIZE", 25)
    a.sweep()
    assert sorted(p.name for p in a.cache_dir.iterdir()) == ["second.pdf", "third.pdf"]


@pytest.fixture
def app(tmp_path):
    res = tmp_path / "res"
    for rom_id in (1, 3):
        (res / f"roms/9/{rom_id}/manual").mkdir(parents=True)
        (res / f"roms/9/{rom_id}/manual/{rom_id}.pdf").write_bytes(OLD)
    s = Settings(tmp_path / "config", tmp_path / "roms", "http://qbit", "u", "p", proxy_key="k",
                 romm_url="http://romm", romm_token="tok", romm_resources=res, archive_pause=0)
    db = DB(":memory:")
    roms = {r["id"]: dict(r) for r in ROMS}
    calls = []
    with respx.mock(assert_all_called=False) as router:
        router.get("http://romm/api/platforms").mock(return_value=answer(
            [{"id": 9, "name": "PlayStation", "display_name": "PlayStation", "fs_slug": "psx", "rom_count": 3}]))
        router.get("http://romm/api/roms").mock(side_effect=lambda request: answer(
            {"items": list(roms.values()), "total": 3}))

        def rom(request, rom_id):
            return answer(roms[int(rom_id)])

        def upload(request, rom_id):
            """What RomM does: the file from the field named like the header, stored as <id>.pdf."""
            rom_id, field = int(rom_id), request.headers["x-upload-filename"]
            body = request.content
            assert f'name="{field}"'.encode() in body
            data = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            calls.append(("upload", rom_id, field, data))
            if data == b"%PDF-refused":
                (res / f"roms/9/{rom_id}/manual/{rom_id}.pdf").unlink(missing_ok=True)     # RomM's cleanup
                return httpx.Response(500, text="broke off")
            (res / f"roms/9/{rom_id}/manual").mkdir(parents=True, exist_ok=True)
            (res / f"roms/9/{rom_id}/manual/{rom_id}.pdf").write_bytes(data)
            roms[rom_id]["path_manual"] = f"roms/9/{rom_id}/manual/{rom_id}.pdf"
            return httpx.Response(201)

        def delete(request, rom_id):
            rom_id = int(rom_id)
            calls.append(("delete", rom_id))
            (res / f"roms/9/{rom_id}/manual/{rom_id}.pdf").unlink()
            roms[rom_id].update(path_manual="", url_manual="")
            return httpx.Response(200)

        router.get(url__regex=r"http://romm/api/roms/(?P<rom_id>\d+)$").mock(side_effect=rom)
        router.post(url__regex=r"http://romm/api/roms/(?P<rom_id>\d+)/manuals$").mock(side_effect=upload)
        router.delete(url__regex=r"http://romm/api/roms/(?P<rom_id>\d+)/manuals$").mock(side_effect=delete)
        router.get("https://archive.org/advancedsearch.php").mock(return_value=answer({"response": {"docs": [
            {"identifier": "moon-garden-scans", "title": "Moon Garden scans", "mediatype": "image"},
            {"identifier": "moon-garden-manual", "title": "Moon Garden manual", "language": "jpn", "mediatype": "texts"}]}}))
        router.get("https://archive.org/metadata/moon-garden-manual").mock(return_value=answer(ITEM))
        router.get("https://archive.org/metadata/moon-garden-scans").mock(return_value=answer(SCANS))
        for pack in manuals.PACKS["psx"]:
            router.get(f"https://archive.org/metadata/{pack}").mock(return_value=answer(PACK))
        router.get("https://archive.org/download/moon-garden-manual/Moon%20Garden.PDF").mock(
            return_value=httpx.Response(200, content=PDF))
        router.get("https://archive.org/download/moon-garden-manual/Moon%20Garden.pdf").mock(
            return_value=httpx.Response(200, content=b"%PDF-refused"))
        for pack in manuals.PACKS["psx"]:
            router.get(f"https://archive.org/download/{pack}/{quote('つきのにわ (Moon Garden).cbz')}").mock(
                return_value=httpx.Response(200, content=CBZ))
        app = create_app(s, mv.Minerva(db, tmp_path / "t"), None, db)
        yield TestClient(app, headers=HEAD), db, calls, res, router


def test_pages_show_what_each_game_has_and_change_nothing(app):
    c, db, calls, res, router = app
    index = c.get("/manuals").text
    assert "PlayStation" in index and 'href="/manuals">Manuals</a>' in index
    todo = c.get("/manuals/p/9").text
    assert "Moon Garden" in todo and "かぜのまち" in todo and "Star Road" not in todo
    assert "ScreenScraper us" in todo and "no manual" in todo
    assert "Star Road" in c.get("/manuals/p/9", params={"show": "all"}).text
    assert c.get("/manuals/p/5").status_code == 404
    page = c.get("/manuals/g/1").text
    assert 'value="つきのにわ; Moon Garden"' in page                                # the search box starts with its titles
    assert "same title" in page and "similar title" in page and "Other Game" not in page    # from the collections
    assert page.index("Moon Garden manual") < page.index("Moon Garden scans")     # Japanese items first
    assert "Show its files" in page and "Look at it" in page
    opened = c.get("/manuals/g/1", params={"item": "moon-garden-scans"}).text
    assert "3 page images" in opened
    linked = c.get("/manuals/g/1", params={"q": "https://archive.org/details/moon-garden-manual"}).text
    assert "Moon Garden.PDF" in linked and "as uploaded" in linked and "compressed copy" in linked
    assert c.get("/manuals/current/1").content == OLD and c.get("/manuals/current/2").status_code == 404
    look = c.get("/manuals/file/moon-garden-manual/Moon Garden.PDF")
    assert look.content == PDF and look.headers["content-type"] == "application/pdf"
    assert look.headers["content-disposition"].startswith("inline")
    assert c.get("/manuals/file/moon-garden-manual/notes.txt").status_code == 502
    assert not calls and (res / "roms/9/1/manual/1.pdf").read_bytes() == OLD     # looking never changes RomM
    assert db.one("SELECT COUNT(*) AS n FROM manual_changes")["n"] == 0


def test_the_ticked_pdf_replaces_the_manual_and_put_back_restores_it(app):
    c, db, calls, res, _ = app
    r = c.post("/manuals/apply", data={"rom": "1", "pick": "moon-garden-manual/Moon Garden.PDF"}, follow_redirects=False)
    assert r.status_code == 303 and "is in RomM" in unquote(r.headers["location"])
    assert calls == [("upload", 1, "manual.pdf", PDF)]
    change = db.one("SELECT * FROM manual_changes")
    assert (change["identifier"], change["file"], change["old_state"]) == ("moon-garden-manual", "Moon Garden.PDF", "ScreenScraper us")
    assert change["sha256"] == hashlib.sha256(PDF).hexdigest() and change["size"] == len(PDF)
    assert Path(change["backup_path"]).read_bytes() == OLD
    assert not list((res.parent / "config/manual-cache").glob("*.pdf"))           # the fetched copy isn't kept
    page = c.get("/manuals/g/1").text
    assert "Internet Archive: moon-garden-manual" in page and "Put back" in page
    assert c.get(f"/manuals/kept/{change['id']}").content == OLD
    assert "Internet Archive: moon-garden-manual" in c.get("/manuals/p/9", params={"show": "all"}).text
    assert "Moon Garden" not in c.get("/manuals/p/9").text                        # it has a Japanese manual now

    r = c.post(f"/manuals/undo/{change['id']}", follow_redirects=False)
    assert "had before is back" in unquote(r.headers["location"])
    assert calls[-1] == ("upload", 1, "manual.pdf", OLD) and (res / "roms/9/1/manual/1.pdf").read_bytes() == OLD
    assert db.one("SELECT undone_at FROM manual_changes")["undone_at"]
    assert "ScreenScraper us" in c.get("/manuals/g/1").text
    assert c.post(f"/manuals/undo/{change['id']}").status_code == 404


def test_a_game_without_a_manual_gets_one_and_put_back_takes_it_out(app):
    c, db, calls, res, _ = app
    c.post("/manuals/apply", data={"rom": "2", "pick": "moon-garden-manual/Moon Garden.PDF"})
    change = db.one("SELECT * FROM manual_changes")
    assert change["backup_path"] is None and change["old_state"] == "no manual"
    assert (res / "roms/9/2/manual/2.pdf").read_bytes() == PDF
    r = c.post(f"/manuals/undo/{change['id']}", follow_redirects=False)
    assert "taken out again" in unquote(r.headers["location"])
    assert calls[-1] == ("delete", 2) and not (res / "roms/9/2/manual/2.pdf").exists()


def test_put_back_leaves_a_manual_that_changed_since(app):
    c, db, calls, res, _ = app
    c.post("/manuals/apply", data={"rom": "1", "pick": "moon-garden-manual/Moon Garden.PDF"})
    change = db.one("SELECT * FROM manual_changes")
    (res / "roms/9/1/manual/1.pdf").write_bytes(b"%PDF-1.4\nput there by someone\n%%EOF")
    assert "ScreenScraper us" in c.get("/manuals/g/1").text                       # no longer called Hakobiya's
    r = c.post(f"/manuals/undo/{change['id']}", follow_redirects=False)
    assert "isn't the one Hakobiya put there" in unquote(r.headers["location"])
    assert len(calls) == 1 and db.one("SELECT undone_at FROM manual_changes")["undone_at"] is None
    (res / "roms/9/1/manual/1.pdf").write_bytes(PDF[:-1] + b"X")                  # same size, other bytes
    c.post(f"/manuals/undo/{change['id']}")
    assert len(calls) == 1


def test_nothing_is_replaced_without_a_tick_a_listed_pdf_and_a_backup(app):
    c, db, calls, res, _ = app
    for pick, why in (("", "No manual was ticked"), ("moon-garden-manual/notes.txt", "no such PDF"),
                      ("../x/y.pdf", "isn't an Internet Archive item name")):
        r = c.post("/manuals/apply", data={"rom": "1", "pick": pick}, follow_redirects=False)
        assert why in unquote(r.headers["location"])
    (res / "roms/9/3/manual/3.pdf").unlink()                                      # RomM names a file Hakobiya can't read
    r = c.post("/manuals/apply", data={"rom": "3", "pick": "moon-garden-manual/Moon Garden.PDF"}, follow_redirects=False)
    assert "couldn't be kept" in unquote(r.headers["location"])
    assert not calls and db.one("SELECT COUNT(*) AS n FROM manual_changes")["n"] == 0
    assert c.post("/manuals/apply", data={"rom": "1", "pick": "x/y.pdf"}, headers={"origin": "http://evil"}).status_code == 403


def test_a_failed_upload_puts_the_old_manual_back(app):
    c, db, calls, res, _ = app
    r = c.post("/manuals/apply", data={"rom": "1", "pick": "moon-garden-manual/Moon Garden.pdf"}, follow_redirects=False)
    assert "The manual RomM had was put back" in unquote(r.headers["location"])
    assert [(k[0], k[3]) for k in calls] == [("upload", b"%PDF-refused"), ("upload", OLD)]
    assert (res / "roms/9/1/manual/1.pdf").read_bytes() == OLD
    assert db.one("SELECT COUNT(*) AS n FROM manual_changes")["n"] == 0


def test_original_scans_come_first_and_go_into_romm_as_a_pdf(app):
    from pypdf import PdfReader
    c, db, calls, res, _ = app
    pack = manuals.PACKS["psx"][0]
    page = c.get("/manuals/g/1").text
    assert "original scans" in page and "4 files in the archive" in page
    assert page.index("つきのにわ (Moon Garden).cbz") < page.index("つきのにわ (Moon Garden).pdf")
    assert "compressed copy" in page and "_jp2.zip" not in page
    look = c.get(f"/manuals/file/{pack}/つきのにわ (Moon Garden).cbz")
    assert look.headers["content-type"] == "application/pdf" and look.content.startswith(b"%PDF-")
    assert "original scans (3 pages)" in c.get("/manuals/g/1").text               # counted once it was made
    assert not calls and db.one("SELECT COUNT(*) AS n FROM manual_changes")["n"] == 0

    r = c.post("/manuals/apply", data={"rom": "1", "pick": f"{pack}/つきのにわ (Moon Garden).cbz"}, follow_redirects=False)
    assert "is in RomM" in unquote(r.headers["location"])
    [(what, rom_id, field, data)] = calls
    assert (what, rom_id, field) == ("upload", 1, "manual.pdf") and data == look.content
    assert len(PdfReader(io.BytesIO(data)).pages) == 3
    change = db.one("SELECT * FROM manual_changes")
    assert (change["identifier"], change["file"]) == (pack, "つきのにわ (Moon Garden).cbz")
    assert Path(change["backup_path"]).read_bytes() == OLD
    c.post(f"/manuals/undo/{change['id']}")
    assert calls[-1] == ("upload", 1, "manual.pdf", OLD)
