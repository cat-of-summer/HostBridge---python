"""Build the application icon from the source artwork.

Run through a container rather than against a local interpreter -- Pillow is needed for
exactly this one job, and the project itself must not grow a dependency on it::

    docker run --rm -v "<repo>:/work" -v "<artwork dir>:/src:ro" python:3.12-slim \
        sh -c "pip install --no-cache-dir pillow && python /work/build/make_icon.py /src/<file>"

The artwork is a wide bridge drawn on white with generous margins, so the circle is built by
*fitting* the trimmed drawing inside it rather than by cropping a circle out of the middle:
a centre crop cuts the ends of the span and the road off the bottom.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent

#: Rendered at this size and downscaled from it. Every smaller size is produced by LANCZOS
#: from one high-resolution master, so the tray and the taskbar cannot disagree about the
#: shape of the mark.
MASTER = 1024

#: How much of the circle's diameter the drawing may occupy. The bridge is much wider than
#: it is tall, so it is its width that touches this bound, and a little air on both sides is
#: what keeps it from reading as clipped.
FIT = 0.80

#: Supersampling for the circular mask: PIL's ellipse has no antialiasing of its own, and a
#: hard-edged 1024px circle is visibly jagged once it lands in a 32px tray slot.
SUPERSAMPLE = 4

BACKGROUND = (255, 255, 255, 255)

#: A hairline of the drawing's own dark grey around the disc. Without it a white circle is
#: invisible against a light taskbar or a light tray, which is half the machines this runs on.
RING_COLOUR = (67, 67, 67, 255)
RING_WIDTH = 0.02

#: Anything this close to white counts as background when the drawing is trimmed. JPEG does
#: not leave a clean 255,255,255 behind, so an exact comparison trims nothing.
WHITE_TOLERANCE = 12

PNG_SIZE = 512
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

DEFAULT_SOURCE = "/src/3217323-bruckensymbol-illustration-kostenlos-vektor.jpg"


def trim(image: Image.Image) -> Image.Image:
    """Drop the white margin, so the drawing itself is what gets fitted."""
    flat = image.convert("RGB")
    white = Image.new("RGB", flat.size, (255, 255, 255))
    difference = ImageChops.difference(flat, white).convert("L")
    mask = difference.point(lambda value: 255 if value > WHITE_TOLERANCE else 0)
    box = mask.getbbox()
    return flat.crop(box) if box else flat


def circular_mask(size: int) -> Image.Image:
    big = Image.new("L", (size * SUPERSAMPLE, size * SUPERSAMPLE), 0)
    ImageDraw.Draw(big).ellipse((0, 0, big.size[0] - 1, big.size[1] - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def build(source: Path) -> Image.Image:
    art = trim(Image.open(source))

    limit = int(MASTER * FIT)
    scale = min(limit / art.width, limit / art.height)
    fitted = (max(1, round(art.width * scale)), max(1, round(art.height * scale)))
    art = art.resize(fitted, Image.LANCZOS)

    canvas = Image.new("RGBA", (MASTER, MASTER), BACKGROUND)
    canvas.paste(art, ((MASTER - art.width) // 2, (MASTER - art.height) // 2))
    ring = max(1, round(MASTER * RING_WIDTH))
    ImageDraw.Draw(canvas).ellipse(
        (ring // 2, ring // 2, MASTER - 1 - ring // 2, MASTER - 1 - ring // 2),
        outline=RING_COLOUR,
        width=ring,
    )
    canvas.putalpha(circular_mask(MASTER))
    return canvas


def main(argv: list[str]) -> int:
    source = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_SOURCE)
    if not source.is_file():
        print(f"no such artwork: {source}", file=sys.stderr)
        return 1

    assets = ROOT / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    master = build(source)
    master.resize((PNG_SIZE, PNG_SIZE), Image.LANCZOS).save(assets / "hostbridge.png")
    # Each size written from the master rather than let the ICO writer downscale the 512:
    # its own resampling is nearest-neighbour and the 16px entry comes out as noise.
    master.save(
        assets / "hostbridge.ico",
        sizes=[(size, size) for size in ICO_SIZES],
    )
    print(f"wrote {assets / 'hostbridge.png'} and {assets / 'hostbridge.ico'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
