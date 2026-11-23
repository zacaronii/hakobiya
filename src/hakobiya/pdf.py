"""A PDF of page pictures: one picture a page, a point a pixel, each picture's data stored as it is given.

Written page by page to a `.part` file next to `out`, so only one page's picture is in memory at a time; the
finished file replaces `out`, and nothing is left behind when a page fails."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


@dataclass(frozen=True)
class Picture:
    width: int
    height: int
    colours: str            # /DeviceRGB or /DeviceGray
    filter: str             # /FlateDecode (8-bit pixels, deflated) or /DCTDecode (a JPEG file as it is)
    data: bytes


def write(out: Path, count: int, pictures: Iterable[Picture]) -> None:
    """Write `count` pages, one for each of `pictures`, to `out`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".part")
    offsets: list[int] = []
    try:
        with part.open("wb") as f:
            def put(*pieces: bytes) -> None:
                offsets.append(f.tell())
                f.write(f"{len(offsets)} 0 obj\n".encode())
                for piece in pieces:
                    f.write(piece)
                f.write(b"\nendobj\n")

            f.write(HEADER)
            kids = [3 + 3 * i for i in range(count)]          # each page's object; its picture and drawing follow
            put(b"<< /Type /Catalog /Pages 2 0 R >>")
            put(f"<< /Type /Pages /Count {count} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode())
            done = 0
            for p in pictures:
                if done == count:
                    raise ValueError("more pages than counted")
                n, done = len(offsets) + 1, done + 1
                draw = f"q {p.width} 0 0 {p.height} 0 0 cm /P Do Q".encode()
                put(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {p.width} {p.height}] /Resources << /XObject"
                    f" << /P {n + 1} 0 R >> >> /Contents {n + 2} 0 R >>".encode())
                put(f"<< /Type /XObject /Subtype /Image /Width {p.width} /Height {p.height} /ColorSpace {p.colours}"
                    f" /BitsPerComponent 8 /Filter {p.filter} /Length {len(p.data)} >>\nstream\n".encode(),
                    p.data, b"\nendstream")
                put(f"<< /Length {len(draw)} >>\nstream\n".encode(), draw, b"\nendstream")
            if done != count:
                raise ValueError("fewer pages than counted")
            xref = f.tell()
            f.write(f"xref\n0 {len(offsets) + 1}\n".encode() + b"0000000000 65535 f \n")
            f.write(b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets))
            f.write(f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
