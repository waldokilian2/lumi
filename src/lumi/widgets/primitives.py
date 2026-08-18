"""Concrete overlay widgets (lumi's ``GraphItem`` subclasses).

Each renders onto a transparent full-panel RGBA layer; a Layout composites them.
Types are registered by their ``type`` string so layout specs (JSON or Python)
can instantiate them by name.

Widget set (mirrors the official app):
  text    → GraphItem (text/data)        bar   → GraphStatuBar
  arc     → GraphArchBar                 spark → GraphLine
  analog  → GraphClock                   image → GraphImage
  plate   → GraphImage (background)
"""
from __future__ import annotations

import math
import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw

from lumi.widgets import (
    ACCENT, INK, MUTED, PLATE_ALPHA, PLATE_RGB, DrawContext, Widget, gradient_image,
    hex_to_rgb, register,
)


def _layer(panel: int) -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (panel, panel), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _rounded_rect(draw, box, radius=0, **kw):
    """rounded_rectangle with a safe fallback to rectangle (Pillow < 8.2)."""
    try:
        draw.rounded_rectangle(box, radius=max(0, int(radius or 0)), **kw)
    except (AttributeError, TypeError):  # pragma: no cover
        draw.rectangle(box, **kw)


# --------------------------------------------------------------------------- text

_ANCHORS = {"center", "n", "s", "e", "w", "nw", "ne", "sw", "se"}


