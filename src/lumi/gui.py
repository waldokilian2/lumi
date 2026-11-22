"""lumi GUI — a PySide6 front-end for the rev C panel driver.

Architecture: a DeviceWorker QObject lives on a background QThread and owns the
Device for the session (keeps the panel awake, fast responses). The UI sends
commands to it via signals and receives results/logs via signals — the UI never
blocks on serial I/O.

Run with:  lumi gui
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt, QTimer
from PySide6.QtGui import QPixmap, QIcon, QMovie, QColor
from pathlib import Path
from datetime import datetime
from typing import NamedTuple


def _find_logo() -> Path:
    """Locate the bundled Lumi.png across editable (src/Lumi.png) and wheel
    (inside the package) layouts. Returns a Path that may not exist."""
    here = Path(__file__).resolve()
    for c in (here.parent.parent / "Lumi.png", here.parent / "Lumi.png"):
        if c.exists():
            return c
    return here.parent.parent / "Lumi.png"  # default; .exists() guards callers


LOGO_PATH = _find_logo()
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout, QPushButton,
    QSlider, QLabel, QListWidget, QFileDialog, QStatusBar, QGroupBox, QMessageBox,
    QComboBox, QAbstractItemView, QProgressDialog, QGridLayout, QCheckBox,
    QTabWidget, QFormLayout, QSpinBox, QColorDialog,
)

from lumi import Device, DeviceError, find_panel
from lumi import media as M
from lumi import state
from lumi.log import logger
from lumi.preview import RoundPreview
import logging
import subprocess


def _daemon_active() -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "-q", "lumi"]).returncode == 0


def _daemon_enabled() -> bool:
    return subprocess.run(["systemctl", "--user", "is-enabled", "-q", "lumi"]).returncode == 0


def _daemon_stop():
    subprocess.run(["systemctl", "--user", "stop", "lumi"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _daemon_start():
    subprocess.run(["systemctl", "--user", "start", "lumi"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _pause_daemon() -> bool:
    """Cancel running and queued service starts before taking the serial port."""
    resume = _daemon_active() or _daemon_enabled()
    _daemon_stop()
    return resume


class _LogSignal(QObject):
    """Marries stdlib logging (worker thread) to a Qt signal (UI thread)."""
    fired = Signal(str)


class GuiLogHandler(logging.Handler):
    def __init__(self, sig: _LogSignal):
        super().__init__()
        self._sig = sig

    def emit(self, record):
        try:
            self._sig.fired.emit(self.format(record))
        except Exception:
            pass


class SensorControls(NamedTuple):
    enabled: QCheckBox
    source: QComboBox
    x: QSpinBox
    y: QSpinBox
    size: QSpinBox
    color: QPushButton
    background: QPushButton
    opacity: QSpinBox


# --------------------------------------------------------------------------- worker

class DeviceWorker(QObject):
    connected = Signal(str)        # device info text
    log = Signal(str)              # log line
    busy = Signal(bool)            # toggle UI busy state
    media_list = Signal(list)      # list of filenames
    brightness_value = Signal(int) # persisted brightness, to sync the slider
    capacity_info = Signal(str)    # formatted storage usage
    now_playing = Signal(str)      # basename now playing ("" = nothing)
    preview_request = Signal(str)  # device path to restore into the preview
    progress = Signal(str, float)  # (stage, frac); frac<0 = indeterminate
    overlay_applied = Signal()     # a clean overlay (re)establish finished

    def __init__(self, size: str = '2.1"'):
        super().__init__()
        self.size = size
        self.dev: Device | None = None
        self.brightness = 50
        self._playing: str | None = None  # basename currently playing, or None
        self._lib_mode: str = "video"     # "video" | "image" — which library to list/play
        self._overlay_renderer = None     # lumi.overlay.OverlayRenderer (clock etc.)
        self._overlay_timer = None        # QTimer driving overlay ticks (this thread)

    # --- helpers ------------------------------------------------------------

    def _ensure(self) -> Device | None:
        if self.dev is None:
            self.log.emit("Connecting to panel…")
            try:
                self.dev = Device(size=self.size, brightness=state.load_brightness())
            except Exception as e:
                # Any failure (DeviceError, SerialException, port race during
                # handoff, sleep transition…) -> treat as "not connected, retry".
                self.log.emit(f"Connect failed ({e.__class__.__name__}); will retry.")
                self.dev = None
        return self.dev

    def _run(self, fn, *a):
        """Run a device op; on serial failure drop the stale device so the next
        call reconnects (the panel may have re-enumerated to a new port)."""
        dev = self._ensure()
        if not dev:
            return None
        try:
            return fn(dev, *a)
        except Exception as e:
            self.log.emit(f"Device error ({e}); will reconnect next time.")
            try:
                dev.close()
            except Exception:
                pass
            self.dev = None
            return None

    def _set_playing(self, device_path: str | None):
        """Track + announce what's currently playing (basename), or None.

        Centralizes preview refresh: every time playback switches to a real
        file, the preview reloads from the matching local copy so the preview,
        the 'now playing' label, and the device stay in sync.
        """
        if device_path:
            base = device_path.rsplit("/", 1)[-1] if "/" in device_path else device_path
            self._playing = base
            self.now_playing.emit(base)
            self.preview_request.emit(device_path)
            # Upload/play paths stop the decoder, which can hide its overlay
            # compositor. Restore the selected overlay after the media command
            # and subsequent library refresh have completed on this thread.
            if self._overlay_renderer is not None:
                from lumi import overlay
                if overlay.load_config().get("enabled"):
                    from PySide6.QtCore import QMetaObject
                    QMetaObject.invokeMethod(self, "apply_overlay",
                                             Qt.ConnectionType.QueuedConnection)
        else:
            self._playing = None
            self.now_playing.emit("")

    # --- slots (run on the worker thread) -----------------------------------

    @Slot()
    def connect_dev(self):
        self.busy.emit(True)
        self.progress.emit("Connecting to panel…", -1.0)
        try:
            # Retry connect until the panel is actually ready. After the daemon
            # is stopped (TURNOFF), the panel drops USB and cycles dormant ->
            # awake, which can take ~30-60s to stabilise. Each Device() attempt
            # now fails fast (bounded HELLO handshake) instead of hanging, so we
            # poll with a short backoff until HELLO succeeds, up to a total cap.
            import time as _t
            connect_deadline = _t.time() + 75.0   # overall cap ~75s
            attempt = 0
            while self.dev is None and _t.time() < connect_deadline:
                attempt += 1
                if self._ensure():
                    break
                self.log.emit(f"Connect retry {attempt}… (panel not ready yet)")
                _t.sleep(3)
            if self.dev:
                lcd = self.dev.lcd
                info = (f"Connected: {getattr(lcd,'sub_revision','?')} "
                        f"ROM {getattr(lcd,'rom_version','?')}  "
                        f"{self.dev.width}x{self.dev.height}")
                self.connected.emit(info)
                self.log.emit(info)
                self.brightness_value.emit(state.load_brightness())
                self.refresh_media()
                # Resume the last-applied media so a GUI restart shows the right thing
                last = state.load_last()
                if last:
                    try:
                        if last.get("kind") == "video" and last.get("device_name"):
                            self.log.emit(f"Resuming last video: {last['device_name']}")
                            if M.play_stored(lcd, last["device_name"], stop_first=False):
                                self._set_playing(last["device_name"])
                        elif last.get("kind") == "image":
                            from pathlib import Path
                            if last.get("device_name"):
                                # Stored image: replay from the device (cmd 140).
                                self.log.emit(f"Resuming last image: {last['device_name']}")
                                if M.play_image_stored(lcd, last["device_name"], stop_first=False):
                                    self._set_playing(last["device_name"])
                            elif last.get("path") and Path(last["path"]).exists():
                                # Legacy live image (not stored): show directly.
                                self.log.emit(f"Resuming last image: {last['path']}")
                                M.stop_media(lcd)
                                self.dev.show_image(last["path"], rotate=0)
                    except Exception as e:
                        self.log.emit(f"resume failed: {e}")
                # Auto-start the live overlay if enabled in config (mirrors the
                # daemon), so opening the GUI shows the clock without a toggle.
                try:
                    from lumi import overlay
                    if overlay.load_config().get("enabled"):
                        self.set_overlay_enabled(True)
                except Exception as e:
                    self.log.emit(f"overlay autostart: {e}")
        except Exception as e:
            self.log.emit(f"connect error ({e.__class__.__name__}); use a command to retry.")
        finally:
            self.busy.emit(False)

    @Slot(int)
    def set_brightness(self, level: int):
        self.brightness = level
        state.save_brightness(level)
        self._run(lambda d: d.set_brightness(level))

    @Slot(str)
    def show_image_path(self, path: str):
        """Display a (already-rendered) image file live on the panel."""
        def _do(d):
            M.stop_media(d.lcd)  # clear any looping video so the image shows
            self.log.emit(f"Showing {path}")
            d.show_image(path, rotate=0)  # positioning already baked in
        self._run(_do)

    @Slot(str)
    def apply_positioned(self, src_path: str):
        """Render the preview framing (geometry in self._geom, rotation in
        self._rotate) and upload to the panel. Runs render + upload on this
        thread so the UI stays responsive."""
        import tempfile
        from pathlib import Path
        geom = getattr(self, "_geom", None)
        rotate = getattr(self, "_rotate", 0)
        if not geom:
            self.log.emit("No geometry; load media first.")
            return
        ext = src_path.lower().rsplit(".", 1)[-1]
        self.busy.emit(True)
        cb = lambda stage, frac: self.progress.emit(stage, frac)

        def _do(d):
            if ext in ("png", "jpg", "jpeg", "bmp"):
                # Render the positioned frame, then STORE it on the device's image
                # library (same pipeline as video) and show via cmd 140.
                out = Path(M.LOCAL_MEDIA_DIR) / (Path(src_path).stem + ".render.png")
                out.parent.mkdir(parents=True, exist_ok=True)
                M.render_image_positioned(src_path, geom, out, rotate_cw=rotate)
                self.progress.emit("Uploading image…", -1.0)
                dp = M.upload_image(d, str(out), play=True, rotate=0, progress=cb)
                self.log.emit(f"Stored as {dp}" if dp else "Image upload FAILED")
                if dp:
                    state.save_last("image", str(out), device_name=dp, rotate=0)
                    self._set_playing(dp)
            else:
                tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
                self.progress.emit("Rendering…", -1.0)
                M.render_video_positioned(src_path, geom, tmp, rotate_cw=rotate)
                self.log.emit("Uploading positioned video…")
                dp = M.upload_video(d, tmp, play=True, progress=cb)  # default lumi_<ts>.mp4
                self.log.emit(f"Stored as {dp}")
                if dp:
                    state.save_last("video", src_path, device_name=dp, rotate=rotate)
                    self._set_playing(dp)
        self._run(_do)
        self.refresh_media()
        self.busy.emit(False)

    @Slot(str)
    def upload_media(self, path: str):
        self.busy.emit(True)
        cb = lambda stage, frac: self.progress.emit(stage, frac)
        def _do(d):
            ext = path.lower().rsplit(".", 1)[-1]
            if ext in ("gif", "mp4", "mov", "avi", "mkv", "webm"):
                self.log.emit(f"Uploading {path} …")
                dp = d.upload_video(path, rotate=0, progress=cb)
                self.log.emit(f"Stored as {dp}")
                if dp:
                    self._set_playing(dp)
            else:
                self.log.emit(f"Uploading image {path} …")
                dp = d.upload_image(path, rotate=0, progress=cb)
                self.log.emit(f"Stored as {dp}" if dp else "Image upload FAILED")
                if dp:
                    self._set_playing(dp)
        self._run(_do)
        self.refresh_media()
        self.busy.emit(False)

    @Slot(str)
    def play(self, name: str):
        mode = getattr(self, "_lib_mode", "video")
        if mode == "image":
            # Images: display a stored image file (cmd 140). No loop/preview sync.
            path = name if name.startswith("/") else f"{M.IMG_DIR}/{name}"
            self.busy.emit(True)
            self.progress.emit("Showing image…", -1.0)
            ok = self._run(lambda d: M.play_image_stored(d.lcd, path))
            self.busy.emit(False)
            self.log.emit(f"Show image {name}: {'OK' if ok else 'FAILED'}")
            return
        path = name if name.startswith("/") else f"{M.VIDEO_DIR}/{name}"
        self.busy.emit(True)
        self.progress.emit("Starting playback…", -1.0)
        ok = self._run(lambda d: M.play_stored(d.lcd, path))
        self.busy.emit(False)
        if ok:
            # Persist so the daemon resumes THIS file after a reboot, not the
            # previously-uploaded one.
            state.save_last("video", path, device_name=path, rotate=0)
            self._set_playing(name)
        self.log.emit(f"Play {name}: {'OK' if ok else 'FAILED'}")

    @Slot()
    def delete_files(self):
        """Delete the selected name(s) from the device (cmd 102).

        Order matters (see media.delete_file): each delete flushes+drains so the
        line stays clean, then ONE refresh at the end re-lists (which also pulls
        any previously-hidden files into the 512-byte window) + updates usage.
        Stops playback first only if the playing clip is among the targets.
        """
        names = getattr(self, "_del_names", []) or []
        self.busy.emit(True)

        def _do(d):
            lcd = d.lcd
            mode = getattr(self, "_lib_mode", "video")
            lib_dir = M.IMG_DIR if mode == "image" else M.VIDEO_DIR
            last = state.load_last()
            playing = last.get("device_name") if last and last.get("kind") == "video" else None
            targets = [(n, (n if n.startswith("/") else f"{lib_dir}/{n}")) for n in names]
            killed_playing = bool(playing and any(p == playing for _, p in targets))
            if killed_playing:
                self.log.emit("Stopping playback before delete…")
                M.stop_media(lcd)
            for i, (n, path) in enumerate(targets, 1):
                M.delete_file(lcd, path)
                state.forget_upload(path)
                self.log.emit(f"Deleted {n}")
                self.progress.emit(f"Deleting {i}/{len(targets)}…", i / len(targets))
            if killed_playing:
                # Last-played was just deleted: clear it and auto-resume the top
                # remaining file (never one we just deleted, in case the device's
                # stale 512-byte listing still shows it). Safe order: delete ->
                # listing -> play.
                state.clear_last()
                self._set_playing(None)
                remaining = [f for f in M.list_media(lcd) if f not in names]
                if remaining:
                    top = remaining[0]
                    top_path = top if top.startswith("/") else f"{M.VIDEO_DIR}/{top}"
                    if M.play_stored(lcd, top_path):
                        state.save_last("video", top_path, device_name=top_path, rotate=0)
                        self.log.emit(f"Auto-resumed: {top}")
                        self._set_playing(top)
                else:
                    self.log.emit("Library empty — nothing to resume.")
        self._run(_do)
        self.refresh_media()   # safe direction: delete -> listing
        self.busy.emit(False)

    @Slot()
    def screen_on(self):
        # Backlight on + resume last media — does NOT power-down the panel
        # (rev_c ScreenOff's TURNOFF drops USB, which breaks a live session).
        self._run(lambda d: d.set_brightness(self.brightness))

    @Slot()
    def screen_off(self):
        # Backlight off only — keep the link alive so the GUI stays in control.
        self._run(lambda d: d.set_brightness(0))
        self.log.emit("Screen off (backlight)")

    @Slot()
    def power_off(self):
        # Real power-down (rev_c ScreenOff incl. TURNOFF) — used on GUI quit.
        # This drops the USB link, which is fine since we're shutting down.
        if self.dev is not None:
            try:
                self.dev.lcd.ScreenOff()
            except Exception:
                pass

    @Slot()
    def refresh_media(self):
        mode = getattr(self, "_lib_mode", "video")
        if mode == "image":
            files = self._run(lambda d: M.list_dir(d.lcd, M.IMG_DIR)[1])
        else:
            files = self._run(lambda d: M.list_media(d.lcd))
        if files is not None:
            self.media_list.emit(files)
        self.refresh_capacity()

    @Slot()
    def refresh_capacity(self):
        stores = self._run(lambda d: M.capacity(d.lcd))
        if not stores:
            return
        s = max(stores, key=lambda x: x["total_kb"])  # media store = largest
        used = s["used_kb"] / 1024
        total = s["total_kb"] / 1024
        free = s["free_kb"] / 1024
        pct = (s["used_kb"] / s["total_kb"] * 100) if s["total_kb"] else 0
        self.capacity_info.emit(f"{used:.0f} / {total:.0f} MB used  ({free:.0f} MB free · {pct:.0f}% used)")

    # --- overlay (clock) ---------------------------------------------------

    @Slot(bool)
    def set_overlay_enabled(self, enabled: bool):
        """Start/stop the live overlay loop on this worker thread. Persists the
        choice so the daemon (restarted on GUI close) keeps the same setting.
        Always rebuilds the renderer from the current config so changed settings
        (format/position/size/colour) take effect on (re)enable."""
        from lumi import overlay
        try:
            overlay.set_enabled(bool(enabled))
        except Exception as e:
            self.log.emit(f"overlay config: {e}")
        # Enabling/reconfiguring replaces the complete layer. Never clear it
        # first: an empty visibility mask can disable the firmware compositor.
        self._stop_overlay(clear=not enabled)
        if not enabled:
            self.log.emit("Overlay off.")
            return
        if not self._ensure():
            self.log.emit("Overlay: no device connected.")
            return
        try:
            cfg = overlay.load_config()
            self._overlay_renderer = overlay.OverlayRenderer(cfg, panel=self.dev.width)
            if self._overlay_timer is None:
                self._overlay_timer = QTimer()
                self._overlay_timer.setSingleShot(True)
                self._overlay_timer.timeout.connect(self._overlay_tick)
            ms = max(100, int(overlay.layout_interval(cfg) * 1000))
            self._overlay_timer.start(ms)
            self.log.emit("Overlay on.")
        except Exception as e:
            self.log.emit(f"overlay start: {e}")

    @Slot()
    def _overlay_tick(self):
        if self._overlay_renderer and self.dev:
            try:
                self._overlay_renderer.tick(self.dev)
            except OSError as e:
                # The panel dropped off USB (or the link died). Stop pushing —
                # spamming a dead device churns the serial port and can wedge it
                # — then drop the stale device and reconnect.
                self.log.emit(f"Panel link lost ({e.__class__.__name__}); reconnecting…")
                self._handle_panel_lost()
            if self.dev and self._overlay_timer is not None:
                # Measure the refresh interval after a completed transfer. A
                # repeating timer immediately fired again when sending took
                # longer than its interval, keeping the compositor busy.
                self._overlay_timer.start(max(100, int(self._overlay_renderer.interval * 1000)))

    def _handle_panel_lost(self):
        """Recover from a lost serial link: stop the overlay loop, drop the stale
        device, and reconnect. Rate-limited so an unstable panel cannot drive a
        tight reconnect loop; connect_dev's own retry loop bounds each attempt."""
        import time as _t
        if self._overlay_timer is not None:
            self._overlay_timer.stop()
        if self._overlay_renderer is not None:
            self._overlay_renderer.reset()
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
        now = _t.time()
        if now - getattr(self, "_last_reconnect", 0.0) >= 8.0:
            self._last_reconnect = now
            self.connected.emit("Reconnecting…")
            from PySide6.QtCore import QMetaObject, Qt
            QMetaObject.invokeMethod(self, "connect_dev", Qt.ConnectionType.QueuedConnection)
        else:
            self.log.emit("Panel unstable; pausing before retry.")

    def _stop_overlay(self, clear=True):
        if self._overlay_timer is not None:
            self._overlay_timer.stop()
        if self._overlay_renderer is not None:
            self._overlay_renderer.reset()
        if clear and self.dev:
            try:
                self.dev.clear_overlay()
            except Exception:
                pass

    @Slot()
    def apply_overlay(self):
        """Replace the selected overlay without touching the media player.

        Consecutive frames were verified on the physical panel. Playback must
        remain undisturbed while updating settings or restoring an overlay.
        """
        from lumi import overlay
        self.busy.emit(True)
        self.progress.emit("Applying overlay…", -1.0)
        try:
            if self._overlay_timer is not None:
                self._overlay_timer.stop()
            if not self._ensure():
                self.log.emit("Overlay: no device connected.")
                return
            overlay.set_enabled(True)
            cfg = overlay.load_config()
            # Fresh renderer -> _prev is None -> tick() does a FULL 202+208 push
            # (replaces colours + mask), swapping the on-device overlay wholesale.
            self._overlay_renderer = overlay.OverlayRenderer(cfg, panel=self.dev.width)
            if not self._overlay_renderer.tick(self.dev):
                raise DeviceError("Overlay frame could not be sent; see the log for details.")
            if self._overlay_timer is None:
                self._overlay_timer = QTimer()
                self._overlay_timer.setSingleShot(True)
                self._overlay_timer.timeout.connect(self._overlay_tick)
            self._overlay_timer.start(max(100, int(overlay.layout_interval(cfg) * 1000)))
            self.log.emit(f"Overlay applied: {cfg.get('layout')}")
            self.overlay_applied.emit()
        except Exception as e:
            self.log.emit(f"overlay apply: {e}")
        finally:
            self.busy.emit(False)

    @Slot()
    def stop_overlay(self):
        """Stop the overlay QTimer on THIS (worker) thread. Invoked via
        BlockingQueuedConnection at GUI close so the timer is never stopped or
        destroyed from the main thread during teardown (which Qt rejects with
        'Timers cannot be stopped from another thread'). No serial writes here
        — the resumed daemon re-establishes the overlay."""
        if self._overlay_timer is not None:
            self._overlay_timer.stop()
        if self._overlay_renderer is not None:
            self._overlay_renderer.reset()

    @Slot()
    def release_panel(self):
        """Finish worker operations, then release the port for the daemon."""
        self.stop_overlay()
        if self.dev is not None:
            self.dev.close()
            self.dev = None


