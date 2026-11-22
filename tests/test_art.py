import json
import struct
import zlib
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hakobiya import art
from hakobiya import minerva as mv
from hakobiya.app import Settings, create_app
from hakobiya.db import DB

HEAD = {"origin": "http://testserver", "x-hakobiya-key": "k"}


def png(w, h):
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IEND", b"")


def test_image_sizes_and_sources():
    assert art.image_size(png(300, 420)) == (300, 420)
    jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 500, 280) + b"\0" * 6
    assert art.image_size(jpeg) == (280, 500)
    assert art.image_size(b"nope") is None
    ss = "https://neoclone.screenscraper.fr/api2/mediaJeu.php?devid=x&media=box-2D%28jp%29&gameid=1"
    assert art.cover_source({"path_cover_large": "/a", "url_cover": ss}) == ("ScreenScraper jp", True)
    assert art.cover_source({"path_cover_large": "/a", "url_cover": ss.replace("jp", "us")})[1] is False
    assert art.cover_source({"path_cover_large": "/a", "url_cover": ""}) == ("uploaded", True)
    assert art.cover_source({"path_cover_large": "", "url_cover": ""}) == ("no cover", False)
    multi = {"has_multiple_files": True, "fs_name": "Aconcagua (Japan)", "fs_name_no_ext": "Aconcagua (Japan)",
             "files": [{"file_name": "Aconcagua (Japan) (Disc 2).chd"}, {"file_name": "Aconcagua (Japan) (Disc 1).chd"},
                       {"file_name": "Aconcagua (Japan).m3u"}]}
    assert art.lookup_names(multi) == ["Aconcagua (Japan) (Disc 1)", "Aconcagua (Japan)"]
    assert art.libretro_name("Rock & Roll: Live?") == "Rock _ Roll_ Live_"


async def test_libretro_follows_symlinks_and_remembers_misses(tmp_path):
    db = DB(":memory:")
    lib = art.Libretro(db, tmp_path)
    base = art.LIBRETRO.format(repo=art.REPOS["nds"])
    with respx.mock() as router:
        router.get(base + "Game%20%28Japan%29%20%28Rev%201%29.png").mock(
            return_value=httpx.Response(200, text="Game (Japan).png"))           # a libretro symlink
        router.get(base + "Game%20%28Japan%29.png").mock(return_value=httpx.Response(200, content=png(1, 1)))
        missing = router.get(base + "Other%20%28Japan%29.png").mock(return_value=httpx.Response(404))
        name, data = await lib.find("nds", ["Game (Japan) (Rev 1)"])
        assert name == "Game (Japan) (Rev 1)" and data[:4] == b"\x89PNG"
        assert await lib.find("nds", ["Other (Japan)"]) is None
        assert await lib.find("nds", ["Other (Japan)"]) is None
        assert missing.call_count == 1                                           # the miss is remembered
        assert await lib.find("wiiu", ["x"]) is None                             # no libretro collection


ROMS = [
    {"id": 1, "name": "Tall One", "fs_name": "Tall One (Japan).zip", "fs_name_no_ext": "Tall One (Japan)",
     "path_cover_small": "/assets/romm/resources/roms/9/1/cover/small.png?ts=2026-11-20 13:30:05",
     "path_cover_large": "/assets/romm/resources/roms/9/1/cover/big.png?ts=2026-11-20 13:30:05",
     "url_cover": "https://x/mediaJeu.php?media=box-2D(jp)", "platform_fs_slug": "nds", "has_multiple_files": False},
    {"id": 2, "name": "Wide One", "fs_name": "Wide One (Japan).zip", "fs_name_no_ext": "Wide One (Japan)",
     "path_cover_small": "/assets/romm/resources/roms/9/2/cover/small.png",
     "path_cover_large": "/assets/romm/resources/roms/9/2/cover/big.png",
     "url_cover": "https://x/mediaJeu.php?media=box-2D(us)", "platform_fs_slug": "nds", "has_multiple_files": False},
    {"id": 3, "name": "Bare One", "fs_name": "Bare One (Japan).zip", "fs_name_no_ext": "Bare One (Japan)",
     "path_cover_small": "", "path_cover_large": "", "url_cover": "", "platform_fs_slug": "nds", "has_multiple_files": False},
]


