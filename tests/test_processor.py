import os
import stat
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from hakobiya.db import DB
from hakobiya.processor import Processor
from hakobiya.worker import Worker

CUE = 'FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    lib = data / "media" / "romm_library" / "roms"
    lib.mkdir(parents=True)
    # Stand-in chdman: checks its arguments and writes the output file.
    fake = tmp_path / "chdman"
    fake.write_text('#!/bin/sh\nset -e\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; shift; done\n'
                    'echo "$0 $*" > /dev/null; printf CHD > "$out"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return data, lib, Processor(data, lib, min_free_tb=0, chdman=str(fake))


def put(data: Path, rel: str, members: dict[str, bytes] | None = None, raw: bytes = b"rom") -> str:
    path = data / "torrents" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if members is None:
        path.write_bytes(raw)
    else:
        with zipfile.ZipFile(path, "w") as z:
            for name, content in members.items():
                z.writestr(name, content)
    return rel


def test_cartridge_zips_are_hardlinked_and_never_overwritten(env):
    data, lib, p = env
    rel = put(data, "Minerva_Myrient/No-Intro/Nintendo - Family Computer Disk System (FDS)/Zelda (Japan).zip")
    out = p.run(1, rel, "fds")
    assert out.state == "done" and Path(out.target) == lib / "fds" / "Zelda (Japan).zip"
    assert os.stat(out.target).st_ino == os.stat(data / "torrents" / rel).st_ino
    assert p.run(2, rel, "fds").state == "exists"


def test_cd_discs_become_chds_in_a_game_folder_with_an_m3u(env):
    data, lib, p = env
    for d in (2, 1):
        rel = put(data, f"Minerva_Myrient/Redump/Sony - PlayStation/Aconcagua (Japan) (Disc {d}).zip",
                  {f"Aconcagua (Japan) (Disc {d}).cue": CUE.encode(), "game.bin": b"\0" * 10})
        out = p.run(10 + d, rel, "psx")
        assert out.state == "done", out
    folder = lib / "psx" / "Aconcagua (Japan)"
    assert sorted(x.name for x in folder.iterdir()) == ["Aconcagua (Japan) (Disc 1).chd", "Aconcagua (Japan) (Disc 2).chd",
                                                         "Aconcagua (Japan).m3u"]
    assert (folder / "Aconcagua (Japan).m3u").read_text() == "Aconcagua (Japan) (Disc 1).chd\nAconcagua (Japan) (Disc 2).chd\n"
    assert not any((data / "torrents" / ".hakobiya-tmp").iterdir())          # temp files cleaned up


def test_single_disc_and_psp_go_flat(env):
    data, lib, p = env
    rel = put(data, "Minerva_Myrient/Redump/Sony - PlayStation Portable/Clannad (Japan).zip", {"Clannad (Japan).iso": b"iso"})
    out = p.run(3, rel, "psp")
    assert out.state == "done" and Path(out.target) == lib / "psp" / "Clannad (Japan).chd"


def test_3ds_is_extracted(env):
    data, lib, p = env
    rel = put(data, "Minerva_Myrient/No-Intro/Nintendo - Nintendo 3DS (Decrypted)/Gyakuten Saiban 5 (Japan).zip",
              {"Gyakuten Saiban 5 (Japan).3ds": b"NCSD" * 10})
    out = p.run(4, rel, "3ds")
    assert out.state == "done" and (lib / "3ds" / "Gyakuten Saiban 5 (Japan).3ds").read_bytes() == b"NCSD" * 10


def test_gamecube_rvz_is_extracted(env):
    data, lib, p = env
    base = "Minerva_Myrient/Redump/Nintendo - GameCube - NKit RVZ [zstd-19-128k]"
    rel = put(data, f"{base}/Paper Mario RPG (Japan).zip", {"Paper Mario RPG (Japan).rvz": b"RVZ\x01"})
    out = p.run(6, rel, "ngc")
    assert out.state == "done" and (lib / "ngc" / "Paper Mario RPG (Japan).rvz").read_bytes() == b"RVZ\x01"
    for disc in (1, 2):
        name = f"Tales of Symphonia (Japan) (Disc {disc})"
        assert p.run(6 + disc, put(data, f"{base}/{name}.zip", {f"{name}.rvz": b"RVZ"}), "ngc").state == "done"
    game = lib / "ngc" / "Tales of Symphonia (Japan)"
    assert (game / "Tales of Symphonia (Japan).m3u").read_text().splitlines() == [
        "Tales of Symphonia (Japan) (Disc 1).rvz", "Tales of Symphonia (Japan) (Disc 2).rvz"]


def test_problems_are_reported_not_hidden(env):
    data, lib, p = env
    assert p.run(5, "x.zip", None).state == "needs_rule"
    assert p.run(6, "missing.zip", "psx").state == "failed"
    rel = put(data, "Minerva_Myrient/Redump/Sony - PlayStation/Empty (Japan).zip", {"readme.txt": b"x"})
    out = p.run(7, rel, "psx")
    assert out.state == "failed" and "no .cue" in out.note
    p.min_free_tb = 10 ** 6
    rel = put(data, "Minerva_Myrient/Redump/Sony - PlayStation/Big (Japan).zip", {"Big (Japan).cue": CUE.encode()})
    assert p.run(8, rel, "psx").state == "waiting_space"


class FakeQ:
    def __init__(self, files):
        self.files = files

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, path, **kw):
        return self.files


async def test_worker_files_finished_downloads_and_asks_romm_to_scan(env, tmp_path):
    data, lib, p = env
    rel = put(data, "Minerva_Myrient/No-Intro/Nintendo - Family Computer Disk System (FDS)/Zelda (Japan).zip")
    db = DB(":memory:")
    db.execute("INSERT INTO sets(name, href, source, platform) VALUES (?,?,?,?)",
               ("No-Intro - Nintendo - Family Computer Disk System (FDS)", "x", "No-Intro",
                "Nintendo - Family Computer Disk System (FDS)"))
    db.execute("INSERT INTO jobs(set_name, idx, path, size, infohash, state, created_at, updated_at)"
               " VALUES (?,?,?,?,?, 'sent', 'now', 'now')",
               ("No-Intro - Nintendo - Family Computer Disk System (FDS)", 1, rel, 3, "h"))
    files = [{"name": rel, "progress": 0.5, "priority": 1}]
    w = Worker(db, p, lambda: FakeQ(files), "http://romm", "tok")
    with respx.mock() as router:
        scan = router.post("http://romm/api/tasks/scan").mock(return_value=httpx.Response(200, json={}))
        await w.tick()
        assert db.one("SELECT state FROM jobs")["state"] == "downloading" and not scan.called
        files[0]["progress"] = 1
        await w.tick()
        assert db.one("SELECT state FROM jobs")["state"] == "done"
        assert (lib / "fds" / "Zelda (Japan).zip").exists()
        assert scan.calls[0].request.headers["authorization"] == "Bearer tok"
        assert b'"platform_fs_slugs":["fds"]' in scan.calls[0].request.content.replace(b" ", b"")
