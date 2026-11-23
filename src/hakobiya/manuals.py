"""Japanese manuals for the games in RomM, from the Internet Archive.

RomM takes a game's manual from ScreenScraper, which has a Japanese one for only some games: the rest have another
region's or none. The Internet Archive holds scans of many Japanese manuals, as single items and as collections of
PDFs or of page images. Hakobiya searches it by a game's titles, lists the PDFs and the uploader's own page images
(a Comic Book ZIP or a zip, made into a PDF here by `scans.py`) of what it finds, and shows one for a look; nothing
goes into RomM until the owner picks one. The manual it replaces is kept for "Put back".

Only archive.org is asked: its search, an item's file list, and the file that was picked. Requests go one at a time
with a pause between them, and the answers are cached."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

from . import __version__, scans as page_scans
from .db import DB
from .regions import region_of

ARCHIVE = "https://archive.org"
USER_AGENT = f"hakobiya/{__version__} (personal game-manual lookup; one user, cached)"
SEARCH_TTL = 7 * 86400
ITEM_TTL = 30 * 86400
CACHE_DAYS = 7                       # a PDF fetched for a look and never used
CACHE_SIZE = 2 * 1024 ** 3           # all of them together: the oldest go first
MAX_SIZE = 300 * 1024 * 1024            # a PDF
MAX_SCANS = 200 * 1024 * 1024           # an archive of page images (a manual's is a few MB)
SCAN_ARCHIVES = (".cbz", ".zip")        # only the uploader's own ("original"): the Archive's _jp2.zip is reworked
ROWS = 12
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MANUAL_WORDS = "manual OR manuals OR scan OR scans OR booklet OR 説明書 OR マニュアル"

# Collections of Japanese manuals with one PDF or archive of page images per game, by RomM platform slug. Their files are matched by name.
PACKS = {
    "psx": ["gamemanuals-psinstructionmanual", "psone-classics-manuals-japan"],
    "psp": ["phantasy-star-portable-2-infinity"],
}


class ArchiveError(RuntimeError):
    pass


# ------------------------------------------------------------------ what a game has now
# manual_changes.identifier values that aren't Archive items
OTHER_SOURCES = {":psn": "PS one Classics package", ":psn-psp": "PSP PlayStation Store package"}


def source_label(identifier: str) -> str:
    """Where a manual Hakobiya put into RomM came from, for the pages."""
    return OTHER_SOURCES.get(identifier) or f"Internet Archive: {identifier}"


def manual_state(rom: dict, change: dict | None = None) -> tuple[str, bool]:
    """(label, is it Japanese) for the manual RomM has for a game. `change` is Hakobiya's own record for the game,
    given only while the file it put there is still the one in RomM: RomM's API doesn't tell an uploaded manual
    from a scraped one whose address it kept."""
    if not rom.get("path_manual"):
        return "no manual", False
    if change:
        return source_label(change["identifier"]), True
    if not rom.get("url_manual"):
        return "uploaded", True
    region = region_of(rom["url_manual"])
    return (f"ScreenScraper {region}", region == "jp") if region else ("scraped", False)


# ------------------------------------------------------------------ names
def has_kana(text: str) -> bool:
    return any("\u3040" <= ch <= "\u30ff" for ch in text)


def has_cjk(text: str) -> bool:
    return has_kana(text) or any("\u4e00" <= ch <= "\u9fff" for ch in text)


def article_first(name: str) -> str:
    """No-Intro's `Title, The - Sub` as `The Title - Sub`."""
    m = re.match(r"^(.*?), (The|A|An)\b(.*)$", name)
    return f"{m.group(2)} {m.group(1)}{m.group(3)}" if m else name


def game_names(rom: dict) -> list[str]:
    """The titles a game goes by, Japanese script first."""
    names = [rom.get("name") or "", article_first(rom.get("fs_name_no_tags") or ""), *(rom.get("alternative_names") or [])]
    names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    seen, out = set(), []
    for n in sorted(names, key=lambda n: 0 if has_kana(n) else 1 if has_cjk(n) else 2):
        if key(n) and key(n) not in seen:
            seen.add(key(n))
            out.append(n)
    return out


def search_terms(rom: dict) -> list[str]:
    """What the search box starts with: up to two Japanese titles and one in Latin letters."""
    names = game_names(rom)
    return [n for n in names if has_cjk(n)][:2] + [n for n in names if not has_cjk(n)][:1]


