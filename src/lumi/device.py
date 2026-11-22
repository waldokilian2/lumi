"""High-level rev C USB-CDC LCD panel driver.

Wraps the vendored LcdCommRevC transport with a clean API and sensible defaults
for the 2.1" round panel (480x480). The transport is resolution-agnostic and
accepts other rev C sizes via width/height, but Lumi is only tested on the 2.1".
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

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
    from serial.tools.list_ports import comports
    # Detection must be read-only. auto_detect_com_port() also opens the wake
    # interface, so polling it during handoff repeatedly wakes the panel.
    return LcdCommRevC._get_awake_com_port(comports())


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

        from lumi import usb
        for attempt in range(2):
            logger.info(f"Opening rev C panel on {com_port} ({width}x{height})")
            lcd = None
            try:
                lcd = LcdCommRevC(com_port=com_port, display_width=width,
                                  display_height=height, update_queue=None)
                self.lcd = lcd
                if reset:
                    lcd.Reset()
                lcd.InitializeComm()  # HELLO handshake
                lcd.ScreenOn()
                lcd.SetBrightness(brightness)
                lcd.SetOrientation(Orientation.PORTRAIT)
                return
            except Exception as e:
                if lcd is not None:
                    try:
                        lcd.closeSerial()
                    except OSError:
                        pass
                if (attempt == 0 and isinstance(e, OSError) and usb.recoverable(e)
                        and usb.reset_panel_usb(com_port)):
                    com_port = self._wake_and_find() or com_port
                    continue
                raise

    # --- construction helpers -------------------------------------------------

    @staticmethod
    def _wake_and_find() -> Optional[str]:
        awake = find_panel()
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
            awake = find_panel()
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
        from lumi import media, overlay
        from lumi.lcd.serialize import image_to_BGRA
        if isinstance(image, (str, Path)):
            from PIL import Image as _I
            image = _I.open(image)
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        bgra, _ = image_to_BGRA(image)
        mask = overlay.encode_mask(bgra, self.width, self.height)
        if not mask:
            # Keep one pixel in the rectangular frame's corner selected. It is
            # hidden by the round bezel and avoids the empty-mask firmware path.
            mask = b"\x00\x00\x00\x00\x01"
        # These settle times match the successful physical-panel diagnostic.
        # Short gaps can yield seq_png_init_sucess but leave the overlay hidden.
        # Drain queued replies without discarding them between protocol stages.
        self.lcd.serial_flush_input()
        media.send_cmd(self.lcd, 0xCA, len(bgra), b"", 0)
        init_reply = media.read_text(self.lcd, 2.0, flush_input=False)
        media.bulk_write(self.lcd, bgra)
        self.lcd.serial_flush_output()
        color_reply = media.read_text(self.lcd, 2.0, flush_input=False)
        # cmd 208: visibility mask (+ EF69 terminator)
        pkt = bytearray(mask) + b"\xef\x69"
        media.send_cmd(self.lcd, 0xD0, len(pkt), b"", 0)
        mask_init_reply = media.read_text(self.lcd, 0.3, flush_input=False)
        media.bulk_write(self.lcd, bytes(pkt))
        self.lcd.serial_flush_output()
        mask_reply = media.read_text(self.lcd, 3.0, flush_input=False)
        logger.info(f"overlay sent: {len(bgra)} color bytes, {len(mask) // 5} mask runs; "
                    f"panel replies: colors={color_reply!r}, mask={mask_reply!r}")
        for reply in (init_reply, color_reply, mask_init_reply, mask_reply):
            if any(word in reply.lower() for word in ("error", "fail", "invalid")):
                raise DeviceError(f"Panel rejected overlay: {reply}")

    def clear_overlay(self) -> None:
        """Hide the overlay using a single corner pixel behind the round bezel.

        Keep the mask nonempty: an empty cmd 208 mask can leave the firmware
        ignoring later overlay frames, even though serial writes succeed.
        """
        from lumi import media
        pkt = b"\x00\x00\x00\x00\x01\xef\x69"
        media.send_cmd(self.lcd, 0xD0, len(pkt), b"", 0)
        media.bulk_write(self.lcd, pkt)
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
