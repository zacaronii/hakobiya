"""Page images out of a zip into a PDF. Every picture here is made up in the test."""

import io
import zipfile

import pytest
from PIL import Image, features
from pypdf import PdfReader

from hakobiya import scans


def picture(fmt: str, size=(40, 60), mode="RGB", colour=(200, 30, 90), **save) -> bytes:
    im = Image.new(mode, size, colour if mode != "L" else 120)
    for x in range(0, size[0], 3):                       # some detail, so lossy coding would show
        im.putpixel((x, (x * 7) % size[1]), (x * 5 % 256, 255 - x, 17) if mode in ("RGB", "RGBA") else x * 9 % 256)
    out = io.BytesIO()
    im.save(out, fmt, **save)
    return out.getvalue()


def archive(path, entries) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)


def pixels(page) -> Image.Image:
    return page.images[0].image


def test_natural_order():
    names = ["p10.jpg", "p2.jpg", "p1.jpg", "P3.jpg", "a/p11.jpg"]
    assert sorted(names, key=scans.natural) == ["a/p11.jpg", "p1.jpg", "p2.jpg", "P3.jpg", "p10.jpg"]


def test_pages_in_order_jpegs_as_they_are_and_junk_skipped(tmp_path):
    first, gray = picture("JPEG", (10, 20), quality=70), picture("JPEG", (50, 60), "L", quality=60)
    second = picture("PNG", (30, 40))
    archive(tmp_path / "m.cbz", [
        ("pages/p10.jpg", gray), ("pages/p2.png", second), ("pages/p1.jpg", first),
        ("__MACOSX/pages/._p1.jpg", b"junk"), ("pages/.hidden.jpg", b"junk"), (".DS_Store", b"junk"),
        ("ComicInfo.xml", b"<ComicInfo/>"), ("pages/", b"")])
    assert scans.make_pdf(tmp_path / "m.cbz", tmp_path / "m.pdf") == 3
    body = (tmp_path / "m.pdf").read_bytes()
    assert first in body and gray in body                                 # the JPEGs' own bytes, not re-encoded
    reader = PdfReader(tmp_path / "m.pdf")
    assert [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages] == [(10, 20), (30, 40), (50, 60)]
    images = [p["/Resources"]["/XObject"]["/P"].get_object() for p in reader.pages]
    assert [(i["/Filter"], i["/ColorSpace"]) for i in images] == [
        ("/DCTDecode", "/DeviceRGB"), ("/FlateDecode", "/DeviceRGB"), ("/DCTDecode", "/DeviceGray")]
    assert pixels(reader.pages[1]).tobytes() == Image.open(io.BytesIO(second)).convert("RGB").tobytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.cbz", "m.pdf"]   # nothing unpacked, nothing half-done


@pytest.mark.parametrize("fmt,save,mode", [
    ("PNG", {}, "L"), ("PNG", {}, "RGBA"), ("WEBP", {"lossless": True}, "RGB"), ("TIFF", {}, "RGB"),
    ("TIFF", {"compression": "tiff_lzw"}, "L"), ("JPEG", {}, "CMYK")])
def test_other_pictures_keep_every_pixel(tmp_path, fmt, save, mode):
    data = picture(fmt, (21, 13), mode, (10, 20, 30, 40) if mode == "CMYK" else (10, 200, 30, 128), **save)
    archive(tmp_path / "m.zip", [(f"page.{fmt.lower()}", data)])
    scans.make_pdf(tmp_path / "m.zip", tmp_path / "m.pdf")
    got = pixels(PdfReader(tmp_path / "m.pdf").pages[0])
    want = Image.open(io.BytesIO(data))
    if mode == "RGBA":                                                    # transparency is laid on white
        want = Image.alpha_composite(Image.new("RGBA", want.size, "white"), want.convert("RGBA"))
    want = want.convert("L" if mode == "L" else "RGB")
    assert got.mode == want.mode and got.tobytes() == want.tobytes()


