"""Web UI: browse Minerva's sets and a set's files, tick files into a basket, send them to qBittorrent.

Every request except /healthz must come through Traefik, which adds X-Hakobiya-Key after Authentik's forward auth;
other containers on the proxy network can reach the app directly and are refused. POSTs must also be same-origin."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, names
from .db import DB
from .library import Library, platform_slug
from .minerva import Minerva, utcnow
from .qbit import QBit, QBitError, QBitView
from .art import ODD_SHAPE, REPOS, Libretro, cover_source, image_size, lookup_names
from . import make as maker
from .genuine import Keeper, ScreenScraper, status_of
from .manuals import Archive, ArchiveError, game_names, item_link, manual_state, search_terms, sha256_of, source_label
from . import packages as psn
from .processor import Processor
from .regions import MEDIA, RegionKeeper, inside
from .romm import Romm, RommError
from .sender import send
from .worker import Worker

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
PAGE = 200


@dataclass
class Settings:
    config_dir: Path
    library_root: Path
    qbit_url: str
    qbit_username: str
    qbit_password: str
    proxy_key: str = ""            # empty: no proxy check (tests, local runs)
    data_root: Path = Path("/data")
    romm_url: str = ""
    romm_token: str = ""
    worker: bool = False           # the background filing loop (on in the container)
    romm_resources: Path = Path("/romm-resources")
    screenscraper: dict | None = None   # RomM's ScreenScraper login, for the weekly genuine-scan check
    region_check: bool = False     # take non-Japanese spines, backs etc. out of RomM every six hours, unasked
    public_url: str = ""           # the address people open (through the proxy), named when a direct request is refused
    archive_pause: float = 1.0     # seconds between two requests to the Internet Archive

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(Path(e.get("HAKOBIYA_CONFIG", "/config")),
                   Path(e.get("ROMM_LIBRARY", "/data/media/romm_library/roms")),
                   e.get("QBIT_URL", "http://qbittorrentvpn:8080"), e.get("QBIT_USERNAME", ""), e.get("QBIT_PASSWORD", ""),
                   e.get("HAKOBIYA_PROXY_KEY", ""), Path(e.get("DATA_ROOT", "/data")), e.get("ROMM_URL", ""),
                   e.get("ROMM_TOKEN", ""), e.get("HAKOBIYA_WORKER", "1") == "1",
                   Path(e.get("ROMM_RESOURCES", "/romm-resources")),
                   {"user": e["SCREENSCRAPER_USER"], "password": e.get("SCREENSCRAPER_PASSWORD", ""),
                    "dev_id": e.get("SCREENSCRAPER_DEV_ID", ""), "dev_password": e.get("SCREENSCRAPER_DEV_PASSWORD", "")}
                   if e.get("SCREENSCRAPER_USER") else None,
                   e.get("HAKOBIYA_REGION_CHECK", "0") == "1", e.get("HAKOBIYA_PUBLIC_URL", ""))


def human(n: int | None) -> str:
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def set_folder(source: str, platform: str) -> str:
    """The folder a set's files sit in inside the torrent (after the Minerva_Myrient root)."""
    return f"{source}/{platform}"


