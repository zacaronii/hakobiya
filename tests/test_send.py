"""Sending: basket, guard, and the qBittorrent calls for a set that's already there and for a new one."""
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hakobiya import minerva as mv
from hakobiya import torrent
from hakobiya.app import Settings, create_app
from hakobiya.db import DB
from hakobiya.qbit import QBit, QBitView

FIX = Path(__file__).parent / "fixtures"
FDS = "No-Intro - Nintendo - Family Computer Disk System (FDS)"
T = torrent.parse((FIX / "fds.torrent").read_bytes())
ORIGIN = {"origin": "http://testserver", "x-hakobiya-key": "k", "x-authentik-username": "someone"}


class FakeQBit:
    """Just enough of qBittorrent's WebAPI, with the state the tests check."""

    def __init__(self, has_set=False, state="uploading", on=()):
        self.torrents = {}
        self.calls = []
        if has_set:
            self._load(state, on)

    def _load(self, state, on=()):
        self.torrents[T.infohash] = {"hash": T.infohash, "name": T.name, "state": state, "tags": "",
                                     "files": [{"index": f.index, "name": f.path, "priority": 1 if f.index in on else 0,
                                                "progress": 0.0} for f in T.files]}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v2/")
        form = parse_qs(request.content.decode("latin-1")) if request.method == "POST" and \
            b"form-data" not in request.headers.get("content-type", "").encode() else {}
        one = lambda k: form[k][0]
        self.calls.append(path)
        if path in ("auth/login", "auth/logout"):
            return httpx.Response(204)
        if path == "torrents/info":
            h = request.url.params.get("hashes")
            ts = [t for t in self.torrents.values() if not h or t["hash"] == h]
            return httpx.Response(200, json=[{k: v for k, v in t.items() if k != "files"} for t in ts])
        if path == "torrents/files":
            return httpx.Response(200, json=self.torrents[request.url.params["hash"]]["files"])
        if path == "torrents/add":
            assert b'name="stopped"' in request.content and b"Minerva_Myrient" in request.content
            self._load("stoppedDL", on=range(len(T.files)))          # qBittorrent adds with every file on
            self.torrents[T.infohash]["tags"] = "hakobiya"
            return httpx.Response(200, text="Ok.")
        if path == "torrents/filePrio":
            t = self.torrents[one("hash")]
            ids = {int(i) for i in one("id").split("|")}
            for f in t["files"]:
                if f["index"] in ids:
                    f["priority"] = int(one("priority"))
            return httpx.Response(200)
        if path == "torrents/start":
            self.torrents[one("hashes")]["state"] = "downloading"
            return httpx.Response(200)
        return httpx.Response(404)

    def on(self):
        return sorted(f["index"] for f in self.torrents[T.infohash]["files"] if f["priority"] > 0)


@pytest.fixture
def make(tmp_path):
    routers = []

    def _make(fake):
        roms = tmp_path / "roms"
        roms.mkdir(exist_ok=True)
        s = Settings(tmp_path / "config", roms, "http://qbit", "u", "p", proxy_key="k")
        db = DB(":memory:")
        router = respx.mock(assert_all_called=False)
        router.start()
        routers.append(router)
        router.get(mv.CDN).mock(return_value=httpx.Response(200, text=(FIX / "index.html").read_text()))
        router.get(url__regex=r".*\(FDS\)\.torrent$").mock(
            return_value=httpx.Response(200, content=(FIX / "fds.torrent").read_bytes()))
        router.route(host="qbit").mock(side_effect=fake.handle)
        view = QBitView(lambda: QBit("http://qbit", "u", "p"))
        c = TestClient(create_app(s, mv.Minerva(db, s.config_dir / "torrents"), view, db), headers=ORIGIN)
        c.get("/")
        return c, db, router
    yield _make
    for r in routers:
        r.stop()


def test_requests_without_the_proxy_key_or_from_another_site_are_refused(make):
    c, *_ = make(FakeQBit())
    refused = c.get("/", headers={"x-hakobiya-key": ""})
    assert refused.status_code == 403 and "through the sign-in" in refused.text
    assert c.get("/healthz", headers={"x-hakobiya-key": ""}).status_code == 200
    assert c.post("/pick", json={"set": FDS, "idx": [1], "on": True},
                  headers={"origin": "https://evil.example"}).status_code == 403


