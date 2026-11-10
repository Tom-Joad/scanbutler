"""Uploaded images: limits, 8-bit conversion and the PDF made from them."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import pikepdf
from PIL import Image, ImageOps

# What one upload may hold. The size limit alone is no bound: a few MB of
# compressed pixels (or a multi-page TIFF) decompress to many gigabytes.
MAX_FRAMES = 200
MAX_MPIXELS = 4000
MAX_PAGE_INCHES = 16.5

_GREY_MODES = ("1", "L", "LA")
_WIDE_MODES = ("I", "I;16", "I;16L", "I;16B", "I;16N")


class ImageRefused(ValueError):
    """The image is too big to convert; the message says why."""


def check(src: Path) -> int:
    """Number of frames of the image; raises ImageRefused when it is too big.

    Reads headers only, so it can run before anything is decoded.
    """
    try:
        with Image.open(src) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > MAX_FRAMES:
                raise ImageRefused(f"the image has {frames} frames; at most {MAX_FRAMES} are accepted")
            pixels = 0
            for index in range(frames):
                image.seek(index)
                pixels += image.width * image.height
                if pixels > MAX_MPIXELS * 10**6:
                    raise ImageRefused(f"the image has more than {MAX_MPIXELS} megapixels in total")
            return frames
    except Image.DecompressionBombError as exc:
        raise ImageRefused("a frame of the image has too many pixels") from exc


def to_8bit(image: Image.Image) -> Image.Image:
    """Greyscale (`L`) or `RGB`, with 16-bit greyscale scaled down, not clipped.

    Pillow's convert("L") clips 16-bit values at 255, which turns a 16-bit
    scan into an almost white page.
    """
    if image.mode in _WIDE_MODES:
        # point() keeps the mode; the values fit 8 bits afterwards, so convert() doesn't clip.
        return image.point(lambda value: value * (1 / 256)).convert("L")
    return image.convert("L" if image.mode in _GREY_MODES else "RGB")


def to_pdf(src: Path, dst: Path) -> None:
    """A PDF of the image's pages, upright, at its stated or an A4-like resolution.

    Frames are converted and written one at a time, so memory holds one frame.
    """
    frames = check(src)
    with Image.open(src) as image:
        image.seek(0)
        dpi = image.info.get("dpi")
        longest = max(image.size)
        stated = float(dpi[0]) if dpi and dpi[0] else 0.0
        # A scanner states its resolution; phones and cameras write 72 dpi or
        # nothing, which would make a 4000-pixel photo a 55-inch page that OCR
        # reads as tiny print. Trust the value only if it gives a page up to A3
        # (16.5 in); otherwise take the long side as A4 (11.7 in).
        if stated >= 100 and longest / stated <= MAX_PAGE_INCHES:
            resolution = stated
        else:
            resolution = min(600.0, max(150.0, longest / 11.7))
        tmp = dst.with_name(dst.name + ".tmp")
        if frames == 1:
            to_8bit(ImageOps.exif_transpose(image)).save(tmp, "PDF", resolution=resolution)
        else:
            _write_frames(image, frames, resolution, tmp)
    tmp.replace(dst)


def _write_frames(image: Image.Image, frames: int, resolution: float, dst: Path) -> None:
    parts = []
    try:
        with ExitStack() as stack, pikepdf.new() as merged:
            for index in range(frames):
                image.seek(index)
                part = dst.with_name(f"{dst.name}.page{index}")
                parts.append(part)
                to_8bit(ImageOps.exif_transpose(image)).save(part, "PDF", resolution=resolution)
                # pikepdf copies pages lazily: the part stays open until the save.
                merged.pages.extend(stack.enter_context(pikepdf.open(part)).pages)
            merged.save(dst)
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
