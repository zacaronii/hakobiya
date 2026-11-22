import io
from urllib.parse import unquote
import json
import struct
import zipfile
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from hakobiya import make as maker
from hakobiya import minerva as mv
from hakobiya.app import Settings, create_app
from hakobiya.db import DB

HEAD = {"origin": "http://testserver", "x-hakobiya-key": "k"}


def solid(size, color):
    return Image.new("RGBA", size, color)


def test_cart_keeps_the_shell_and_logo_band_and_prints_art_and_code():
    ref = solid((571, 600), (200, 205, 210, 255))
    ref.paste((250, 250, 245, 255), (59, 65, 487, 517))                         # the label paper
    ref.paste((10, 10, 10, 255), (100, 80, 400, 120))                             # the logo in its band
    out = maker.make("3ds", ref, solid((640, 570), (200, 30, 30, 255)), "AJ2J")
    assert out.size == ref.size
    assert out.getpixel((20, 300)) == ref.getpixel((20, 300))                     # shell untouched
    assert out.getpixel((200, 100)) == ref.getpixel((200, 100))                   # logo band kept
    assert out.getpixel((270, 300))[:3] == (200, 30, 30)                          # art printed in the label
    band = out.crop((59, 517 - 44, 487, 517)).convert("L")
    assert band.getextrema()[0] < 100                                             # the product code was drawn


def test_disc_keeps_hub_and_outside_and_prints_the_ring():
    ref = solid((600, 600), (0, 0, 0, 0))
    ref.paste((230, 230, 230, 255), (0, 0, 600, 600), _circle(600, 300, 291))
    ref.paste((40, 40, 40, 255), (0, 0, 600, 600), _circle(600, 300, 40))
    out = maker.make("psx", ref, solid((500, 500), (20, 60, 200, 255)))
    assert out.getpixel((300, 300)) == ref.getpixel((300, 300))                   # hub
    assert out.getpixel((2, 2)) == ref.getpixel((2, 2))                           # outside the disc stays clear
    assert out.getpixel((300, 120))[:3] == (20, 60, 200)                          # printed ring


def _circle(size, c, r):
    from PIL import ImageDraw
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).ellipse((c - r, c - r, c + r, c + r), fill=255)
    return m


def test_product_codes_from_rom_headers(tmp_path):
    nds = bytearray(0x200)
    nds[0:12] = b"KYOKUGEN    "
    nds[0x0C:0x10] = b"BSKJ"
    z = tmp_path / "game (Japan).zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("game (Japan).nds", bytes(nds))
    assert maker.product_code("nds", z) == "BSKJ"
    cci = bytearray(0x4000)
    cci[0x100:0x104] = b"NCSD"
    cci[0x120:0x124] = struct.pack("<I", 0x10)                                    # partition 0 at 0x10 * 0x200
    cci[0x2000 + 0x150:0x2000 + 0x15A] = b"CTR-P-AJ2J"
    p = tmp_path / "game (Japan).3ds"
    p.write_bytes(bytes(cci))
    assert maker.product_code("3ds", p) == "AJ2J"
    assert maker.product_code("psp", p) is None
    assert maker.product_code("nds", tmp_path / "missing.zip") is None


def test_physical_status(tmp_path):
    (tmp_path / "roms/1/2/physical").mkdir(parents=True)
    (tmp_path / "roms/1/2/physical/physical.png").write_bytes(b"x")
    ss = {"physical_path": "roms/1/2/physical/physical.png"}
    jp = "https://x/mediaJeu.php?media=support-2D%28jp%29"
    assert maker.physical_status({"ss_metadata": {}}, tmp_path)[0] == "missing"
    assert maker.physical_status({"ss_metadata": {**ss, "physical_url": jp}}, tmp_path)[0] == "japanese"
    assert maker.physical_status({"ss_metadata": {**ss, "physical_url": jp.replace("jp", "us")}}, tmp_path) == \
        ("foreign", "ScreenScraper us")
    copy = tmp_path / "copy.png"
    copy.write_bytes(b"x")                                                        # Hakobiya's copy matches RomM's file
    assert maker.physical_status({"ss_metadata": {**ss, "physical_url": None}}, tmp_path, copy)[0] == "generated"
    copy.write_bytes(b"other")
    assert maker.physical_status({"ss_metadata": {**ss, "physical_url": jp}}, tmp_path, copy)[0] == "japanese"


