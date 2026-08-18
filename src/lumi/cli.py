"""lumi CLI — drive a rev C USB-CDC LCD panel from the command line."""
from __future__ import annotations

import argparse
import sys

from lumi import Device, DeviceError, find_panel
from lumi.log import logger
from lumi import state


def _open(args) -> Device:
    try:
        return Device(
            com_port=args.port,
            size=args.size,
            brightness=state.load_brightness(),
            width=args.width, height=args.height,
            reset=args.reset,
        )
    except Exception as e:
        # Serial/open failures now raise (they used to os._exit the process).
        # Surface them as a DeviceError so main() reports cleanly.
        raise DeviceError(str(e)) from e


def cmd_info(args):
    port = find_panel()
    print(f"Awake COM port: {port}")
    if not port:
        print("Panel asleep or not connected; trying to wake via Device()...")
    with _open(args) as dev:
        print(f"Display ID : {getattr(dev.lcd, 'display_id', '?')}")
        print(f"Sub-rev    : {getattr(dev.lcd, 'sub_revision', '?')}")
        print(f"ROM version: {getattr(dev.lcd, 'rom_version', '?')}")
        print(f"Size       : {dev.width}x{dev.height} ({dev.size})")


def cmd_brightness(args):
    with _open(args) as dev:
        dev.set_brightness(args.level)
        state.save_brightness(args.level)
        print(f"Brightness set to {args.level}")


def cmd_image(args):
    """Convert & store an image on the panel's image library, then show it (cmd 140)."""
    with _open(args) as dev:
        dp = dev.upload_image(args.path, dest_name=args.name, rotate=args.rotate)
        if dp:
            state.save_last("image", args.path, device_name=dp, rotate=args.rotate)
        print("Image: " + ("stored -> " + dp if dp else "FAILED"))


def cmd_off(args):
    with _open(args) as dev:
        dev.screen_off()
        print("Screen off")


def cmd_video(args):
    with _open(args) as dev:
        device_path = dev.upload_video(
            args.path, dest_name=args.name, play=not args.no_play,
            rotate=args.rotate, transcode=not args.no_transcode,
        )
        if device_path:
            state.save_last("video", args.path, device_name=device_path, rotate=args.rotate)
        print("Video upload: " + ("OK -> " + device_path if device_path else "FAILED"))


def cmd_ls(args):
    from lumi import media
    with _open(args) as dev:
        keyword = args.path
        merged = False
        if keyword == "video":
            path = media.VIDEO_DIR
            merged = True
        elif keyword in ("img", "images"):
            path = media.IMG_DIR
            merged = True
        else:
            path = keyword
        if merged:
            # The media libraries are merged with our locally-tracked uploads
            # (the device listing is 512-byte capped); other paths list raw.
            files = media.list_media(dev.lcd, path)
            dirs = []
        else:
            dirs, files = media.list_dir(dev.lcd, path)
        tag = "  (merged w/ local uploads)" if merged else ""
        print(f"{path}  ({len(dirs)} dirs, {len(files)} files){tag}")
        for d in dirs:
            print(f"  d  {d}/")
        for f in files:
            print(f"  f  {f}")


def cmd_play(args):
    """Play a stored video by name (from /mnt/SDCARD/video/)."""
    from lumi import media
    name = args.name
    device_path = name if name.startswith("/") else f"{media.VIDEO_DIR}/{name}"
    with _open(args) as dev:
        ok = media.play_stored(dev.lcd, device_path)
        if ok:
            state.save_last("video", device_path, device_name=device_path, rotate=0)
        print("Play: " + ("OK" if ok else "FAILED (not found / not a video?)"))


def cmd_play_img(args):
    """Display a stored image by name (from /mnt/SDCARD/img/)."""
    from lumi import media
    name = args.name
    device_path = name if name.startswith("/") else f"{media.IMG_DIR}/{name}"
    with _open(args) as dev:
        ok = media.play_image_stored(dev.lcd, device_path)
        print("Play image: " + ("OK" if ok else "FAILED"))


def cmd_stop(args):
    """Stop current playback (cmd 121 STOP_VIDEO + cmd 150 STOP_MEDIA)."""
    from lumi import media
    with _open(args) as dev:
        ok = media.stop_media(dev.lcd)
        print("Stop: " + ("OK" if ok else "no media_stop ack"))


