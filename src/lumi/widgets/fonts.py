"""TrueType font resolution shared by all overlay widgets.

Extracted from ``overlay._font`` so every widget (and the legacy clock) shares
one cache and one fallback chain. Monospaced faces are preferred first so
clock digits don't jitter as they change.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import ImageFont

from lumi.log import logger

_FONT_DIRS = [
    "/usr/share/fonts/TTF",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/liberation",
    "/usr/share/fonts",
    str(Path.home() / ".local/share/fonts"),
    str(Path.home() / ".fonts"),
]
# Monospaced figures first (stable digit widths), then solid fallbacks.
_FONT_FILES = [
    "FantasqueSansMNerdFontMono-Bold.ttf",
    "DejaVuSansMono-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]


class FontResolver:
    """Resolves a TrueType font at a given pixel size, caching the result."""

    def __init__(self) -> None:
        self._cache: dict = {}

    def get(self, size: int):
        size = max(1, int(size))
        if size in self._cache:
            return self._cache[size]
        for name in _FONT_FILES:
            for d in _FONT_DIRS:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    try:
                        f = ImageFont.truetype(p, size)
                        self._cache[size] = f
                        return f
                    except Exception:  # pragma: no cover - corrupt font
                        pass
        # First miss logs once, then the builtin is cached silently.
        if not getattr(self, "_warned", False):
            logger.warning("overlay: no TrueType font found; using PIL builtin.")
            self._warned = True
        try:
            f = ImageFont.load_default(size=size)  # type: ignore[call-arg]
        except TypeError:
            f = ImageFont.load_default()
        self._cache[size] = f
        return f
