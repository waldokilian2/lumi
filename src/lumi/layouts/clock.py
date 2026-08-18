"""Built-in 'clock' layout — a big digital clock on a soft dark rounded plate.

Reproduces the legacy hand-rendered clock (TextWidget plate defaults match the
old geometry: pad ~0.42×text height, radius ~text height/2), so it looks
identical to the previous implementation."""
from __future__ import annotations

SPEC = {
    "id": "clock",
    "label": "Clock",
    "interval": 1.0,
    "widgets": [
        {
            "type": "text",
            "data": "clock.time",
            "pos": [240, 240],
            "anchor": "center",
            "size": 100,
            "color": "#F0F4FA",
            "plate": True,
        },
    ],
}
