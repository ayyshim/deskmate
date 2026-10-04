"""Screenshots of the desk for agents: one monitor or both, the pointer marked, regions zoomed.

Coordinates agents see and send are relative to the monitor they name, so a session never
has to know that monitor 2 starts at x=1280.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from . import config, desk

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def monitor_box(monitor: int) -> tuple[int, int, int, int]:
    count, w, h = config.monitors()
    if monitor == 0:
        return 0, 0, w * count, h
    if not 1 <= monitor <= count:
        raise ValueError(f"monitor must be 1–{count}, or 0 for all of them")
    x = (monitor - 1) * w
    return x, 0, x + w, h


def to_screen(monitor: int, x: float, y: float) -> tuple[int, int]:
    left, top, right, bottom = monitor_box(monitor)
    if not (0 <= x < right - left and 0 <= y < bottom - top):
        raise ValueError(f"({x}, {y}) is outside monitor {monitor} ({right - left}×{bottom - top})")
    return int(left + x), int(top + y)


def _font(size: int):
    try:
        return ImageFont.truetype(_FONT, size)
    except OSError:
        return ImageFont.load_default()


def _pointer(draw: ImageDraw.ImageDraw, x: float, y: float, scale: float = 1.0) -> None:
    r = 11 * scale
    draw.ellipse([x - r, y - r, x + r, y + r], outline=(230, 40, 40), width=max(2, int(2 * scale)))
    draw.line([x - r - 4, y, x + r + 4, y], fill=(230, 40, 40), width=1)
    draw.line([x, y - r - 4, x, y + r + 4], fill=(230, 40, 40), width=1)


def _jpeg(img: Image.Image, quality: int = 78) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


async def capture(monitor: int = 1, region: list[float] | None = None) -> tuple[bytes, str]:
    """JPEG bytes and a one-line caption."""
    png = await desk.screenshot_png()
    full = Image.open(io.BytesIO(png))
    left, top, right, bottom = monitor_box(monitor)
    try:
        px, py = await desk.cursor()
    except desk.DeskError:
        px = py = -100

    if region:
        if len(region) != 4:
            raise ValueError("region is [x1, y1, x2, y2] on that monitor")
        x1, y1, x2, y2 = (int(v) for v in region)
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        w, h = right - left, bottom - top
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
        if x2 - x1 < 8 or y2 - y1 < 8:
            raise ValueError("region is too small")
        crop = full.crop((left + x1, top + y1, left + x2, top + y2))
        scale = max(1.0, min(4.0, 1200 / crop.width, 900 / crop.height))
        img = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
        draw = ImageDraw.Draw(img, "RGBA")
        step = 10 if (x2 - x1) <= 160 else 25 if (x2 - x1) <= 400 else 50
        font = _font(11)
        for gx in range((x1 // step + 1) * step, x2, step):
            sx = (gx - x1) * scale
            draw.line([sx, 0, sx, img.height], fill=(0, 120, 255, 60), width=1)
            draw.text((sx + 2, 1), str(gx), fill=(0, 70, 200, 255), font=font)
        for gy in range((y1 // step + 1) * step, y2, step):
            sy = (gy - y1) * scale
            draw.line([0, sy, img.width, sy], fill=(0, 120, 255, 60), width=1)
            draw.text((2, sy + 1), str(gy), fill=(0, 70, 200, 255), font=font)
        mx, my = px - left, py - top
        if x1 <= mx < x2 and y1 <= my < y2:
            _pointer(draw, (mx - x1) * scale, (my - y1) * scale, scale)
        caption = f"monitor {monitor}, region ({x1},{y1})–({x2},{y2}) zoomed ×{scale:.1f}; grid labels are monitor coordinates"
        return _jpeg(img, 85), caption

    img = full.crop((left, top, right, bottom))
    draw = ImageDraw.Draw(img)
    if left <= px < right and top <= py < bottom:
        _pointer(draw, px - left, py - top)
    if monitor == 0 and img.width > 1600:
        img = img.resize((1600, int(img.height * 1600 / img.width)), Image.LANCZOS)
        count, w, _h = config.monitors()
        caption = f"all {count} monitors side by side, scaled to 1600 px wide (each monitor is {w} px); use monitor=1 or 2 for coordinates"
    else:
        if not monitor:
            caption = ""
        elif left <= px < right and top <= py < bottom:
            caption = f"monitor {monitor}, {right - left}×{bottom - top}; pointer marked in red at ({px - left}, {py - top})"
        else:
            caption = f"monitor {monitor}, {right - left}×{bottom - top}; the pointer is on another monitor"
    return _jpeg(img), caption
