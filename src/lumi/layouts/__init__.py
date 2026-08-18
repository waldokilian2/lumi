"""Overlay layouts — lumi's analogue of the official app's ``Theme``
(``UsbMonitorL/Theme.cs``): an ordered list of widgets with geometry and data
bindings, declared as a spec dict.

A layout is panel-agnostic in principle, but geometry is authored in 480-space
(Lumi targets the 2.1" round panel). Built-ins live here as Python modules
(so they can reference the palette); user overlays are JSON files dropped into
``~/.config/lumi/overlays/*.json`` — the safe, text-based replacement for the
official ``.turtheme`` binary format.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image

from lumi.log import logger
from lumi.widgets import DrawContext, make_widget

STATE_DIR = Path.home() / ".config" / "lumi"
USER_DIR = STATE_DIR / "overlays"


@dataclass
class Layout:
    """A composable overlay: an ordered list of widgets rendered alpha-over."""
    id: str
    label: str = ""
    interval: float = 1.0
    widgets: list = field(default_factory=list)

    @classmethod
    def from_spec(cls, spec: dict) -> "Layout":
        widgets = []
        for w in spec.get("widgets", []):
            try:
                widgets.append(make_widget(w))
            except Exception as e:
                logger.warning(f"layout {spec.get('id')}: bad widget {w!r}: {e}")
        return cls(
            id=str(spec.get("id", "layout")),
            label=str(spec.get("label", spec.get("id", "layout"))),
            interval=float(spec.get("interval", 1.0)),
            widgets=widgets,
        )

    @property
    def keys(self) -> List[str]:
        ks: List[str] = []
        for w in self.widgets:
            ks.extend(getattr(w, "keys", []) or [])
        return ks

    def render(self, ctx: DrawContext) -> Image.Image:
        canvas = Image.new("RGBA", (ctx.panel, ctx.panel), (0, 0, 0, 0))
        for w in self.widgets:
            try:
                layer = w.render(ctx)
            except Exception as e:
                logger.warning(f"widget {getattr(w, 'type', w)} render failed: {e}")
                continue
            if layer is not None:
                canvas = Image.alpha_composite(canvas, layer)
        return canvas


# --- built-in registry -----------------------------------------------------

def _load_builtins() -> Dict[str, dict]:
    """Import the built-in layout modules and collect their SPEC dicts."""
    from lumi.layouts import clock, sysstats, analog  # noqa: F401 (side-effect)
    out: Dict[str, dict] = {}
    for mod in (clock, sysstats, analog):
        spec = getattr(mod, "SPEC", None)
        if isinstance(spec, dict) and spec.get("id"):
            out[spec["id"]] = spec
    return out


def _user_specs() -> Dict[str, dict]:
    """Load user overlay JSON files from USER_DIR (best-effort)."""
    out: Dict[str, dict] = {}
    if not USER_DIR.is_dir():
        return out
    for p in sorted(USER_DIR.glob("*.json")):
        try:
            spec = json.loads(p.read_text())
            if isinstance(spec, dict) and spec.get("id"):
                out[str(spec["id"])] = spec
        except Exception as e:
            logger.warning(f"overlay: skipping user layout {p.name}: {e}")
    return out


def all_specs() -> Dict[str, dict]:
    """All layouts: built-ins overlaid by user files (user wins on id clash)."""
    specs = _load_builtins()
    specs.update(_user_specs())
    return specs


def list_layouts() -> list:
    """For the GUI dropdown / ``--list``: ``[{"id", "label"}, ...]``."""
    return [{"id": i, "label": s.get("label", i)} for i, s in all_specs().items()]


def get_spec(layout_id: Optional[str]) -> Optional[dict]:
    if not layout_id:
        return None
    return all_specs().get(layout_id)


def load_layout(layout_id: Optional[str]) -> Layout:
    """Resolve ``layout_id`` to a Layout, falling back to 'clock'."""
    spec = get_spec(layout_id)
    if spec is None:
        if layout_id:
            logger.warning(f"overlay: unknown layout {layout_id!r}; using 'clock'.")
        spec = get_spec("clock") or {"id": "clock", "label": "Clock", "widgets": []}
    return Layout.from_spec(spec)