@pytest.fixture
def app(tmp_path):
    s = Settings(tmp_path / "config", tmp_path / "roms", "http://qbit", "u", "p", proxy_key="k",
                 romm_url="http://romm", romm_token="tok")
    db = DB(":memory:")
    uploads = []
    with respx.mock(assert_all_called=False) as router:
        router.get("http://romm/api/platforms").mock(return_value=httpx.Response(200, json=[
            {"id": 9, "name": "Nintendo DS", "display_name": "Nintendo DS", "fs_slug": "nds", "rom_count": 3},
            {"id": 8, "name": "Empty", "fs_slug": "n64", "rom_count": 0}]))
        router.get("http://romm/api/roms").mock(return_value=httpx.Response(200, json={"items": ROMS, "total": 3}))
        for r in ROMS:
            router.get(f"http://romm/api/roms/{r['id']}").mock(return_value=httpx.Response(200, json=r))
        router.get(url__regex=r"http://romm/assets/romm/resources/roms/9/1/cover/(small|big)\.png.*").mock(
            return_value=httpx.Response(200, content=png(300, 270)))
        router.get(url__regex=r"http://romm/assets/romm/resources/roms/9/2/cover/(small|big)\.png.*").mock(
            return_value=httpx.Response(200, content=png(300, 500)))

        def upload(request):
            uploads.append((int(request.url.path.rsplit("/", 1)[1]), request.content))
            return httpx.Response(200, json={})
        router.put(url__regex=r"http://romm/api/roms/\d+").mock(side_effect=upload)
        base = art.LIBRETRO.format(repo=art.REPOS["nds"])
        router.get(base + "Wide%20One%20%28Japan%29.png").mock(return_value=httpx.Response(200, content=png(290, 260)))
        router.get(url__regex=r"https://raw.githubusercontent.com/.*").mock(return_value=httpx.Response(404))
        c = TestClient(create_app(s, mv.Minerva(db, tmp_path / "t"), None, db), headers=HEAD)
        yield c, db, uploads, tmp_path


def test_platform_page_flags_and_compares(app):
    c, db, uploads, _ = app
    index = c.get("/art").text
    assert "Nintendo DS" in index and "Empty" not in index
    page = c.get("/art/p/9", params={"alt": "true"}).text
    assert page.count('class="card"') == 3
    assert "not Japanese" in page and "no cover" in page and "odd shape" in page
    assert "ScreenScraper us" in page and "300×500" in page
    assert c.get("/art/alt/2").status_code == 200 and c.get("/art/alt/1").status_code == 404
    assert c.get("/art/cover/1").content[:4] == b"\x89PNG"
    assert not uploads                                                           # looking never changes RomM


def test_apply_uploads_only_the_picked_covers_and_undo_restores(app):
    c, db, uploads, tmp = app
    r = c.post("/art/apply", data={"platform": "9", "rom": ["2", "3"]}, follow_redirects=False)
    where = unquote(r.headers["location"])
    assert "Replaced 1 cover" in where and "#3: no libretro cover" in where
    assert [u[0] for u in uploads] == [2] and b'name="artwork"' in uploads[0][1]
    change = db.one("SELECT * FROM art_changes")
    assert change["source"] == "libretro: Wide One (Japan)" and Path(change["backup_path"]).read_bytes() == png(300, 500)
    c.post(f"/art/undo/{change['id']}")
    assert len(uploads) == 2 and png(300, 500) in uploads[1][1]
    assert db.one("SELECT undone_at FROM art_changes")["undone_at"]
