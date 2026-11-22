import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hakobiya import minerva as mv
from hakobiya import regions
from hakobiya.app import Settings, create_app
from hakobiya.db import DB

HEAD = {"origin": "http://testserver", "x-hakobiya-key": "k"}
SS = "https://x/mediaJeu.php?devid=d&media={}&gameid=1"


def rom(rom_id, name, **media):
    """A RomM game whose saved images are given as kind="region" (or kind=None for an image without a region)."""
    ss = {}
    for kind, region in media.items():
        ss[f"{kind}_path"] = f"roms/9/{rom_id}/{kind}/{kind}.png"
        ss[f"{kind}_url"] = SS.format(f"tag%28{region}%29" if region else "fanart")
    return {"id": rom_id, "platform_id": 9, "name": name, "fs_name": name + ".zip", "ss_metadata": ss}


def test_region_comes_from_the_media_tag():
    assert regions.region_of(SS.format("box-2D-side%28us%29")) == "us"
    assert regions.region_of(SS.format("support-2D(jp)[1]")) == "jp"
    assert regions.region_of(SS.format("fanart")) is None
    assert regions.region_of(None) is None
    game = rom(1, "Game", box2d_side="us", box2d_back="jp", logo="wor", fanart=None)
    game["ss_metadata"]["box3d_url"] = SS.format("box-3D(us)")              # a URL RomM never downloaded
    assert [(i["kind"], i["region"], i["auto"]) for i in regions.foreign(game)] == [("box2d_side", "us", True),
                                                                                   ("logo", "wor", False)]
    assert regions.inside(Path("/r"), "../etc/passwd") is None


@pytest.fixture
def app(tmp_path):
    s = Settings(tmp_path / "config", tmp_path / "roms", "http://qbit", "u", "p", proxy_key="k",
                 romm_url="http://romm", romm_token="tok", romm_resources=tmp_path / "res")
    db = DB(":memory:")
    games = {1: rom(1, "Mixed", box2d_side="us", title_screen="wor", logo="us", box3d="jp"),
             2: rom(2, "Japanese", box2d_side="jp", fanart=None)}
    for g in games.values():
        for key, path in g["ss_metadata"].items():
            if key.endswith("_path"):
                (s.romm_resources / path).parent.mkdir(parents=True, exist_ok=True)
                (s.romm_resources / path).write_bytes(b"\x89PNG " + key.encode())
    with respx.mock(assert_all_called=False) as router:
        router.get("http://romm/api/platforms").mock(return_value=httpx.Response(200, json=[
            {"id": 9, "name": "Nintendo DS", "fs_slug": "nds", "rom_count": 2}]))
        router.get("http://romm/api/roms").mock(
            side_effect=lambda request: httpx.Response(200, json={"items": list(games.values()), "total": 2}))
        router.get(url__regex=r"http://romm/api/roms/\d+").mock(
            side_effect=lambda request: httpx.Response(200, json=games[int(request.url.path.rsplit("/", 1)[1])]))

        def put(request):                            # RomM stores the metadata it is sent
            rom_id = int(request.url.path.rsplit("/", 1)[1])
            body = request.content.decode()
            games[rom_id]["ss_metadata"] = json.loads(body[body.index("{"):body.rindex("}") + 1])
            return httpx.Response(200, json={})
        router.put(url__regex=r"http://romm/api/roms/\d+").mock(side_effect=put)
        c = TestClient(create_app(s, mv.Minerva(db, tmp_path / "t"), None, db), headers=HEAD)
        yield c, db, games, s.romm_resources


def test_check_removes_only_the_automatic_kinds_and_keeps_a_backup(app):
    c, db, games, res = app
    page = c.get("/art/regions").text
    assert page.count('class="card"') == 3 and "Japanese</div>" not in page
    assert "never runs on its own" in page                                        # the timed check is opt-in
    assert c.get("/art/regions/image/1/logo").content.startswith(b"\x89PNG")
    r = c.post("/art/regions/check", follow_redirects=False)
    assert "2 image(s) removed" in unquote(r.headers["location"])
    ss = games[1]["ss_metadata"]
    assert ss["box2d_side_path"] is None and ss["title_screen_path"] is None
    assert ss["logo_path"] and ss["box3d_path"] and ss["box2d_side_url"]          # the rest is untouched
    assert not (res / "roms/9/1/box2d_side/box2d_side.png").exists() and (res / "roms/9/1/logo/logo.png").exists()
    assert games[2]["ss_metadata"]["box2d_side_path"]
    rows = db.all("SELECT * FROM media_changes ORDER BY kind")
    assert [(x["kind"], x["region"], x["removed_by"]) for x in rows] == [("box2d_side", "us", None),
                                                                         ("title_screen", "wor", None)]
    assert Path(rows[0]["backup_path"]).read_bytes() == b"\x89PNG box2d_side_path"
    assert c.get(f"/art/regions/removed/{rows[0]['id']}").status_code == 200
    assert "0 image(s) removed" in unquote(c.post("/art/regions/check", follow_redirects=False).headers["location"])


def test_put_back_restores_the_image_and_the_check_leaves_it(app):
    c, db, games, res = app
    c.post("/art/regions/check")
    change = db.one("SELECT * FROM media_changes WHERE kind='box2d_side'")
    c.post(f"/art/regions/undo/{change['id']}")
    assert games[1]["ss_metadata"]["box2d_side_path"] == "roms/9/1/box2d_side/box2d_side.png"
    assert (res / "roms/9/1/box2d_side/box2d_side.png").read_bytes() == b"\x89PNG box2d_side_path"
    assert "0 image(s) removed" in unquote(c.post("/art/regions/check", follow_redirects=False).headers["location"])
    assert "put back by you" in c.get("/art/regions").text
    assert c.post(f"/art/regions/undo/{change['id']}").status_code == 404


def test_listed_kinds_go_only_when_picked(app):
    c, db, games, res = app
    r = c.post("/art/regions/remove", data={"item": ["1:logo", "2:box2d_side", "1:nonsense"]}, follow_redirects=False)
    assert "Removed 1 image" in unquote(r.headers["location"])
    assert games[1]["ss_metadata"]["logo_path"] is None and games[1]["ss_metadata"]["box2d_side_path"]
    assert games[2]["ss_metadata"]["box2d_side_path"]                              # Japanese: never removed
    assert db.one("SELECT removed_by FROM media_changes")["removed_by"] == "you"
