"""Overlay widget framework — lumi's analogue of the official app's
``GraphItem`` hierarchy (``UsbMonitorL/GraphItem.cs`` and subclasses).

A ``Widget`` renders onto a transparent full-panel RGBA layer; a ``Layout``
composites many widgets (alpha-over) into one layer that the existing overlay
engine diffs and pushes to the device via cmd 202 + cmd 208.

Design rules:
  * Widgets never touch the device or the serial port — pure rendering.
  * A widget binds to a metric *by key*; data flows in via ``DrawContext.metrics``.
  * Ink that moves (digits, bar fills) is drawn on an opaque plate so repainting
    is idempotent and never ghosts under source-over compositing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from PIL import Image

from lumi.widgets.fonts import FontResolver

# Brand palette — kept in sync with theme.py (the GUI stylesheet) by hand so
# built-in overlays stay on-brand without importing PySide6 here.
ACCENT_STOPS: List[Tuple[float, str]] = [
    (0.0, "#6FD8FF"),
    (0.1667, "#5C8FFF"),
    (0.3333, "#7A63FF"),
    (0.5, "#D55BFF"),
    (0.6667, "#FF6CB4"),
    (0.8333, "#FF8B7A"),
    (1.0, "#FFB44C"),
]
ACCENT = "#D55BFF"
INK = "#F0F4FA"      # default text colour
MUTED = "#8A8A96"
PLATE_RGB = "#0C0E16"
PLATE_ALPHA = 195


# --------------------------------------------------------------------------- colour

def hex_to_rgb(c) -> Optional[Tuple[int, int, int]]:
    if c is None:
        return None
    if isinstance(c, (tuple, list)):
        return tuple(int(x) for x in c[:3])  # type: ignore[return-value]
    s = str(c).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError):
        return None


def gradient_image(w: int, h: int, vertical: bool = False,
                   stops: Optional[List[Tuple[float, str]]] = None) -> Image.Image:
    """An RGB gradient image of the brand sweep, resized to (w, h)."""
    import numpy as np
    stops = stops if stops is not None else ACCENT_STOPS
    stops = sorted(stops, key=lambda p: p[0])
    xs = [p for p, _ in stops]
    cols = [hex_to_rgb(c) for _, c in stops]
    pts = np.linspace(0.0, 1.0, max(2, w if not vertical else h))
    out = np.zeros((len(pts), 3), dtype=np.uint8)
    for ch in range(3):
        out[:, ch] = np.interp(pts, xs, [c[ch] for c in cols])
    if vertical:
        base = Image.fromarray(np.repeat(out[:, None, :], h, axis=1), "RGB")
    else:
        base = Image.fromarray(np.repeat(out[None, :, :], h, axis=0), "RGB")
    return base.resize((max(1, w), max(1, h)))


# --------------------------------------------------------------------------- context

@dataclass
class DrawContext:
    """Everything a widget needs to render one frame."""
    panel: int
    now: datetime
    metrics: object                      # lumi.sensors.Metrics
    fonts: FontResolver = field(default_factory=FontResolver)


# --------------------------------------------------------------------------- base + registry

REGISTRY: Dict[str, type] = {}


def register(cls):
    """Class decorator: register a Widget subclass by its ``type``."""
    REGISTRY[cls.type] = cls
    return cls


def make_widget(spec: dict) -> "Widget":
    """Instantiate a widget from a layout spec dict ``{"type": ..., ...}``."""
    spec = dict(spec)
    t = spec.pop("type", None)
    cls = REGISTRY.get(t)  # type: ignore[arg-type]
    if cls is None:
        raise ValueError(f"unknown widget type {t!r}")
    return cls(**spec)


class Widget:
    """Base widget. Subclasses read config via ``self._g(key, default)`` and
    bind a metric via ``self.data`` / ``self._sample(ctx)``."""
    type = "base"

    def __init__(self, data: Optional[str] = None, **cfg):
        self.cfg = cfg
        self.data = data

    def _g(self, key, default=None):
        return self.cfg.get(key, default)

    @property
    def keys(self) -> List[str]:
        return [self.data] if self.data else []

    def _sample(self, ctx: DrawContext):
        return ctx.metrics.get(self.data) if self.data else None

    def render(self, ctx: DrawContext) -> Optional[Image.Image]:
        raise NotImplementedError


def _empty_layer(panel: int) -> Image.Image:
    return Image.new("RGBA", (panel, panel), (0, 0, 0, 0))


# Pull the concrete widgets in so they self-register on import.
from lumi.widgets import primitives  # noqa: E402,F401  (side-effect: registry)
