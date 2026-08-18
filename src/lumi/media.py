"""Video upload for rev C USB-CDC LCD panels.

Implements the stored-file pipeline (beyond rev_c, which has no video/storage):
  ffmpeg transcode -> cmd 111 create (filename + 4B LE size) -> bulk stream
  -> file_rev_done -> cmd 120 play -> play_video_success

Source of truth: docs/2.1inch-display-and-media-upload.md (§4g, §6c, §10a),
reverse-engineered from the cleaned UsbMonitorR source.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from lumi.log import logger

CHUNK = 249          # bulk payload size (GClass2.int_0)
BLOCK = 254976       # file read block = 1024 * 249 (GClass3.method_72)

# On-device media library dirs (device runs Linux; confirmed via cmd 101 probe).
VIDEO_DIR = "/mnt/SDCARD/video"
IMG_DIR = "/mnt/SDCARD/img"

# Each upload gets a unique timestamped name so the device file, the local
# preview copy, and the "now playing" indicator all stay in sync by name.
# (This is viable now that the protocol HAS a delete command — cmd 102 — so old
# clips can be cleaned up rather than relying on a fixed-name overwrite.)
def default_upload_name(ext: str = "mp4") -> str:
    from datetime import datetime
    return f"lumi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"

# Local store: a copy of every clip we upload, named to match the device file.
# The preview restores from here on reopen so it matches what's actually playing.
LOCAL_MEDIA_DIR = os.path.expanduser("~/.local/share/lumi/media")


def local_copy_path(device_name: str) -> Path:
    """Local preview file matching a device upload (basename preserved)."""
    base = device_name.rsplit("/", 1)[-1] if "/" in device_name else device_name
    return Path(LOCAL_MEDIA_DIR) / base


# --- low-level command helpers --------------------------------------------

def _pad250(msg: bytearray) -> bytes:
    """Pad to next multiple of 250 with 0x00 (matches GClass2.method_20)."""
    if len(msg) % 250:
        msg += bytes(250 * ((len(msg) // 250) + 1) - len(msg))
    return bytes(msg)


def send_cmd(lcd, cmd: int, arg: int, payload: bytes = b"", sub: int = 0) -> None:
    """Build & send a 250-byte command packet: [cmd][EF 69][arg:BE32][sub][..][payload@10]."""
    buf = bytearray(250)
    buf[0] = cmd & 0xFF
    buf[1], buf[2] = 0xEF, 0x69
    buf[3] = (arg >> 24) & 0xFF
    buf[4] = (arg >> 16) & 0xFF
    buf[5] = (arg >> 8) & 0xFF
    buf[6] = arg & 0xFF
    buf[7] = sub & 0xFF
    if payload:
        buf[10:10 + len(payload)] = payload[:240]
    lcd.WriteData(bytes(buf))


def bulk_write(lcd, data: bytes) -> None:
    """Stream raw bytes in 249-payload chunks of a 250-byte buffer (GClass2.method_21)."""
    off = 0
    while off < len(data):
        chunk = bytearray(250)
        n = min(CHUNK, len(data) - off)
        chunk[:n] = data[off:off + n]
        lcd.WriteData(bytes(chunk))
        off += CHUNK


def read_text(lcd, timeout: float = 3.0, size: int = 1024) -> str:
    """Read a UTF-8 text ACK from the device, draining for up to `timeout` seconds."""
    lcd.serial_flush_input()
    end = time.time() + timeout
    raw = b""
    while time.time() < end:
        got = lcd.serial_readall() if hasattr(lcd, "serial_readall") else b""
        if got:
            raw += got
            # Keep reading a little longer in case more follows
            end = max(end, time.time() + 0.3)
        else:
            time.sleep(0.05)
    return raw.decode("utf-8", "replace").rstrip("\x00 ").rstrip() if raw else ""


# --- ffmpeg ---------------------------------------------------------------

def transcode_video(src: str | Path, dst: str | Path, size: tuple[int, int] = (480, 480),
                    rotate_cw: int = 0) -> Path:
    """Transcode to the panel's MP4 format (libx264, 24fps, yuv420p)."""
    src, dst = Path(src), Path(dst)
    vf = [f"scale={size[0]}:{size[1]}"]
    if rotate_cw == 90:
        vf.append("transpose=1")        # 90° CW
    elif rotate_cw == 270:
        vf.append("transpose=2")        # 90° CCW
    elif rotate_cw == 180:
        vf.append("transpose=1,transpose=1")
    vf.append("setsar=1:1")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-b:v", "2400k", "-bufsize", "2400k",
        "-r", "24", "-pix_fmt", "yuv420p", "-an", "-f", "mp4", str(dst),
    ]
    logger.info("ffmpeg: " + " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst


def transcode_cached(src: str | Path, size: tuple[int, int] = (480, 480),
                     rotate_cw: int = 0) -> Path:
    """Transcode to the local media cache, reusing a previous result if fresh.

    Cache key = source basename + mtime + size + rotation. Re-applying a GIF or
    video you've already uploaded skips ffmpeg entirely.
    """
    import glob
    src = Path(src)
    os.makedirs(LOCAL_MEDIA_DIR, exist_ok=True)
    stem = src.stem
    key = f"{int(src.stat().st_mtime)}_{size[0]}x{size[1]}_r{rotate_cw}"
    # Reuse existing cache entry for this source if the key matches
    for existing in glob.glob(os.path.join(LOCAL_MEDIA_DIR, f"{stem}.*{key}*.mp4")):
        return Path(existing)
    dst = Path(LOCAL_MEDIA_DIR) / f"{stem}.{key}.mp4"
    transcode_video(src, dst, size, rotate_cw)
    return dst


def render_image_positioned(src: str | Path, geom: dict, out: str | Path,
                            panel: int = 480, circle_mask: bool = True,
                            rotate_cw: int = 0) -> Path:
    """Composite a source image onto a panel-sized canvas using preview geometry.

    geom: from RoundPreview.render_geom() — {'src_w','src_h','scale','offset':(dx,dy)}.
    Corners masked to transparent (inscribed circle); then rotated `rotate_cw`
    clockwise to compensate the panel mounting. Output is a PNG (RGBA).
    """
    from PIL import Image, ImageDraw
    src = Image.open(src).convert("RGBA")
    new_w = max(1, int(geom["src_w"] * geom["scale"]))
    new_h = max(1, int(geom["src_h"] * geom["scale"]))
    scaled = src.resize((new_w, new_h))
    ox, oy = geom["offset"]
    pos_x = int((panel - new_w) / 2 + ox)
    pos_y = int((panel - new_h) / 2 + oy)

    canvas = Image.new("RGBA", (panel, panel), (0, 0, 0, 0))
    canvas.paste(scaled, (pos_x, pos_y), scaled)  # use scaled's own alpha

    if circle_mask:
        mask = Image.new("L", (panel, panel), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, panel - 1, panel - 1], fill=255)
        canvas.putalpha(mask)  # corners transparent within the circle

    if rotate_cw:
        canvas = canvas.rotate(-rotate_cw, expand=False)  # PIL rotate is CCW

    canvas.save(out)
    return Path(out)