@pytest.fixture
def app(tmp_path):
    config, res = tmp_path / "config", tmp_path / "res"
    (config / "make-templates").mkdir(parents=True)
    ref = solid((571, 600), (200, 205, 210, 255))
    ref.save(config / "make-templates" / "3ds.png")
    old = res / "roms/1/5/physical/physical.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"US cart")
    box = io.BytesIO()
    solid((640, 570), (10, 120, 40, 255)).save(box, "PNG")
    rom = {"id": 5, "name": "Gyakuten", "fs_name": "Gyakuten (Japan).3ds", "fs_path": "roms/3ds", "platform_id": 1,
           "platform_fs_slug": "3ds", "ss_id": 77, "path_cover_large": "/assets/romm/resources/roms/1/5/cover/big.png",
           "ss_metadata": {"physical_path": "roms/1/5/physical/physical.png", "box2d_path": "x",
                           "physical_url": "https://x/mediaJeu.php?media=support-2D(us)"}}
    puts = []
    s = Settings(config, tmp_path / "lib" / "roms", "http://qbit", "u", "p", proxy_key="k", romm_url="http://romm",
                 romm_token="t", romm_resources=res,
                 screenscraper={"user": "u", "password": "p", "dev_id": "d", "dev_password": "dp"})
    db = DB(":memory:")
    with respx.mock(assert_all_called=False) as router:
        router.get("http://romm/api/platforms").mock(return_value=httpx.Response(200, json=[
            {"id": 1, "name": "Nintendo 3DS", "fs_slug": "3ds", "rom_count": 1}]))
        router.get("http://romm/api/roms").mock(return_value=httpx.Response(200, json={"items": [rom], "total": 1}))
        router.get("http://romm/api/roms/5").mock(side_effect=lambda req: httpx.Response(200, json=rom))
        router.get(url__regex=r"http://romm/assets/.*").mock(return_value=httpx.Response(200, content=box.getvalue()))

        def put(req):
            body = req.content.decode("utf-8", "replace")
            data = json.loads(body.split("\r\n\r\n", 1)[1].rsplit("\r\n--", 1)[0])
            puts.append(data)
            # Like RomM: its response schema drops metadata keys it doesn't know (such as a marker of ours).
            rom["ss_metadata"] = {k: v for k, v in data.items() if k != "hakobiya_generated"}
            return httpx.Response(200, json=rom)
        router.put("http://romm/api/roms/5").mock(side_effect=put)
        ss_game = {"medias": [{"type": "support-2D", "region": "us", "url": "https://ss/us.png"}]}
        router.get(url__startswith="https://api.screenscraper.fr/api2/jeuInfos.php").mock(
            side_effect=lambda req: httpx.Response(200, json={"response": {"jeu": ss_game}}))
        genuine = io.BytesIO()
        solid((50, 50), (1, 2, 3, 255)).save(genuine, "PNG")
        router.get(url__startswith="https://neoclone.screenscraper.fr/").mock(
            return_value=httpx.Response(200, content=genuine.getvalue()))
        app_ = TestClient(create_app(s, mv.Minerva(db, tmp_path / "t"), None, db), headers=HEAD)
        app_.rom, app_.ss_game, app_.genuine = rom, ss_game, genuine.getvalue()
        yield app_, db, res, puts


def test_preview_save_and_undo(app):
    c, db, res, puts = app
    assert "Other region" in c.get("/make").text
    page = c.get("/make/p/1").text
    assert "ScreenScraper us" in page and 'name="focus_5"' in page
    prev = c.get("/make/preview/5", params={"focus": 0.2, "zoom": 1.5})
    assert prev.status_code == 200 and Image.open(io.BytesIO(prev.content)).size == (571, 600)
    assert not puts                                                               # previews change nothing
    c.post("/make/save", data={"platform": "1", "rom": "5", "focus_5": "0.2", "zoom_5": "1.5"})
    target = res / "roms/1/5/physical/physical.png"
    assert target.read_bytes()[:4] == b"\x89PNG"
    assert puts[-1]["hakobiya_generated"] is True and puts[-1]["physical_url"] is None
    assert puts[-1]["box2d_path"] == "x"                                          # the rest of the metadata kept
    change = db.one("SELECT * FROM make_changes")
    assert json.loads(change["settings"]) == {"focus": 0.2, "zoom": 1.5}
    c.post(f"/make/undo/{change['id']}")
    assert target.read_bytes() == b"US cart"
    assert "hakobiya_generated" not in puts[-1] and "support-2D(us)" in puts[-1]["physical_url"]


def test_hakobiya_restores_after_a_romm_refresh_and_hands_over_to_a_genuine_scan(app):
    c, db, res, puts = app
    c.post("/make/save", data={"platform": "1", "rom": "5"})
    change = db.one("SELECT * FROM make_changes")
    kept = Path(change["generated_path"]).read_bytes()
    target = res / "roms/1/5/physical/physical.png"
    page = c.get("/make/p/1", params={"show": "all"}).text
    assert "managed here" in page and "now: made here" in page
    assert "Every game here already has Japanese" in c.get("/make/p/1").text   # no longer listed as needing art

    # A RomM metadata refresh puts ScreenScraper's US cart back: Hakobiya notices and offers Restore.
    target.write_bytes(b"US cart again")
    c.rom["ss_metadata"] = {"physical_path": "roms/1/5/physical/physical.png",
                            "physical_url": "https://x/mediaJeu.php?media=support-2D(us)"}
    page = c.get("/make/p/1", params={"show": "all"}).text
    assert "replaced it with another region" in page and "/make/restore/" in page
    c.post(f"/make/restore/{change['id']}")
    assert target.read_bytes() == kept and puts[-1]["hakobiya_generated"] is True

    # No Japanese scan on ScreenScraper yet: the check changes nothing.
    c.post("/make/check", data={"back": "/make"})
    assert target.read_bytes() == kept and db.one("SELECT superseded_at FROM make_changes")[0] is None

    # ScreenScraper gets a Japanese scan: it replaces the generated one, without the login in the stored URL.
    c.ss_game["medias"].append({"type": "support-2D", "region": "jp", "support": "1",
                                "url": "https://neoclone.screenscraper.fr/api2/mediaJeu.php?devid=d&devpassword=dp"
                                       "&ssid=u&sspassword=p&jeuid=1&media=support-2D(jp)"})
    r = c.post("/make/check", data={"back": "/make"}, follow_redirects=False)
    assert "1 replaced by a genuine Japanese scan" in unquote(r.headers["location"])
    assert target.read_bytes() == c.genuine
    assert "hakobiya_generated" not in puts[-1] and "sspassword" not in puts[-1]["physical_url"]
    assert "support-2D%28jp%29" in puts[-1]["physical_url"] or "support-2D(jp)" in unquote(puts[-1]["physical_url"])
    assert db.one("SELECT superseded_by FROM make_changes")[0] == "ScreenScraper jp scan"