def create_app(settings: Settings | None = None, minerva: Minerva | None = None, qview: QBitView | None = None,
               db: DB | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    db = db or DB(s.config_dir / "hakobiya.sqlite")
    minerva = minerva or Minerva(db, s.config_dir / "torrents")
    qview = qview or QBitView(lambda: QBit(s.qbit_url, s.qbit_username, s.qbit_password))
    library = Library(s.library_root)
    worker = Worker(db, Processor(s.data_root, s.library_root), lambda: QBit(s.qbit_url, s.qbit_username, s.qbit_password),
                    s.romm_url, s.romm_token)

    romm = Romm(s.romm_url, s.romm_token) if s.romm_url and s.romm_token else None
    libretro = Libretro(db, s.config_dir / "art-cache")
    backups = s.config_dir / "art-backup"
    keeper = Keeper(db, romm, ScreenScraper(s.screenscraper) if s.screenscraper else None, s.romm_resources) \
        if romm else None
    regions = RegionKeeper(db, romm, s.romm_resources, s.config_dir / "media-backup") if romm else None
    packages = psn.Packages(db, s.config_dir / "psn-keys.txt", s.config_dir / "package-manuals", s.data_root,
                            lambda: QBit(s.qbit_url, s.qbit_username, s.qbit_password))

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(worker.run()), asyncio.create_task(packages.run())] if s.worker else []
        if s.worker and keeper:
            tasks.append(asyncio.create_task(keeper.run()))
            if s.region_check:
                tasks.append(asyncio.create_task(regions.run()))
        yield
        for task in tasks:
            task.cancel()
        await minerva.aclose()

    app = FastAPI(title="Hakobiya", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["human"] = human
    templates.env.filters["manual_source"] = source_label
    templates.env.globals["version"] = __version__

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path != "/healthz":
            if s.proxy_key and not hmac.compare_digest(request.headers.get("x-hakobiya-key", ""), s.proxy_key):
                where = s.public_url or "Hakobiya's own address"
                return PlainTextResponse(f"Use {where} (through the sign-in).", status_code=403)
            if request.method == "POST":
                origin = request.headers.get("origin") or request.headers.get("referer") or ""
                host = request.headers.get("host", "")
                if not origin or urlparse(origin).netloc != host:
                    return PlainTextResponse("Cross-site request refused.", status_code=403)
        return await call_next(request)

    def render(request: Request, name: str, **ctx):
        user = request.headers.get("x-authentik-username")
        return templates.TemplateResponse(request, name, {"user": user, **ctx})

    async def qbit_sets() -> tuple[dict[str, dict], str | None]:
        """Minerva torrents already in qBittorrent, by the set folder their files sit in."""
        try:
            out = {}
            for t in await qview.torrents():
                if t["name"] != "Minerva_Myrient":
                    continue
                files = await qview.files(t["hash"])
                if files:
                    parts = files[0]["name"].split("/")
                    if len(parts) > 3:
                        out[f"{parts[1]}/{parts[2]}"] = t
            return out, None
        except Exception as e:                  # qBittorrent down: browsing still works
            log.warning("qBittorrent lookup failed: %s", e)
            return {}, str(e)

    @app.get("/healthz")
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    async def sets_page(request: Request):
        error = None
        try:
            await minerva.refresh_index()
        except Exception as e:
            log.warning("index refresh failed: %s", e)
            error = f"Couldn't refresh Minerva's index ({e}); showing the cached copy."
        have, qerr = await qbit_sets()
        rows = []
        for r in db.all("SELECT * FROM sets ORDER BY source, platform"):
            t = have.get(set_folder(r["source"], r["platform"]))
            rows.append({**dict(r), "slug": platform_slug(r["source"], r["platform"]), "have": t})
        sources = sorted({r["source"] for r in rows}, key=lambda x: (x not in ("No-Intro", "Redump"), x))
        age = minerva.index_age()
        return render(request, "sets.html", rows=rows, sources=sources, error=error, qerr=qerr,
                      have_count=sum(1 for r in rows if r["have"]),
                      age_hours=None if age is None else round(age.total_seconds() / 3600, 1))

    @app.post("/refresh")
    async def refresh():
        await minerva.refresh_index(force=True)
        return RedirectResponse("/", status_code=303)

    async def listing(name: str, q: str, regions: list[str], shown_tags: set[str], missing: bool, state: str):
        """The set's files that pass the filters, with RomM and qBittorrent state."""
        row = db.one("SELECT * FROM sets WHERE name=?", (name,))
        if row is None:
            raise HTTPException(404, "no such set in the index")
        t = await minerva.open_set(name)
        row = db.one("SELECT * FROM sets WHERE name=?", (name,))
        slug = platform_slug(row["source"], row["platform"])
        # qBittorrent: the same torrent (by info hash), or another torrent of the same set folder
        have, qerr = await qbit_sets()
        qt = next((x for x in have.values() if x["hash"] == t.infohash), None)
        other = None if qt else have.get(set_folder(row["source"], row["platform"]))
        qfiles = {f["name"]: f for f in await qview.files(qt["hash"])} if qt else {}
        picked = {r["idx"] for r in db.all("SELECT idx FROM picks WHERE set_name=?", (name,))}
        sent = {r["idx"] for r in db.all("SELECT idx FROM jobs WHERE set_name=?", (name,))}

        files = []
        needle = names.strip_ext(q).casefold().strip()
        for f in db.all("SELECT * FROM files WHERE set_name=? ORDER BY game, disc, filename", (name,)):
            r = json.loads(f["regions"])
            tags = set(json.loads(f["tags"]))
            if regions and "all" not in regions and not (set(r) & set(regions)):
                continue
            if tags & (names.HIDDEN_BY_DEFAULT - shown_tags):
                continue
            if needle and needle not in f["filename"].casefold():
                continue
            inlib = library.has(slug, names.strip_ext(f["filename"]), f["game"])
            if missing and inlib:
                continue
            qf = qfiles.get(f["path"])
            st = ("done" if qf["progress"] >= 1 else "downloading") if qf and qf["priority"] > 0 else \
                 ("skipped" if qf else "")
            if state and st != state:
                continue
            files.append({**dict(f), "regions": r, "tags": sorted(tags), "in_library": inlib, "state": st,
                          "progress": qf["progress"] if qf else None, "picked": f["idx"] in picked,
                          "sent": f["idx"] in sent})
        return row, t, slug, qt, other, qerr, files

    def basket(name: str) -> dict:
        r = db.one("SELECT count(*) n, coalesce(sum(f.size), 0) size FROM picks p JOIN files f"
                   " ON f.set_name=p.set_name AND f.idx=p.idx WHERE p.set_name=?", (name,))
        return {"count": r["n"], "size": r["size"], "size_text": human(r["size"])}

    def filters_from(params) -> tuple[str, list[str], set[str], bool, str]:
        regions = params.getlist("region") or ["Japan"]
        return (params.get("q", ""), regions, set(params.getlist("show")),
                params.get("missing") in ("true", "on", "1"), params.get("state", ""))

    @app.get("/set/{name:path}", response_class=HTMLResponse)
    async def set_page(request: Request, name: str, q: str = "", region: list[str] | None = Query(None),
                       show: list[str] | None = Query(None), missing: bool = False, state: str = "", page: int = 1,
                       msg: str = "", err: str = ""):
        regions = region if region is not None else ["Japan"]
        shown_tags = set(show or [])
        try:
            row, t, slug, qt, other, qerr, files = await listing(name, q, regions, shown_tags, missing, state)
        except HTTPException:
            raise
        except Exception as e:
            log.warning("opening %s failed: %s", name, e)
            return render(request, "error.html", message=f"Couldn't get this set's torrent from Minerva: {e}")
        pages = max(1, math.ceil(len(files) / PAGE))
        page = min(max(page, 1), pages)
        return render(request, "set.html", set=row, slug=slug, files=files[(page - 1) * PAGE: page * PAGE],
                      total=len(files), total_size=sum(f["size"] for f in files), page=page, pages=pages,
                      q=q, regions=regions, shown=shown_tags, missing=missing, state=state, qt=qt, other=other,
                      qerr=qerr, hideable=names.HIDEABLE, hidden_default=names.HIDDEN_BY_DEFAULT,
                      region_choices=["Japan", "World", "USA", "Europe", "Asia", "Korea", "all"],
                      basket=basket(name), msg=msg, err=err, query=request.url.query)

    # ------------------------------------------------------------- basket
    @app.post("/pick")
    async def pick(request: Request):
        """Tick or untick files: {"set": name, "idx": [..], "on": bool}. Answers the basket total."""
        body = await request.json()
        name, on = str(body.get("set", "")), bool(body.get("on"))
        ids = [int(i) for i in body.get("idx", [])]
        known = {r["idx"] for r in db.all("SELECT idx FROM files WHERE set_name=?", (name,))}
        if not ids or not set(ids) <= known:
            raise HTTPException(400, "unknown files for this set")
        for i in ids:
            db.execute("INSERT OR IGNORE INTO picks(set_name, idx) VALUES (?,?)" if on else
                       "DELETE FROM picks WHERE set_name=? AND idx=?", (name, i))
        return basket(name)

    @app.post("/pick/all")
    async def pick_all(request: Request):
        """Tick every file the set page's current filters show (all pages), or untick the whole basket."""
        form = await request.form()
        name = str(form.get("set", ""))
        back = form.get("back", "")
        if form.get("clear"):
            db.execute("DELETE FROM picks WHERE set_name=?", (name,))
        else:
            q, regions, shown, missing, state = filters_from(form)
            *_, files = await listing(name, q, regions, shown, missing, state)
            for f in files:
                if not f["sent"]:
                    db.execute("INSERT OR IGNORE INTO picks(set_name, idx) VALUES (?,?)", (name, f["idx"]))
        return RedirectResponse(f"/set/{quote(name)}?{back}", status_code=303)

    @app.post("/send")
    async def send_basket(request: Request):
        form = await request.form()
        name = str(form.get("set", ""))
        back = str(form.get("back", ""))
        picks = [r["idx"] for r in db.all("SELECT idx FROM picks WHERE set_name=? ORDER BY idx", (name,))]
        if not picks:
            return RedirectResponse(f"/set/{quote(name)}?{back}", status_code=303)
        row = db.one("SELECT * FROM sets WHERE name=?", (name,))
        t = await minerva.open_set(name)
        data = Path(row["torrent_path"]).read_bytes()
        try:
            async with QBit(s.qbit_url, s.qbit_username, s.qbit_password) as q:
                result = await send(q, t, data, f"Minerva_Myrient - {name}.torrent", picks)
        except (QBitError, OSError) as e:
            log.warning("send %s failed: %s", name, e)
            return RedirectResponse(f"/set/{quote(name)}?{back}&err={quote(str(e))}", status_code=303)
        qview.forget()
        now = utcnow().isoformat()
        user = request.headers.get("x-authentik-username")
        by_idx = {f.index: f for f in t.files}
        for i in picks:
            f = by_idx[i]
            db.execute("INSERT INTO jobs(set_name, idx, path, size, infohash, state, sent_by, created_at, updated_at)"
                       " VALUES (?,?,?,?,?, 'sent', ?,?,?) ON CONFLICT(set_name, idx) DO UPDATE SET"
                       " state='sent', updated_at=excluded.updated_at",
                       (name, i, f.path, f.size, t.infohash, user, now, now))
        db.execute("DELETE FROM picks WHERE set_name=?", (name,))
        worker.wake.set()
        what = "added the set to qBittorrent with only your files" if result.mode == "added" else \
            f"switched on {result.turned_on} file(s) in the set you already have" + \
            (f" ({result.already} were already on)" if result.already else "")
        log.info("sent %d file(s) from %s: %s", len(picks), name, result)
        return RedirectResponse(f"/set/{quote(name)}?{back}&msg={quote('Sent: ' + what + '.')}", status_code=303)

    # ------------------------------------------------------------- queue
    @app.get("/queue", response_class=HTMLResponse)
    async def queue(request: Request):
        jobs = [dict(r) for r in db.all("SELECT * FROM jobs ORDER BY created_at DESC, path LIMIT 500")]
        progress: dict[tuple[str, str], float] = {}
        qerr = None
        live = [j for j in jobs if j["state"] in ("sent", "downloading")]
        if live:
            try:
                async with QBit(s.qbit_url, s.qbit_username, s.qbit_password) as q:
                    for h in {j["infohash"] for j in live}:
                        for f in await q.get("torrents/files", hash=h):
                            progress[(h, f["name"])] = f["progress"]
            except Exception as e:
                qerr = str(e)
        for j in jobs:
            j["filename"] = j["path"].rsplit("/", 1)[-1]
            j["progress"] = progress.get((j["infohash"], j["path"]))
            j["where"] = str(Path(j["target"]).relative_to(s.library_root)) if j["target"] and \
                Path(j["target"]).is_relative_to(s.library_root) else j["target"]
        return render(request, "queue.html", jobs=jobs, qerr=qerr, worker_error=worker.last_error)

    @app.post("/job/{job_id}/retry")
    async def retry(job_id: int):
        db.execute("UPDATE jobs SET state='sent', note='retrying' WHERE id=? AND state IN"
                   " ('failed', 'needs_rule', 'waiting_space')", (job_id,))
        worker.wake.set()
        return RedirectResponse("/queue", status_code=303)

    # ------------------------------------------------------------- art
    def need_romm():
        if romm is None:
            raise HTTPException(503, "RomM isn't configured (ROMM_URL / ROMM_TOKEN)")
        return romm

    async def sizes(roms: list[dict]) -> dict[int, tuple[int, int]]:
        """Cover sizes (from the small covers' headers), cached per cover version."""
        out, todo = {}, []
        for r in roms:
            path = r.get("path_cover_small")
            if not path:
                continue
            hit = db.one("SELECT width, height FROM art_sizes WHERE path=?", (path,))
            if hit:
                out[r["id"]] = (hit["width"], hit["height"])
            else:
                todo.append(r)
        sem = asyncio.Semaphore(6)

        async def one(r):
            async with sem:
                data = await need_romm().asset(r["path_cover_small"])
            size = image_size(data or b"")
            if size:
                db.execute("INSERT OR REPLACE INTO art_sizes(path, width, height) VALUES (?,?,?)",
                           (r["path_cover_small"], *size))
                out[r["id"]] = size
        await asyncio.gather(*(one(r) for r in todo))
        return out

    def flag(roms: list[dict], dims: dict[int, tuple[int, int]]) -> list[dict]:
        ratios = sorted(h / w for w, h in dims.values() if w)
        usual = ratios[len(ratios) // 2] if ratios else None
        cards = []
        for r in roms:
            label, jp = cover_source(r)
            d = dims.get(r["id"])
            ratio = d[1] / d[0] if d and d[0] else None
            odd = bool(usual and ratio and abs(ratio - usual) / usual > ODD_SHAPE)
            flags = (["no cover"] if label == "no cover" else []) + \
                    (["not Japanese"] if label not in ("no cover",) and not jp else []) + (["odd shape"] if odd else [])
            cards.append({"id": r["id"], "name": r["name"] or r["fs_name"], "fs_name": r["fs_name"], "source": label,
                          "size": d, "flags": flags, "has_cover": bool(r.get("path_cover_large"))})
        return cards

    @app.get("/art", response_class=HTMLResponse)
    async def art_index(request: Request):
        try:
            platforms = await need_romm().platforms()
            rows = []
            for p in sorted(platforms, key=lambda p: p.get("display_name") or p["name"]):
                roms = await romm.roms(p["id"])
                labels = [cover_source(r) for r in roms]
                rows.append({**p, "games": len(roms), "missing": sum(1 for l, _ in labels if l == "no cover"),
                             "not_jp": sum(1 for l, jp in labels if l != "no cover" and not jp)})
        except RommError as e:
            return render(request, "error.html", message=f"RomM isn't answering: {e}")
        changes = [dict(r) for r in db.all("SELECT * FROM art_changes ORDER BY id DESC LIMIT 20")]
        return render(request, "art.html", platforms=rows, changes=changes)

    # ------------------------------------------------------------- images from other regions
    def image_response(path: Path | None) -> Response:
        if path is None or not path.is_file():
            raise HTTPException(404, "no such image")
        data = path.read_bytes()
        return Response(data, media_type="image/png" if data[:4] == b"\x89PNG" else "image/jpeg",
                        headers={"Cache-Control": "private, max-age=300"})

    @app.get("/art/regions", response_class=HTMLResponse)
    async def art_regions(request: Request, msg: str = "", err: str = ""):
        try:
            need_romm()
            items = await regions.found()
        except RommError as e:
            return render(request, "error.html", message=f"RomM isn't answering: {e}")
        items.sort(key=lambda i: (i["platform"], (i["name"] or "").casefold(), i["kind"]))
        changes = [dict(r) | {"label": MEDIA.get(r["kind"], r["kind"])}
                   for r in db.all("SELECT * FROM media_changes ORDER BY id DESC LIMIT 200")]
        return render(request, "art_regions.html", items=items, changes=changes, msg=msg, err=err,
                      unasked=s.region_check)

    @app.get("/art/regions/image/{rom_id}/{kind}")
    async def art_regions_image(rom_id: int, kind: str):
        if kind not in MEDIA:
            raise HTTPException(404, "no such image")
        ss = (await need_romm().rom(rom_id)).get("ss_metadata") or {}
        return image_response(inside(s.romm_resources, ss.get(f"{kind}_path") or "-"))

    @app.get("/art/regions/removed/{change_id}")
    async def art_regions_removed(change_id: int):
        c = db.one("SELECT backup_path FROM media_changes WHERE id=?", (change_id,))
        return image_response(Path(c["backup_path"]) if c and c["backup_path"] else None)

    @app.post("/art/regions/remove")
    async def art_regions_remove(request: Request):
        picked: dict[int, set[str]] = {}
        for value in (await request.form()).getlist("item"):
            rom_id, _, kind = value.partition(":")
            if rom_id.isdigit() and kind in MEDIA:
                picked.setdefault(int(rom_id), set()).add(kind)
        need_romm()
        done, failed = 0, []
        for rom_id, kinds in picked.items():
            try:
                done += await regions.remove(rom_id, kinds, request.headers.get("x-authentik-username") or "you")
            except (RommError, OSError) as e:
                failed.append(f"#{rom_id}: {e}")
        q = f"msg={quote(f'Removed {done} image(s); they are kept below for undo.')}" + \
            (f"&err={quote('; '.join(failed)[:500])}" if failed else "")
        return RedirectResponse(f"/art/regions?{q}", status_code=303)

    @app.post("/art/regions/check")
    async def art_regions_check():
        need_romm()
        try:
            n = await regions.sweep()
        except (RommError, OSError) as e:
            return RedirectResponse(f"/art/regions?err={quote(str(e)[:300])}", status_code=303)
        return RedirectResponse(f"/art/regions?msg={quote(f'Checked: {n} image(s) removed.')}", status_code=303)

    @app.post("/art/regions/undo/{change_id}")
    async def art_regions_undo(change_id: int):
        c = db.one("SELECT * FROM media_changes WHERE id=? AND undone_at IS NULL", (change_id,))
        if c is None:
            raise HTTPException(404, "nothing to undo")
        need_romm()
        try:
            await regions.undo(c)
        except (RommError, OSError) as e:
            return RedirectResponse(f"/art/regions?err={quote(str(e)[:300])}", status_code=303)
        return RedirectResponse(f"/art/regions?msg={quote('Image put back; the automatic check leaves it alone now.')}",
                                status_code=303)

    @app.get("/art/p/{platform_id}", response_class=HTMLResponse)
    async def art_platform(request: Request, platform_id: int, show: str = "all", alt: bool = False,
                           msg: str = "", err: str = ""):
        try:
            platform = next(p for p in await need_romm().platforms() if p["id"] == platform_id)
            roms = await romm.roms(platform_id)
        except StopIteration:
            raise HTTPException(404, "no such platform with games in RomM")
        except RommError as e:
            return render(request, "error.html", message=f"RomM isn't answering: {e}")
        cards = flag(roms, await sizes(roms))
        if show == "flagged":
            cards = [c for c in cards if c["flags"]]
        cards.sort(key=lambda c: (not c["flags"], c["name"].casefold()))
        changes = [dict(r) for r in db.all("SELECT * FROM art_changes WHERE platform_id=? ORDER BY id DESC LIMIT 50",
                                           (platform_id,))]
        return render(request, "art_platform.html", platform=platform, cards=cards, show=show, alt=alt, msg=msg,
                      err=err, changes=changes, has_libretro=platform["fs_slug"] in REPOS)

    @app.get("/art/cover/{rom_id}")
    async def art_cover(rom_id: int, size: str = "small"):
        r = await need_romm().rom(rom_id)
        data = await romm.asset(r.get("path_cover_large" if size == "big" else "path_cover_small") or "")
        if not data:
            raise HTTPException(404, "no cover")
        return Response(data, media_type="image/png" if data[:4] == b"\x89PNG" else "image/jpeg",
                        headers={"Cache-Control": "private, max-age=300"})

    async def candidate(rom_id: int) -> tuple[dict, tuple[str, bytes] | None]:
        r = await need_romm().rom(rom_id)
        return r, await libretro.find(r["platform_fs_slug"], lookup_names(r))

    @app.get("/art/alt/{rom_id}")
    async def art_alt(rom_id: int):
        _, hit = await candidate(rom_id)
        if not hit:
            raise HTTPException(404, "libretro has no cover under this game's name")
        return Response(hit[1], media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/art/apply")
    async def art_apply(request: Request):
        form = await request.form()
        platform_id = int(form.get("platform", 0))
        ids = [int(i) for i in form.getlist("rom")]
        done, failed = 0, []
        backups.mkdir(parents=True, exist_ok=True)
        for rom_id in ids:
            try:
                r, hit = await candidate(rom_id)
                if not hit:
                    failed.append(f"#{rom_id}: no libretro cover")
                    continue
                old = await romm.asset(r.get("path_cover_large") or "")
                backup = None
                if old:
                    backup = backups / f"{rom_id}-{utcnow().strftime('%Y%m%dT%H%M%S')}.img"
                    backup.write_bytes(old)
                await romm.upload_cover(rom_id, hit[1])
                db.execute("INSERT INTO art_changes(rom_id, platform_id, name, source, backup_path, applied_at, applied_by)"
                           " VALUES (?,?,?,?,?,?,?)", (rom_id, platform_id, r["name"] or r["fs_name"],
                                                       f"libretro: {hit[0]}", str(backup) if backup else None,
                                                       utcnow().isoformat(), request.headers.get("x-authentik-username")))
                done += 1
            except (RommError, OSError) as e:
                failed.append(f"#{rom_id}: {e}")
        q = f"msg={quote(f'Replaced {done} cover(s).')}" + (f"&err={quote('; '.join(failed)[:500])}" if failed else "")
        return RedirectResponse(f"/art/p/{platform_id}?alt=1&{q}", status_code=303)

    @app.post("/art/undo/{change_id}")
    async def art_undo(change_id: int):
        c = db.one("SELECT * FROM art_changes WHERE id=? AND undone_at IS NULL", (change_id,))
        if c is None:
            raise HTTPException(404, "nothing to undo")
        if not c["backup_path"] or not Path(c["backup_path"]).exists():
            return RedirectResponse(f"/art/p/{c['platform_id']}?err={quote('No backup of the old cover (it had none).')}",
                                    status_code=303)
        await need_romm().upload_cover(c["rom_id"], Path(c["backup_path"]).read_bytes())
        db.execute("UPDATE art_changes SET undone_at=? WHERE id=?", (utcnow().isoformat(), change_id))
        return RedirectResponse(f"/art/p/{c['platform_id']}?msg={quote('Previous cover put back.')}", status_code=303)

    # ------------------------------------------------------------- make (generated disc / cart art)
    templates_dir = s.config_dir / "make-templates"
    make_backups = s.config_dir / "make-backup"

    def generated_copy(rom_id: int) -> Path | None:
        """Hakobiya's copy of the generated image it still manages for this game, if any."""
        row = db.one("SELECT generated_path FROM make_changes WHERE rom_id=? AND undone_at IS NULL AND"
                     " superseded_at IS NULL ORDER BY id DESC LIMIT 1", (rom_id,))
        return Path(row["generated_path"]) if row and row["generated_path"] else None

    def make_slugs() -> set[str]:
        return {slug for slug in maker.TEMPLATES if (templates_dir / f"{slug}.png").is_file()}

    def rom_file(r: dict) -> Path:
        return s.library_root.parent / r.get("fs_path", "") / r.get("fs_name", "")

    def first_disc(r: dict) -> Path:
        path = rom_file(r)
        if path.is_dir():
            discs = sorted(p for p in path.iterdir() if p.suffix.lower() in (".chd", ".3ds", ".zip", ".nds"))
            return discs[0] if discs else path
        return path

    async def generated(rom_id: int, focus: float, zoom: float) -> tuple[dict, bytes]:
        r = await need_romm().rom(rom_id)
        slug = r["platform_fs_slug"]
        if slug not in make_slugs():
            raise HTTPException(404, "no template for this platform")
        cover = await romm.asset(r.get("path_cover_large") or "")
        if not cover:
            raise HTTPException(404, "this game has no box art to print")
        from io import BytesIO
        from PIL import Image
        code = await asyncio.to_thread(maker.product_code, slug, first_disc(r))
        im = await asyncio.to_thread(maker.make, slug, Image.open(templates_dir / f"{slug}.png"),
                                     Image.open(BytesIO(cover)), code, focus, zoom)
        return r, await asyncio.to_thread(maker.png_bytes, im)

    @app.get("/make", response_class=HTMLResponse)
    async def make_index(request: Request, msg: str = ""):
        try:
            platforms = [p for p in await need_romm().platforms() if p["fs_slug"] in make_slugs()]
            rows = []
            for p in sorted(platforms, key=lambda p: p.get("display_name") or p["name"]):
                states = [maker.physical_status(await romm.rom(r["id"]), s.romm_resources, generated_copy(r["id"]))[0]
                          for r in await romm.roms(p["id"])]
                rows.append({**p, "games": len(states), "missing": states.count("missing"),
                             "foreign": states.count("foreign"), "generated": states.count("generated")})
        except RommError as e:
            return render(request, "error.html", message=f"RomM isn't answering: {e}")
        return render(request, "make.html", platforms=rows, templates=sorted(make_slugs()), msg=msg)

    @app.get("/make/p/{platform_id}", response_class=HTMLResponse)
    async def make_platform(request: Request, platform_id: int, show: str = "needed", msg: str = "", err: str = ""):
        try:
            platform = next(p for p in await need_romm().platforms() if p["id"] == platform_id)
            cards = []
            for brief in await romm.roms(platform_id):
                r = await romm.rom(brief["id"])
                state, label = maker.physical_status(r, s.romm_resources, generated_copy(r["id"]))
                if show == "needed" and state not in ("missing", "foreign"):
                    continue
                cards.append({"id": r["id"], "name": r["name"] or r["fs_name"], "fs_name": r["fs_name"], "state": state,
                              "label": label, "has_physical": state != "missing", "has_cover": bool(r.get("path_cover_large"))})
        except StopIteration:
            raise HTTPException(404, "no such platform with games in RomM")
        except RommError as e:
            return render(request, "error.html", message=f"RomM isn't answering: {e}")
        cards.sort(key=lambda c: (c["state"] not in ("missing", "foreign"), c["name"].casefold()))
        changes = []
        for row in db.all("SELECT * FROM make_changes WHERE platform_id=? ORDER BY id DESC LIMIT 50", (platform_id,)):
            c = dict(row)
            if not c["undone_at"] and not c["superseded_at"]:
                c["live"] = status_of(row, await romm.rom(c["rom_id"]), s.romm_resources)
                if c["live"] == "genuine":       # RomM brought a Japanese scan: it takes over
                    db.execute("UPDATE make_changes SET superseded_at=?, superseded_by='RomM refresh' WHERE id=?",
                               (utcnow().isoformat(), c["id"]))
                    c["superseded_at"], c["superseded_by"] = "now", "RomM refresh"
            changes.append(c)
        return render(request, "make_platform.html", platform=platform, cards=cards, show=show, msg=msg, err=err,
                      changes=changes, ready=platform["fs_slug"] in make_slugs())

    @app.get("/make/physical/{rom_id}")
    async def make_physical(rom_id: int):
        r = await need_romm().rom(rom_id)
        path = (r.get("ss_metadata") or {}).get("physical_path")
        f = (s.romm_resources / path).resolve() if path else None
        if not f or not f.is_file() or not f.is_relative_to(s.romm_resources.resolve()):
            raise HTTPException(404, "no disc/cart art")
        return Response(f.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/make/preview/{rom_id}")
    async def make_preview(rom_id: int, focus: float = 0.45, zoom: float = 1.0):
        _, data = await generated(rom_id, focus, zoom)
        return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})

    @app.post("/make/save")
    async def make_save(request: Request):
        form = await request.form()
        platform_id = int(form.get("platform", 0))
        saved, failed = 0, []
        make_backups.mkdir(parents=True, exist_ok=True)
        for raw in form.getlist("rom"):
            rom_id = int(raw)
            focus = float(form.get(f"focus_{rom_id}", 0.45))
            zoom = float(form.get(f"zoom_{rom_id}", 1.0))
            try:
                r, data = await generated(rom_id, focus, zoom)
                ss = dict(r.get("ss_metadata") or {})
                if not r.get("ss_id"):
                    failed.append(f"{r['name']}: not matched to ScreenScraper, so RomM has nowhere to record it")
                    continue
                rel = f"roms/{r['platform_id']}/{r['id']}/physical/physical.png"
                target = s.romm_resources / rel
                backup = None
                old = s.romm_resources / ss["physical_path"] if ss.get("physical_path") else None
                if old and old.is_file():
                    backup = make_backups / f"{rom_id}-{utcnow().strftime('%Y%m%dT%H%M%S')}.png"
                    backup.write_bytes(old.read_bytes())
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(target)
                kept = s.config_dir / "make-generated" / f"{rom_id}.png"        # Hakobiya's own copy, for Restore
                kept.parent.mkdir(parents=True, exist_ok=True)
                kept.write_bytes(data)
                new_ss = {**ss, "physical_path": rel, "physical_url": None, "physical_disc": "1",
                          "physical_extra_discs": [], "hakobiya_generated": True}
                await romm.set_ss_metadata(rom_id, new_ss)
                # A newer save for the same game takes over managing it.
                db.execute("UPDATE make_changes SET superseded_at=?, superseded_by='saved again' WHERE rom_id=?"
                           " AND undone_at IS NULL AND superseded_at IS NULL", (utcnow().isoformat(), rom_id))
                db.execute("INSERT INTO make_changes(rom_id, platform_id, name, settings, physical_path, backup_path,"
                           " old_ss_metadata, applied_at, applied_by, generated_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (rom_id, platform_id, r["name"] or r["fs_name"], json.dumps({"focus": focus, "zoom": zoom}),
                            rel, str(backup) if backup else None, json.dumps(ss, ensure_ascii=False),
                            utcnow().isoformat(), request.headers.get("x-authentik-username"), str(kept)))
                saved += 1
            except (RommError, OSError, HTTPException) as e:
                failed.append(f"#{rom_id}: {getattr(e, 'detail', e)}")
        q = f"msg={quote(f'Saved {saved} into RomM.')}" + (f"&err={quote('; '.join(failed)[:500])}" if failed else "")
        return RedirectResponse(f"/make/p/{platform_id}?show=all&{q}", status_code=303)

    @app.post("/make/restore/{change_id}")
    async def make_restore(change_id: int):
        c = db.one("SELECT * FROM make_changes WHERE id=? AND undone_at IS NULL AND superseded_at IS NULL",
                   (change_id,))
        if c is None or not c["generated_path"] or not Path(c["generated_path"]).is_file():
            raise HTTPException(404, "nothing to restore")
        await keeper.restore(c)
        return RedirectResponse(f"/make/p/{c['platform_id']}?show=all&msg={quote('Generated art put back.')}",
                                status_code=303)

    @app.post("/make/check")
    async def make_check(request: Request):
        """Ask ScreenScraper now (instead of at the weekly check) whether genuine Japanese scans exist."""
        form = await request.form()
        results = []
        for c in keeper.active() if keeper else []:
            results.append(await keeper.check(c))
        n = results.count("genuine")
        msg = f"Checked {len(results)}: {n} replaced by a genuine Japanese scan." if results else "Nothing to check."
        back = form.get("back") or "/make"
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg={quote(msg)}", status_code=303)

    @app.post("/make/undo/{change_id}")
    async def make_undo(change_id: int):
        c = db.one("SELECT * FROM make_changes WHERE id=? AND undone_at IS NULL", (change_id,))
        if c is None:
            raise HTTPException(404, "nothing to undo")
        old_ss = json.loads(c["old_ss_metadata"])
        target = s.romm_resources / c["physical_path"]
        if c["backup_path"] and Path(c["backup_path"]).is_file():
            target.write_bytes(Path(c["backup_path"]).read_bytes())
        elif target.is_file() and not old_ss.get("physical_path"):
            target.unlink()
        await need_romm().set_ss_metadata(c["rom_id"], old_ss)
        db.execute("UPDATE make_changes SET undone_at=? WHERE id=?", (utcnow().isoformat(), change_id))
        return RedirectResponse(f"/make/p/{c['platform_id']}?show=all&msg={quote('Previous disc/cart art put back.')}",
                                status_code=303)

    # ------------------------------------------------------------- manuals (Japanese ones from the Internet Archive
    # and from inside PS one Classics packages)
    archive = Archive(db, s.config_dir / "manual-cache", pause=s.archive_pause)
    manual_backups = s.config_dir / "manual-backup"

    def manual_file(r: dict) -> Path | None:
        """The game's manual as RomM saved it, read from RomM's resources folder."""
        path = inside(s.romm_resources, r["path_manual"]) if r.get("path_manual") else None
        return path if path and path.is_file() else None

    def manual_changes() -> dict[int, dict]:
        """Hakobiya's latest standing manual change per game."""
        rows = db.all("SELECT * FROM manual_changes WHERE undone_at IS NULL ORDER BY id")
        return {c["rom_id"]: dict(c) for c in rows}

    def manual_now(r: dict, changes: dict[int, dict]) -> tuple[str, bool]:
        """(label, Japanese) for a game's manual; Hakobiya's own only while RomM's file still has its size."""
        change, path = changes.get(r["id"]), manual_file(r)
        if change and (path is None or path.stat().st_size != change["size"]):
            change = None
        return manual_state(r, change)

    def document(path: Path | None) -> Response:
        if path is None or not path.is_file():
            raise HTTPException(404, "no such manual")
        pdf = path.suffix.lower() == ".pdf"
        return FileResponse(path, media_type="application/pdf" if pdf else "text/plain; charset=utf-8",
                            content_disposition_type="inline", filename="manual" + path.suffix.lower(),
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @app.get("/manuals", response_class=HTMLResponse)
    async def manuals_index(request: Request, msg: str = "", err: str = ""):
        try:
            platforms = await need_romm().platforms()
            changes, rows = manual_changes(), []
            for p in sorted(platforms, key=lambda p: p.get("display_name") or p["name"]):
                states = [manual_now(r, changes) for r in await romm.roms(p["id"])]
                rows.append({**p, "games": len(states), "missing": sum(1 for l, _ in states if l == "no manual"),
                             "not_jp": sum(1 for l, jp in states if l != "no manual" and not jp),
                             "japanese": sum(1 for _, jp in states if jp)})
        except RommError as e:
            return render(request, "error.html", message=f"RomM isn't answering: {e}")
        recent = [dict(r) for r in db.all("SELECT * FROM manual_changes ORDER BY id DESC LIMIT 20")]
        return render(request, "manuals.html", platforms=rows, changes=recent, msg=msg, err=err)

    @app.get("/manuals/p/{platform_id}", response_class=HTMLResponse)
    async def manuals_platform(request: Request, platform_id: int, show: str = "todo"):
        try:
            platform = next((p for p in await need_romm().platforms() if p["id"] == platform_id), None)
            if platform is None:
                raise HTTPException(404, "no such platform")
            roms = await romm.roms(platform_id)
        except RommError as e:
            return render(request, "error.html", message=f"RomM isn't answering: {e}")
        changes, games = manual_changes(), []
        for r in sorted(roms, key=lambda r: (r.get("name") or r["fs_name"]).casefold()):
            label, japanese = manual_now(r, changes)
            if show == "all" or not japanese:
                games.append({"id": r["id"], "name": r.get("name") or r["fs_name"], "fs_name": r["fs_name"],
                              "state": label, "japanese": japanese})
        return render(request, "manuals_platform.html", platform=platform, games=games, show=show, total=len(roms))

    @app.get("/manuals/g/{rom_id}", response_class=HTMLResponse)
    async def manuals_game(request: Request, rom_id: int, q: str = "", wide: bool = False, fresh: bool = False,
                           item: str = "", msg: str = "", err: str = ""):
        try:
            r = await need_romm().rom(rom_id)
        except RommError as e:
            return render(request, "error.html", message=f"RomM isn't answering: {e}")
        terms = [t.strip() for t in q.split(";") if t.strip()] or search_terms(r)
        link, packs, found = item_link(q), [], []
        try:
            if link:
                found = [await archive.item(link)]
            else:
                packs = await archive.in_packs(r.get("platform_fs_slug") or "", game_names(r))
                found = await archive.search(terms, not wide, fresh)
                opened = await archive.item(item) if item else None
                found = [opened if opened and opened["identifier"] == f["identifier"] else
                         {**f, **(archive.known(f["identifier"]) or {})} for f in found]
                if opened and all(f["identifier"] != opened["identifier"] for f in found):
                    found.insert(0, opened)
                found.sort(key=lambda f: not f["japanese"])
        except ArchiveError as e:
            err = err or str(e)
        label, japanese = manual_now(r, manual_changes())
        changes = [dict(c) for c in db.all("SELECT * FROM manual_changes WHERE rom_id=? ORDER BY id DESC", (rom_id,))]
        return render(request, "manuals_game.html", rom=r, game=r.get("name") or r["fs_name"], state=label,
                      japanese=japanese, has_file=manual_file(r) is not None, q="; ".join(terms) if not link else q,
                      wide=wide, packs=packs, found=found, link=link, changes=changes, msg=msg, err=err,
                      psn=await package_rows(r))

    async def package_rows(r: dict) -> dict | None:
        """What the game's page says about PS one Classics packages; None on other platforms' pages."""
        if r.get("platform_fs_slug") not in psn.SLUGS:
            return None
        try:
            if packages.keys() is None:
                return {"keys": False, "key_file": packages.key_file.name}
        except psn.PackageError as e:
            return {"keys": False, "key_file": packages.key_file.name, "problem": str(e)}
        if not packages.loaded():
            return {"keys": True, "loaded": False}
        files = packages.matches(game_names(r))
        for f in files:
            if f["row"] and f["row"]["state"] == "downloading":
                try:
                    got = next((x for x in await qview.files(f["row"]["infohash"]) if x["name"] == f["path"]), None)
                    f["percent"] = math.floor(got["progress"] * 100) if got else None
                except (QBitError, OSError):
                    f["percent"] = None
        return {"keys": True, "loaded": True, "files": files, "source": psn.SOURCE}

    @app.get("/manuals/current/{rom_id}")
    async def manuals_current(rom_id: int):
        return document(manual_file(await need_romm().rom(rom_id)))

    @app.get("/manuals/kept/{change_id}")
    async def manuals_kept(change_id: int):
        c = db.one("SELECT backup_path FROM manual_changes WHERE id=?", (change_id,))
        return document(Path(c["backup_path"]) if c and c["backup_path"] else None)

    @app.get("/manuals/file/{identifier}/{name:path}")
    async def manuals_file(identifier: str, name: str):
        """A PDF from the Internet Archive for a look: the same cached file that would go into RomM."""
        try:
            return document(await archive.fetch(identifier, name))
        except ArchiveError as e:
            raise HTTPException(502, str(e))

    # manuals inside PS one Classics packages: a package is fetched only when its button is pressed
    def psn_back(form, msg: str = "", err: str = "") -> RedirectResponse:
        rom = str(form.get("rom", ""))
        note = f"?msg={quote(msg)}" if msg else f"?err={quote(err[:500])}" if err else ""
        return RedirectResponse(f"/manuals/g/{int(rom) if rom.isdigit() else 0}{note}", status_code=303)

    @app.post("/manuals/psn/open")
    async def manuals_psn_open(request: Request):
        """Load the set's file list (its .torrent from Minerva's CDN, once), as opening the set's own page does."""
        form = await request.form()
        try:
            if db.one("SELECT 1 FROM sets WHERE name=?", (psn.SET,)) is None:
                await minerva.refresh_index()
            await minerva.open_set(psn.SET)
        except KeyError:
            return psn_back(form, err="Minerva's index doesn't list the PS one Classics set.")
        except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
            return psn_back(form, err=f"The set's list couldn't be loaded from Minerva: {e}")
        return psn_back(form)

    @app.post("/manuals/psn/fetch")
    async def manuals_psn_fetch(request: Request):
        form = await request.form()
        idx = str(form.get("idx", ""))
        try:
            await packages.ask(minerva, int(idx) if idx.isdigit() else -1, request.headers.get("x-authentik-username"))
        except (psn.PackageError, QBitError, httpx.HTTPError, KeyError, ValueError, OSError) as e:
            return psn_back(form, err=f"The package wasn't sent to qBittorrent: {e}")
        qview.forget()
        return psn_back(form, msg="qBittorrent is fetching the package. Hakobiya reads its manual when it's there; "
                                  "reload this page to see it.")

    @app.post("/manuals/psn/stop")
    async def manuals_psn_stop(request: Request):
        form = await request.form()
        row = str(form.get("row", ""))
        try:
            await packages.stop(int(row) if row.isdigit() else 0)
        except (QBitError, OSError) as e:
            return psn_back(form, err=f"Stopped here, but qBittorrent didn't answer, so the package may still be in it: {e}")
        qview.forget()
        return psn_back(form, msg="Stopped.")

    @app.get("/manuals/psn/{row_id}")
    async def manuals_psn_file(row_id: int):
        """A manual read out of a package, for a look: the same file that would go into RomM."""
        return document(packages.pdf(row_id))

    async def put_manual(r: dict, pdf: Path, backup: Path | None) -> None:
        """Upload `pdf` as the game's manual. RomM writes over the manual it had and deletes the file when an
        upload breaks off, so if the old one is gone after a failure, the kept copy goes back."""
        try:
            await romm.upload_manual(r["id"], pdf)
        except RommError as e:
            old = manual_file(r)
            if backup is None or (old and old.stat().st_size == backup.stat().st_size):
                raise
            try:
                await romm.upload_manual(r["id"], backup)
            except RommError:
                raise RommError(f"{e}. RomM's old manual couldn't be put back either; Hakobiya's copy is {backup}") from e
            raise RommError(f"{e}. The manual RomM had was put back") from e

    @app.post("/manuals/apply")
    async def manuals_apply(request: Request):
        form = await request.form()
        rom_id = int(form.get("rom", 0))
        identifier, _, name = str(form.get("pick", "")).partition("/")
        back = f"/manuals/g/{rom_id}"
        try:
            r = await need_romm().rom(rom_id)
            if not name:
                raise ArchiveError("No PDF was ticked.")
            if identifier == psn.SOURCE:
                pdf = packages.pdf(int(name)) if name.isdigit() else None
                if pdf is None:
                    raise ArchiveError("That package's manual isn't here any more. Fetch the package again.")
                name = packages.name_of(int(name))
            else:
                pdf = await archive.fetch(identifier, name)
            was, _ = manual_now(r, manual_changes())
            old, backup = manual_file(r), None
            if r.get("path_manual") and old is None:
                raise ArchiveError("RomM's manual for this game can't be read from its resources folder, so it "
                                   "couldn't be kept for Put back. Nothing was changed.")
            if old:
                manual_backups.mkdir(parents=True, exist_ok=True)
                backup = manual_backups / f"{rom_id}-{utcnow().strftime('%Y%m%dT%H%M%S')}{old.suffix.lower()}"
                await asyncio.to_thread(shutil.copy2, old, backup)
            digest, size = await asyncio.to_thread(sha256_of, pdf), pdf.stat().st_size
            await put_manual(r, pdf, backup)
            db.execute("INSERT INTO manual_changes(rom_id, platform_id, name, identifier, file, sha256, size, old_state,"
                       " backup_path, applied_at, applied_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (rom_id, r.get("platform_id") or 0, r.get("name") or r["fs_name"], identifier, name, digest, size,
                        was, str(backup) if backup else None, utcnow().isoformat(),
                        request.headers.get("x-authentik-username")))
            if identifier != psn.SOURCE:
                archive.forget(identifier, name)
        except (RommError, ArchiveError, OSError) as e:
            return RedirectResponse(f"{back}?err={quote(str(e)[:500])}", status_code=303)
        note = "The manual is in RomM." + (" The one it had is kept: Put back restores it." if backup else "")
        return RedirectResponse(f"{back}?msg={quote(note)}", status_code=303)

    @app.post("/manuals/undo/{change_id}")
    async def manuals_undo(change_id: int):
        c = db.one("SELECT * FROM manual_changes WHERE id=? AND undone_at IS NULL", (change_id,))
        if c is None:
            raise HTTPException(404, "nothing to undo")
        back = f"/manuals/g/{c['rom_id']}"
        try:
            r = await need_romm().rom(c["rom_id"])
            now = manual_file(r)
            if r.get("path_manual") and now is None:
                raise ArchiveError("RomM's manual for this game can't be read from its resources folder. Nothing was changed.")
            if now and (now.stat().st_size != c["size"] or await asyncio.to_thread(sha256_of, now) != c["sha256"]):
                raise ArchiveError("The manual in RomM isn't the one Hakobiya put there any more, so it was left as it is.")
            backup = Path(c["backup_path"]) if c["backup_path"] else None
            if backup:
                if not backup.is_file():
                    raise ArchiveError("Hakobiya's copy of the old manual is gone. Nothing was changed.")
                await romm.upload_manual(c["rom_id"], backup)
            elif now:
                await romm.delete_manual(c["rom_id"])
            db.execute("UPDATE manual_changes SET undone_at=? WHERE id=?", (utcnow().isoformat(), change_id))
        except (RommError, ArchiveError, OSError) as e:
            return RedirectResponse(f"{back}?err={quote(str(e)[:500])}", status_code=303)
        note = "The manual RomM had before is back." if backup else "The manual was taken out again (the game had none before)."
        return RedirectResponse(f"{back}?msg={quote(note)}", status_code=303)

    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), proxy_headers=True,
                forwarded_allow_ips="*", log_level="warning")