@pytest.mark.skipif(not features.check_codec("jpg_2000"), reason="this Pillow has no JPEG 2000")
def test_jpeg_2000_pages_when_pillow_reads_them(tmp_path, monkeypatch):
    data = picture("JPEG2000", (16, 16))
    archive(tmp_path / "m.cbz", [("01.jp2", data)])
    scans.make_pdf(tmp_path / "m.cbz", tmp_path / "m.pdf")
    got = pixels(PdfReader(tmp_path / "m.pdf").pages[0])
    assert got.tobytes() == Image.open(io.BytesIO(data)).convert("RGB").tobytes()
    monkeypatch.setattr(scans.features, "check_codec", lambda codec: False)
    with pytest.raises(scans.ScanError, match="JPEG 2000"):
        scans.make_pdf(tmp_path / "m.cbz", tmp_path / "again.pdf")
    assert not (tmp_path / "again.pdf").exists()


def test_archives_that_are_refused(tmp_path, monkeypatch):
    page = picture("JPEG", (8, 8))
    tiff = io.BytesIO()
    Image.new("L", (4, 4)).save(tiff, "TIFF", save_all=True, append_images=[Image.new("L", (4, 4), 9)])
    cases = [
        ([("../p1.jpg", page)], "outside the archive"),
        ([("/abs/p1.jpg", page)], "outside the archive"),
        ([("a\\..\\p1.jpg", page)], "outside the archive"),
        ([("C:/p1.jpg", page)], "outside the archive"),
        ([("notes.txt", b"x"), ("__MACOSX/p1.jpg", page)], "no page images"),
        ([("p1.jpg", b"not a picture")], "p1.jpg can't be read"),
        ([("p1.tif", tiff.getvalue())], "more than one picture"),
        ([("p1.jpg", page)], "encrypted"),
    ]
    for i, (entries, says) in enumerate(cases):
        archive(tmp_path / f"{i}.cbz", entries)
        if says == "encrypted":                    # zipfile can't write one, so the flag is set by hand
            raw = bytearray((tmp_path / f"{i}.cbz").read_bytes())
            raw[6] |= 1
            raw[raw.index(b"PK\x01\x02") + 8] |= 1
            (tmp_path / f"{i}.cbz").write_bytes(bytes(raw))
        with pytest.raises(scans.ScanError, match=says):
            scans.make_pdf(tmp_path / f"{i}.cbz", tmp_path / f"{i}.pdf")
    (tmp_path / "x.cbz").write_bytes(b"PK but not really a zip")
    with pytest.raises(scans.ScanError, match="can't be read"):
        scans.make_pdf(tmp_path / "x.cbz", tmp_path / "x.pdf")

    archive(tmp_path / "ok.cbz", [("p1.jpg", page), ("p2.jpg", page)])
    for limit, value, says in (("MAX_ENTRY", 100, "a page is larger"), ("MAX_TOTAL", len(page) + 1, "come to more"),
                               ("MAX_PAGES", 1, "more than 1 pages"), ("MAX_ENTRIES", 1, "more than 1 entries"),
                               ("MAX_PIXELS", 63, "8×8 pixels"), ("MAX_PDF", len(page) + 1, "PDF would be larger")):
        with monkeypatch.context() as m:
            m.setattr(scans, limit, value)
            with pytest.raises(scans.ScanError, match=says):
                scans.make_pdf(tmp_path / "ok.cbz", tmp_path / "ok.pdf")
    assert not [p for p in tmp_path.iterdir() if p.suffix in (".pdf", ".part")]   # nothing left of a refused one
    assert scans.make_pdf(tmp_path / "ok.cbz", tmp_path / "ok.pdf") == 2


def test_an_entry_is_read_no_further_than_its_limit(tmp_path, monkeypatch):
    """A header that understates an entry's size doesn't get more than MAX_ENTRY bytes read."""
    page = picture("PNG", (30, 30))
    archive(tmp_path / "m.cbz", [("p1.png", page)])
    with zipfile.ZipFile(tmp_path / "m.cbz") as zf:
        info = zf.infolist()[0]
        monkeypatch.setattr(scans, "MAX_ENTRY", len(page) - 1)
        with pytest.raises(scans.ScanError, match="larger than"):
            scans.read(zf, info)
