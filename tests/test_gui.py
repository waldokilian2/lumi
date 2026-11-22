from unittest.mock import patch

import pytest

from lumi import overlay, sensors


@pytest.fixture
def sensor_editor(qt_app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMainWindow

    from lumi.gui import DeviceWorker, MainWindow

    monkeypatch.setattr(overlay, "STATE_DIR", tmp_path)
    monkeypatch.setattr(overlay, "CONFIG_FILE", tmp_path / "overlay.json")
    overlay.save_config(
        {
            "enabled": False,
            "layout": "sensors",
            "stats": {"gpu.temp": {"source": "temp.removed.gpu"}},
        }
    )
    source = sensors.Source()
    source.poll = lambda: {
        "cpu.freq.core0": (4200, "MHz", 0),
        "temp.gpu.edge": (50, "°C", 0.5),
    }
    monkeypatch.setattr(sensors, "default_sources", lambda: [source])
    monkeypatch.setattr(MainWindow, "_refresh_ov_preview", lambda self: None)
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window.worker = DeviceWorker(size='2.1"')
    window.setCentralWidget(window._build_overlay_page())
    window._sensor_scan.result(timeout=5)
    window._finish_sensor_scan()
    try:
        yield window
    finally:
        window._closing = True
        window._ov_preview_timer.stop()
        if window._sensor_scan is not None:
            window._sensor_scan.result(timeout=5)
            window._finish_sensor_scan()
        window.deleteLater()
        qt_app.processEvents()


def test_sensor_controls_persist_and_layout_switch_keeps_settings(sensor_editor):
    from PySide6.QtGui import QColor

    controls = sensor_editor._stat_controls["cpu.freq"]
    controls.x.setValue(140)
    controls.y.setValue(320)
    controls.size.setValue(42)
    controls.opacity.setValue(45)
    controls.source.setCurrentIndex(controls.source.findData("cpu.freq.core0"))
    with patch("lumi.gui.QColorDialog.getColor", return_value=QColor("#223344")):
        sensor_editor._choose_stat_color("cpu.freq", background=True)
    saved = overlay.load_config()["stats"]["cpu.freq"]
    assert (saved["x"], saved["y"], saved["size"]) == (140, 320, 42)
    assert saved["source"] == "cpu.freq.core0"
    assert saved["background_color"] == "#223344"
    assert saved["background_opacity"] == 45
    assert saved["color"] == "#FFFFFF"
    sensor_editor.ov_preset.setCurrentIndex(sensor_editor.ov_preset.findData("clock"))
    assert sensor_editor.ov_stats.isHidden()
    assert overlay.load_config()["stats"]["cpu.freq"] == saved


def test_sensor_discovery_preserves_an_unavailable_selection(sensor_editor):
    controls = sensor_editor._stat_controls["gpu.temp"]
    assert controls.source.currentData() == "temp.removed.gpu"
    assert "unavailable" in controls.source.currentText()
    assert controls.source.findData("temp.gpu.edge") >= 0
    assert "fastest core" in sensor_editor._stat_controls["cpu.freq"].source.itemText(0)
