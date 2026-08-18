"""Live overlays for the 2.1" round panel (e.g. a clock or system stats over media).

Architecture (mirrors the official ID-COOLING app's Theme/GraphItem/M_Data):

  SOURCES (sensors.Metrics)  →  WIDGETS (widgets.*)  →  LAYOUT (layouts.*)
       named metrics              reusable renderers      ordered composition
                                    │
                                    ▼
              this module: compose one RGBA layer, diff vs the previous frame,
              and push the changed region via the persistent-overlay protocol.

Persistence mechanism (confirmed on the 2.1" rev C, ROM 89): one cmd 202
(colours) + one cmd 208 (mask) establishes a layer that the device keeps over
the playing video, so we only re-push when the frame actually changes.

Config is JSON at ``~/.config/lumi/overlay.json``: ``{"enabled", "layout"}``.
The legacy ``"preset"`` key is read as an alias of ``"layout"`` and is still
written back for backward compatibility with older callers.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from lumi import layouts, sensors
from lumi.log import logger
from lumi.sensors import Metrics
from lumi.widgets import DrawContext
from lumi.widgets.fonts import FontResolver

# Overlay config lives next to the rest of lumi's persisted state.
STATE_DIR = Path.home() / ".config" / "lumi"
CONFIG_FILE = STATE_DIR / "overlay.json"

DEFAULT_CONFIG: dict = {"enabled": True, "layout": "clock"}


# --- layout helpers --------------------------------------------------------

def get_layout(cfg: Optional[dict]):
    """Resolve the active Layout from a config dict (``layout`` or legacy
    ``preset`` key)."""
    cfg = cfg or {}
    return layouts.load_layout(cfg.get("layout") or cfg.get("preset"))


def layout_interval(cfg: Optional[dict]) -> float:
    return float(get_layout(cfg).interval)


# --- config ----------------------------------------------------------------

def load_config() -> dict:
    """Load the overlay config. Normalises the legacy ``preset`` key to
    ``layout`` and returns both (so older callers reading ``preset`` still
    work). Writes defaults on first run; resets on corruption."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            user = json.loads(f.read())
        if isinstance(user, dict):
            if "enabled" in user:
                cfg["enabled"] = bool(user["enabled"])
            lid = user.get("layout") or user.get("preset")
            if lid and lid in layouts.all_specs():
                cfg["layout"] = lid
    except FileNotFoundError:
        save_config(cfg)               # first run: persist defaults
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"overlay: {CONFIG_FILE} is corrupt ({e}); resetting to defaults.")
        save_config(cfg)
    except Exception as e:
        logger.warning(f"overlay: could not read {CONFIG_FILE}: {e}; using defaults.")
    # Back-compat alias: always surface the active layout as ``preset`` too.
    cfg["preset"] = cfg["layout"]
    return cfg


