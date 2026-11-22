"""Reading .torrent files: a small bencode decoder, the info hash, and the file list (v1, v2 or hybrid)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


class TorrentError(ValueError):
    pass


def _decode(data: bytes, i: int):
    """(value, next index). Dict keys stay bytes; the info dict's raw span is found separately."""
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        out, i = [], i + 1
        while data[i:i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        out, i = {}, i + 1
        while data[i:i + 1] != b"e":
            k, i = _decode(data, i)
            v, i = _decode(data, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        n = int(data[i:colon])
        start = colon + 1
        if start + n > len(data):
            raise TorrentError("truncated string")
        return data[start:start + n], start + n
    raise TorrentError(f"bad bencode at byte {i}")


def bdecode(data: bytes):
    try:
        value, end = _decode(data, 0)
    except (IndexError, ValueError) as e:
        raise TorrentError(f"not a torrent file: {e}") from None
    if end != len(data):
        raise TorrentError("trailing data after the torrent")
    return value


def _info_span(data: bytes) -> tuple[int, int]:
    """Byte range of the top-level info dict, which is what the info hash covers."""
    if data[:1] != b"d":
        raise TorrentError("not a torrent file")
    i = 1
    while data[i:i + 1] != b"e":
        k, i = _decode(data, i)
        start = i
        _, i = _decode(data, i)
        if k == b"info":
            return start, i
    raise TorrentError("torrent has no info dict")


@dataclass(frozen=True)
class TorrentFile:
    index: int          # qBittorrent's file index (order of the v1 list, or the v2 tree walk)
    path: str           # as qBittorrent shows it: root folder / ... / name
    size: int


@dataclass(frozen=True)
class Torrent:
    name: str
    infohash: str       # what qBittorrent calls the torrent's "hash": v1 SHA-1, or truncated SHA-256 for v2-only
    infohash_v2: str | None
    files: list[TorrentFile]

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


def _text(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def _v2_walk(tree: dict, prefix: list[str], out: list[tuple[str, int]]) -> None:
    for k in sorted(tree):                     # v2 file trees are ordered by name
        node = tree[k]
        if k == b"" and isinstance(node, dict) and b"length" in node:
            out.append(("/".join(prefix), node[b"length"]))
        elif isinstance(node, dict):
            _v2_walk(node, prefix + [_text(k)], out)


def parse(data: bytes) -> Torrent:
    meta = bdecode(data)
    start, end = _info_span(data)
    raw = data[start:end]
    info = meta.get(b"info")
    if not isinstance(info, dict) or b"name" not in info:
        raise TorrentError("torrent info has no name")
    name = _text(info[b"name"])
    v2 = info.get(b"meta version") == 2 and isinstance(info.get(b"file tree"), dict)
    has_v1 = b"files" in info or b"length" in info
    entries: list[tuple[str, int]] = []
    if b"files" in info:
        for f in info[b"files"]:
            parts = [_text(p) for p in f.get(b"path.utf-8", f[b"path"])]
            if b"p" in f.get(b"attr", b"") or (parts and parts[-1].startswith(".pad")):
                entries.append(("", -1))        # padding file: keeps qBittorrent's indexes, hidden from the list
                continue
            entries.append(("/".join([name] + parts), f[b"length"]))
    elif b"length" in info:
        entries.append((name, info[b"length"]))
    elif v2:
        walked: list[tuple[str, int]] = []
        _v2_walk(info[b"file tree"], [name], walked)
        entries = walked
    else:
        raise TorrentError("torrent lists no files")
    files = [TorrentFile(i, p, s) for i, (p, s) in enumerate(entries) if s >= 0]
    sha256 = hashlib.sha256(raw).hexdigest() if v2 else None
    infohash = hashlib.sha1(raw).hexdigest() if has_v1 else sha256[:40]
    return Torrent(name, infohash, sha256, files)
