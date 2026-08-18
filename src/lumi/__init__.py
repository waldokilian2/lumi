"""lumi — driver library + CLI for rev C USB-CDC round LCD panels (2.1").

Vendor-forks the rev C transport from turing-smart-screen-python (GPL-3.0-or-later,
Copyright (C) 2021 Matthieu Houdebine et al.) and adds a media pipeline
(image / GIF / video), a round-preview desktop GUI, and device-storage support.
"""
from lumi.device import Device, DeviceError, find_panel

__all__ = ["Device", "DeviceError", "find_panel"]
__version__ = "0.1.0"
