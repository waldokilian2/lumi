"""Round display preview widget for the lumi GUI.

Shows loaded media inside a circular viewport that mirrors the physical 2.1"
panel (the bezel clips corners). Drag to pan, scroll to zoom — WYSIWYG. The
chosen transform is exported so the upload step can bake the same framing into
the rendered output.

Media support:
  - Images (PNG/JPG/BMP): static QPixmap.
  - GIFs: animated via QMovie (the preview animates).
  - Video: first frame only (extracted via ffmpeg) — positioning applies on
    upload through the ffmpeg filter chain.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import (
    QPixmap, QMovie, QPainter, QPen, QColor, QFont,
    QLinearGradient, QRadialGradient, QBrush,
)
from lumi.theme import GRADIENT_STOPS
from PySide6.QtWidgets import QWidget


class RoundPreview(QWidget):
    """A circular preview of the 2.1" panel; drag to pan, wheel to zoom."""

    def __init__(self, size: int = 320, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._diameter = size
        # The device circle is inset from the widget edge so there's room for a
        # soft glow halo behind it. Content is laid out in this device square
        # (it maps 1:1 to the 480px panel), keeping the preview WYSIWYG.
        self._device_dia = size - 50
        # Source pixmap (current frame) + viewing transform
        self._source: QPixmap | None = None
        self._placeholder: str | None = None
        self._movie: QMovie | None = None
        # Animated-video preview: a short extracted frame loop driven by a timer.
        self._frames: list[QPixmap] = []
        self._frame_idx = 0
        self._frame_timer: QTimer | None = None
        self._scale = 1.0
        self._offset = QPoint(0, 0)          # pan offset (in widget coords)
        self._drag_start: QPoint | None = None
        self._offset_start = QPoint(0, 0)
        # "Fit" transform defaults so a freshly loaded image is centered+scaled
        self._auto_fit = True
        self.setMouseTracking(True)
        # Live overlay (clock) shown WYSIWYG over the media, rendered with the
        # same PIL path the device uses and refreshed once per second.
        self._overlay_on = False
        self._overlay_pm: QPixmap | None = None
        self._overlay_timer: QTimer | None = None

    # ---- loading ----------------------------------------------------------

    def load_image(self, pixmap: QPixmap):
        self._stop_movie()
        self._source = pixmap
        self._placeholder = None
        if self._auto_fit:
            self._reset_transform_to_fit()

    def show_placeholder(self, text: str):
        """Clear the preview and show a centered message (e.g. when the
        now-playing file has no local copy to restore from)."""
        self._stop_movie()
        self._source = None
        self._placeholder = text
        self.update()

    def load_file(self, path: str):
        self._placeholder = None
        p = Path(path)
        suf = p.suffix.lower()
        if suf == ".gif":
            self._stop_movie()
            movie = QMovie(path)
            if not movie.isValid():
                self.show_placeholder("Couldn't load preview")
                return
            self._movie = movie
            self._movie.frameChanged.connect(self._on_movie_frame)
            self._source = self._movie.currentPixmap()
            if self._auto_fit:
                self._reset_transform_to_fit()
            self._movie.start()
        elif suf in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            # Animated preview: extract a short frame loop and cycle it on a timer.
            self._stop_movie()
            frames, fps = self._extract_video_frames(path)
            if frames:
                self._frames = frames
                self._frame_idx = 0
                self._source = frames[0]
                if self._auto_fit:
                    self._reset_transform_to_fit()
                self._start_frame_timer(round(1000 / fps))
            else:
                # Fallback: show a single extracted frame.
                pm = self._extract_one_frame(path)
                if not pm.isNull():
                    self.load_image(pm)
                else:
                    self.show_placeholder("Couldn't load preview")
        else:
            pm = QPixmap(path)
            if not pm.isNull():
                self.load_image(pm)
            else:
                self.show_placeholder("Couldn't load preview")

    def _stop_movie(self):
        """Stop any animation (QMovie for GIFs, frame timer for videos)."""
        if self._movie is not None:
            self._movie.stop()
            self._movie = None
        if self._frame_timer is not None:
            self._frame_timer.stop()
        self._frames = []
        self._frame_idx = 0

    # ---- video frame loop ------------------------------------------------

    def _extract_video_frames(self, path: str):
        """Extract a short loop of frames for an animated preview.
        Returns (list[QPixmap], fps). Capped to stay light."""
        import subprocess, tempfile, os, glob
        fps = 10
        max_frames = 72
        scale = max(240, self._device_dia + 40)
        tmpd = tempfile.mkdtemp(prefix="lumi_prev_")
        pat = os.path.join(tmpd, "f_%05d.png")
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-an",
             "-r", str(fps), "-frames:v", str(max_frames),
             "-vf", f"scale={scale}:-2:flags=lanczos", pat],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        frames = [QPixmap(f) for f in sorted(glob.glob(os.path.join(tmpd, "f_*.png")))]
        frames = [pm for pm in frames if not pm.isNull()]
        for f in glob.glob(os.path.join(tmpd, "*")):
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(tmpd)
        except OSError:
            pass
        return frames, fps

    def _extract_one_frame(self, path: str) -> QPixmap:
        """Grab a single frame via ffmpeg (seek ~0.3s in; fall back to frame 0)."""
        import subprocess, tempfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
        for seek in (["-ss", "0.3"], []):
            cmd = ["ffmpeg", "-y", *seek, "-i", path, "-frames:v", "1", "-an", tmp]
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                break
        pm = QPixmap(tmp)
        if os.path.exists(tmp):
            os.unlink(tmp)
        return pm

    def _start_frame_timer(self, ms: int):
        if self._frame_timer is None:
            self._frame_timer = QTimer(self)
            self._frame_timer.timeout.connect(self._advance_frame)
        self._frame_timer.start(max(20, int(ms)))

    def _advance_frame(self):
        if not self._frames:
            return
        self._frame_idx = (self._frame_idx + 1) % len(self._frames)
        self._source = self._frames[self._frame_idx]
        self.update()

    def _on_movie_frame(self):
        if self._movie:
            self._source = self._movie.currentPixmap()
            self.update()

    # ---- overlay (clock) -------------------------------------------------

    def set_overlay_enabled(self, on: bool):
        """Show/hide the WYSIWYG clock overlay in the preview."""
        self._overlay_on = bool(on)
        if on:
            self._render_overlay_pixmap()
            if self._overlay_timer is None:
                self._overlay_timer = QTimer(self)
                self._overlay_timer.timeout.connect(self._render_overlay_pixmap)
            self._overlay_timer.start(1000)
        else:
            if self._overlay_timer is not None:
                self._overlay_timer.stop()
            self._overlay_pm = None
        self.update()

    def _render_overlay_pixmap(self):
        try:
            from datetime import datetime
            from lumi import overlay
            from PIL import ImageQt
            layer = overlay.render_layer(overlay.load_config(), 480, datetime.now())
            qim = ImageQt.ImageQt(layer)
            self._overlay_pm = QPixmap.fromImage(qim)
        except Exception:
            self._overlay_pm = None
        self.update()

    # ---- transform --------------------------------------------------------

    def _reset_transform_to_fit(self):
        """Center the source and scale so it just fits (contain) the circle."""
        if self._source is None or self._source.isNull():
            return
        sw, sh = self._source.width(), self._source.height()
        # Inscribed square of the circle = device diameter
        fit = min(self._device_dia / sw, self._device_dia / sh)
        self._scale = fit
        self._offset = QPoint(0, 0)
        self.update()

    # ---- interaction ------------------------------------------------------

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.position().toPoint()
            self._offset_start = QPoint(self._offset)

    def mouseMoveEvent(self, e):
        if self._drag_start is not None:
            delta = e.position().toPoint() - self._drag_start
            self._offset = self._offset_start + delta
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag_start = None

    def wheelEvent(self, e):
        # Zoom around the cursor
        steps = e.angleDelta().y() / 120
        factor = 1.15 ** steps
        new_scale = max(0.05, self._scale * factor)
        # Keep the point under the cursor stable
        cursor = e.position().toPoint() - QPoint(self.width() // 2, self.height() // 2)
        self._offset = cursor - (cursor - self._offset) * (new_scale / self._scale)
        self._scale = new_scale
        self.update()

    # ---- paint ------------------------------------------------------------

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Card background
        painter.fillRect(self.rect(), QColor(33, 33, 42))

        cx = cy = self._diameter / 2
        half = self._diameter / 2
        dev_r = self._device_dia / 2          # screen radius (inscribed circle)
        bezel_outer = dev_r + 10              # outer edge of the glowing frame

        # 1) Glow halo behind the device — soft brand bloom (violet → magenta).
        bloom = QRadialGradient(cx, cy, half)
        t = dev_r / half
        bloom.setColorAt(0.0, QColor(122, 99, 255, 0))
        bloom.setColorAt(max(0.0, t - 0.05), QColor(122, 99, 255, 0))
        bloom.setColorAt(t, QColor(213, 91, 255, 150))
        bloom.setColorAt(1.0, QColor(213, 91, 255, 0))
        painter.setBrush(QBrush(bloom)); painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPoint(int(cx), int(cy)), int(half), int(half))

        # 2) Drop shadow under the device for depth.
        sh = QRadialGradient(cx + 4, cy + 8, bezel_outer + 14)
        sh.setColorAt(0.0, QColor(0, 0, 0, 150))
        sh.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setBrush(QBrush(sh)); painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPoint(int(cx + 3), int(cy + 7)),
                            int(bezel_outer + 12), int(bezel_outer + 12))

        # 3) Luminous gradient bezel — the logo's cool→warm sweep as the frame.
        grad = QLinearGradient(cx - bezel_outer, cy, cx + bezel_outer, cy)
        for pos, col in GRADIENT_STOPS:
            grad.setColorAt(pos, QColor(col))
        painter.setBrush(QBrush(grad)); painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPoint(int(cx), int(cy)), int(bezel_outer), int(bezel_outer))

        # 4) Screen: clip to the inscribed circle, fill "off" black, draw content.
        painter.save()
        painter.setClipRegion(QRegion_circle(int(cx), int(cy), int(dev_r)))
        painter.fillRect(self.rect(), QColor(8, 8, 12))
        if self._source is not None and not self._source.isNull():
            painter.translate(cx + self._offset.x(), cy + self._offset.y())
            painter.scale(self._scale, self._scale)
            painter.drawPixmap(-self._source.width() / 2, -self._source.height() / 2, self._source)
        elif self._placeholder:
            painter.setPen(QColor(150, 150, 160))
            painter.setFont(QFont("Sans", 10))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
        # Overlay (clock) in panel space: fixed to the round screen, not panned
        # with the media. Drawn while still clipped to the inscribed circle.
        if self._overlay_on and self._overlay_pm is not None and not self._overlay_pm.isNull():
            painter.resetTransform()
            painter.drawPixmap(
                int(cx - dev_r), int(cy - dev_r), int(2 * dev_r), int(2 * dev_r),
                self._overlay_pm,
            )
        painter.restore()

        # 5) Thin dark separator between the screen and the glowing bezel.
        painter.setPen(QPen(QColor(0, 0, 0, 150), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPoint(int(cx), int(cy)), int(dev_r), int(dev_r))

    # ---- export transform for rendering -----------------------------------

    def render_geom(self) -> dict:
        """Return how to bake the preview's framing into a 480x480 render.

        'fit_scale' = scale applied to source so the widget's view maps to 480px.
        'offset' = (dx, dy) in 480-space (relative to centered).
        """
        panel = 480
        widget = self._device_dia
        k = panel / widget  # widget px -> panel px
        return {
            "src_w": self._source.width() if self._source else 0,
            "src_h": self._source.height() if self._source else 0,
            "scale": self._scale * k,            # source px -> panel px
            "offset": (self._offset.x() * k, self._offset.y() * k),
        }


# QRegion circle helper (avoids constructing a QPainterPath each paint)
def QRegion_circle(cx: int, cy: int, r: int):
    from PySide6.QtGui import QRegion
    return QRegion(cx - r, cy - r, r * 2, r * 2, QRegion.RegionType.Ellipse)

