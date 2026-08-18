"""Built-in 'sysstats' layout — CPU load / temperature / memory over the media.

Showcases composing several reusable widgets (text + bars) bound to live Linux
sensor metrics via the sensors registry.

Design note: this layout deliberately avoids the ``arc`` widget. A circular arc
is prohibitively expensive in the device's scan-line visibility mask (cmd 208):
every scan line that crosses the ring is its own mask record, so a single
full-circle ring ≈ hundreds of records, enough to overflow the device's mask
buffer and make regions of the overlay silently drop out. Horizontal bars are
cheap (a handful of records each), so they carry the gauges here. See
``lumi.overlay.encode_mask`` for the budget details.
"""
from __future__ import annotations

SPEC = {
    "id": "sysstats",
    "label": "System Stats",
    "interval": 2.0,
    "widgets": [
        # CPU load, big — digits float over the video (no plate: the device
        # composites overlay pixels opaquely, so a plate would read as a solid
        # block, and a semi-transparent one removes the video colour entirely).
        {
            "type": "text",
            "data": "cpu.load",
            "format": "{value:.0f}{unit}",
            "pos": [240, 188],
            "anchor": "center",
            "size": 78,
            "color": "#FF6CB4",
        },
        # CPU load gauge (gradient fill only, no bg track).
        {
            "type": "bar",
            "data": "cpu.load",
            "rect": [150, 246, 180, 10],
            "fg": "gradient",
            "radius": 5,
        },
        # CPU temperature value.
        {
            "type": "text",
            "data": "cpu.temp",
            "format": "{value:.0f}{unit}",
            "pos": [240, 296],
            "anchor": "center",
            "size": 30,
            "color": "#FF8B7A",
        },
        # Memory load bar (fill only) + used amount.
        {
            "type": "bar",
            "data": "mem.load",
            "rect": [150, 344, 180, 12],
            "fg": "#5C8FFF",
            "radius": 6,
        },
        {
            "type": "text",
            "data": "mem.used",
            "format": "{value:.1f}{unit}",
            "pos": [240, 372],
            "anchor": "center",
            "size": 18,
            "color": "#6FD8FF",
        },
    ],
}
