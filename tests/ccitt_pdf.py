"""A scan whose image is a CCITT fax image without /DecodeParms.

Valid PDF (the spec has defaults: K=0, Columns=1728), but pikepdf refuses to
decode it, so ocrmypdf's output check fails with exit code 4. Used by the
unit tests and, run as a script inside the image, by the smoke test. A line
of real text, like a fax header, makes ocrmypdf keep the image (redo and
plain modes) instead of rendering the page anew (scan mode):
    python3 ccitt_pdf.py out.pdf
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pikepdf
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1728, 2200  # 1728: the default /Columns
LINES = ["Laboratory report", "Leukocytes 6.2 /nl", "Fax test page"]


def make_ccitt_pdf(path: Path) -> None:
    img = Image.new("1", (WIDTH, HEIGHT), 1)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 64)
    except OSError:
        font = ImageFont.load_default(size=64)
    for i, line in enumerate(LINES):
        draw.text((150, 200 + i * 120), line, fill=0, font=font)
    buf = io.BytesIO()
    img.save(buf, "TIFF", compression="group3")
    tif = Image.open(io.BytesIO(buf.getvalue()))
    raw = buf.getvalue()
    data = b"".join(raw[o : o + c] for o, c in zip(tif.tag_v2[273], tif.tag_v2[279]))

    pdf = pikepdf.new()
    image = pikepdf.Stream(pdf, data)
    image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    image.Width, image.Height = WIDTH, HEIGHT
    image.BitsPerComponent, image.ColorSpace = 1, pikepdf.Name.DeviceGray
    image.Filter = pikepdf.Name.CCITTFaxDecode  # deliberately without /DecodeParms
    w, h = 612, 612 * HEIGHT / WIDTH
    page = pdf.add_blank_page(page_size=(w, h))
    font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica)
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image), Font=pikepdf.Dictionary(F1=font))
    header = f"BT /F1 9 Tf 20 {h - 14} Td (FAX 2026-10-04 Page 1/1) Tj ET"
    page.Contents = pdf.make_stream(f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q {header}".encode())
    pdf.save(path)


if __name__ == "__main__":
    make_ccitt_pdf(Path(sys.argv[1]))