def key(text: str) -> str:
    """A title reduced to its letters and digits, for comparing."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def file_keys(filename: str) -> set[str]:
    """The titles in a collection's file name. They come as `Title.pdf`, `Title (o).pdf`, `Title (o)_text.pdf`,
    `日本語の題 (Romaji Title).pdf`, and the same with `.cbz`, `.zip` or `_jp2.zip` for page images."""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = re.sub(r"_(text|jp2)$", "", stem)
    stem = re.sub(r"\s*\(o\)$", "", stem)
    parts = [stem] + re.findall(r"\(([^()]+)\)", stem) + [re.sub(r"\s*\([^()]*\)", "", stem)]
    return {key(p) for p in parts if len(key(p)) >= 2}


def name_match(names: list[str], filename: str) -> str | None:
    """`same` when a title equals one in the file name, `close` when one contains the other, else None."""
    mine, theirs = {key(n) for n in names if key(n)}, file_keys(filename)
    if mine & theirs:
        return "same"
    for a in mine:
        for b in theirs:
            short, long = sorted((a, b), key=len)
            if len(short) >= (4 if has_cjk(short) else 8) and short in long:
                return "close"
    return None


# ------------------------------------------------------------------ the search
def phrase(term: str) -> str:
    """A title as a quoted phrase, without the characters the Archive's search reads as syntax."""
    return re.sub(r"\s+", " ", re.sub(r'["\\()\[\]{}:^~*?!+/|&<>=]', " ", term)).strip()


def search_query(terms: list[str], manuals_only: bool = True) -> str:
    titles = " OR ".join(f'title:("{phrase(t)}")' for t in terms if phrase(t))
    if not titles:
        return ""
    return f"({titles})" + (f" AND (title:({MANUAL_WORDS}) OR subject:(manual OR 説明書))" if manuals_only else "")


def item_link(text: str) -> str | None:
    """The item named by an archive.org address typed into the search box."""
    m = re.fullmatch(r"\s*(?:https?://)?(?:www\.)?archive\.org/(?:details|download)/([^/?#\s]+)\S*\s*", text)
    return m.group(1) if m and IDENTIFIER.fullmatch(m.group(1)) else None


def is_japanese(language) -> bool:
    values = language if isinstance(language, list) else [language]
    return any(str(v).strip().lower() in ("japanese", "jpn", "ja", "jp") for v in values if v)


def first(value) -> str:
    return str((value[0] if value else "") if isinstance(value, list) else value or "")


