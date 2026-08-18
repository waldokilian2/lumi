# Lumi

**Lumi is the open-source replacement for ID-COOLING's LCD software on Linux.**

ID-COOLING's official customization software is **Windows-only** — Lumi brings
full control of the round display to Linux. Show images, GIFs, and videos on
your AIO cooler's screen, position them in a live round preview (drag / zoom /
rotate), then push them to the panel. Open-source and built natively for Linux.

![Lumi](ui_preview.png)

> **Built for:** the ID-COOLING **Space Series AIO** (SL240, SL360, …) — liquid
> CPU coolers with a built-in 2.1" round LCD. Also compatible with other rev C
> panels (SAMA / TURZX) and anything speaking the same USB-CDC serial protocol.

## Features

- **Live round preview** — load an image, GIF, or video, then **drag to pan,
  scroll to zoom**, and rotate (0 / 90 / 180 / 270°). What you see is exactly
  what shows on the device, corners clipped by the bezel.
- **One-click upload** — images, GIFs, and videos are transcoded (ffmpeg →
  480² H.264, 24 fps) and pushed to the panel's media library, then loop.
- **Stored-media browser** — list and play anything already on the device.
- **Brightness, screen on/off, and power off.**
- **Live overlay (clock)** — render a real-time clock over the playing media
  (the panel plays the video underneath; Lumi pushes a tiny overlay frame each
  second). Designed to extend to more widgets (gauges, stats) later.
- **Autostart + clean shutdown** — an optional daemon resumes your last media on
  boot and turns the panel off when the PC shuts down.
- **Persistent settings** — brightness and last media survive restarts.

## Supported panel

Lumi was built and tested against the **ID-COOLING Space Series AIO** (e.g.
SL240 / SL360) — the **2.1" round (480 × 480)** rev C USB-CDC panel built into
those coolers. The whole app is tailored to it: a live round preview, a
circle-masked media pipeline, and a 480² H.264 transcode.

The underlying rev C serial transport is resolution-agnostic (vendored from
[turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python)),
so other rev C sizes from the same family (2.8" / 3.5" / 5" / 8.8") *can* be
targeted with `lumi --size` / `--width` / `--height`, but they're **untested**
and the round-focused UI isn't designed for the rectangular panels.

The panel enumerates asleep as `1a86:ca21` ("UsbMonitor", serial `CT21INCH`);
Lumi wakes it and it re-enumerates as `1d6b:0121`. All of this is automatic.

## Install (Linux)

Clone or download this folder **anywhere**, then:

```bash
./install.sh
```

That's it. The install is self-contained under your home directory (a persistent
venv at `~/.local/share/lumi/venv`, with `lumi` placed on `~/.local/bin`), so you
can **move or delete this folder afterwards**. You'll get two sudo prompts:

1. A udev rule so the panel is accessible without root.
2. `loginctl enable-linger` so the daemon can start at boot.

Then `lumi` works in any shell (fish / bash / zsh) with no activation.

## Quick start

```bash
lumi gui                  # the desktop app (main interface)
lumi info                 # detect the panel + show sub-rev / ROM
lumi brightness 60        # 60 % backlight
lumi video clip.mp4       # transcode → upload → loop
lumi image photo.jpg      # convert → upload → show
lumi gif anim.gif         # GIF → MP4 → upload → loop
lumi ls video             # list videos on the device
lumi ls images            # list images on the device
lumi play SPACE_03.mp4    # play a stored clip
lumi --help               # everything else
```

### The GUI

`lumi gui` opens the main window:

- **Left** — the round preview. **Load Media…**, position it (drag / scroll /
  rotate), then **Apply to Panel**.
- **Right** — brightness, Screen On / Off / **Power Off**, and the list of stored
  videos (double-click to play).
- **Bottom** — a log of the serial traffic (ACKs, transcodes, errors).

The GUI and the daemon share one serial port: launching the GUI **pauses the
daemon** and takes over; closing the GUI **hands back to the daemon**, so
boot-resume and shutdown power-off keep working.

## Autostart + clean shutdown

The install enables a systemd **user** daemon that:

- On **boot**: wakes the panel and resumes the last media you applied.
- On **shutdown**: sends a power-off so the panel goes dark.

Logs: `journalctl --user -u lumi -f`. Control: `systemctl --user {start,stop,restart} lumi`.

## Uninstall

