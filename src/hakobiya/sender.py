"""Sending picked files to qBittorrent.

A set already in qBittorrent (same info hash): the picked files that are off get priority 1; nothing else changes,
and nothing is ever switched off. A new set: added stopped (qBittorrent here pre-allocates whole files, so starting
it first would reserve the full set), every file set to 0, the picks to 1, then started. Files are matched by path,
not by index, so padding files or a client that numbers them differently can't shift a pick onto the wrong file."""

from __future__ import annotations

from dataclasses import dataclass

from .qbit import STOPPED, QBit, QBitError
from .torrent import Torrent

TAG = "hakobiya"


@dataclass
class SendResult:
    mode: str              # "existing" | "added"
    turned_on: int         # files switched from off to on
    already: int           # picks that were already on
    started: bool


async def send(q: QBit, t: Torrent, torrent_bytes: bytes, filename: str, picks: list[int],
               tags: str = TAG) -> SendResult:
    """`tags` go on a torrent this adds; one already in qBittorrent keeps its own."""
    wanted = {f.path for f in t.files if f.index in set(picks)}
    if len(wanted) != len(set(picks)):
        raise QBitError("some picked files aren't in this torrent")
    existing = await q.get("torrents/info", hashes=t.infohash)
    if existing:
        files = await q.get("torrents/files", hash=t.infohash)
        by_path = {f["name"]: f for f in files}
        missing = wanted - by_path.keys()
        if missing:
            raise QBitError(f"qBittorrent's copy of this torrent lacks {len(missing)} picked file(s)")
        need = [by_path[p]["index"] for p in sorted(wanted) if by_path[p]["priority"] == 0]
        if need:
            await q.set_priority(t.infohash, need, 1)
        started = existing[0]["state"] in STOPPED and bool(need)
        if started:
            await q.start(t.infohash)
        return SendResult("existing", len(need), len(wanted) - len(need), started)

    await q.add_stopped(torrent_bytes, filename, tags)
    files = await q.wait_for(t.infohash, len(t.files))
    by_path = {f["name"]: f for f in files}
    missing = wanted - by_path.keys()
    if missing:
        raise QBitError(f"the added torrent lacks {len(missing)} picked file(s); it was left stopped")
    keep = sorted(by_path[p]["index"] for p in wanted)
    off = sorted(f["index"] for f in files if f["name"] not in wanted)
    await q.set_priority(t.infohash, off, 0)
    await q.set_priority(t.infohash, keep, 1)
    await q.start(t.infohash)
    return SendResult("added", len(keep), 0, True)