def allowed(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" and (u.hostname == "archive.org" or (u.hostname or "").endswith(".archive.org"))


class Archive:
    def __init__(self, db: DB, cache_dir: Path, client: httpx.AsyncClient | None = None, pause: float = 1.0):
        self.db, self.cache_dir, self.pause = db, cache_dir, pause
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), headers={"User-Agent": USER_AGENT})
        self.turn = asyncio.Lock()          # one request at a time
        self.making = asyncio.Lock()        # one archive of page images made into a PDF at a time
        self.last = 0.0

    async def _wait(self) -> None:
        gap = self.last + self.pause - time.monotonic()
        if gap > 0:
            await asyncio.sleep(gap)
        self.last = time.monotonic()

    async def _json(self, cache_key: str, ttl: float, url: str, params, fresh: bool = False):
        row = self.db.one("SELECT fetched_at, body FROM manual_cache WHERE key=?", (cache_key,))
        if row and not fresh and time.time() - row["fetched_at"] < ttl:
            return json.loads(row["body"]), None
        async with self.turn:
            await self._wait()
            try:
                r = await self.http.get(url, params=params)
            except httpx.HTTPError as e:
                raise ArchiveError(f"The Internet Archive isn't answering ({type(e).__name__}).") from e
        if r.status_code != 200:
            raise ArchiveError(f"The Internet Archive isn't answering (HTTP {r.status_code}).")
        try:
            return None, r.json()
        except ValueError as e:
            raise ArchiveError("The Internet Archive's answer wasn't readable.") from e

    def _keep(self, cache_key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO manual_cache(key, fetched_at, body) VALUES (?,?,?)",
                        (cache_key, time.time(), json.dumps(value, ensure_ascii=False)))

    async def search(self, terms: list[str], manuals_only: bool = True, fresh: bool = False) -> list[dict]:
        """Items whose title has one of `terms`: [{identifier, title, japanese, mediatype}], most downloaded first."""
        query = search_query(terms, manuals_only)
        if not query:
            return []
        cache_key = "search:" + hashlib.sha1(query.encode()).hexdigest()
        cached, body = await self._json(cache_key, SEARCH_TTL, f"{ARCHIVE}/advancedsearch.php", [
            ("q", query), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "language"), ("fl[]", "mediatype"),
            ("rows", str(ROWS)), ("sort[]", "downloads desc"), ("output", "json")], fresh)
        if cached is not None:
            return cached
        docs = (body.get("response") or {}).get("docs") or [] if isinstance(body, dict) else []
        found = [{"identifier": d["identifier"], "title": first(d.get("title")) or d["identifier"],
                  "japanese": is_japanese(d.get("language")), "mediatype": first(d.get("mediatype"))}
                 for d in docs if isinstance(d, dict) and IDENTIFIER.fullmatch(str(d.get("identifier", "")))]
        self._keep(cache_key, found)
        return found

    def known(self, identifier: str) -> dict | None:
        """An item's file list if it was read before (however long ago); asks nothing."""
        row = self.db.one("SELECT body FROM manual_cache WHERE key=?", ("item:" + identifier,))
        return json.loads(row["body"]) if row else None

    async def item(self, identifier: str, fresh: bool = False) -> dict:
        """An item's manuals: {identifier, title, japanese, scans: [{name, size, files, original, scans}],
        pdfs: [{name, size, original}], images}. `scans` are the uploader's own archives of page images; the PDFs
        come the uploader's first, then the ones the Archive made."""
        if not IDENTIFIER.fullmatch(identifier):
            raise ArchiveError("That isn't an Internet Archive item name.")
        cache_key = "item:" + identifier
        cached, body = await self._json(cache_key, ITEM_TTL, f"{ARCHIVE}/metadata/{identifier}", None, fresh)
        if cached is not None and "scans" not in cached:        # kept before archives of page images were listed
            try:
                cached, body = await self._json(cache_key, ITEM_TTL, f"{ARCHIVE}/metadata/{identifier}", None, True)
            except ArchiveError:
                return {**cached, "scans": []}
        if cached is not None:
            return cached
        if not isinstance(body, dict) or not body.get("files"):
            raise ArchiveError(f"The Internet Archive has no item named {identifier}.")
        meta = body.get("metadata") or {}
        files = [f for f in body["files"] if isinstance(f, dict) and isinstance(f.get("name"), str)]

        def number(value) -> int | None:
            return int(value) if str(value or "").isdigit() else None

        pdfs = [{"name": f["name"], "size": number(f.get("size")), "original": f.get("source") == "original"}
                for f in files if f["name"].lower().endswith(".pdf")]
        pdfs.sort(key=lambda f: (not f["original"], f["name"].casefold()))
        archives = [{"name": f["name"], "size": number(f.get("size")), "files": number(f.get("filecount")),
                     "original": True, "scans": True}
                    for f in files if f.get("source") == "original" and f["name"].lower().endswith(SCAN_ARCHIVES)]
        archives.sort(key=lambda f: f["name"].casefold())
        images = sum(1 for f in files if f.get("source") == "original"
                     and f["name"].lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".jp2", ".webp")))
        found = {"identifier": identifier, "title": first(meta.get("title")) or identifier,
                 "japanese": is_japanese(meta.get("language")), "scans": archives, "pdfs": pdfs, "images": images}
        self._keep(cache_key, found)
        return found

    async def in_packs(self, slug: str, names: list[str]) -> list[dict]:
        """Manuals for a game in the platform's collections: [{identifier, title, match, name, size, original,
        scans?, files?}], the same titles first, then the uploader's page images, PDFs, and the Archive's PDFs."""
        out = []
        for identifier in PACKS.get(slug, []):
            pack = await self.item(identifier)
            for f in pack["scans"] + pack["pdfs"]:
                match = name_match(names, f["name"])
                if match:
                    out.append({"identifier": identifier, "title": pack["title"], "match": match, **f})
        return sorted(out, key=lambda f: (f["match"] != "same", not f.get("scans"), not f["original"]))

    def pages(self, identifier: str, name: str) -> int | None:
        """How many pages an archive of page images came to, while its PDF is in the cache; asks nothing."""
        if not self._path(identifier, name).is_file():
            return None
        row = self.db.one("SELECT body FROM manual_cache WHERE key=?", (f"pages:{identifier}/{name}",))
        return json.loads(row["body"]) if row else None

    def _path(self, identifier: str, name: str) -> Path:
        return self.cache_dir / (hashlib.sha1(f"{identifier}/{name}".encode()).hexdigest()[:24] + ".pdf")

    def sweep(self) -> None:
        """Drop PDFs fetched for a look more than a week ago and downloads that never finished, then the
        oldest ones while the cache holds more than CACHE_SIZE."""
        if not self.cache_dir.is_dir():
            return
        kept = []
        for path in self.cache_dir.iterdir():
            if not path.is_file():
                continue
            stat = path.stat()
            if time.time() - stat.st_mtime > CACHE_DAYS * 86400:
                path.unlink(missing_ok=True)
            else:
                kept.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in kept)
        for _, size, path in sorted(kept, key=lambda k: k[0]):
            if total <= CACHE_SIZE:
                break
            path.unlink(missing_ok=True)
            total -= size

    def forget(self, identifier: str, name: str) -> None:
        self._path(identifier, name).unlink(missing_ok=True)

    async def fetch(self, identifier: str, name: str) -> Path:
        """The manual `name` of an item as a PDF in Hakobiya's cache. Only a file the item lists is fetched, only
        from archive.org's own hosts: a PDF no larger than MAX_SIZE, or the uploader's archive of page images no
        larger than MAX_SCANS, which is made into a PDF here and not kept."""
        item = await self.item(identifier)
        archive = next((f for f in item.get("scans", []) if f["name"] == name), None)
        listed = archive or next((f for f in item["pdfs"] if f["name"] == name), None)
        if listed is None:
            raise ArchiveError("That item has no such PDF or page images.")
        what, limit = ("That archive of page images", MAX_SCANS) if archive else ("That PDF", MAX_SIZE)
        if listed["size"] and listed["size"] > limit:
            raise ArchiveError(f"{what} is larger than {limit // 2**20} MB.")
        path = self._path(identifier, name)
        if path.is_file():
            return path
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sweep()
        if not archive:
            await self._download(identifier, name, path, limit, what, b"%PDF-", 1024)
            return path
        async with self.making:                 # one at a time, so the same pages aren't fetched twice
            if path.is_file():
                return path
            raw = path.with_suffix(".download")
            try:
                await self._download(identifier, name, raw, limit, what, b"PK", 2)
                try:
                    pages = await asyncio.to_thread(page_scans.make_pdf, raw, path)
                except page_scans.ScanError as e:
                    raise ArchiveError(f"The page images couldn't be made into a PDF: {e}.") from None
            finally:
                raw.unlink(missing_ok=True)
        self._keep(f"pages:{identifier}/{name}", pages)
        return path

    async def _download(self, identifier: str, name: str, path: Path, limit: int, what: str, magic: bytes,
                        within: int) -> None:
        """Fetch an item's file to `path`, refusing it when `magic` isn't within its first `within` bytes."""
        part = path.with_suffix(".part")
        url = f"{ARCHIVE}/download/{identifier}/{quote(name)}"
        kind = "a PDF" if magic == b"%PDF-" else "a zip archive"
        async with self.turn:
            if path.is_file():                      # fetched while this request waited its turn
                return
            await self._wait()
            try:
                for _ in range(6):                  # the Archive redirects a download to the host that has the file
                    async with self.http.stream("GET", url) as r:
                        if r.status_code in (301, 302, 303, 307, 308):
                            url = urljoin(url, r.headers.get("location", ""))
                            if not allowed(url):
                                raise ArchiveError("The download led away from archive.org; not followed.")
                            continue
                        if r.status_code != 200:
                            raise ArchiveError(f"The Internet Archive didn't send the file (HTTP {r.status_code}).")
                        size, head = 0, b""
                        with part.open("wb") as out:
                            async for chunk in r.aiter_bytes(1 << 16):
                                size += len(chunk)
                                if size > limit:
                                    raise ArchiveError(f"{what} is larger than {limit // 2**20} MB.")
                                if len(head) < within:
                                    head += chunk[:within - len(head)]
                                    if len(head) == within and magic not in head:
                                        raise ArchiveError(f"That file isn't {kind}.")
                                out.write(chunk)
                        if magic not in head:
                            raise ArchiveError(f"That file isn't {kind}.")
                        part.replace(path)
                        return
                raise ArchiveError("Too many redirects from the Internet Archive.")
            except httpx.HTTPError as e:
                raise ArchiveError(f"The download from the Internet Archive failed ({type(e).__name__}).") from e
            finally:
                part.unlink(missing_ok=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
