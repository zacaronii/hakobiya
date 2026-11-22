from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hakobiya import minerva as mv
from hakobiya.app import Settings, create_app
from hakobiya.db import DB

FIX = Path(__file__).parent / "fixtures"
FDS = "No-Intro - Nintendo - Family Computer Disk System (FDS)"
FDS_HASH = "5f79b184dc775e754d1f935b2fe568e43a5725c7"


class FakeQ:
    def __init__(self, torrents=(), files=None, fail=False):
        self._t, self._f, self.fail = list(torrents), files or {}, fail

    async def torrents(self):
        if self.fail:
            raise RuntimeError("connection refused")
        return self._t

    async def files(self, h):
        return self._f.get(h, [])


def fds_files(picked=()):
    from hakobiya import torrent
    t = torrent.parse((FIX / "fds.torrent").read_bytes())
    return [{"index": f.index, "name": f.path, "priority": 1 if f.index in picked else 0,
             "progress": 1.0 if f.index in picked else 0.0} for f in t.files]


@pytest.fixture
def env(tmp_path):
    roms = tmp_path / "roms"
    (roms / "fds").mkdir(parents=True)
    (roms / "fds" / "Ai Senshi Nicol (Japan).zip").write_bytes(b"x")
    s = Settings(tmp_path / "config", roms, "http://qbit", "u", "p")
    db = DB(":memory:")
    with respx.mock(assert_all_called=False) as router:
        idx = router.get(mv.CDN).mock(return_value=httpx.Response(200, text=(FIX / "index.html").read_text(),
                                                                   headers={"etag": '"v1"'}))
        tor = router.get(url__regex=r".*Family%20Computer%20Disk%20System%20\(FDS\)\.torrent$").mock(
            return_value=httpx.Response(200, content=(FIX / "fds.torrent").read_bytes()))
        yield s, db, idx, tor


def client(env, q):
    s, db, *_ = env
    return TestClient(create_app(s, mv.Minerva(db, s.config_dir / "torrents"), q, db))


def test_set_list_marks_sets_already_in_qbittorrent(env):
    q = FakeQ([{"name": "Minerva_Myrient", "hash": FDS_HASH}], {FDS_HASH: fds_files()})
    c = client(env, q)
    page = c.get("/").text
    assert "Family Computer Disk System (FDS)" in page and "in qBittorrent" in page
    assert env[2].call_count == 1
    c.get("/")
    assert env[2].call_count == 1                      # cached for a day: no second request to Minerva


def test_index_refresh_is_conditional(env):
    c = client(env, FakeQ())
    c.get("/")
    env[2].mock(return_value=httpx.Response(304))
    c.post("/refresh", headers={"origin": "http://testserver"})
    assert env[2].calls[-1].request.headers["if-none-match"] == '"v1"'


def test_set_page_filters_and_states(env):
    q = FakeQ([{"name": "Minerva_Myrient", "hash": FDS_HASH}], {FDS_HASH: fds_files(picked={1})})
    c = client(env, q)
    c.get("/")
    page = c.get("/set/" + FDS).text
    assert "Adian no Tsue (Japan).zip" in page and "(Beta)" not in page        # Japan only, betas hidden
    assert "downloaded" in page and "in RomM" in page
    assert "[BIOS]" not in page
    assert "(Beta)" in c.get("/set/" + FDS, params={"show": "Beta"}).text
    missing = c.get("/set/" + FDS, params={"missing": "true"}).text
    assert "Ai Senshi Nicol (Japan).zip" not in missing and "Adian no Tsue (Japan).zip" in missing
    only = c.get("/set/" + FDS, params={"state": "done"}).text
    assert "Adian no Tsue (Japan).zip" in only and "Ai Senshi Nicol" not in only
    assert env[3].call_count == 1                      # the .torrent is downloaded once, then kept
    c.get("/set/" + FDS, params={"q": "nicol"})
    assert env[3].call_count == 1


def test_works_without_qbittorrent(env):
    c = client(env, FakeQ(fail=True))
    assert "isn't answering" in c.get("/").text
    page = c.get("/set/" + FDS).text
    assert "Adian no Tsue (Japan).zip" in page


def test_a_different_torrent_for_the_same_set_is_flagged(env):
    files = [{"index": 0, "name": "Minerva_Myrient/No-Intro/Nintendo - Family Computer Disk System (FDS)/x.zip",
              "priority": 1, "progress": 1}]
    c = client(env, FakeQ([{"name": "Minerva_Myrient", "hash": "f" * 40}], {"f" * 40: files}))
    c.get("/")
    assert "different torrent" in c.get("/set/" + FDS).text
