"""Dark theme for the lumi GUI (Qt Style Sheet + brand gradient).

The accent is the logo's luminous gradient, flowing cool→warm:
  Ice Blue #6FD8FF → Electric Blue #5C8FF → Violet #7A63FF →
  Magenta #D55BFF → Hot Pink #FF6CB4 → Coral #FF8B7A → Amber #FFB44C

QSS supports gradients only for `background`, so we use the gradient there and a
solid focal color (Magenta) for borders/text/focus where a gradient won't apply.
"""

# Continuous gradient stops (cool → warm), matching Lumi.png.
GRADIENT_STOPS = [
    (0.0,    "#6FD8FF"),  # ice blue
    (0.1667, "#5C8FFF"),  # electric blue
    (0.3333, "#7A63FF"),  # violet
    (0.5,    "#D55BFF"),  # magenta (focal)
    (0.6667, "#FF6CB4"),  # hot pink
    (0.8333, "#FF8B7A"),  # coral
    (1.0,    "#FFB44C"),  # amber
]

# Solid accent = the gradient's focal magenta, for borders/text/focus.
ACCENT = "#D55BFF"


def qss_gradient(stops=GRADIENT_STOPS) -> str:
    """A horizontal qlineargradient string for use as a QSS `background`."""
    return ("qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            + ", ".join(f"stop:{p:.4f} {c}" for p, c in stops) + ")")


_QSS = """
* {
    font-family: 'Segoe UI', 'Inter', 'Cantarell', 'Sans Serif';
    font-size: 13px;
    color: #e6e6ea;
    outline: 0;
}
QMainWindow, QWidget { background: #15151a; }
/* Labels inherit the dark QWidget bg by default; make them transparent so they
   sit cleanly on the lighter card panels instead of showing a dark patch. */
QLabel { background: transparent; }

QLabel#h1   { font-size: 20px; font-weight: 700; color: #ffffff; }
QLabel#h2   { font-size: 13px; font-weight: 600; color: #c8c8d2; }
QLabel#dim  { color: #8a8a96; font-size: 11px; }

QFrame#card {
    background: #21212a;
    border: 1px solid #2e2e38;
    border-radius: 14px;
}

QGroupBox {
    border: 1px solid #2e2e38;
    border-radius: 10px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    background: transparent;
    font-weight: 600;
    color: #b0b0bc;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }

QPushButton {
    background: #2c2c36; border: 1px solid #3a3a46; border-radius: 9px;
    padding: 8px 14px; color: #e6e6ea; min-height: 18px;
}
QPushButton:hover    { background: #36364a; border-color: @ACCENT@; }
QPushButton:pressed  { background: #23232e; }
QPushButton:disabled { background: #22222a; color: #5a5a66; border-color: #2a2a34; }
QPushButton#primary {
    background: @ACCENT_GRADIENT@; color: #0b0b0f; border: 1px solid transparent;
    font-weight: 700;
}
QPushButton#primary:hover    { border: 1px solid rgba(255,255,255,0.5); }
QPushButton#primary:pressed  { border: 1px solid rgba(0,0,0,0.35); }
QPushButton#primary:disabled { background: #2a2a36; color: #5a5a66; }
QPushButton#danger {
    background: #2a2a34; color: #ff6b6b; border: 1px solid #5a2a2a;
}
QPushButton#danger:hover { background: #3a2a2a; border-color: #ff6b6b; }

QSlider::groove:horizontal { height: 6px; background: #2c2c36; border-radius: 3px; }
QSlider::sub-page:horizontal { background: @ACCENT_GRADIENT@; border-radius: 3px; }
QSlider::add-page:horizontal { background: #2c2c36; border-radius: 3px; }
QSlider::handle:horizontal {
    background: #ffffff; width: 16px; height: 16px; margin: -6px 0;
    border-radius: 8px; border: 2px solid @ACCENT@;
}
QSlider::handle:horizontal:hover { background: @ACCENT@; border-color: #ffffff; }

QListWidget {
    background: #181820; border: 1px solid #2e2e38; border-radius: 10px; padding: 6px;
}
QListWidget::item { padding: 7px 10px; border-radius: 6px; color: #c8c8d2; }
QListWidget::item:hover    { background: #24242e; }
QListWidget::item:selected { background: @ACCENT_GRADIENT@; color: #0b0b0f; font-weight: 600; }
QListWidget#log { font-family: 'Mono', 'Menlo', 'Consolas'; font-size: 11px; color: #7a7a86; }
QListWidget#log::item { padding: 1px 6px; }
QListWidget#log::item:selected { background: transparent; color: #c8c8d2; }

QComboBox {
    background: #2c2c36; border: 1px solid #3a3a46; border-radius: 9px;
    padding: 6px 12px; min-width: 60px;
}
QComboBox:hover { border-color: @ACCENT@; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #21212a; border: 1px solid #2e2e38; border-radius: 8px; padding: 4px;
    selection-background-color: @ACCENT@; selection-color: #0b0b0f;
}

QStatusBar { background: #111116; color: #6a6a76; }
QStatusBar::item { border: none; }
"""

QSS = _QSS.replace("@ACCENT_GRADIENT@", qss_gradient()).replace("@ACCENT@", ACCENT)


def apply_theme(app):
    app.setStyleSheet(QSS)