def save_config(cfg: dict) -> None:
    """Persist the config ATOMICALLY (temp file + rename)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    clean = {"enabled": bool(cfg.get("enabled", True)),
             "layout": cfg.get("layout", "clock")}
    # Keep the legacy key on disk too so downgrades/old tools still parse it.
    clean["preset"] = clean["layout"]
    tmp = str(CONFIG_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(clean, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def set_enabled(enabled: bool) -> None:
    cfg = load_config()
    cfg["enabled"] = bool(enabled)
    save_config(cfg)


def set_layout(layout_id: str) -> None:
    """Select the active overlay layout (persisted; picked up on next enable)."""
    if layout_id not in layouts.all_specs():
        logger.warning(f"overlay: unknown layout {layout_id!r}; ignoring.")
        return
    cfg = load_config()
    cfg["layout"] = layout_id
    save_config(cfg)


def set_preset(preset_id: str) -> None:
    """Back-compat alias for :func:`set_layout`."""
    set_layout(preset_id)


def list_layouts() -> list:
    """Layouts for the GUI dropdown / ``--list``: ``[{"id", "label"}, ...]``."""
    return layouts.list_layouts()


def list_presets() -> list:
    """Back-compat alias for :func:`list_layouts`."""
    return list_layouts()


# --- rendering -------------------------------------------------------------

def render_layer(cfg: dict, panel: int, now: datetime) -> Image.Image:
    """Render the active layout to a transparent RGBA layer. Shared by the
    device push path and the GUI preview (WYSIWYG). Polls a fresh metric
    snapshot each call (suitable for ~1 Hz preview refresh)."""
    layout = get_layout(cfg)
    metrics = Metrics()
    try:
        metrics.poll()
    except Exception as e:
        logger.warning(f"overlay: metric poll failed in render_layer: {e}")
    ctx = DrawContext(panel=panel, now=now, metrics=metrics, fonts=FontResolver())
    return layout.render(ctx)


# --- wire format -----------------------------------------------------------

def encode_mask(bgra: bytes, w: int, h: int, cap: int = 65000) -> bytes:
    """Visibility mask of the overlay, ported from the official app's
    ``smethod_33`` (SPACE_LCD ``洔.柶``).

    Walks the BGRA frame and emits one 5-byte record per run of *visible*
    pixels (alpha > 15): ``[start_index BE24][run_length BE16]``. No colour
    bytes — it just tells the device which regions to composite over the video.
    Sent as the cmd 208 (0xD0) payload alongside cmd 202 (the colours).
    """
    out = bytearray(w * h * 4)
    num5 = 0      # write position
    num2 = 0      # current run length
    flag = False
    num3 = 0      # run-start offset
    n = w * h
    for i in range(n):
        a = bgra[i * 4 + 3]
        if a > 15 and num2 < cap:
            if not flag:
                flag = True
                out[num5] = (i >> 16) & 0xFF
                out[num5 + 1] = (i >> 8) & 0xFF
                out[num5 + 2] = i & 0xFF
                num3 = num5
            num2 += 1
        elif flag:
            out[num3 + 3] = (num2 >> 8) & 0xFF
            out[num3 + 4] = num2 & 0xFF
            flag = False
            num2 = 0
            num5 += 5
    if flag:
        out[num3 + 3] = (num2 >> 8) & 0xFF
        out[num3 + 4] = num2 & 0xFF
        num5 += 5
    result = bytes(out[:num5])
    # The device's cmd 208 mask parser has a finite buffer. Empirically, a
    # layout near/above ~400 records / ~2000 bytes starts dropping regions (the
    # 'sysstats' layout with a full 360° arc ring blew past this and only the
    # temperature rendered). Warn so over-budget layouts are easy to spot.
    nrec = num5 // 5
    if nrec > 400:
        logger.warning(
            f"overlay mask is large ({nrec} records, {len(result)} bytes); "
            f"the device may drop some regions. Avoid full-circle arcs/rings "
            f"(each scan line through a ring is a record) and prefer text/bars.")
    return result


# --- renderer --------------------------------------------------------------

class OverlayRenderer:
    """Renders one overlay frame and pushes the changed region to the device.

    Maintains the previous RGBA layer to skip re-pushing identical frames. Call
    ``tick(dev)`` once per refresh interval. ``reset()`` drops history (e.g.
    after the base media changes) so the next tick re-pushes the full overlay.
    """

    def __init__(self, cfg: dict, panel: int = 480):
        self.panel = panel
        self.layout = get_layout(cfg)
        self.metrics = Metrics()
        self.fonts = FontResolver()
        self._prev: Optional[np.ndarray] = None  # previous layer (H,W,4) uint8
        self._ticks = 0
        try:
            self.metrics.poll()  # prime so the first frame has real data
        except Exception as e:
            logger.warning(f"overlay: initial metric poll failed: {e}")

    @property
    def interval(self) -> float:
        return float(self.layout.interval)

    def reset(self) -> None:
        self._prev = None

    def tick(self, dev) -> bool:
        """Render one frame and push it if changed. Returns True if pushed."""
        self._ticks += 1
        try:
            self.metrics.poll()
        except Exception as e:
            logger.warning(f"overlay: metric poll failed: {e}")
        now = datetime.now()
        ctx = DrawContext(panel=self.panel, now=now,
                          metrics=self.metrics, fonts=self.fonts)
        new = np.asarray(self.layout.render(ctx))
        changed = self._prev is None or bool(np.any(new != self._prev))
        # Safety re-establishment ~once a minute, in case the overlay layer was
        # cleared externally (media change, glitch).
        due = (self._ticks % 60 == 0)
        self._prev = new
        if not (changed or due):
            return False
        try:
            dev.push_overlay_frame(Image.fromarray(new, mode="RGBA"))
            logger.info("overlay refreshed (cmd 202 + 208 mask)")
            return True
        except OSError:
            # The serial link is gone (USB drop, port closed, ...). Propagate so
            # the caller can stop the loop and reconnect, instead of retrying
            # every tick — spamming a dead device churns the serial port and can
            # wedge the panel. (serial.SerialException is an OSError subclass.)
            self._prev = None
            raise
        except Exception as e:
            logger.warning(f"overlay: push failed: {e}")
            self._prev = None  # force a full re-push next tick
            return False


# --- loop (thread wrapper for the daemon / CLI) ----------------------------

class OverlayLoop:
    """Drives an ``OverlayRenderer`` on a background thread.

    The caller owns the ``Device`` (and thus the serial port); the loop only
    reads ``dev.lcd`` via ``dev.push_overlay_frame``. Stop the loop before using
    the device for other serial work or closing it.
    """

    def __init__(self, dev, cfg: Optional[dict] = None, panel: int = 480):
        self.dev = dev
        cfg = cfg if cfg is not None else load_config()
        self.renderer = OverlayRenderer(cfg, panel)
        self.interval = float(self.renderer.interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lumi-overlay", daemon=True)
        self._thread.start()
        logger.info("overlay loop started")

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        logger.info("overlay loop stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.renderer.tick(self.dev)
            except Exception as e:  # never let the loop die silently
                logger.warning(f"overlay tick error: {e}")
            # Wait the interval, but wake promptly on stop.
            self._stop.wait(self.interval)