def cmd_rm(args):
    """Delete file(s) from the device by name (cmd 102).

    Works for ANY known filename, including ones hidden beyond the device's
    512-byte listing window. With --all, wipes the whole tracked library.
    Deleting frees space in the listing window, so previously-hidden files
    may surface in a subsequent `lumi ls`.
    """
    from lumi import media

    if args.all:
        with _open(args) as dev:
            names = media.list_media(dev.lcd)  # device listing + tracked uploads
            if not names:
                print("Nothing to delete.")
                return
            last = state.load_last()
            playing = last.get("device_name") if last and last.get("kind") == "video" else None
            if playing:
                media.stop_media(dev.lcd)
            for n in names:
                path = n if n.startswith("/") else f"{media.VIDEO_DIR}/{n}"
                media.delete_file(dev.lcd, path)
                state.forget_upload(path)
                print(f"Deleted {n}")
            state.clear_uploads()
            if playing:
                state.clear_last()
        print(f"Wiped {len(names)} file(s).")
        return

    with _open(args) as dev:
        last = state.load_last()
        playing = last.get("device_name") if last and last.get("kind") == "video" else None
        targets = [(n, (n if n.startswith("/") else f"{media.VIDEO_DIR}/{n}")) for n in args.names]
        if playing and any(p == playing for _, p in targets):
            media.stop_media(dev.lcd)  # don't delete the playing clip live
        for n, path in targets:
            media.delete_file(dev.lcd, path)
            state.forget_upload(path)
            print(f"Deleted {n}")
        if playing and any(p == playing for _, p in targets):
            state.clear_last()
            print("(cleared last media — it was deleted)")


def cmd_gif(args):
    """Convert a GIF to MP4 (cached) and upload+loop it. Same pipeline as `video`."""
    with _open(args) as dev:
        device_path = dev.upload_video(
            args.path, dest_name=args.name, play=not args.no_play,
            rotate=args.rotate, transcode=True,
        )
        if device_path:
            state.save_last("video", args.path, device_name=device_path, rotate=args.rotate)
        print("GIF upload: " + ("OK -> " + device_path if device_path else "FAILED"))


def cmd_df(args):
    """Show device storage capacity / free space (cmd 100)."""
    from lumi import media
    with _open(args) as dev:
        stores = media.capacity(dev.lcd)
    if not stores:
        print("No capacity info returned.")
        return
    for i, s in enumerate(stores, 1):
        free = s["free_kb"]
        print(f"Store {i}: {s['used_kb']/1024:.1f} / {s['total_kb']/1024:.1f} MB used"
              f"  ({free/1024:.1f} MB free)"
              + ("  <- media library" if s == max(stores, key=lambda x: x["total_kb"]) else ""))


def cmd_ls_local(args):
    """List locally cached transcoded media (re-apply without re-transcoding)."""
    from lumi import media
    files = media.list_local()
    print(f"{media.LOCAL_MEDIA_DIR}  ({len(files)} files)")
    for f in files:
        print(f"  {f.name}  ({f.stat().st_size} B)")


def cmd_daemon(args):
    """Keep the panel alive and show an idle image; TURNOFF on SIGTERM/SIGINT.

    Designed for a systemd user service: starts at boot (with enable-linger),
    and on shutdown/stop the signal handler blanks the screen before exit.
    """
    import signal
    import time

    stop = {"flag": False}

    def on_signal(signum, frame):
        logger.info(f"Received signal {signum}; turning screen off and exiting.")
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    with _open(args) as dev:
        # Resume last media if any (so reboots / restarts restore state).
        last = state.load_last()
        if last:
            if last.get("kind") == "video" and last.get("device_name"):
                from lumi import media
                logger.info(f"Resuming last video: {last['device_name']}")
                media.play_stored(dev.lcd, last["device_name"])
            elif last.get("kind") == "image":
                from pathlib import Path
                from lumi import media
                if last.get("device_name"):
                    logger.info(f"Resuming last image: {last['device_name']}")
                    media.play_image_stored(dev.lcd, last["device_name"])
                elif last.get("path") and Path(last["path"]).exists():
                    logger.info(f"Resuming last image: {last['path']}")
                    media.stop_media(dev.lcd)
                    dev.show_image(last["path"], rotate=last.get("rotate", 0))
                else:
                    logger.warning("Last image gone")
        elif args.image:
            dev.show_image(args.image, rotate=args.rotate)
            logger.info(f"Showing idle image {args.image}")
        else:
            logger.info("Daemon running (no last media / idle image); panel kept awake.")

        # Start the live overlay (e.g. clock) over the playing media, if enabled.
        overlay_loop = None
        try:
            from lumi import overlay
            ocfg = overlay.load_config()
            if ocfg.get("enabled"):
                time.sleep(2)  # let the resumed video start before overlaying it
                overlay_loop = overlay.OverlayLoop(dev, ocfg, panel=dev.width)
                overlay_loop.start()
                logger.info("Live overlay enabled (clock).")
        except Exception as e:
            logger.warning(f"Overlay not started: {e}")

        while not stop["flag"]:
            time.sleep(1)
        # Stop the overlay loop before touching the serial line on shutdown.
        if overlay_loop:
            overlay_loop.stop()
        # Clean shutdown: blank the panel before the USB link drops.
        try:
            dev.screen_off()
        except Exception as e:
            logger.warning(f"TURNOFF failed during shutdown: {e}")


