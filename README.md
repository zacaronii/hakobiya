# Hakobiya 運び屋

A courier between [Minerva](https://minerva-archive.org)'s torrent sets and a RomM library: browse a set's files,
pick the games you want, send just those to qBittorrent, and (later milestones) file the finished downloads into
RomM in the right format.

- **Milestone 1:** the set list from Minerva's CDN index and a searchable file browser per set (region, tags,
  "not in RomM", qBittorrent state).
- **Milestone 2 (now):** tick files into a per-set basket (kept across pages and filters; "Pick all shown" follows
  the filters), then Send: a set already in qBittorrent gets just those files switched on; a new set is added
  stopped, everything else switched off, then started. The Sent page shows progress.
  Files a set already has switched on in qBittorrent (a set you added there yourself) can be ticked and sent
  too: nothing is downloaded again, they are just filed.
- **Milestone 3 (now):** finished files are filed into RomM automatically, one at a time: cartridge zips hardlinked,
  CD/DVD/UMD images converted to CHD (multi-disc games in a folder with an .m3u), 3DS extracted; then RomM scans
  that platform. Originals keep seeding and nothing in the library is overwritten.
- **Milestone 4 (now):** the Art tab. Per RomM platform: every cover with where it came from (ScreenScraper region,
  IGDB, uploaded), its size, and flags (no cover, not Japanese, odd shape for the platform). Optionally compares each
  with libretro-thumbnails' Japanese box (looked up by the game's own No-Intro/Redump file name, cached) and uploads
  only the ones you tick. Every replaced cover is backed up, with Undo.
- **Milestone 5 (now):** the Make tab. For games whose disc/cartridge image is missing or from another region, it
  builds a Japanese one from a real Japanese disc/cart of the same platform (template images in
  `/config/make-templates/<slug>.png`: 3DS, DS, PSP, PS1) with the game's box art printed on and, for carts, the
  product code read from the ROM header. Live preview with position/zoom sliders; saving writes RomM's
  `physical/physical.png` and records it in the game's ScreenScraper metadata (`hakobiya_generated`), with Undo.
  RomM can't lock disc art (only covers and manuals) and a metadata refresh rewrites it, so Hakobiya manages saved
  images afterwards: it keeps its own copy and offers Restore when RomM replaced or dropped one, and once a week
  (or on "Check ScreenScraper now") swaps in a genuine Japanese scan as soon as ScreenScraper has one.
- **Images from other regions (0.7.0):** Art → "images from other regions" lists every image RomM saved that isn't
  Japanese (RomM falls back to us/wor/eu media when ScreenScraper has no Japanese one, and that can't be switched
  off). "Check now" takes out the non-Japanese spines, box backs, 3D boxes, marquees and title screens; fronts,
  disc/cartridge images and logos go only when ticked. Each removed file is kept in `/config/media-backup` with
  "Put back", and an image put back is left alone afterwards. `HAKOBIYA_REGION_CHECK=1` runs the check every six
  hours as well (off unless set).
- Plan and house rules for Minerva access: [PLAN.md](PLAN.md).

Run: `python -m hakobiya` (env: `HAKOBIYA_CONFIG`, `ROMM_LIBRARY`, `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`,
`HAKOBIYA_PROXY_KEY`: when set, requests without that `X-Hakobiya-Key` header are refused; `ROMM_URL`, `ROMM_TOKEN`
for the scan; `DATA_ROOT` (default /data, where `torrents/` lives); `HAKOBIYA_WORKER=0` turns off the filing loop;
`HAKOBIYA_PUBLIC_URL`: the address named when a request without the key is refused). An example deployment behind
Traefik and Authentik is in [deploy/docker-compose.yml](deploy/docker-compose.yml).
Tests: `pip install -e ".[dev]" && pytest`.