# --------------------------------------------------------------------------- preview file dialog

class PreviewFileDialog(QFileDialog):
    """QFileDialog with a live preview sidebar.

    Adds a QLabel to the dialog's grid layout (right-hand sidebar) and updates
    it as the user navigates via `currentChanged` -- images, animated GIFs, and
    the first frame of a video. No reparenting of internal widgets, so the
    native dialog layout stays intact.
    """

    def __init__(self, parent=None, caption="", directory="", filter=""):
        super().__init__(parent, caption, directory, filter)
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self._movie = None
        self._last_path = ""

        self._pv = QLabel("Select a file to preview")
        self._pv.setMinimumSize(300, 300)
        self._pv.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pv.setWordWrap(True)
        self._pv.setStyleSheet(
            "QLabel{background:#1c1c24;border:1px solid #2e2e38;border-radius:10px;color:#8a8a96;}"
        )

        # Slot the preview into the dialog grid as a right-hand sidebar spanning
        # all rows (new column at the end -- does not disturb existing layout).
        lay = self.layout()
        if isinstance(lay, QGridLayout):
            col = lay.columnCount()
            rows = lay.rowCount()
            lay.addWidget(self._pv, 0, col, rows, 1)
            lay.setColumnStretch(col, 1)
        self.resize(self.size().width() + 340, max(self.size().height(), 600))

        self.currentChanged.connect(self._on_current_changed)

    # -- preview update -----------------------------------------------------

    def _on_current_changed(self, path):
        if path == self._last_path:
            return
        self._last_path = path
        self._stop_anim()
        p = Path(path)
        if not p.is_file():
            self._set_text("Select a file to preview")
            return
        ext = p.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".bmp"):
            self._show_pixmap(QPixmap(path))
        elif ext == ".gif":
            self._show_gif(path)
        elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            self._show_video_frame(path)
        else:
            self._set_text("No preview for " + ext[1:].upper())

    def _show_pixmap(self, pm):
        if pm.isNull():
            self._set_text("Failed to load image")
            return
        self._pv.setPixmap(pm.scaled(
            self._pv.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _show_gif(self, path):
        m = QMovie(path, parent=self)
        if not m.isValid():
            self._set_text("Failed to load GIF")
            return
        self._movie = m
        m.setScaledSize(self._pv.size())
        self._pv.setMovie(m)
        m.start()

    def _show_video_frame(self, path):
        import subprocess, tempfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
        try:
            for seek in (["-ss", "0.3"], []):
                r = subprocess.run(
                    ["ffmpeg", "-y"] + seek + ["-i", path, "-frames:v", "1", "-an",
                     "-vf", "scale=320:-2:flags=lanczos", tmp],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, timeout=8)
                if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    break
            self._show_pixmap(QPixmap(tmp))
        except Exception:
            self._set_text("Video (no preview)")
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- helpers ------------------------------------------------------------

    def _set_text(self, text):
        self._pv.setMovie(None)
        self._pv.clear()
        self._pv.setText(text)

    def _stop_anim(self):
        if self._movie is not None:
            self._movie.stop()
            self._movie = None
        self._pv.setMovie(None)
# --------------------------------------------------------------------------- window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lumi — LCD Panel")
        self.resize(560, 560)

        # --- worker thread -------------------------------------------------
        self.thread = QThread()
        self.worker = DeviceWorker(size='2.1"')
        self.worker.moveToThread(self.thread)
        # wire worker signals -> UI
        self.worker.log.connect(self._log)
        self.worker.busy.connect(self._set_busy)
        self.worker.connected.connect(self._on_connected)
        self.worker.media_list.connect(self._on_media)
        self.worker.brightness_value.connect(self._on_brightness_value)
        self.worker.capacity_info.connect(self._on_capacity)
        self.worker.now_playing.connect(self._on_now_playing)
        self.worker.preview_request.connect(self._on_preview_request)
        self.worker.progress.connect(self._on_progress)
        self.worker.overlay_applied.connect(self._on_overlay_applied)
        self.thread.start()
        self._prog: QProgressDialog | None = None

        # Bridge the lumi library logger into the GUI log panel (thread-safe).
        self._log_sig = _LogSignal()
        self._log_sig.fired.connect(self._log)
        self._log_handler = GuiLogHandler(self._log_sig)
        self._log_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("lumi").addHandler(self._log_handler)

        self._build_ui()
        # Take over from the daemon (they can't share the serial port). The
        # daemon is restarted on close so boot-resume + shutdown-TURNOFF persist.
        self._resume_daemon_on_close = _pause_daemon()
        # Stop also cancels a pending systemd auto-restart. is-active alone
        # misses that state and allows the daemon to race the GUI for the port.
        if self._resume_daemon_on_close:
            self._log("Stopped the lumi daemon to take over the panel.")
            # Wait for the daemon to release the device.
            import time as _t
            for _ in range(20):  # up to ~10s
                if not _daemon_active():
                    break
                _t.sleep(0.5)
            # The daemon powers the panel down on exit (TURNOFF), which drops the
            # USB link; the awake port disappears and the device re-enumerates
            # dormant. Wait for that awake port to go away, so the connect below
            # wakes a dormant panel cleanly — instead of racing the dying port
            # (whose mid-handshake drop used to abort the whole process).
            for _ in range(20):  # up to ~10s for TURNOFF to take effect
                try:
                    if find_panel() is None:
                        break
                except Exception:
                    break
                _t.sleep(0.5)
        # Auto-connect on start (runs connect_dev on the worker thread)
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self.worker, "connect_dev", Qt.ConnectionType.QueuedConnection)

    # --- UI ----------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        page = QVBoxLayout(root)
        page.setContentsMargins(18, 18, 18, 12)
        page.setSpacing(12)

        # ---- Header -------------------------------------------------------
        header = QHBoxLayout()
        if LOGO_PATH.exists():
            logo = QLabel()
            pm = QPixmap(str(LOGO_PATH))
            if not pm.isNull():
                # Logo includes the "Lumi" wordmark, so it stands in for the title.
                logo.setPixmap(pm.scaledToHeight(40, Qt.TransformationMode.SmoothTransformation))
                header.addWidget(logo)
        self.status_label = QLabel("Not connected"); self.status_label.setObjectName("dim")
        header.addStretch(1); header.addWidget(self.status_label)
        page.addLayout(header)

        outer = QHBoxLayout()
        outer.setSpacing(12)
        page.addLayout(outer, 1)

        # ---- Left card: round preview + load/apply -----------------------
        left = QVBoxLayout()
        left.setContentsMargins(16, 16, 16, 16); left.setSpacing(12)
        # Header: "Preview" title left, rotate control top-right
        top = QHBoxLayout()
        lh = QLabel("Preview"); lh.setObjectName("h2")
        self.rotate = QComboBox()
        self.rotate.addItems(["0", "90", "180", "270"])
        self.rotate.setCurrentText("0")
        self.rotate.setToolTip("Degrees clockwise to rotate media when applying to the panel")
        rl = QLabel("Rotate:"); rl.setObjectName("dim")
        top.addWidget(lh); top.addStretch(1); top.addWidget(rl); top.addWidget(self.rotate)
        left.addLayout(top)
        self.preview = RoundPreview(size=330)
        left.addWidget(self.preview, alignment=Qt.AlignmentFlag.AlignCenter)
        self.now_label = QLabel("Nothing playing"); self.now_label.setObjectName("dim")
        self.now_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left.addWidget(self.now_label)
        row = QHBoxLayout()
        b_load = QPushButton("Load Media…"); b_load.clicked.connect(self._load_media)
        b_apply = QPushButton("Apply Media to Panel"); b_apply.setObjectName("primary")
        b_apply.clicked.connect(self._apply)
        b_reset = QPushButton("Reset Fit"); b_reset.clicked.connect(self.preview._reset_transform_to_fit)
        row.addWidget(b_load); row.addWidget(b_apply, 1); row.addWidget(b_reset)
        left.addLayout(row)
        hint = QLabel("Drag to pan · scroll to zoom inside the round display.")
        hint.setObjectName("dim"); hint.setWordWrap(True)
        left.addWidget(hint)
        outer.addWidget(self._card(left), 1)

        # ---- Right card: controls + library ------------------------------
        right = QVBoxLayout()
        right.setContentsMargins(16, 16, 16, 16); right.setSpacing(10)

        gb = QGroupBox("Brightness")
        h = QHBoxLayout(gb)
        self.slider = QSlider(Qt.Orientation.Horizontal); self.slider.setRange(0, 100); self.slider.setValue(50)
        self.slider.valueChanged.connect(lambda v: self.bright_val.setText(f"{v}%"))
        self.slider.sliderReleased.connect(lambda: self._invoke("set_brightness", self.slider.value()))
        self.bright_val = QLabel("50%"); self.bright_val.setMinimumWidth(40)
        h.addWidget(self.slider, 1); h.addWidget(self.bright_val)
        right.addWidget(gb)

        row2 = QHBoxLayout()
        b_on = QPushButton("Screen On"); b_on.clicked.connect(lambda: self._call("screen_on"))
        b_off = QPushButton("Screen Off"); b_off.clicked.connect(lambda: self._call("screen_off"))
        b_pwr = QPushButton("Power Off"); b_pwr.setObjectName("danger")
        b_pwr.clicked.connect(self._power_off)
        row2.addWidget(b_on); row2.addWidget(b_off); row2.addWidget(b_pwr)
        right.addLayout(row2)

        lib_row = QHBoxLayout()
        lib_label = QLabel("Library"); lib_label.setObjectName("h2")
        self.lib_mode = QComboBox()
        self.lib_mode.addItems(["Videos", "Images"])
        self.lib_mode.currentIndexChanged.connect(lambda: self._on_lib_mode())
        lib_row.addWidget(lib_label); lib_row.addStretch(1); lib_row.addWidget(self.lib_mode)
        right.addLayout(lib_row)
        self.usage_label = QLabel("Storage: …"); self.usage_label.setObjectName("dim")
        right.addWidget(self.usage_label)
        self.media = QListWidget()
        self.media.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.media.setToolTip(
            "Device directory listings are capped at 512 bytes (~15–18 entries).\n"
            "Files beyond that are hidden but still stored on the flash.\n\n"
            "• Deleting files frees space in this window, so previously-hidden\n"
            "  files surface as you remove others.\n"
            "• Files you upload are remembered locally and always shown here,\n"
            "  even when the device hides them.\n"
            "• Delete works on any known name (hidden or not)."
        )
        right.addWidget(self.media, 1)
        row3 = QHBoxLayout()
        b_refresh = QPushButton("Refresh list"); b_refresh.clicked.connect(self._refresh)
        b_play = QPushButton("Play"); b_play.setObjectName("primary")
        b_play.clicked.connect(self._play_selected)
        b_del = QPushButton("Delete"); b_del.setObjectName("danger")
        b_del.clicked.connect(self._delete)
        row3.addWidget(b_refresh); row3.addWidget(b_play, 1); row3.addWidget(b_del)
        right.addLayout(row3)
        outer.addWidget(self._card(right), 1)

        # ---- Log card (bottom) -------------------------------------------
        logc = QVBoxLayout(); logc.setContentsMargins(16, 12, 16, 12); logc.setSpacing(6)
        ll = QLabel("Log"); ll.setObjectName("h2"); logc.addWidget(ll)
        self.logw = QListWidget(); self.logw.setObjectName("log"); self.logw.setMaximumHeight(110)
        logc.addWidget(self.logw)
        page.addWidget(self._card(logc))

        # ---- Tabs: Media + Overlay ---------------------------------------
        self.tabs = QTabWidget()
        self.tabs.addTab(root, "Media")
        self.tabs.addTab(self._build_overlay_page(), "Overlay")
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar())
        self._loaded_path: str | None = None
        self.resize(940, 660)

    @staticmethod
    def _card(layout: QVBoxLayout) -> QFrame:
        f = QFrame(); f.setObjectName("card")
        layout.setContentsMargins(16, 16, 16, 16)
        f.setLayout(layout)
        return f

    # --- Overlay settings page --------------------------------------------

    def _build_overlay_page(self) -> QWidget:
        from lumi import overlay
        from PySide6.QtCore import QTimer
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 18, 18, 12); lay.setSpacing(12)

        cfg = overlay.load_config()

        self.overlay_cb = QCheckBox("Enable overlay over the playing media")
        self.overlay_cb.setToolTip("Persists to the daemon, so it stays on after the GUI closes.")
        self.overlay_cb.toggled.connect(self._on_overlay_toggled)
        lay.addWidget(self.overlay_cb)

        form = QFormLayout()
        self.ov_preset = QComboBox()
        cur = cfg.get("layout") or cfg.get("preset") or "clock"
        for p in overlay.list_layouts():
            self.ov_preset.addItem(p["label"], p["id"])
            if p["id"] == cur:
                self.ov_preset.setCurrentIndex(self.ov_preset.count() - 1)
        self.ov_preset.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow("Overlay", self.ov_preset)
        lay.addLayout(form)

        self.ov_stats = QGroupBox("Sensor readouts")
        grid = QGridLayout(self.ov_stats)
        for col, title in enumerate(("Show", "Sensor", "X", "Y", "Font (px)",
                                     "Text color", "Background", "Opacity")):
            grid.addWidget(QLabel(title), 0, col)
        self._stat_controls: dict[str, SensorControls] = {}
        for row, (key, settings) in enumerate(overlay.stat_settings(cfg).items(), 1):
            enabled = QCheckBox(overlay.STAT_LABELS[key])
            enabled.setChecked(settings["enabled"])
            source = QComboBox()
            source.setMinimumWidth(170)
            source.addItem("Automatic (fastest core)" if key == "cpu.freq" else "Automatic", key)
            if settings["source"] != key:
                source.addItem(settings["source"], settings["source"])
                source.setCurrentIndex(1)
            spins = []
            for name, maximum in (("x", 479), ("y", 479), ("size", 96)):
                spin = QSpinBox()
                spin.setRange(8 if name == "size" else 0, maximum)
                spin.setValue(settings[name])
                spin.setToolTip("Text center in 480 × 480 panel pixels" if name != "size"
                                else "Font size in panel pixels")
                spins.append(spin)
            color = QPushButton(settings["color"])
            color.setStyleSheet(f"color: {settings['color']}")
            color.clicked.connect(lambda _=False, k=key: self._choose_stat_color(k))
            background = QPushButton(settings["background_color"])
            self._style_background_button(background)
            background.clicked.connect(lambda _=False, k=key: self._choose_stat_color(k, True))
            opacity = QSpinBox()
            opacity.setRange(0, 100)
            opacity.setSuffix("%")
            opacity.setValue(settings["background_opacity"])
            opacity.setToolTip("Background opacity: 0% transparent, 100% solid. Text stays opaque.")
            controls = SensorControls(enabled, source, *spins, color, background, opacity)
            self._stat_controls[key] = controls
            for col, control in enumerate(controls):
                grid.addWidget(control, row, col)
            enabled.toggled.connect(self._save_stat_settings)
            source.currentIndexChanged.connect(self._save_stat_settings)
            for spin in spins:
                spin.valueChanged.connect(self._save_stat_settings)
            opacity.valueChanged.connect(self._save_stat_settings)
        refresh = QPushButton("Refresh available sensors")
        refresh.clicked.connect(self._discover_stat_sensors)
        grid.addWidget(refresh, 4, 0, 1, 2)
        self.ov_sensor_status = QLabel("Automatic selection; unavailable readings show --.")
        self.ov_sensor_status.setWordWrap(True)
        grid.addWidget(self.ov_sensor_status, 4, 2, 1, 6)
        self.ov_stats.setVisible(cur == "sensors")
        lay.addWidget(self.ov_stats)

        # Explicit Apply: cleanly (re)establish the selected overlay on the panel
        # with the UI locked. Changing the dropdown only updates the preview +
        # persists the choice (no live push — that accumulated corruption).
        self.ov_apply = QPushButton("Apply Overlay to Panel")
        self.ov_apply.setObjectName("primary")
        self.ov_apply.setToolTip("Replace the panel overlay with the current settings "
                                 "while keeping the media playing.")
        self.ov_apply.clicked.connect(self._on_apply_overlay)
        lay.addWidget(self.ov_apply)

        # Live WYSIWYG preview of the selected overlay — composed over a dark
        # round background via the same render path the device uses, refreshed
        # each second so clocks tick and stats update.
        pl = QLabel("Preview"); pl.setObjectName("h2")
        lay.addWidget(pl)
        self.ov_preview = QLabel()
        self.ov_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ov_preview.setMinimumSize(190, 190)
        self.ov_preview.setStyleSheet(
            "QLabel{background:#15151a;border:1px solid #2e2e38;border-radius:14px;}")
        lay.addWidget(self.ov_preview, alignment=Qt.AlignmentFlag.AlignCenter)

        lay.addStretch(1)
        hint = QLabel("Pick an overlay from the list. Drop a JSON file in "
                      "~/.config/lumi/overlays/ to add your own.")
        hint.setObjectName("dim"); hint.setWordWrap(True)
        lay.addWidget(hint)

        self._ov_preview_timer = QTimer(self)
        self._ov_preview_timer.timeout.connect(self._refresh_ov_preview)
        self._ov_preview_timer.start(1000)
        # Initial state is display-only. connect_dev owns resume + autostart;
        # emitting toggled here queued a second connection/start before resume.
        self.overlay_cb.blockSignals(True)
        self.overlay_cb.setChecked(bool(cfg.get("enabled")))
        self.overlay_cb.blockSignals(False)
        self._refresh_ov_preview()
        if cur == "sensors":
            self._discover_stat_sensors()
        return w

    def _refresh_ov_preview(self):
        """Render the currently-selected overlay into the Overlay-tab preview."""
        from lumi import overlay
        from PIL import Image, ImageDraw, ImageQt
        try:
            pid = self.ov_preset.currentData() or "clock"
            cfg = overlay.load_config()
            cfg["layout"] = pid
            layer = overlay.render_layer(cfg, 480, datetime.now())
            bg = Image.new("RGBA", (480, 480), (15, 15, 22, 255))
            comp = Image.alpha_composite(bg, layer)
            mask = Image.new("L", (480, 480), 0)
            ImageDraw.Draw(mask).ellipse([2, 2, 477, 477], fill=255)
            comp.putalpha(mask)
            comp = comp.resize((184, 184))
            self.ov_preview.setPixmap(QPixmap.fromImage(ImageQt.ImageQt(comp)))
        except Exception as e:
            logger.warning(f"overlay preview: {e}")

    def _on_preset_changed(self, _idx: int):
        """Layout changed in the dropdown — update the live preview and persist
        the choice. Use the Apply button to push it to the panel (a clean,
        locked re-establishment; live switching accumulated device-side
        corruption)."""
        from lumi import overlay
        pid = self.ov_preset.currentData()
        if pid:
            overlay.set_layout(pid)
        self.ov_stats.setVisible(pid == "sensors")
        if pid == "sensors":
            self._discover_stat_sensors()
        self._refresh_ov_preview()

    def _save_stat_settings(self, *_):
        from lumi import overlay
        cfg = overlay.load_config()
        cfg["stats"] = {}
        for key, controls in self._stat_controls.items():
            cfg["stats"][key] = {
                "enabled": controls.enabled.isChecked(),
                "source": controls.source.currentData(),
                "x": controls.x.value(), "y": controls.y.value(),
                "size": controls.size.value(), "color": controls.color.text(),
                "background_color": controls.background.text(),
                "background_opacity": controls.opacity.value(),
            }
        overlay.save_config(cfg)
        self._refresh_ov_preview()

    @staticmethod
    def _style_background_button(button):
        color = QColor(button.text())
        ink = "#000000" if color.lightnessF() > .5 else "#FFFFFF"
        button.setStyleSheet(f"background-color: {color.name()}; color: {ink};")

    def _choose_stat_color(self, key, background=False):
        controls = self._stat_controls[key]
        button = controls.background if background else controls.color
        color = QColorDialog.getColor(QColor(button.text()), self,
                                      "Background color" if background else "Readout color")
        if color.isValid():
            button.setText(color.name().upper())
            if background:
                self._style_background_button(button)
            else:
                button.setStyleSheet(f"color: {color.name()}")
            self._save_stat_settings()

    def _discover_stat_sensors(self):
        """Poll in a background thread so slow driver utilities do not freeze Qt."""
        from concurrent.futures import ThreadPoolExecutor
        from lumi.sensors import Metrics
        if getattr(self, "_sensor_scan", None) is not None:
            return
        self.ov_sensor_status.setText("Detecting sensors…")
        def scan():
            metrics = Metrics()
            metrics.poll()
            return metrics.snapshot()
        self._sensor_executor = ThreadPoolExecutor(max_workers=1)
        self._sensor_scan = self._sensor_executor.submit(scan)
        self._sensor_scan_timer = QTimer(self)
        self._sensor_scan_timer.timeout.connect(self._finish_sensor_scan)
        self._sensor_scan_timer.start(100)

    def _finish_sensor_scan(self):
        if self._sensor_scan is None or not self._sensor_scan.done():
            return
        self._sensor_scan_timer.stop()
        self._sensor_scan_timer.deleteLater()
        try:
            samples = self._sensor_scan.result()
            for key, controls in self._stat_controls.items():
                combo = controls.source
                selected = combo.currentData()
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("Automatic (fastest core)" if key == "cpu.freq" else "Automatic", key)
                for name, sample in sorted(samples.items()):
                    if name == key:
                        continue
                    if (key == "cpu.freq" and sample.unit == "MHz") or (
                            key != "cpu.freq" and sample.unit == "°C"):
                        combo.addItem(name, name)
                idx = combo.findData(selected)
                if idx < 0:
                    combo.addItem(f"{selected} (unavailable)", selected)
                    idx = combo.count() - 1
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
            self.ov_sensor_status.setText("Sensors refreshed. Choose a sensor, then Apply to Panel.")
        except Exception as e:
            self.ov_sensor_status.setText(f"Sensor discovery failed: {e}")
        finally:
            self._sensor_executor.shutdown(wait=False)
            self._sensor_scan = None

    def _on_apply_overlay(self):
        """Push the selected overlay to the panel via a clean, locked
        re-establishment (runs on the worker thread; the busy/progress dialog
        locks the UI for the whole operation)."""
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self.worker, "apply_overlay",
                                 Qt.ConnectionType.QueuedConnection)

    def _on_overlay_applied(self):
        """After a clean Apply: sync the enable checkbox + media-preview overlay
        to the persisted config."""
        from lumi import overlay
        try:
            on = bool(overlay.load_config().get("enabled"))
        except Exception:
            on = True
        self.overlay_cb.blockSignals(True)
        self.overlay_cb.setChecked(on)
        self.overlay_cb.blockSignals(False)
        self.preview.set_overlay_enabled(on)
        self._refresh_ov_preview()

    # --- UI helpers --------------------------------------------------------

    def _log(self, msg: str):
        self.logw.addItem(msg); self.logw.scrollToBottom()

    def _set_busy(self, on: bool):
        self.slider.setEnabled(not on)
        # Lock the overlay controls too (Apply/connect/upload must not race with
        # an overlay switch — and the device can't be overwhelmed mid-transfer).
        for w in (getattr(self, "ov_apply", None), getattr(self, "ov_preset", None),
                  getattr(self, "ov_stats", None),
                  getattr(self, "overlay_cb", None)):
            if w is not None:
                w.setEnabled(not on)
        if on:
            # Blocking modal progress dialog — only shown for slow ops (busy is
            # emitted solely by connect/upload/apply/delete). Indeterminate until
            # a progress signal makes it determinate. No cancel button: these
            # ops can't be safely aborted mid-serial-transfer.
            if self._prog is None:
                self._prog = QProgressDialog("Working…", "", 0, 100, self)
                self._prog.setWindowModality(Qt.WindowModality.WindowModal)
                self._prog.setMinimumDuration(0)
                self._prog.setCancelButton(None)
                self._prog.setAutoClose(False)
                self._prog.setAutoReset(False)
            self._prog.setLabelText("Working…")
            self._prog.setRange(0, 0)   # indeterminate busy bar
            self._prog.setValue(0)
            self._prog.show()
        elif self._prog is not None:
            self._prog.reset()
            self._prog.hide()

    def _on_connected(self, info: str):
        self.status_label.setText(info)
        # Sync the overlay checkbox + editor to the persisted config (without
        # firing the toggle handler, which would double-start the worker loop).
        try:
            from lumi import overlay
            cfg = overlay.load_config()
            on = bool(cfg.get("enabled"))
        except Exception:
            cfg = None; on = False
        self.overlay_cb.blockSignals(True)
        self.overlay_cb.setChecked(on)
        self.overlay_cb.blockSignals(False)
        self.preview.set_overlay_enabled(on)
        if cfg is not None:
            pid = cfg.get("layout") or cfg.get("preset") or "clock"
            for i in range(self.ov_preset.count()):
                if self.ov_preset.itemData(i) == pid:
                    self.ov_preset.blockSignals(True)
                    self.ov_preset.setCurrentIndex(i)
                    self.ov_preset.blockSignals(False)
                    break
        if hasattr(self, "_refresh_ov_preview"):
            self._refresh_ov_preview()

    def _on_media(self, files: list):
        self.media.clear()
        for f in files:
            self.media.addItem(f)

    def _on_brightness_value(self, level: int):
        # Sync the slider to the persisted value without re-firing the command.
        self.slider.blockSignals(True)
        self.slider.setValue(level)
        self.bright_val.setText(f"{level}%")
        self.slider.blockSignals(False)

    def _on_capacity(self, text: str):
        self.usage_label.setText("💾  " + text)

    def _on_now_playing(self, name: str):
        self.now_label.setText(f"▶  Now playing: {name}" if name else "Nothing playing")

    def _on_preview_request(self, device_path: str):
        """Restore the preview from the local copy of a device file (reopen /
        play-from-list), so the preview matches what's actually on the panel."""
        local = M.local_copy_path(device_path)
        if local.exists():
            self.preview.load_file(str(local))
            self._loaded_path = str(local)
            self._log(f"Preview restored: {local.name}")
        else:
            self.preview.show_placeholder("No preview available")
            self._loaded_path = None
            self._log(f"No local preview for {device_path.rsplit('/', 1)[-1]}")

    def _on_progress(self, stage: str, frac: float):
        if self._prog is None:
            return
        self._prog.setLabelText(stage)
        if frac >= 0.0:
            self._prog.setRange(0, 100)
            self._prog.setValue(int(frac * 100))
        else:
            self._prog.setRange(0, 0)   # indeterminate

    # --- actions -----------------------------------------------------------

    def _load_media(self):
        dialog = PreviewFileDialog(
            self, "Choose media to preview",
            "", "Media (*.png *.jpg *.jpeg *.bmp *.gif *.mp4 *.mov *.avi *.mkv *.webm)"
        )
        dialog.setViewMode(QFileDialog.ViewMode.List)
        if dialog.exec() == QFileDialog.DialogCode.Accepted:
            path = dialog.selectedFiles()[0] if dialog.selectedFiles() else ""
            if path:
                self._loaded_path = path
                self.preview.load_file(path)
                self._log(f"Loaded {path} — position it, then Apply.")

    def _apply(self):
        """Render the preview framing (position + rotation) and send to panel."""
        if not self._loaded_path:
            self._log("Load media first.")
            return
        # Hand geometry + rotation to the worker, then run apply_positioned.
        self.worker._geom = self.preview.render_geom()
        self.worker._rotate = int(self.rotate.currentText())
        self._invoke("apply_positioned", self._loaded_path)

    def _power_off(self):
        """Actually power down the panel (TURNOFF) — it'll drop off USB and sleep."""
        self._call("power_off")
        self._log("Panel powered off (USB link will drop).")

    def _refresh(self):
        self._call("refresh_media")

    def _on_lib_mode(self):
        """Switch the library list between Videos and Images."""
        self.worker._lib_mode = "image" if self.lib_mode.currentText() == "Images" else "video"
        self._refresh()

    def _on_overlay_toggled(self, on: bool):
        """Start/stop the live overlay on the worker thread; also mirror it into
        the media preview (WYSIWYG over the loaded media)."""
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        self.preview.set_overlay_enabled(on)
        QMetaObject.invokeMethod(self.worker, "set_overlay_enabled",
                                 Qt.ConnectionType.QueuedConnection,
                                 Q_ARG("bool", bool(on)))

    def _play_selected(self):
        """Play the currently selected list item (dedicated button, not a click)."""
        items = self.media.selectedItems()
        if not items:
            self._log("Select a file to play first.")
            return
        self._invoke("play", items[0].text())

    def _delete(self):
        """Delete the selected file(s) from the device after confirmation."""
        items = self.media.selectedItems()
        if not items:
            self._log("Select one or more files to delete first.")
            return
        names = [it.text() for it in items]
        what = names[0] if len(names) == 1 else f"{len(names)} files"
        if self._confirm_delete(what):
            # Hand the names to the worker, then invoke the no-arg slot (avoids
            # marshalling a list through QMetaObject.invokeMethod).
            self.worker._del_names = names
            self._call("delete_files")

    def _confirm_delete(self, what: str) -> bool:
        b = QMessageBox.warning(
            self, "Delete from device",
            f"Delete {what} from the device?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return b == QMessageBox.StandardButton.Yes

    def _call(self, method: str):
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self.worker, method, Qt.ConnectionType.QueuedConnection)

    def _invoke(self, method: str, arg):
        _invoke_method(self.worker, method, arg)

    def closeEvent(self, event):
        """On quit: leave the panel playing its last media; restart the daemon
        (if it was running) so boot-resume + shutdown-TURNOFF keep working."""
        from PySide6.QtCore import QCoreApplication, QMetaObject, Qt
        if getattr(self, "_closing", False):
            event.accept()
            return
        self._closing = True
        if getattr(self, "_sensor_scan", None) is not None:
            self._sensor_scan_timer.stop()
            self._sensor_executor.shutdown(wait=False, cancel_futures=True)
            self._sensor_scan = None
        QCoreApplication.processEvents()
        # Stop the worker's overlay QTimer on its OWN thread before quitting,
        # so it isn't stopped/destroyed from the main thread at teardown.
        if self.thread.isRunning():
            try:
                QMetaObject.invokeMethod(self.worker, "release_panel",
                                         Qt.ConnectionType.BlockingQueuedConnection)
            except Exception:
                pass
        self.thread.quit()
        self.thread.wait(2000)
        super().closeEvent(event)
        # Hand control back to the daemon so shutdown still TURNOFFs the panel.
        if self._resume_daemon_on_close or _daemon_enabled():
            _daemon_start()


# Q_ARG helper for str/int args
def _invoke_method(worker, method, arg):
    from PySide6.QtCore import QMetaObject, Qt, Q_ARG
    t = "QString" if isinstance(arg, str) else "int"
    QMetaObject.invokeMethod(worker, method, Qt.ConnectionType.QueuedConnection, Q_ARG(t, arg))


def main():
    import sys
    import signal
    app = QApplication(sys.argv)
    if LOGO_PATH.exists():
        app.setWindowIcon(QIcon(str(LOGO_PATH)))
    from lumi.theme import apply_theme
    apply_theme(app)
    w = MainWindow()
    # Finish any in-flight serial transfer before exiting on SIGTERM. Abrupt
    # termination can leave the panel waiting for the remainder of a frame.
    signal.signal(signal.SIGTERM, lambda *_: w.close())
    signal.signal(signal.SIGINT, lambda *_: w.close())
    w.show()
    try:
        return app.exec()
    finally:
        # Cover quitting the event loop as well as closing the window, so the
        # port is released and the daemon resumes for either exit path.
        if not getattr(w, "_closing", False):
            w.close()
        w.thread.quit()
        w.thread.wait(2000)


if __name__ == "__main__":
    import sys
    sys.exit(main())