```bash
./uninstall.sh            # removes app + service + venv; keeps your settings
./uninstall.sh --purge    # also wipes ~/.config/lumi + ~/.local/share/lumi
```

It stops/disables the service, removes the `lumi` command and the persistent
venv, and (with sudo) the udev rule. Your brightness/last-media settings are kept
by default so a reinstall resumes where you left off; `--purge` wipes them.

## Orientation

2.1" round panels are typically mounted rotated 90° CCW, so Lumi pre-rotates
content clockwise by default (`--rotate 90` on the CLI; a dropdown in the GUI).
Override with `--rotate 0|90|180|270` if your panel is mounted differently.

## Live overlay (clock)

Lumi can render a **live clock over a playing video** — the same feature the
official Windows app offers on this panel.

The mechanism (reverse-engineered from the official app and verified on the 2.1"
rev C, ROM 89): while a video plays from device storage, Lumi sends two commands
that establish a **persistent overlay layer** on top of it —

* **cmd 202 (0xCA)** — the overlay *colours* (the full 480×480 frame, raw BGRA);
* **cmd 208 (0xD0)** — the *visibility mask* (the regions where the overlay is
  opaque, i.e. where the clock is), so the video shows through everywhere else.

The overlay persists until re-sent, so the clock only needs refreshing when its
digits change (once a minute for `HH:MM`). It runs from the daemon, so it stays
on after you close the GUI.

```bash
lumi overlay            # play the last video + run the clock overlay (Ctrl-C to stop)
lumi overlay --bg IMG   # clock over a static image instead of video
lumi overlay --enable   # enable the overlay for the daemon, then exit
lumi overlay --disable  # disable it, then exit
```

- **GUI:** tick *Show clock overlay*; drawn live on the panel and mirrored in the
  round preview.
- **Config:** `~/.config/lumi/overlay.json` — format, position, font size,
  colour, and the plate behind the digits:

  ```json
  {
    "enabled": true,
    "interval": 1.0,
    "widgets": [
      { "type": "clock", "format": "%H:%M", "x": 240, "y": 240,
        "size": 60, "color": "#FFFFFF", "plate_alpha": 255 }
    ]
  }
  ```

The widget list is designed to grow — a `"type": "clock"` is the first entry,
and the same framework can later carry gauges/stats (CPU, GPU, …).

> Technical note: the overlay colours/mask commands (cmd 202/208) are distinct
> from the *primary* bitmap command (cmd 200/0xC8), which the video decoder
> output covers. Sending cmd 200 over a playing video does nothing visible and
> must not be hammered (it can desync the panel — recover with `sudo usbreset
> 1d6b:0121` or a replug).

## How media is stored (power-user notes)

The device runs Linux and stores media in its onboard flash:

```
/mnt/SDCARD/video/   video library (clips loop on play)
/mnt/SDCARD/img/     image library
/mnt/UDISK/...       USB-disk media (if present)
```

- Uploaded videos keep the source name; re-uploading the same name **overwrites**.
- `lumi ls <path>` lists any device directory.
- The serial protocol has **no file-delete command** in the stock app (its delete
  button only removed local references). Lumi additionally implements a delete
  opcode to actually free space — see `lumi rm --help`.

## Troubleshooting

- **"could not open port / No such file"** — the panel is asleep or mid-wake.
  Retry; Lumi wakes it automatically and retries several times.
- **Permission denied on the serial port** — make sure the udev rule is installed
  (`sudo cp 99-lumi.rules /etc/udev/rules.d/ && sudo udevadm control --reload &&
  sudo udevadm trigger`), then unplug/replug the panel.
- **Wrong video on restart** — apply the media you want from the GUI (or
  `lumi play <name>`); Lumi persists the last-applied media for resume.
- **Panel didn't turn off at shutdown** — ensure the daemon is running
  (`systemctl --user is-active lumi`) and linger is enabled
  (`loginctl show-user $USER | grep Linger`). The daemon must own the panel at
  shutdown (close the GUI first).

## Credits & license

GPL-3.0-or-later. The rev C serial transport in `src/lumi/lcd/` is vendored from
[`turing-smart-screen-python`](https://github.com/mathoudebine/turing-smart-screen-python)
(GPL-3.0-or-later, © Matthieu Houdebine et al.). The media pipeline, GUI,
round-preview, and device-storage support are original to Lumi.
