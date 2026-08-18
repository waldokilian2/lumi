"""High-level rev C USB-CDC LCD panel driver.

Wraps the vendored LcdCommRevC transport with a clean API and sensible defaults
for the 2.1" round panel (480x480). The transport is resolution-agnostic and
accepts other rev C sizes via width/height, but Lumi is only tested on the 2.1".
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

from lumi.log import logger
from lumi.lcd.lcd_comm_rev_c import LcdCommRevC
from lumi.lcd.lcd_comm import Orientation

# Known sizes (portrait WxH), from turing-smart-screen-python display.py
SIZES = {
    '2.1"': (480, 480),
    '2.8"': (480, 480),
    '3.5"': (320, 480),
    '5"':   (480, 800),
    '8.8"': (480, 1920),
}


class DeviceError(RuntimeError):
    pass


def find_panel() -> Optional[str]:
    """Return the awake COM port if a panel is connected and awake, else None.

    The panel wakes from the dormant 1a86:ca21 interface to an awake PID
    (1d6b:0121 / 0525:a4a7, serial '20080411'). Use Device() to wake it
    automatically if only the dormant interface is present.
    """
    return LcdCommRevC.auto_detect_com_port()


class Device:
    """A connected, initialized rev C panel.

    Usage:
        with Device() as dev:          # auto-wakes, handshakes, 2.1" default
            dev.set_brightness(120)
            dev.show_image("photo.png")
    """

    def __init__(
        self,
        com_port: str = "AUTO",
        size: str = '2.1"',
        width: Optional[int] = None,
        height: Optional[int] = None,
        reset: bool = False,
        brightness: int = 50,
    ) -> None:
        if width is None or height is None:
            if size not in SIZES:
                raise DeviceError(f"Unknown size {size!r}; known: {list(SIZES)}")
            width, height = SIZES[size]
        self.size = size
        self.width, self.height = width, height

        if com_port == "AUTO":
            com_port = self._wake_and_find()
            if not com_port:
                raise DeviceError(
                    "No panel found. Connect a rev C USB-CDC LCD panel "
                    "( asleep 1a86:ca21 / awake 1d6b:0121 / 0525:a4a7 )."
                )

        logger.info(f"Opening rev C panel on {com_port} ({width}x{height})")
        self.lcd = LcdCommRevC(
            com_port=com_port,
            display_width=width,
            display_height=height,
            update_queue=None,
        )
        if reset:
            self.lcd.Reset()
        self.lcd.InitializeComm()  # HELLO handshake
        self.lcd.ScreenOn()
        self.lcd.SetBrightness(brightness)
        # Portrait by default; themes may override.
        self.lcd.SetOrientation(Orientation.PORTRAIT)

    # --- construction helpers -------------------------------------------------

    @staticmethod
    def _wake_and_find() -> Optional[str]:
        awake = LcdCommRevC.auto_detect_com_port()
        if awake:
            return awake
        # Force a wake cycle: open/close the dormant port up to 15x.
        from serial.tools import list_ports
        import serial
        sport = None
        for p in list_ports.comports():
            if (p.vid, p.pid) == (0x1A86, 0xCA21) or (p.product or "").lower() == "usbmonitor":
                sport = p.device
        if not sport:
            return None
        logger.info(f"Waking dormant panel on {sport} ...")
        for _ in range(15):
            try:
                serial.Serial(sport, 115200, timeout=1, rtscts=True).close()
            except Exception:
                pass
            time.sleep(1)
            awake = LcdCommRevC.auto_detect_com_port()
            if awake:
                time.sleep(1)
                return awake
        return None

    # --- public API -----------------------------------------------------------

    def set_brightness(self, level: int) -> None:
        """Backlight brightness as a percentage, 0-100 (scaled to 0-255 on the wire)."""
        self.lcd.SetBrightness(int(level))

    def screen_off(self) -> None:
        self.lcd.ScreenOff()

    def screen_on(self) -> None:
        self.lcd.ScreenOn()

    def show_image(self, image, x: int = 0, y: int = 0,
                   w: Optional[int] = None, h: Optional[int] = None,
                   rotate: int = 0) -> None:
        """Display an image (path or PIL.Image), auto-fit to the panel.

        rotate: degrees clockwise to pre-rotate the source image before
        fitting. Use this to compensate for how the panel is physically mounted
        (defaults to 0; set 90/180/270 if a given mount is rotated).
        """
        if isinstance(image, (str, Path)):
            image = Image.open(image)
        if image.mode != "RGB":
            image = image.convert("RGB")
        if rotate:
            # PIL.rotate is CCW, so negate for clockwise; expand keeps the full image
            image = image.rotate(-rotate, expand=True)
        w = w or self.width
        h = h or self.height
        if image.size != (w, h):
            image = image.resize((w, h))
        self.lcd.DisplayPILImage(image, x, y, w, h)

    def push_overlay_frame(self, image) -> None:
        """Establish/refresh the persistent overlay (e.g. the clock) over the
        playing media, using the official app's two-command sequence:

          * cmd 202 (0xCA) -- the overlay COLOURS (raw BGRA), with a short gap
            before the bulk payload (matches the official app).
          * cmd 208 (0xD0) -- the visibility MASK: regions where alpha > 15,
            so the device composites those pixels over the video and lets the
            video show through elsewhere.

        The overlay persists over a playing video; re-send when it changes.
        ``image`` is a full 480x480 PIL RGBA layer (transparent outside widgets).
        """
        import time
        from lumi import media, overlay
        from lumi.lcd.serialize import image_to_BGRA
        if isinstance(image, (str, Path)):
            from PIL import Image as _I
            image = _I.open(image)
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        bgra, _ = image_to_BGRA(image)
        mask = overlay.encode_mask(bgra, self.width, self.height)
        # cmd 202: overlay colours. A short settle before the bulk payload so the
        # device has allocated its receive buffer; kept small because a full
        # 480x480 BGRA frame (~900 KB) already takes noticeable time to stream,
        # and the analog clock relies on pushes landing roughly once a second.
        media.send_cmd(self.lcd, 0xCA, len(bgra), b"", 0)
        time.sleep(0.1)
        media.bulk_write(self.lcd, bgra)
        media.read_text(self.lcd, 1.0)
        # cmd 208: visibility mask (+ EF69 terminator)
        pkt = bytearray(mask) + b"\xef\x69"
        media.send_cmd(self.lcd, 0xD0, len(pkt), b"", 0)
        media.bulk_write(self.lcd, bytes(pkt))
        media.read_text(self.lcd, 1.0)

    def clear_overlay(self) -> None:
        """Remove the persistent overlay (e.g. when disabling it) by sending an
        empty visibility mask (cmd 208): the device composites no regions, so the
        underlying video shows through everywhere."""
        from lumi import media
        media.send_cmd(self.lcd, 0xD0, 2, b"", 0)
        media.bulk_write(self.lcd, b"\xef\x69")
        media.read_text(self.lcd, 1.0)

    def reset(self) -> None:
        self.lcd.Reset()

    def upload_video(self, path, dest_name: Optional[str] = None,
                     play: bool = True, rotate: Optional[int] = 0,
                     transcode: bool = True, progress=None) -> Optional[str]:
        """Transcode (optional) and upload a video to the panel, then play it.

        rotate: degrees CW to pre-rotate (compensate panel mounting); 0 = as-is.
        transcode: re-encode to panel format (libx264/480²/24fps). Set False if
        the input is already a panel-ready MP4.
        progress: optional callback(stage, frac) for GUI progress feedback.
        """
        import os
        from lumi import media
        # Default dest name = a unique timestamped name so the device file, the
        # local preview copy, and "now playing" stay in sync by name. Caller can
        # pass dest_name to override.
        if dest_name is None:
            dest_name = media.default_upload_name()
        mp4 = str(path)
        tmp = None
        try:
            if transcode:
                # Cache the transcode so re-applying the same source skips ffmpeg.
                if progress:
                    progress("Transcoding…", -1.0)
                mp4 = str(media.transcode_cached(path, (self.width, self.height), rotate or 0))
            return media.upload_video(self, mp4, dest_name, play, progress=progress)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def upload_image(self, path, dest_name: Optional[str] = None,
                     play: bool = True, rotate: int = 0) -> Optional[str]:
        """Convert + upload an image to the panel's image library, then show it.

        Mirrors upload_video: the image is converted to a panel-ready JPG, stored
        on the device (/mnt/SDCARD/img/) via cmd 111, and shown via cmd 140.
        """
        from lumi import media
        if dest_name is None:
            dest_name = media.default_upload_name("jpg")
        return media.upload_image(self, path, dest_name, play, rotate=rotate)

    # --- context manager ------------------------------------------------------

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.lcd.closeSerial()
        except Exception:
            pass
