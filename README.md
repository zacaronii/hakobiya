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
- **Manuals (0.8.0):** the Manuals tab. Per RomM platform: what each game's manual is now (none, Japanese, from
  another region, uploaded). For one game it searches the Internet Archive under the game's titles (the Japanese
  one first) and looks the game up in a few collections of Japanese manual scans; an item's address can be pasted
  too. "Look at it" opens a PDF, and only the PDF you tick goes into RomM. Nothing is fetched or changed on a
  timer. The manual RomM had is kept in `/config/manual-backup` with "Put back"; a game that had none gets its
  manual taken out again. Only archive.org is contacted, one request at a time; search answers are kept for a
  week and file lists for a month. A PDF fetched for a look is kept for a week at most (`/config/manual-cache`,
  2 GB at most). Items that hold only loose page images aren't turned into a manual yet (but see 0.11.0).
- **Manuals out of PlayStation Store packages (PS one Classics 0.9.0, PSP 0.10.0):** Sony's PS one Classics
  release of a game carries its manual (PNG pages, 480 pixels wide: readable, not sharp), and a PSP game's package
  can carry one the same way. On a PlayStation or PSP game's manual page, "Fetch the package" has qBittorrent
  fetch that one package from Minerva's set (PS one Classics, or the encrypted PSP PlayStation Store set; several
  hundred MB for a few MB of manual). Hakobiya reads the manual out of it, keeps it as a PDF in
  `/config/package-manuals`, and it is looked at and ticked like any other; nothing is fetched or put into RomM
  unasked. The packages are encrypted, and the keys are not in this repository: put them into
  `/config/psn-keys.txt` as `name = hex` lines (`pkg_psp` or `pkg_ps3`, 16 bytes; `document_key` and
  `document_iv`, 8 bytes each; `#` starts a comment). Without that file the section only says so.
  PSP packages need more names in the same file: `pkg_psp`; `kirk_38`, `kirk_39` and `kirk_63` (the AES keys with
  these numbers in the PSP's key engine, 16 bytes each); `pgd_mac_xor`, `pgd_seed_xor` and `pgd_stream_xor` (the
  three 16-byte constants of the PSP's PGD files: the one XORed onto a hash, the one XORed onto the cipher's seed
  before key 0x39, and the one XORed onto what comes out of it); `document_xor` (8 bytes, XORed onto the key a
  package's `DOCINFO.EDAT` holds). Until all of them are there, a PSP game's page lists the missing names and
  offers nothing; PlayStation games are offered as before.
  After a package is read, Hakobiya **removes its torrent and files from qBittorrent again**, but only a torrent
  it added itself for manuals (tag `hakobiya-manual`), and only when no other package of it is still on its way,
  nothing was sent from the set's own page, and no other file in it is switched on or downloaded. Otherwise, or
  when the set already was in qBittorrent, nothing is removed or switched off, and the page says so. A package
  that couldn't be read stays for "Try again"; "Stop" gives one up under the same rule.
- **Original scans (0.11.0):** in some items, the collections among them, the uploader's own file for a manual is a
  Comic Book ZIP (`.cbz`) or a `.zip` of page images, and the only PDF is the one the Archive made of it, which is
  heavily compressed and can be hard to read. Such an archive is now listed first, as "original scans" (its size
  before, its page count after a look); the Archive's PDFs are listed after it as "the Archive's compressed copy".
  "Look at it" fetches the archive (200 MB at most, same rules as a PDF) and makes a PDF of its pages in name order
  (page2 before page10), a point a pixel: JPEG pages go in as they are, byte for byte; PNG, WebP, TIFF and JPEG 2000
  pages are stored losslessly. The archive itself isn't kept, only the PDF, in the same cache as fetched PDFs.
  Ticking and Put back work as for any other manual. `.cbr` (RAR) archives aren't read: that would need an unrar
  tool in the image.
- Plan and house rules for Minerva access: [PLAN.md](PLAN.md).

Run: `python -m hakobiya` (env: `HAKOBIYA_CONFIG`, `ROMM_LIBRARY`, `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`,
`HAKOBIYA_PROXY_KEY`: when set, requests without that `X-Hakobiya-Key` header are refused; `ROMM_URL`, `ROMM_TOKEN`
for the scan; `DATA_ROOT` (default /data, where `torrents/` lives); `HAKOBIYA_WORKER=0` turns off the filing loop;
`HAKOBIYA_PUBLIC_URL`: the address named when a request without the key is refused). An example deployment behind
Traefik and Authentik is in [deploy/docker-compose.yml](deploy/docker-compose.yml).
Tests: `pip install -e ".[dev]" && pytest`.

Licence: GPL-3.0-only, version 3 of the GNU General Public License and not later versions ([LICENSE](LICENSE)).
