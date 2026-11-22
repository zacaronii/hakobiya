"""No-Intro / Redump file names: title, regions, languages, and the tags used for filtering.

`Adian no Tsue (Japan) (Beta) (1986-10-29).zip`, `Final Fantasy VII (Japan) (Disc 2).zip`,
`[BIOS] Twin Famicom (Japan) (En).zip`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REGIONS = {
    "Japan", "USA", "Europe", "World", "Asia", "Korea", "China", "Taiwan", "Hong Kong", "Australia", "Brazil",
    "Canada", "France", "Germany", "Italy", "Spain", "Netherlands", "Sweden", "Norway", "Denmark", "Finland",
    "UK", "Russia", "Scandinavia", "Latin America", "Mexico", "Poland", "Portugal", "Greece", "Belgium",
    "Switzerland", "Austria", "Ireland", "New Zealand", "India", "South Africa", "Unknown",
}
LANG_RE = re.compile(r"^[A-Z][a-z](-[A-Z][a-z]+)?(,[A-Z][a-z](-[A-Z][a-z]+)?)*$")
# Tags a filter can hide; the first five are hidden by default.
HIDEABLE = ["Beta", "Proto", "Demo", "Sample", "Kiosk", "Pirate", "Unl", "Aftermarket", "Program", "BIOS"]
HIDDEN_BY_DEFAULT = {"Beta", "Proto", "Demo", "Sample", "Kiosk", "Pirate", "Program", "BIOS"}
EXTENSIONS = (".zip", ".7z", ".rar", ".chd", ".iso", ".cue", ".bin", ".gdi", ".cso", ".rvz", ".wbfs")


@dataclass
class Name:
    stem: str                                   # file name without extension
    title: str                                  # before the first tag
    regions: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)  # HIDEABLE ones found
    disc: int | None = None
    rev: str | None = None
    game: str = ""                              # the stem without the disc tag: discs of one game share it


def strip_ext(filename: str) -> str:
    low = filename.lower()
    for ext in EXTENSIONS:
        if low.endswith(ext):
            return filename[: -len(ext)]
    return filename


def parse(filename: str) -> Name:
    stem = strip_ext(filename.rsplit("/", 1)[-1])
    n = Name(stem=stem, title=stem)
    body = stem
    if body.startswith("[BIOS]"):
        n.tags.add("BIOS")
        body = body[len("[BIOS]"):].strip()
    m = re.search(r"\s[\(\[]", body)
    n.title = body[: m.start()].strip() if m else body.strip()
    groups = re.findall(r"\(([^()]*)\)", body)
    for g in groups:
        parts = [p.strip() for p in g.split(",")]
        if not n.regions and parts and all(p in REGIONS for p in parts):
            n.regions = parts
        elif LANG_RE.match(g.replace(" ", "")):
            n.languages = g.replace(" ", "").split(",")
        elif (d := re.fullmatch(r"Disc (\d+)", g)):
            n.disc = int(d.group(1))
        elif (r := re.fullmatch(r"Rev ([\w.]+)", g)) or (r := re.fullmatch(r"(v\d[\w.]*)", g)):
            n.rev = r.group(1)
        else:
            for tag in HIDEABLE:
                if re.fullmatch(rf"{tag}( \d+| [\w-]+)?", g) or g == tag:
                    n.tags.add(tag)
    n.game = re.sub(r"\s*\(Disc \d+\)", "", stem).strip()
    return n