def render_video_positioned(src: str | Path, geom: dict, dst: str | Path,
                            panel: int = 480, rotate_cw: int = 0) -> Path:
    """Composite a video/GIF onto a panel-sized canvas using preview geometry,
    via ffmpeg overlay on a black background (handles zoom-in crop + letterbox).

    geom: from RoundPreview.render_geom() — {'src_w','src_h','scale','offset':(dx,dy)}.
    """
    src, dst = Path(src), Path(dst)
    sw = max(1, int(geom["src_w"] * geom["scale"]))
    sh = max(1, int(geom["src_h"] * geom["scale"]))
    dx, dy = geom["offset"]
    px = int((panel - sw) / 2 + dx)
    py = int((panel - sh) / 2 + dy)

    rot = ""
    if rotate_cw == 90:
        rot = ",transpose=1"
    elif rotate_cw == 270:
        rot = ",transpose=2"
    elif rotate_cw == 180:
        rot = ",transpose=1,transpose=1"

    bg = f"color=c=black:s={panel}x{panel}:r=24"
    # eof_action=endall: stop output when the source clip ends (otherwise the
    # infinite black bg + overlay repeat would encode forever).
    graph = f"[0:v]scale={sw}:{sh}[fg];[1:v][fg]overlay={px}:{py}:eof_action=endall{rot},setsar=1:1"
    # For animated GIFs: read once (ffmpeg loops them forever by default).
    pre = ["-ignore_loop", "1"] if str(src).lower().endswith(".gif") else []
    cmd = [
        "ffmpeg", "-y", *pre, "-i", str(src), "-f", "lavfi", "-i", bg,
        "-filter_complex", graph, "-shortest",
        "-c:v", "libx264", "-b:v", "2400k", "-r", "24",
        "-pix_fmt", "yuv420p", "-an", "-f", "mp4", str(dst),
    ]
    logger.info("ffmpeg: " + " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst


def list_local() -> list[Path]:
    """List locally cached transcoded media."""
    os.makedirs(LOCAL_MEDIA_DIR, exist_ok=True)
    return sorted(Path(LOCAL_MEDIA_DIR).glob("*.mp4"))


def stop_media(lcd, max_tries: int = 20) -> bool:
    """Stop current playback before switching media (matches GClass3.method_85).

    Sequence: cmd 121 (STOP_VIDEO), then cmd 150 (STOP_MEDIA) polled until the
    device replies 'media_stop' (up to max_tries). Without this, playing a new
    video while one is running is ignored and the device may sleep.
    """
    send_cmd(lcd, 121, 1, b"", 0)  # STOP_VIDEO
    for _ in range(max_tries):
        send_cmd(lcd, 150, 1, b"", 0)  # STOP_MEDIA
        ack = read_text(lcd, 1.5)
        if "media_stop" in ack:
            logger.info("media stopped.")
            return True
    logger.warning("media_stop not seen after retries.")
    return False


def play_stored(lcd, device_path: str, stop_first: bool = True) -> bool:
    """Play a video already on the panel's flash (cmd 120, no re-upload).

    device_path: full on-device path, e.g. '/mnt/SDCARD/video/clip.mp4'.
    sub=1 matches the original app's looping play path (GClass3 cmd 120).
    """
    if stop_first:
        stop_media(lcd)
    path_b = device_path.encode("ascii")
    send_cmd(lcd, 120, len(path_b), path_b, 1)
    ack = read_text(lcd, 5.0)
    logger.info(f"play(stored) {device_path!r} -> {ack!r}")
    return "play_video_success" in ack


def play_image_stored(lcd, device_path: str, stop_first: bool = True) -> bool:
    """Display a stored image file by path (cmd 140 -> play_img_ok).

    When switching from a looping video, the device's media player needs a moment
    to fully release after stop (the original app awaits 'media end' before cmd
    140). Without this settle, cmd 140 can be acknowledged 'play_img_ok' yet the
    video loop visually re-asserts. A brief pause after stop closes that race.
    """
    if stop_first:
        stop_media(lcd)
        time.sleep(0.5)  # let the video decoder fully release before showing the image
    path_b = device_path.encode("ascii")
    send_cmd(lcd, 140, len(path_b), path_b, 0)
    ack = read_text(lcd, 5.0)
    logger.info(f"play_img(stored) {device_path!r} -> {ack!r}")
    return "play_img_ok" in ack


def delete_file(lcd, device_path: str) -> None:
    """Delete a file from the device by path (cmd 102, reverse-engineered).

    The device returns NO text ACK for delete; success is inferred from the
    subsequent listing/capacity change. Works for ANY known path, including
    files hidden beyond the 512-byte listing window.

    MUST run on a quiet line: the input buffer is flushed first and the (empty)
    reply drained after. Do NOT interleave with a cmd 101 listing read in the
    same burst — its ~10 KB response can still be streaming when cmd 102 lands
    and the delete is silently dropped. (Delete -> listing is the safe order.)
    """
    lcd.serial_flush_input()
    pb = device_path.encode("ascii")
    send_cmd(lcd, 102, len(pb), pb, 0)
    read_text(lcd, 1.0)  # drain the empty reply; keeps the line clean


def list_dir(lcd, device_path: str) -> tuple[list[str], list[str]]:
    """List a directory on the device via cmd 101.

    Returns (subdirs, files). Format: 'result:dir:<dirs>/file:<files>/'.
    """
    pb = device_path.encode("ascii")
    send_cmd(lcd, 101, len(pb), pb, 0)
    resp = read_text(lcd, 2.0)
    resp = resp.split("\x00", 1)[0]
    if not resp.startswith("result:"):
        logger.warning(f"list {device_path!r} unexpected: {resp!r}")
        return [], []
    resp = resp[len("result:"):]
    if resp.startswith("nodir"):
        return [], []  # dir was missing (and may have been created)
    if not resp.startswith("dir:"):
        return [], []
    rest = resp[len("dir:"):]
    dirs_part, _, files_part = rest.partition("file:")
    dirs = [d for d in dirs_part.split("/") if d]
    files = [f for f in files_part.split("/") if f]
    return dirs, files


def list_media(lcd, device_path: str = VIDEO_DIR) -> list[str]:
    """Media filenames in a device directory, merging the (512-byte-capped)
    device listing with our locally-tracked uploads in that same directory, so
    nothing we uploaded is hidden."""
    from lumi import state
    _, files = list_dir(lcd, device_path)
    merged = list(files)
    prefix = device_path.rstrip("/") + "/"
    for up in state.load_uploads():
        # uploads are stored as full paths; only merge those in this directory.
        if up.startswith(prefix):
            base = up.rsplit("/", 1)[-1]
            if base not in merged:
                merged.append(base)
    return merged


def capacity(lcd) -> list[dict]:
    """Query device storage capacity (cmd 100). Returns a list of stores, each
    {'total_kb','used_kb','free_kb'}. Format: total-used-free per store."""
    send_cmd(lcd, 100, 1, b"", 0)
    raw = read_text(lcd, 2.0).replace("\x00", "")
    nums = [int(x) for x in raw.split("-") if x.strip().isdigit()]
    stores = []
    for i in range(0, len(nums) - 2, 3):
        stores.append({"total_kb": nums[i], "used_kb": nums[i + 1], "free_kb": nums[i + 2]})
    return stores


# --- high-level video upload ----------------------------------------------

def upload_video(device, mp4_path: str | Path, dest_name: Optional[str] = None,
                 play: bool = True, progress=None) -> Optional[str]:
    """Upload an MP4 to the panel's video library and (optionally) play it.

    dest_name: basename on device (default: a unique `lumi_<timestamp>.mp4`, so
    the device file, the local preview copy, and "now playing" stay in sync by
    name). The file is stored under /mnt/SDCARD/video/. Pass a path starting
    with '/' to override. Returns the full on-device path on success, else None.

    progress: optional callback(stage: str, frac: float) called as the upload
    advances; frac in 0..1 (determinate) or <0 (indeterminate). Used by the GUI
    to drive a progress dialog.
    """
    lcd = device.lcd
    mp4_path = Path(mp4_path)
    name = dest_name or default_upload_name()
    if not name.startswith("/"):
        name = f"{VIDEO_DIR}/{name}"
    size = mp4_path.stat().st_size
    logger.info(f"Uploading {mp4_path} ({size} B) -> {name!r}")

    # 0) STOP current playback first (the original app stops before upload too)
    stop_media(lcd)

    # 1) CREATE: cmd 111, arg=path length, payload=ASCII(path)+4B LE size
    if progress:
        progress("Uploading…", 0.0)
    path_b = name.encode("ascii")
    payload = path_b + size.to_bytes(4, "little")
    send_cmd(lcd, 111, len(path_b), payload, 0)
    ack = read_text(lcd, 3.0)
    logger.info(f"create -> {ack!r}")
    if "create_success" not in ack:
        low = ack.lower()
        if "room" in low or "space" in low or "full" in low or "noroom" in low:
            logger.error("Device storage is full (create rejected). Reuse a filename to "
                         "overwrite, or reformat the device — there's no serial delete command.")
        else:
            logger.error(f"create failed ({ack!r}); aborting.")
        return None

    # 2) STREAM raw bytes (254976-byte reads -> 249-byte chunks)
    with open(mp4_path, "rb") as f:
        sent = 0
        while True:
            block = f.read(BLOCK)
            if not block:
                break
            bulk_write(lcd, block)
            sent += len(block)
            logger.info(f"streamed {sent}/{size} B ({sent*100//size}%)")
            if progress:
                progress(f"Uploading… {sent*100//size}%", sent / size)
    ack = read_text(lcd, 5.0)
    logger.info(f"stream -> {ack!r}")
    if "file_rev_done" not in ack:
        logger.warning("file_rev_done not seen; playback may fail.")

    # 3) PLAY: cmd 120, arg=path length, payload=ASCII(path), sub=1 (loop)
    if play:
        if progress:
            progress("Starting playback…", -1.0)
        send_cmd(lcd, 120, len(path_b), path_b, 1)
        ack = read_text(lcd, 5.0)
        logger.info(f"play -> {ack!r}")
        if "play_video_success" in ack:
            logger.info("Playback started (looping).")
    # Track locally: the device listing is 512-byte capped and may hide this.
    try:
        from lumi import state
        state.log_upload(name)
    except Exception:
        pass
    # Save a local copy named to match the device file, so the preview can show
    # exactly what's playing (and restore it on reopen).
    try:
        import shutil
        os.makedirs(LOCAL_MEDIA_DIR, exist_ok=True)
        shutil.copy2(str(mp4_path), str(local_copy_path(name)))
    except Exception as e:
        logger.warning(f"could not save local preview copy: {e}")
    return name


def upload_image(device, img_path: str | Path, dest_name: Optional[str] = None,
                 play: bool = True, rotate: int = 0, progress=None) -> Optional[str]:
    """Store an image on the panel's image library and (optionally) show it.

    Mirrors upload_video for images: the input is converted to a panel-ready JPG
    (the format the firmware decodes — boot.jpg confirms it), saved locally named
    to match the device file, streamed via cmd 111, then shown via cmd 140.
    Returns the full on-device path on success, else None.
    """
    from PIL import Image
    lcd = device.lcd
    img = Image.open(img_path)
    if rotate:
        img = img.rotate(-rotate, expand=True)  # PIL rotate is CCW
    img = img.convert("RGB")
    # Fit inside the 480x480 panel (aspect preserved); PNG alpha drops to black,
    # which the round bezel covers anyway.
    panel = 480
    if img.size != (panel, panel):
        img.thumbnail((panel, panel))

    name = dest_name or default_upload_name("jpg")
    if not name.startswith("/"):
        name = f"{IMG_DIR}/{name}"
    os.makedirs(LOCAL_MEDIA_DIR, exist_ok=True)
    local = local_copy_path(name)
    img.save(str(local), "JPEG", quality=92)
    size = local.stat().st_size
    logger.info(f"Uploading image {img_path} -> {name!r} ({size} B)")

    stop_media(lcd)
    if progress:
        progress("Uploading…", 0.0)
    path_b = name.encode("ascii")
    payload = path_b + size.to_bytes(4, "little")
    send_cmd(lcd, 111, len(path_b), payload, 0)
    ack = read_text(lcd, 3.0)
    logger.info(f"create -> {ack!r}")
    if "create_success" not in ack:
        logger.error(f"image create failed ({ack!r}); aborting.")
        return None

    with open(local, "rb") as f:
        bulk_write(lcd, f.read())
    if progress:
        progress("Uploading… 100%", 1.0)
    ack = read_text(lcd, 5.0)
    logger.info(f"stream -> {ack!r}")

    if play:
        if progress:
            progress("Starting playback…", -1.0)
        send_cmd(lcd, 140, len(path_b), path_b, 0)
        ack = read_text(lcd, 5.0)
        logger.info(f"play_img -> {ack!r}")

    try:
        from lumi import state
        state.log_upload(name)
    except Exception:
        pass
    return name
