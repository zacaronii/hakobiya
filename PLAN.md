# Hakobiya (運び屋) — build plan (approved 2026-11-20; formerly "Minerva toolbox")

A small self-hosted web app with three tabs:

- **Grab**: browse Minerva's set index, pick files, send them to qBittorrent, then put the finished games into RomM.
- **Art**: view RomM's artwork per platform and fix gaps.
- **Make**: generate disc and cartridge art where none exists.

This plan covers **Grab** in detail. Art and Make get their own plans later.

## Rules for Minerva access (agreed with the user)

- **Only the CDN index** at `https://cdn.minerva-archive.org/`. Its robots.txt is `Allow: /`, and its Content-Signal is `ai-train=no, use=reference`. The main site's `/browse/`, `/api/`, `/rom/` and `/account/` paths are disallowed by robots.txt and are never touched.
- **The index** is fetched when the tool opens and the cached copy is over 24 h old, or when the user presses Refresh.
  - It's a conditional GET (`If-Modified-Since`/ETag).
  - All 1,050 entries are dated 2026-03-31, so it rarely changes.
- **A set's `.torrent`** is fetched only when the user opens that set, and is kept forever. There is no prefetching or crawling.
- **Limits:** one request at a time, and a User-Agent that names the tool (`minerva-toolbox/0.1 (personal)`).
- **Cache size:** index about 0.5 MB; `.torrent` files 37 KB median, 26 MB max (about 700 MB if every set were opened). Realistic use is a few tens of MB.
- **Fallback** if Minerva ever objects: a `magnet:` link handler (registered with `navigator.registerProtocolHandler`), with the user browsing the site by hand.

## Deployment

