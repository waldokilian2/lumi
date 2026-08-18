"""Built-in 'analog' layout — an analog clock (hands) with the date beneath.

Showcases the AnalogClockWidget consuming the clock.* rate metrics.

No clock face is drawn: the device composites overlay pixels opaquely (no
alpha-blend), so even a semi-transparent face would render as a solid disc that
hides the video behind it. With a transparent face the hands + ticks float over
the playing media. Note this leaves the mask fairly fragmented (~440 records,
just over the conservative budget) because each long diagonal hand is one mask
record per scan line — ``encode_mask`` will log a warning if the device drops
regions; if that happens, shorten the hands or restore a face."""
from __future__ import annotations

SPEC = {
    "id": "analog",
    "label": "Analog Clock",
    "interval": 1.0,
    "widgets": [
        {
            "type": "analog",
            "center": [240, 240],
            "radius": 196,
            "tick": "#8A8A96",
            "hand": "#F0F4FA",
            "second": "#FF6CB4",
            "hand_width": 5,
        },
        {
            "type": "text",
            "data": "clock.date",
            "pos": [240, 372],
            "anchor": "center",
            "size": 22,
            "color": "#8A8A96",
        },
    ],
}