def test_basket_survives_pages_and_pick_all_follows_the_filters(make):
    c, db, _ = make(FakeQBit())
    c.get("/set/" + FDS)
    assert c.post("/pick", json={"set": FDS, "idx": [1, 2], "on": True}).json()["count"] == 2
    assert c.post("/pick", json={"set": FDS, "idx": [2], "on": False}).json()["count"] == 1
    assert c.post("/pick", json={"set": FDS, "idx": [99999], "on": True}).status_code == 400
    c.post("/pick/all", data={"set": FDS, "q": "zelda", "region": "Japan", "back": ""})
    names = {r["filename"] for r in db.all("SELECT f.filename FROM picks p JOIN files f USING (set_name, idx)")}
    assert "Adian no Tsue (Japan).zip" in names and any("Zelda" in n for n in names)
    assert not any("(Beta)" in n for n in names)                    # hidden by the filters, so not picked
    c.post("/pick/all", data={"set": FDS, "clear": "1", "back": ""})
    assert db.one("SELECT count(*) n FROM picks")["n"] == 0


def test_a_new_set_is_added_stopped_with_only_the_picks_on(make):
    fake = FakeQBit()
    c, db, _ = make(fake)
    c.get("/set/" + FDS)
    c.post("/pick", json={"set": FDS, "idx": [1, 2], "on": True})
    r = c.post("/send", data={"set": FDS, "back": ""}, follow_redirects=False)
    assert r.status_code == 303 and "Sent" in r.headers["location"]
    assert fake.on() == [1, 2] and fake.torrents[T.infohash]["state"] == "downloading"
    assert fake.calls.index("torrents/start") > max(i for i, p in enumerate(fake.calls) if p == "torrents/filePrio")
    assert [r["idx"] for r in db.all("SELECT idx FROM jobs ORDER BY idx")] == [1, 2]
    assert db.one("SELECT count(*) n FROM picks")["n"] == 0
    assert fake.calls.count("auth/login") == fake.calls.count("auth/logout")
    page = c.get("/queue").text
    assert "Adian no Tsue (Japan).zip" in page and "%" in page


def test_a_set_already_there_only_gets_files_switched_on(make):
    fake = FakeQBit(has_set=True, state="stoppedUP", on={5})
    c, db, _ = make(fake)
    c.get("/set/" + FDS)
    c.post("/pick", json={"set": FDS, "idx": [1, 5], "on": True})
    r = c.post("/send", data={"set": FDS, "back": "region=Japan"}, follow_redirects=False)
    where = unquote(r.headers["location"])
    assert "switched on 1 file" in where and "(1 were already on)" in where
    assert fake.on() == [1, 5]                                          # nothing switched off
    assert "torrents/add" not in fake.calls and fake.torrents[T.infohash]["state"] == "downloading"


def test_files_downloaded_outside_hakobiya_can_be_picked_and_sent(make):
    fake = FakeQBit(has_set=True, state="stalledUP", on={1, 2})
    for f in fake.torrents[T.infohash]["files"]:
        f["progress"] = 1.0 if f["index"] in (1, 2) else 0.0
    c, db, _ = make(fake)
    page = c.get("/set/" + FDS).text
    assert 'class="pick" value="1"' in page and "Already sent" not in page    # on in qBittorrent, but never sent here
    c.post("/pick/all", data={"set": FDS, "state": "done", "back": ""})
    assert [r["idx"] for r in db.all("SELECT idx FROM picks ORDER BY idx")] == [1, 2]
    r = c.post("/send", data={"set": FDS, "back": ""}, follow_redirects=False)
    assert "(2 were already on)" in unquote(r.headers["location"])
    assert fake.on() == [1, 2] and "torrents/start" not in fake.calls           # nothing to switch on or start
    assert [r["idx"] for r in db.all("SELECT idx FROM jobs ORDER BY idx")] == [1, 2]
    page = c.get("/set/" + FDS).text
    assert 'class="pick" value="1"' not in page and "Already sent" in page


def test_a_qbittorrent_error_keeps_the_basket(make):
    fake = FakeQBit()
    fake.handle = lambda request: httpx.Response(403) if "add" in request.url.path else FakeQBit.handle(fake, request)
    c, db, router = make(fake)
    router.route(host="qbit").mock(side_effect=fake.handle)
    c.get("/set/" + FDS)
    c.post("/pick", json={"set": FDS, "idx": [1], "on": True})
    r = c.post("/send", data={"set": FDS, "back": ""}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert db.one("SELECT count(*) n FROM picks")["n"] == 1 and db.one("SELECT count(*) n FROM jobs")["n"] == 0