- One Compose project, on the reverse proxy's network `proxy`, reachable from the local network only.
- **Login: Authentik forward auth** (user's choice, 2026-11-20). Nothing uses forward auth yet; Authentik is already on the
  `proxy` network, so Traefik can reach its embedded outpost. Needs:
  1. In Authentik: a Proxy Provider ("Forward auth (single application)", external host = the toolbox URL), an
     Application for it, and the provider added to the embedded outpost.
  2. In Traefik: an `authentik` forwardAuth middleware in `dynamic/middlewares.yml`
     (`http://authentik:9000/outpost.goauthentik.io/auth/traefik`, trustForwardHeader, authResponseHeaders
     X-authentik-username/email/groups) and a router for `/outpost.goauthentik.io/` on the toolbox host.
  The middleware is reusable for other apps later. Fallback if that stalls: a basic login page in the app.
- **Mounts:**
  - the data share → `/data`, one mount so hardlinks between `/data/torrents` and `/data/media/romm_library` work;
  - appdata → `/config` (cache, DB, secrets).
- **Secrets** in `/config/secrets.env` (mode 600):
  - qBittorrent credentials;
  - RomM token (its own RomM API key).
- **Image:** python:3.13-slim with FastAPI + htmx (no JS build step), libtorrent's Python bindings (only for parsing and hashing `.torrent` files), `chdman` (mame-tools 0.276), `unzip` and `7z`. It runs as 99:100.
- A GitHub repo with CI building the GHCR image.

## Grab: the flow

1. **Index.** Index is parsed into SQLite (name, date, size). The set list is searchable and grouped by source (No-Intro, Redump, T-En, Internet Archive…) and platform, with a "you already have this set" badge.
2. **Open a set.** Its `.torrent` is fetched (once) and parsed into a file table.
   - The torrent's infohash is compared with qBittorrent's torrents. The five existing `Minerva_Myrient` sets are recognised, and their files show **seeding / selected / not selected**.
3. **File browser.**
   - Instant search;
   - No-Intro/Redump-aware filters: region (Japan by default; World, USA, Europe), and hiding Beta / Demo / Proto / Sample / Kiosk / Pirate / Aftermarket;
   - "latest revision only";
   - discs of one game grouped together (`(Disc 1)`, `(Disc 2)`);
   - an "in RomM already" marker, matched by file name in the platform folder.
   - Ticked files show a running total size.
4. **Send.**
   - **A set already in qBittorrent:** `torrents/filePrio` turns on the chosen files. Nothing else changes.
   - **A new set:** the `.torrent` is added **stopped**, to `/data/torrents` (root folder `Minerva_Myrient`, matching the existing sets). All files are set to 0, the chosen ones to 1, then it's started. No unwanted pieces download, though edge pieces shared with neighbouring files may be partly fetched, as in qBittorrent itself.
   - A queue row is recorded per chosen file (set, path, platform, state).
   - The qBittorrent session is opened per action and logged out after (5.3 caps WebUI sessions at 15).
5. **Watch.** A background loop every 60 s checks `torrents/files` progress for queued files. When a file is complete, its post-processing job runs.
6. **Post-process (automatic).** See the next section. Each job has a state (`waiting → downloading → processing → done / failed`) shown on a Jobs page, with the log of the commands run and a Retry button.
7. **RomM.** After a batch finishes, the toolbox triggers a RomM scan for the affected platforms, without a full rescan.

## Post-processing rules per platform

The originals in `/data/torrents/Minerva_Myrient/...` are **never modified or moved**, so they keep seeding.

| Kind | Platforms (examples) | Action |
|---|---|---|
| Cartridge / small | FC, FDS, SFC, GB, GBC, GBA, MD/Genesis, PCE, N64, NDS, Game Gear | **Hardlink the `.zip`** into `romm_library/roms/<slug>/`. No extra space. RomM hashes zip contents (checked in its source), so matching works. |
| CD | PS1, Saturn, PCE-CD, Mega-CD, Dreamcast (GDI) | Unzip to a temp dir under `/data/torrents/.toolbox-tmp` (same array, deleted after), then `chdman createcd`. Multi-disc goes in one folder `<Game>/` with `<Game>.m3u`. |
| DVD/UMD | PSP, PS2 | Temp unzip, then `chdman createdvd` (PSP with `-hs 2048`). Multi-disc folder + `.m3u`. |
| 3DS | Nintendo 3DS (Decrypted) | Extract the `.3ds` (needs the space of a full copy). Never Z3DS/`.zcci`. |
| Anything unknown | — | Put the job in **needs rule** and do nothing. The user picks a rule once per set. |

- **Set → RomM slug mapping:** a small editable table, seeded from the slugs already present (`nes`, `snes`, `gb`, `gba`, `gbc`, `genesis`, `n64`, `nds`, `psx`, `psp`, `ps2`, `saturn`, `dc`, `3ds`, `tg16`, `turbografx-cd`…), plus new ones like `fds`. A missing slug means a new folder; RomM creates the platform on scan.
- **Before any conversion, check free space:** stop if the array has less than 0.75 TB free (the slimmer's brake).
- **Never overwrite a file** that already exists in the library. Mark the job "exists" instead.
- **CHD jobs run one at a time.** chdman is CPU-heavy.

## Data model (SQLite in /config)

- `sets(name, url, size, date, torrent_path, infohash, fetched_at)`
- `files(set, idx, path, size, platform_guess, region, flags)`: filled when a set is opened.
- `jobs(id, set, file_idx(es), kind, target, state, log, created, updated)`.
- `rules(set_pattern, slug, kind)`.

## Tests

- **Name parsing** (regions, flags, discs, revisions) on real set file names from the cached torrents.
- **The send logic** against a fake qBittorrent (existing set → filePrio only; new set → add stopped, priorities, start).
- **Post-processing:**
  - hardlink: a tmp file pair with the same inode;
  - CHD: a small synthetic cue/bin and ISO, checked with `chdman verify`;
  - m3u grouping;
  - existing target → no overwrite.
- **Live checks, read-only first:**
  1. Index and one set open.
  2. One cartridge game hardlinked; RomM matches it.
  3. One PS1 multi-disc game.

## Milestones (1-5 deployed 2026-11-20)

1. Skeleton container, index cache, set browser (read-only: no qBittorrent writes).
2. File browser + Send (qBittorrent writes), tested on one small FDS game.
3. Post-processing + RomM scan.
4. Art tab (RomM read + libretro JP cover fixer from the earlier one-off script).
5. Make tab (cartridge/UMD/disc templates).

## Art tab: box art policy (user, 2026-11-20)

- **Never automatic.** RomM's scan stays the default source of covers. The toolbox doesn't touch art after a grab.
- **On request, two actions:**
  - **Fill gaps:** games with no cover, or no physical/disc art.
  - **Replace:** games whose RomM cover doesn't match the preferences (English/US box, wrong aspect for the platform).
- **Both** show candidates side by side (current vs proposed) before anything is uploaded, per game or per platform in a batch.
- **Sources, in order:** libretro-thumbnails Named_Boxarts (JP names), ScreenScraper JP region media via RomM's own data,
  then Make-tab generated art for disc/cart images.
- **Uploading:** the cover goes up as RomM `artwork` (locked), as done for the 15 PS1 covers.

## Images from other regions (2026-11-21)

- The owner wants every image Japanese. RomM's scan takes another region's image when ScreenScraper has no Japanese
  one; `regions.py` finds those from the `media=` tag of the stored URL (nothing is fetched, URLs are never shown).
- Removal is on request: "Check now" for spines, backs, 3D boxes, marquees and title screens; ticked items for the
  front, disc/cartridge and logo. Backup + "Put back"; a put-back image is never removed again by the check.
- The six-hourly check is opt-in (`HAKOBIYA_REGION_CHECK=1`), in line with "never automatic" above, until the owner
  says otherwise. Manuals and screenshots aren't covered (they aren't `ss_metadata` paths).

## Manuals (2026-11-23)

- The owner wants Japanese manuals. ScreenScraper has one for some games only, and RomM takes another region's
  when there is no Japanese one. No other metadata provider has them; the scans that exist are on the Internet
  Archive, as single items and as a few collections (`manuals.py`: `PACKS`).
- **Never automatic**, like box art: a search per game on request, a look at the PDF, and only the ticked PDF is
  uploaded (RomM's own manual upload, so it counts as "uploaded" there). The manual a game had is kept, with "Put
  back"; if an upload fails, the old manual is put back at once (RomM removes the file when an upload fails).
- Access: archive.org only (the search, an item's file list, its PDFs; redirects are followed only to
  archive.org's own hosts), one request at a time with a pause, a User-Agent that names Hakobiya, answers cached.
- **PS one Classics packages (0.9.0, `packages.py`):** a second source for PlayStation games, also only on
  request: one package of Minerva's set through qBittorrent, its `DOCUMENT.DAT` read into a PDF, the package
  removed again. Decisions:
  - The fixed keys the packages are encrypted with stay out of the repository; they come from
    `/config/psn-keys.txt`, and the feature is off without it.
  - Minerva: nothing new. The set's `.torrent` is opened like on the set's own page (once, then kept).
  - Removal is the one thing here that deletes on its own, so it is narrow: only a torrent Hakobiya added for
    manuals (its tag), never one that is used for anything else (another package on its way, a job from the set's
    page, any other file on or downloaded), and never after a failed read. Nothing is ever switched off in a
    torrent that stays.
  - The PDF is written here (each page's pixels as they are, a point a pixel), so nothing is recompressed.
- **PSP packages (0.10.0):** the same for PSP games, out of Minerva's encrypted PSP PlayStation Store set.
  Decisions:
  - The sets are a small table in `packages.py` (Minerva's set, the RomM folders, the kind of manual); a game's
    page offers its own platform's set only, and a manual that went into RomM is recorded under its set's source.
  - A PSP manual is under a key of its own, which the package's `DOCINFO.EDAT` holds: a PGD under fixed keys of
    the PSP's key engine. Those are further names in the same key file; the PSP set is offered only when all of
    them are there, and a key file with only the PS one names works as it did.
  - What tells that the keys fit is the PGD's check of its data table, then the manual's `DOC `. The checksums
    inside `DOCUMENT.DAT` and the PGD header's check under its fixed key aren't read: they would need three more
    keys, and a page that isn't a picture is refused anyway.
  - So far read only out of packages built for the tests, laid out the way a real package was found to be.
- **Original scans (0.11.0, `scans.py`):** the uploader's own `.cbz`/`.zip` of page images is offered before the
  Archive's PDFs, which are its derivatives and heavily compressed. Decisions:
  - Only files the item marks `original`; the Archive's `_jp2.zip` is reworked as well and isn't offered.
  - The archive is read in memory, entry by entry, with limits (entries, pages, bytes per page and in all, pixels
    per page, the PDF's size); names pointing outside it (absolute, `..`) or encrypted entries refuse the archive.
  - One PDF writer (`pdf.py`) for packages and scans: JPEG pages are put in as they are (DCTDecode), others as
    deflated pixels; a point a pixel. The packages' PDFs come out byte for byte as before.
  - `.cbr` is left out: RAR needs an unrar tool the image doesn't have.
- Not done: items with only loose page images (no archive, no PDF); the manuals inside 3DS ROMs.

## Decisions

- **Login:** Authentik forward auth (above), with a basic login as the fallback.
- **The earlier manual conversions** (about 58 GB of PSP, PS1 and 3DS) stay as they are. The toolbox only handles new grabs.
- **Box art:** opt-in fill/replace only (Art tab section above).

## Backlog (noted 2026-11-20, after milestones 1-5 and managed disc art; none urgent)

- **More Make templates:** Saturn, PS2, GBA, Famicom carts etc., each from one of the library's own real Japanese
  disc/cart images (geometry measured per reference image, as for 3DS/DS/PSP/PS1). Add when those systems get grabbed.
- **Set → RomM folder mapping editor:** sets without a rule show "no RomM folder for this set" on the Sent page; a
  small settings page to map them (and pick hardlink / CHD / extract) instead of a code change.
- **ScreenScraper as a second Art source:** libretro had Japanese boxes for only 2 of 14 PSP games; SS box-2D(jp)
  via the RomM ScreenScraper login Hakobiya now holds.
- **Product codes on generated PSP/PS1 disc labels** (e.g. ULJS-00219, SLPS-01234), as carts already get theirs.