@register
class TextWidget(Widget):
    """Text or a formatted metric value, optionally on an opaque rounded plate.

    Reproduces the legacy clock plate geometry by default (pad ~0.42×text
    height, radius ~text height/2), so the built-in 'clock' layout looks
    identical to the previous hand-rendered clock.
    """
    type = "text"

    def __init__(self, data=None, **cfg):
        super().__init__(data=data, **cfg)

    def _text_string(self, ctx) -> Tuple[str, bool]:
        static = self._g("text")
        if static is not None:
            return str(static), True
        s = self._sample(ctx)
        if s is None:
            return str(self._g("placeholder", "--")), True
        if s.text:
            return s.text, True
        fmt = self._g("format", "{value:.0f}{unit}")
        try:
            return fmt.format(value=s.value, unit=s.unit, rate=s.rate), True
        except Exception:
            return f"{s.value}{s.unit}", True

    def render(self, ctx) -> Image.Image:
        panel = ctx.panel
        img, draw = _layer(panel)
        size = self._g("size", int(panel * 0.12))
        color = hex_to_rgb(self._g("color", INK)) or hex_to_rgb(INK)
        text, _ = self._text_string(ctx)
        font = ctx.fonts.get(size)

        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        tw, th = r - l, b - t
        ax, ay = self._g("pos", [panel // 2, panel // 2])
        ax, ay = int(ax), int(ay)
        anchor = self._g("anchor", "center")
        if anchor not in _ANCHORS:
            anchor = "center"

        # ink top-left target from anchor point, then draw origin (− bearing)
        if anchor == "center":
            itx, ity = ax - tw / 2, ay - th / 2
        elif anchor == "n":
            itx, ity = ax - tw / 2, ay
        elif anchor == "s":
            itx, ity = ax - tw / 2, ay - th
        elif anchor == "w":
            itx, ity = ax, ay - th / 2
        elif anchor == "e":
            itx, ity = ax - tw, ay - th / 2
        elif anchor == "nw":
            itx, ity = ax, ay
        elif anchor == "ne":
            itx, ity = ax - tw, ay
        elif anchor == "sw":
            itx, ity = ax, ay - th
        else:  # se
            itx, ity = ax - tw, ay - th

        plate = self._g("plate")
        if plate:
            pc = hex_to_rgb(plate.get("color", PLATE_RGB) if isinstance(plate, dict) else PLATE_RGB) \
                or hex_to_rgb(PLATE_RGB)
            pa = int(plate.get("alpha", PLATE_ALPHA) if isinstance(plate, dict) else PLATE_ALPHA)
            pad = int(plate.get("pad", max(6, int(th * 0.42))) if isinstance(plate, dict)
                      else max(6, int(th * 0.42)))
            radius = int(plate.get("radius", max(10, th // 2)) if isinstance(plate, dict)
                         else max(10, th // 2))
            _rounded_rect(draw, [itx - pad, ity - pad, itx + tw + pad, ity + th + pad],
                          radius=radius, fill=(pc[0], pc[1], pc[2], pa))

        draw.text((int(itx - l), int(ity - t)), text, font=font,
                  fill=(color[0], color[1], color[2], 255))
        return img


@register
class PlateWidget(Widget):
    """A plain rounded background plate (for grouping widgets)."""
    type = "plate"

    def render(self, ctx) -> Image.Image:
        panel = ctx.panel
        img, draw = _layer(panel)
        x, y, w, h = self._g("rect", [0, 0, panel, panel])
        x, y, w, h = int(x), int(y), int(w), int(h)
        color = hex_to_rgb(self._g("color", PLATE_RGB)) or hex_to_rgb(PLATE_RGB)
        alpha = int(self._g("alpha", PLATE_ALPHA))
        radius = int(self._g("radius", max(10, h // 4)))
        _rounded_rect(draw, [x, y, x + w, y + h], radius=radius,
                      fill=(color[0], color[1], color[2], alpha))
        return img


# --------------------------------------------------------------------------- bar

@register
class BarWidget(Widget):
    """Linear progress bar (horizontal or vertical) driven by a metric's rate."""
    type = "bar"

    def render(self, ctx) -> Image.Image:
        panel = ctx.panel
        img, draw = _layer(panel)
        x, y, w, h = self._g("rect", [0, 0, panel, 12])
        x, y, w, h = int(x), int(y), int(w), int(h)
        vertical = bool(self._g("vertical", False))
        rate = self._g("value")
        if rate is None:
            s = self._sample(ctx)
            rate = s.rate if s else 0.0
        rate = max(0.0, min(1.0, float(rate)))
        radius = int(self._g("radius", max(2, min(w, h) // 2)))
        # Background track is opt-in: only draw when 'bg' is given. The device
        # composites overlay pixels opaquely (no alpha-blend), so an opaque track
        # reads as a solid bar over the video — let layouts request it explicitly.
        bg = hex_to_rgb(self._g("bg"))
        if bg:
            _rounded_rect(draw, [x, y, x + w, y + h], radius=radius,
                          fill=(bg[0], bg[1], bg[2], 255))

        fg = self._g("fg", ACCENT)
        if rate <= 0:
            return img
        # Fill tile (solid or brand gradient), masked to the rounded bar.
        if fg == "gradient":
            tile = gradient_image(w, h, vertical=vertical).convert("RGBA")
        else:
            fc = hex_to_rgb(fg) or hex_to_rgb(ACCENT)
            tile = Image.new("RGBA", (w, h), (fc[0], fc[1], fc[2], 255))
        mask = Image.new("L", (w, h), 0)
        _rounded_rect(ImageDraw.Draw(mask), [0, 0, w - 1, h - 1], radius=radius, fill=255)
        if vertical:
            fh = max(1, int(h * rate))
            seg = tile.crop((0, h - fh, w, h))
            img.paste(seg, (x, y + h - fh), mask.crop((0, h - fh, w, h)))
        else:
            fw = max(1, int(w * rate))
            seg = tile.crop((0, 0, fw, h))
            img.paste(seg, (x, y), mask.crop((0, 0, fw, h)))
        return img


# --------------------------------------------------------------------------- arc

def _stroke_arc(draw, cx, cy, r, start_deg, end_deg, width, fill):
    """Draw a thick arc as a smoothed polyline (robust across Pillow versions)."""
    if end_deg <= start_deg:
        return
    span = end_deg - start_deg
    n = max(8, int(span))
    pts = []
    for i in range(n + 1):
        a = math.radians(start_deg + span * i / n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.line(pts, fill=fill, width=max(1, int(width)), joint="curve")


@register
class ArcWidget(Widget):
    """Circular arc progress gauge — a natural fit for the round panel."""
    type = "arc"

    def render(self, ctx) -> Image.Image:
        panel = ctx.panel
        img, draw = _layer(panel)
        cx, cy = self._g("center", [panel // 2, panel // 2])
        cx, cy = int(cx), int(cy)
        r = int(self._g("radius", panel // 3))
        width = int(self._g("width", 8))
        start = float(self._g("start", -90))
        sweep = float(self._g("sweep", 360))
        rate = self._g("value")
        if rate is None:
            s = self._sample(ctx)
            rate = s.rate if s else 0.0
        rate = max(0.0, min(1.0, float(rate)))

        # Background track is opt-in (see BarWidget). NOTE: a full 360° ring is
        # very expensive in the device's scan-line mask format (~2 records per
        # scan line × the ring's full height ≈ hundreds of records), enough to
        # overflow the device's mask buffer on its own. Prefer partial arcs or
        # bars, and only set 'bg' when you really need a full track.
        bg = hex_to_rgb(self._g("bg"))
        if bg:
            _stroke_arc(draw, cx, cy, r, 0, 360, width, (bg[0], bg[1], bg[2], 255))

        fg = self._g("fg", "gradient")
        if rate <= 0:
            return img
        end = start + sweep * rate
        if fg == "gradient":
            # Stroke the arc in short segments, sampling the brand gradient per
            # segment so the gauge sweeps cool→warm with its progress.
            import numpy as np
            span = end - start
            n = max(8, int(abs(span)))
            ramp = np.asarray(gradient_image(max(2, n), 1))[0]  # (n,3)
            for i in range(n):
                a0 = start + span * i / n
                a1 = start + span * (i + 1) / n
                px = ramp[min(i, n - 1)]
                _stroke_arc(draw, cx, cy, r, a0, a1, width,
                            (int(px[0]), int(px[1]), int(px[2]), 255))
        else:
            fc = hex_to_rgb(fg) or hex_to_rgb(ACCENT)
            _stroke_arc(draw, cx, cy, r, start, end, width,
                        (fc[0], fc[1], fc[2], 255))
        return img


# --------------------------------------------------------------------------- spark

@register
class SparkWidget(Widget):
    """A sparkline line graph from a metric's history."""
    type = "spark"

    def render(self, ctx) -> Image.Image:
        panel = ctx.panel
        img, draw = _layer(panel)
        x, y, w, h = self._g("rect", [0, 0, panel, 40])
        x, y, w, h = int(x), int(y), int(w), int(h)
        s = self._sample(ctx)
        hist = list(s.history) if s else []
        if len(hist) < 2:
            return img
        lo = self._g("min")
        hi = self._g("max")
        lo = min(hist) if lo is None else float(lo)
        hi = max(hist) if hi is None else float(hi)
        if hi <= lo:
            hi = lo + 1.0
        n = len(hist)
        pts = []
        for i, v in enumerate(hist):
            px = x + (w * i / (n - 1))
            py = y + h - (max(lo, min(hi, v)) - lo) / (hi - lo) * h
            pts.append((px, py))
        fillc = hex_to_rgb(self._g("fill")) if self._g("fill") else None
        if fillc:
            poly = pts + [(x + w, y + h), (x, y + h)]
            draw.polygon(poly, fill=(fillc[0], fillc[1], fillc[2], 90))
        lc = hex_to_rgb(self._g("fg", ACCENT)) or hex_to_rgb(ACCENT)
        lw = int(self._g("width", 2))
        draw.line(pts, fill=(lc[0], lc[1], lc[2], 255), width=lw, joint="curve")
        # current-value dot
        draw.ellipse([pts[-1][0] - lw, pts[-1][1] - lw, pts[-1][0] + lw, pts[-1][1] + lw],
                     fill=(lc[0], lc[1], lc[2], 255))
        return img


# --------------------------------------------------------------------------- analog clock

@register
class AnalogClockWidget(Widget):
    """An analog clock (hour/minute/second hands) from clock.* metrics."""
    type = "analog"

    def _hand(self, draw, cx, cy, frac, length, color, width):
        # 0 at top, clockwise. angle = -90 + frac*360 in standard (y-down) terms.
        a = math.radians(-90 + frac * 360)
        ex = cx + length * math.cos(a)
        ey = cy + length * math.sin(a)
        draw.line([(cx, cy), (ex, ey)], fill=color, width=max(1, int(width)),
                  joint="curve")

    def render(self, ctx) -> Image.Image:
        panel = ctx.panel
        img, draw = _layer(panel)
        cx, cy = self._g("center", [panel // 2, panel // 2])
        cx, cy = int(cx), int(cy)
        r = int(self._g("radius", panel // 2 - 20))

        face = self._g("face")
        if face:
            fc = hex_to_rgb(face.get("color", PLATE_RGB) if isinstance(face, dict) else face) \
                or hex_to_rgb(PLATE_RGB)
            fa = int(face.get("alpha", 120) if isinstance(face, dict) else 120)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                         fill=(fc[0], fc[1], fc[2], fa))

        tickc = hex_to_rgb(self._g("tick", MUTED)) or hex_to_rgb(MUTED)
        for i in range(12):
            a = math.radians(-90 + i * 30)
            major = (i % 3 == 0)
            inn = r - (10 if major else 6)
            out = r
            draw.line([(cx + inn * math.cos(a), cy + inn * math.sin(a)),
                       (cx + out * math.cos(a), cy + out * math.sin(a))],
                      fill=(tickc[0], tickc[1], tickc[2], 220), width=2 if major else 1)

        hr = ctx.metrics.get("clock.hour")
        mn = ctx.metrics.get("clock.minute")
        sec = ctx.metrics.get("clock.second")
        hc = hex_to_rgb(self._g("hand", INK)) or hex_to_rgb(INK)
        sc = hex_to_rgb(self._g("second", "#FF6CB4")) or hex_to_rgb("#FF6CB4")
        hw = int(self._g("hand_width", 4))
        if hr:
            self._hand(draw, cx, cy, hr.rate, r * 0.55, (hc[0], hc[1], hc[2], 255), hw)
        if mn:
            self._hand(draw, cx, cy, mn.rate, r * 0.78, (hc[0], hc[1], hc[2], 255), max(2, hw - 1))
        if sec and self._g("show_second", True):
            self._hand(draw, cx, cy, sec.rate, r * 0.85, (sc[0], sc[1], sc[2], 255), 2)
        draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(hc[0], hc[1], hc[2], 255))
        return img


# --------------------------------------------------------------------------- image

_IMAGE_CACHE: dict = {}


@register
class ImageWidget(Widget):
    """A static image (e.g. an icon), positioned by anchor."""
    type = "image"

    def render(self, ctx) -> Optional[Image.Image]:
        path = self._g("path")
        if not path or not os.path.isfile(path):
            return None
        size = self._g("size")
        key = (path, tuple(size) if size else None)
        src = _IMAGE_CACHE.get(key)
        if src is None:
            try:
                src = Image.open(path).convert("RGBA")
                if size:
                    src = src.resize((int(size[0]), int(size[1])))
                _IMAGE_CACHE[key] = src
            except Exception:
                return None
        panel = ctx.panel
        img = Image.new("RGBA", (panel, panel), (0, 0, 0, 0))
        ax, ay = self._g("pos", [panel // 2, panel // 2])
        anchor = self._g("anchor", "center")
        tw, th = src.size
        if anchor == "center":
            ox, oy = ax - tw // 2, ay - th // 2
        elif anchor == "nw":
            ox, oy = ax, ay
        else:
            ox, oy = ax - tw // 2, ay - th // 2
        img.paste(src, (int(ox), int(oy)), src)
        return img
