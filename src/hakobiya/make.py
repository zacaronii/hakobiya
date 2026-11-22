"""Generated disc and cartridge art, built from a real Japanese disc/cart image of the same platform.

The reference image (one of the library's own ScreenScraper JP physical images, saved as the platform's template)
supplies everything generic: the cartridge shell and its NINTENDO 3DS / DS logo band, the UMD housing and hub, the
CD's centre ring. The game's own Japanese box art is printed into the label (or the disc's printed ring), and
cartridges get the product code from the ROM header, as on a real label."""

from __future__ import annotations

import io
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


@dataclass
class Template:
    kind: str                                   # "cart" | "disc"
    # cart: label box (x0, y0, x1, y1) on the reference image, the logo band kept from it, the serial band at the bottom
    label: tuple[int, int, int, int] = (0, 0, 0, 0)
    logo_band: int = 0
    serial_band: int = 0
    serial: str = "{code}"
    # disc: centre and the printed ring (outer radius, inner radius: inside it the reference's hub stays)
    center: tuple[int, int] = (0, 0)
    r_out: int = 0
    r_in: int = 0
    # box art: fractions trimmed off (left, top, right, bottom) to drop the platform band (e.g. 3DS's black side band)
    trim: tuple[float, float, float, float] = (0, 0, 0, 0)
    corner: int = 8


TEMPLATES: dict[str, Template] = {
    "3ds": Template("cart", label=(59, 65, 487, 517), logo_band=76, serial_band=44, serial="CTR-{code}-JPN",
                    trim=(0, 0, 0.105, 0)),
    "nds": Template("cart", label=(74, 47, 498, 517), logo_band=62, serial_band=50, serial="NTR-{code}-JPN",
                    trim=(0.125, 0, 0, 0)),
    "psp": Template("disc", center=(302, 293), r_out=262, r_in=130, trim=(0, 0.09, 0, 0)),
    "psx": Template("disc", center=(300, 300), r_out=291, r_in=74, trim=(0, 0, 0, 0)),
}


def product_code(slug: str, path: Path) -> str | None:
    """The 4-character game code from a DS (.nds, possibly zipped) or 3DS (.3ds/.cci) header, e.g. ANOJ / AJ2J."""
    try:
        if slug == "nds":
            data = _head(path, 0x10)
            code = data[0x0C:0x10].decode("ascii")
        elif slug == "3ds":
            head = _head(path, 0x200)
            if head[0x100:0x104] != b"NCSD":
                return None
            offset = struct.unpack("<I", head[0x120:0x124])[0] * 0x200       # first partition (the game)
            ncch = _read_at(path, offset, 0x200)
            pcode = ncch[0x150:0x160].split(b"\0")[0].decode("ascii")        # e.g. CTR-P-AJ2J
            code = pcode.rsplit("-", 1)[-1]
        else:
            return None
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, struct.error, KeyError):
        return None
    return code if re.fullmatch(r"[A-Z0-9]{4}", code) else None


def _head(path: Path, n: int) -> bytes:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            member = max(z.infolist(), key=lambda i: i.file_size)
            with z.open(member) as f:
                return f.read(n)
    return _read_at(path, 0, n)


def _read_at(path: Path, offset: int, n: int) -> bytes:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            member = max(z.infolist(), key=lambda i: i.file_size)
            with z.open(member) as f:
                f.seek(offset)
                return f.read(n)
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(n)


def _font(size: int) -> ImageFont.ImageFont:
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def _trimmed(box: Image.Image, t: Template) -> Image.Image:
    w, h = box.size
    l, tp, r, b = t.trim
    return box.crop((int(w * l), int(h * tp), int(w * (1 - r)), int(h * (1 - b))))


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return m


def make(slug: str, reference: Image.Image, box: Image.Image, code: str | None = None,
         focus: float = 0.45, zoom: float = 1.0) -> Image.Image:
    """`focus` is where the art is centred vertically (0 top .. 1 bottom); `zoom` > 1 crops in closer."""
    t = TEMPLATES[slug]
    ref = reference.convert("RGBA")
    art = _trimmed(box.convert("RGB"), t)
    if zoom > 1:
        w, h = art.size
        cw, ch = w / zoom, h / zoom
        top = (h - ch) * min(max(focus, 0), 1)
        art = art.crop((int((w - cw) / 2), int(top), int((w + cw) / 2), int(top + ch)))
    out = ref.copy()
    if t.kind == "cart":
        x0, y0, x1, y1 = t.label
        lw, lh = x1 - x0, y1 - y0
        label = Image.new("RGBA", (lw, lh))
        paper = ref.getpixel((x0 + 12, y0 + 10))                                   # the label's paper
        label.paste(paper, (0, 0, lw, lh))
        label.paste(ref.crop((x0, y0, x1, y0 + t.logo_band)), (0, 0))                 # NINTENDO 3DS / DS band
        art_h = lh - t.logo_band - t.serial_band
        label.paste(ImageOps.fit(art, (lw, art_h), Image.LANCZOS, centering=(0.5, focus)), (0, t.logo_band))
        if code:
            d = ImageDraw.Draw(label)
            text = t.serial.format(code=code)
            f = _font(int(t.serial_band * 0.62))
            tw = d.textlength(text, font=f)
            d.text(((lw - tw) / 2, lh - t.serial_band / 2), text, font=f, fill=(40, 40, 40, 255), anchor="lm")
        out.paste(label, (x0, y0), _rounded_mask((lw, lh), t.corner))
        return out
    # disc: print the art into the ring, keep the reference's hub and edge
    cx, cy = t.center
    d = 2 * t.r_out
    disc_art = ImageOps.fit(art, (d, d), Image.LANCZOS, centering=(0.5, focus)).convert("RGBA")
    ring = Image.new("L", (d, d), 0)
    dr = ImageDraw.Draw(ring)
    dr.ellipse((0, 0, d - 1, d - 1), fill=255)
    r = t.r_out - t.r_in
    dr.ellipse((r, r, d - 1 - r, d - 1 - r), fill=0)
    ring = ring.filter(ImageFilter.GaussianBlur(0.8))
    out.paste(disc_art, (cx - t.r_out, cy - t.r_out), ring)
    edge = ImageDraw.Draw(out)
    edge.ellipse((cx - t.r_out, cy - t.r_out, cx + t.r_out, cy + t.r_out), outline=(150, 150, 155, 255), width=2)
    return out


def png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def physical_status(rom: dict, resources: Path, generated_copy: Path | None = None) -> tuple[str, str]:
    """(state, label) of the game's disc/cart art: missing | foreign | generated | japanese.

    "generated" is decided by Hakobiya's own copy of what it saved (RomM's API filters unknown metadata keys, so a
    marker stored in RomM doesn't come back)."""
    ss = rom.get("ss_metadata") or {}
    path = ss.get("physical_path")
    if not path or not (resources / path).is_file():
        return "missing", "no disc/cart art"
    if generated_copy and generated_copy.is_file() and (resources / path).read_bytes() == generated_copy.read_bytes():
        return "generated", "made here"
    m = re.search(r"media=support-2D\((\w+)\)", unquote(ss.get("physical_url") or ""))
    region = m.group(1) if m else None
    if region == "jp":
        return "japanese", "ScreenScraper jp"
    return "foreign", f"ScreenScraper {region}" if region else "unknown source"