def cmd_on(args):
    with _open(args) as dev:
        dev.screen_on()
        print("Screen on")


def cmd_overlay(args):
    """Run / toggle the live overlay (clock, system stats, ...) over the panel.

    By default this plays the last-applied video (or a black background if there
    is none) and runs the overlay loop, which establishes a PERSISTENT overlay
    via cmd 202 (colours) + cmd 208 (mask) -- the overlay stays on top of the
    playing video and updates when it changes.

    With --on/--off it only edits the config (for the daemon) and exits.
    --list/--show print info and exit. --layout selects an overlay then runs.
    --metrics / --sample KEY inspect sensor data without touching the device.
    """
    import time
    from PIL import Image
    from lumi import overlay, media, sensors

    if args.on:
        overlay.set_enabled(True)
        print("Overlay enabled (the daemon will show it after media resume).")
        return
    if args.off:
        overlay.set_enabled(False)
        print("Overlay disabled.")
        return
    if args.list:
        active = overlay.load_config().get("layout")
        for p in overlay.list_layouts():
            mark = " *" if p["id"] == active else ""
            print(f"{p['id']:<12} {p['label']}{mark}")
        return
    if args.metrics:
        m = sensors.Metrics()
        m.poll()
        if not m.keys():
            print("No metrics available (install psutil / lm-sensors for more).")
            return
        for k in sorted(m.keys()):
            s = m.get(k)
            tail = f"  text={s.text!r}" if s.text else ""
            print((f"{k:<14} {s.value} {s.unit}").rstrip() + tail)
        return
    if args.sample:
        m = sensors.Metrics()
        m.poll()
        s = m.get(args.sample)
        if s is None:
            print(f"unknown metric: {args.sample!r} (try: lumi overlay --metrics)")
            return 2
        extra = f" (rate {s.rate:.3f})" if not s.text else f" text={s.text!r}"
        print(f"{args.sample}: {s.value}{s.unit}{extra}")
        return
    if args.show:
        import json
        print(json.dumps(overlay.load_config(), indent=2))
        return
    layout = getattr(args, "layout", None) or args.preset
    if layout:
        overlay.set_layout(layout)
        logger.info(f"overlay: layout <- {layout}")

    with _open(args) as dev:
        if args.bg:
            # Static image background instead of video.
            bg = Image.open(args.bg).convert("RGB")
            bg.thumbnail((dev.width, dev.height))
            dev.show_image(bg)
            logger.info(f"overlay: static background <- {args.bg}")
        else:
            # Play the last video so the overlay has a video to sit on.
            last = state.load_last()
            vname = last.get("device_name") if last and last.get("kind") == "video" else None
            if vname:
                media.play_stored(dev.lcd, vname)
                logger.info(f"overlay: playing {vname}")
            else:
                dev.show_image(Image.new("RGB", (dev.width, dev.height), (0, 0, 0)))
                logger.info("overlay: black background (no last video)")
            time.sleep(2)  # let the video start before establishing the overlay

        cfg = overlay.load_config()
        loop = overlay.OverlayLoop(dev, cfg, panel=dev.width)
        loop.start()
        print("Overlay running (Ctrl-C to stop).")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            loop.stop()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lumi", description="rev C USB-CDC LCD panel driver")
    p.add_argument("--port", default="AUTO", help="COM port (default: AUTO)")
    p.add_argument("--size", default='2.1"', choices=['2.1"', '2.8"', '3.5"', '5"', '8.8"'])
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--reset", action="store_true", help="Reset screen on connect")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="Show detected device info").set_defaults(func=cmd_info)
    sub.add_parser("on", help="Turn screen on").set_defaults(func=cmd_on)
    sub.add_parser("off", help="Turn screen off").set_defaults(func=cmd_off)

    b = sub.add_parser("brightness", help="Set backlight brightness")
    b.add_argument("level", type=int, help="brightness percentage 0-100")
    b.set_defaults(func=cmd_brightness)

    i = sub.add_parser("image", help="Convert & store an image on the panel, then show it")
    i.add_argument("path", help="Image file path")
    i.add_argument("--name", default=None, help="Destination filename on device (default: lumi_<timestamp>.jpg)")
    i.add_argument("--rotate", type=int, default=0,
                   help="Degrees clockwise to pre-rotate (default 0)")
    i.set_defaults(func=cmd_image)

    v = sub.add_parser("video", help="Transcode & upload a video, then play it")
    v.add_argument("path", help="Video file path")
    v.add_argument("--name", default=None, help="Destination filename on device (default: basename)")
    v.add_argument("--rotate", type=int, default=0, help="Degrees CW to rotate (default 0)")
    v.add_argument("--no-transcode", action="store_true", help="Input is already a panel-ready MP4")
    v.add_argument("--no-play", action="store_true", help="Upload without starting playback")
    v.set_defaults(func=cmd_video)

    d = sub.add_parser("daemon", help="Keep panel awake; TURNOFF on shutdown (for systemd)")
    d.add_argument("--image", default=None, help="Idle image to show (optional)")
    d.add_argument("--rotate", type=int, default=0, help="Degrees CW for idle image (default 0)")
    d.set_defaults(func=cmd_daemon)

    ls = sub.add_parser("ls", help="List a directory on the device")
    ls.add_argument("path", nargs="?", default="video",
                    help="'video' | 'images' (a.k.a. 'img') | or a full path (default: video library)")
    ls.set_defaults(func=cmd_ls)

    pl = sub.add_parser("play", help="Play a stored video by name")
    pl.add_argument("name", help="filename in /mnt/SDCARD/video/, or a full path")
    pl.set_defaults(func=cmd_play)

    pi = sub.add_parser("play-img", help="Display a stored image by name")
    pi.add_argument("name", help="filename in /mnt/SDCARD/img/, or a full path")
    pi.set_defaults(func=cmd_play_img)

    g = sub.add_parser("gif", help="Convert a GIF to MP4 (cached) and upload+loop")
    g.add_argument("path", help="GIF file path")
    g.add_argument("--name", default=None, help="Destination filename on device")
    g.add_argument("--rotate", type=int, default=0, help="Degrees CW to rotate (default 0)")
    g.add_argument("--no-play", action="store_true", help="Upload without starting playback")
    g.set_defaults(func=cmd_gif)

    sub.add_parser("ls-local", help="List locally cached transcoded media").set_defaults(func=cmd_ls_local)

    sub.add_parser("df", help="Show device storage capacity / free space").set_defaults(func=cmd_df)

    sub.add_parser("stop", help="Stop current playback").set_defaults(func=cmd_stop)

    rm = sub.add_parser("rm", help="Delete file(s) from the device (cmd 102)")
    rm.add_argument("names", nargs="*", help="filename(s) in /mnt/SDCARD/video/, or full paths")
    rm.add_argument("--all", action="store_true", help="Delete every tracked file in the library")
    rm.set_defaults(func=cmd_rm)

    sub.add_parser("gui", help="Launch the PySide6 GUI").set_defaults(func=cmd_gui)

    ov = sub.add_parser("overlay", help="Run/toggle the live overlay (clock, system stats, ...) over the panel")
    ov.add_argument("--on", action="store_true",
                    help="Enable the overlay in config (for the daemon) and exit")
    ov.add_argument("--off", action="store_true",
                    help="Disable the overlay in config and exit")
    ov.add_argument("--list", action="store_true",
                    help="List available overlay layouts and exit")
    ov.add_argument("--layout", default=None, metavar="ID",
                    help="Select an overlay layout (then run for preview)")
    ov.add_argument("--preset", default=None, metavar="ID",
                    help="Alias of --layout (back-compat)")
    ov.add_argument("--metrics", action="store_true",
                    help="List available sensor metrics and exit (no device needed)")
    ov.add_argument("--sample", default=None, metavar="KEY",
                    help="Print one metric value and exit (no device needed)")
    ov.add_argument("--show", action="store_true",
                    help="Print the current overlay config and exit")
    ov.add_argument("--bg", default=None, metavar="IMAGE",
                    help="Static background image instead of the last video")
    ov.set_defaults(func=cmd_overlay)

    return p


def cmd_gui(args):
    """Launch the PySide6 GUI."""
    try:
        from lumi import gui
    except ImportError as e:
        print("GUI needs PySide6: pip install lumi[gui]  (or: pip install PySide6)")
        print(f"({e})")
        return 3
    return gui.main()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except DeviceError as e:
        logger.error(str(e))
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
