from pathlib import Path

import pytest

from hakobiya import minerva, names, torrent
from hakobiya.library import platform_slug

FIX = Path(__file__).parent / "fixtures"


def test_no_intro_names():
    n = names.parse("Adian no Tsue (Japan) (Beta) (1986-10-29).zip")
    assert (n.title, n.regions, n.tags) == ("Adian no Tsue", ["Japan"], {"Beta"})
    n = names.parse("Final Fantasy VII (Japan) (Disc 2).zip")
    assert (n.disc, n.game) == (2, "Final Fantasy VII (Japan)")
    n = names.parse("[BIOS] Twin Famicom (Japan) (En).zip")
    assert n.tags == {"BIOS"} and n.languages == ["En"] and n.title == "Twin Famicom"
    n = names.parse("Zelda no Densetsu (Japan, USA) (En,Ja) (Rev 1).zip")
    assert n.regions == ["Japan", "USA"] and n.languages == ["En", "Ja"] and n.rev == "1"
    assert names.parse("Game (Japan) (Demo 2).zip").tags == {"Demo"}
    assert names.parse("Game (Japan) (Proto) (Unl).zip").tags == {"Proto", "Unl"}


def test_fds_torrent():
    t = torrent.parse((FIX / "fds.torrent").read_bytes())
    assert t.name == "Minerva_Myrient" and len(t.files) == 414
    assert t.infohash == "5f79b184dc775e754d1f935b2fe568e43a5725c7"
    assert t.files[1].path.endswith("/Nintendo - Family Computer Disk System (FDS)/Adian no Tsue (Japan).zip")


def test_v2_only_and_padding():
    def b(v):
        if isinstance(v, int):
            return b"i%de" % v
        if isinstance(v, bytes):
            return b"%d:%s" % (len(v), v)
        if isinstance(v, list):
            return b"l" + b"".join(b(x) for x in v) + b"e"
        return b"d" + b"".join(b(k) + b(v[k]) for k in sorted(v)) + b"e"
    v1 = b({b"info": {b"name": b"root", b"piece length": 16384, b"pieces": b"x" * 20, b"files": [
        {b"path": [b"a.zip"], b"length": 5}, {b"path": [b".pad", b"123"], b"length": 7, b"attr": b"p"},
        {b"path": [b"b.zip"], b"length": 9}]}})
    t = torrent.parse(v1)
    assert [(f.index, f.path) for f in t.files] == [(0, "root/a.zip"), (2, "root/b.zip")]   # qBittorrent's indexes
    v2 = b({b"info": {b"name": b"root", b"meta version": 2, b"piece length": 16384,
                      b"file tree": {b"b.zip": {b"": {b"length": 9}}, b"a.zip": {b"": {b"length": 5}}}}})
    t = torrent.parse(v2)
    assert [f.path for f in t.files] == ["root/a.zip", "root/b.zip"] and len(t.infohash) == 40 and t.infohash_v2
    with pytest.raises(torrent.TorrentError):
        torrent.parse(b"<html>not a torrent</html>")


def test_index_page():
    rows = minerva.parse_index((FIX / "index.html").read_text())
    by = {r["name"]: r for r in rows}
    fds = by["No-Intro - Nintendo - Family Computer Disk System (FDS)"]
    assert (fds["source"], fds["platform"], fds["size_text"]) == ("No-Intro", "Nintendo - Family Computer Disk System (FDS)", "68K")
    assert fds["href"].endswith(".torrent") and "%20" in fds["href"]
    assert by["Redump - NEC - PC Engine CD & TurboGrafx CD"]["href"].count("&amp;") == 0     # &amp; unescaped
    assert platform_slug("Redump", "NEC - PC Engine CD & TurboGrafx CD") == "turbografx-cd"
    assert platform_slug("No-Intro", "Nintendo - Family Computer Disk System (FDS) (Aftermarket)") == "fds"
    assert platform_slug("TOSEC", "Nintendo - Famicom") is None
