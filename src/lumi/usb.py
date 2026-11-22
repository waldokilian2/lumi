"""Linux USB recovery for the panel belonging to a specific serial port."""

from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path

import serial

from lumi.log import logger

USBDEVFS_RESET = 0x5514
PANEL_IDS = {("1d6b", "0121"), ("1d6b", "0106"), ("0525", "a4a7")}
SYS_TTY = Path("/sys/class/tty")
USB_DEV = Path("/dev/bus/usb")
LOCK_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", str(Path.home() / ".cache"))) / "lumi"


def recoverable(error: OSError) -> bool:
    """A busy/exclusively locked port must never trigger a USB reset."""
    return isinstance(error, serial.SerialTimeoutException) or error.errno in (
        errno.EPIPE,
        errno.EIO,
        errno.ENODEV,
    )


def panel_usb_node(com_port: str):
    """Find only this tty's known panel, never another USB device by ID alone."""
    tty = Path(com_port).resolve().name
    device = (SYS_TTY / tty / "device").resolve()
    for parent in (device, *device.parents):
        if not (parent / "idVendor").is_file():
            continue
        identity = (
            (parent / "idVendor").read_text().strip().lower(),
            (parent / "idProduct").read_text().strip().lower(),
        )
        if identity not in PANEL_IDS:
            return None
        bus = int((parent / "busnum").read_text())
        number = int((parent / "devnum").read_text())
        return USB_DEV / f"{bus:03d}" / f"{number:03d}"
    return None


def reset_panel_usb(com_port: str, cooldown: float = 60) -> bool:
    """Reset a wedged panel once per cooldown across GUI/service restarts.

    Updated udev rules grant access to known panels; this never invokes sudo,
    saves credentials, or changes USB permissions itself.
    """
    try:
        node = panel_usb_node(com_port)
        if node is None:
            return False
        LOCK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (LOCK_DIR / "usb-reset.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.seek(0)
            try:
                previous = float(lock.read())
            except ValueError:
                previous = float("-inf")
            now = time.monotonic()
            if 0 <= now - previous < cooldown:
                logger.warning(
                    "Panel USB recovery cooldown active; waiting before another reset."
                )
                return False
            fd = os.open(str(node), os.O_WRONLY)
            try:
                logger.warning(
                    f"Resetting unresponsive panel USB connection: {com_port} ({node})"
                )
                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            finally:
                os.close(fd)
            lock.seek(0)
            lock.truncate()
            lock.write(str(time.monotonic()))
            lock.flush()
        time.sleep(1)  # allow CDC endpoints to re-enumerate before opening them
        return True
    except PermissionError:
        logger.error(
            "USB recovery needs the updated 99-lumi.rules installed; "
            "the panel USB node is not writable."
        )
    except BlockingIOError:
        logger.warning("Another Lumi process is already recovering the panel.")
    except (OSError, ValueError) as e:
        logger.warning(f"Panel USB reset failed: {e}")
    return False
