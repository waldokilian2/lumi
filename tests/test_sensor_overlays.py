import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from lumi import overlay, sensors
from lumi.device import Device
from lumi.lcd.lcd_comm import LcdComm
from lumi.media import read_text
from lumi.widgets import DrawContext
from lumi.widgets.fonts import FontResolver


def test_settings_roundtrip_and_legacy_config(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "STATE_DIR", tmp_path)
    monkeypatch.setattr(overlay, "CONFIG_FILE", tmp_path / "overlay.json")
    overlay.CONFIG_FILE.write_text(json.dumps({"preset": "sysstats", "enabled": False}))
    cfg = overlay.load_config()
    assert cfg["layout"] == "sysstats"
    cfg["layout"] = "sensors"
    cfg["stats"]["gpu.temp"].update(
        source="gpu.temp.GPU-123", x=130, y=280, size=42, color="#123ABC"
    )
    overlay.save_config(cfg)
    overlay.set_enabled(True)
    loaded = overlay.load_config()
    assert loaded["stats"] == cfg["stats"]
    assert loaded["layout"] == "sensors"
    assert loaded["enabled"] is True
    overlay.set_layout("clock")
    assert overlay.load_config()["stats"] == cfg["stats"]


def test_invalid_settings_are_bounded():
    cfg = {"stats": {"cpu.freq": {"x": -8, "y": 999, "size": "oops", "color": "red"}}}
    settings = overlay.stat_settings(cfg)["cpu.freq"]
    assert (settings["x"], settings["y"], settings["size"], settings["color"]) == (
        0,
        479,
        30,
        "#FFFFFF",
    )


def test_layout_uses_selected_sensor_position_color_and_size():
    cfg = {
        "layout": "sensors",
        "stats": {
            "cpu.freq": {"enabled": False},
            "cpu.temp": {"enabled": False},
            "gpu.temp": {
                "source": "temp.amdgpu.edge",
                "x": 120,
                "y": 140,
                "size": 24,
                "color": "#12AB34",
            },
        },
    }
    layout = overlay.get_layout(cfg)
    assert layout.keys == ["temp.amdgpu.edge"]
    source = sensors.Source()
    source.poll = lambda: {"temp.amdgpu.edge": (54, "°C", 0.54)}
    metrics = sensors.Metrics([source])
    metrics.poll()
    ctx = DrawContext(480, datetime.now(), metrics, FontResolver())
    image = layout.render(ctx)
    box = image.getbbox()
    assert box and abs((box[0] + box[2]) / 2 - 120) <= 1
    assert abs((box[1] + box[3]) / 2 - 140) <= 1
    assert box[3] - box[1] <= 42
    assert bytes((18, 171, 52, 255)) in image.tobytes()
    # The device renderer resolves the same layout/config as the preview.
    with patch("lumi.sensors.default_sources", return_value=[]):
        renderer = overlay.OverlayRenderer(cfg)
    assert renderer.layout.keys == layout.keys


def test_default_readouts_fit_device_mask_budget():
    source = sensors.Source()
    source.poll = lambda: {
        "cpu.freq": (4200, "MHz", 0),
        "cpu.temp": (55, "°C", 0.55),
        "gpu.temp": (60, "°C", 0.6),
    }
    metrics = sensors.Metrics([source])
    metrics.poll()
    image = overlay.get_layout({"layout": "sensors"}).render(
        DrawContext(480, datetime.now(), metrics)
    )
    mask = overlay.encode_mask(image.tobytes("raw", "BGRA"), 480, 480)
    assert 0 < len(mask) // 5 < 400


def test_disconnected_and_nonfinite_sensors_do_not_show_stale_values():
    source = sensors.Source()
    source.poll = lambda: {"cpu.temp": (50, "°C", 0.5)}
    metrics = sensors.Metrics([source])
    metrics.poll()
    assert metrics.get("cpu.temp").value == 50
    source.poll = lambda: {"cpu.temp": (float("nan"), "°C", 0)}
    metrics.poll()
    assert metrics.get("cpu.temp") is None
    source.poll = lambda: {}
    metrics.poll()
    assert metrics.keys() == []


def test_lm_sensors_exposes_all_channels_and_representative_readings():
    source = sensors.LmSensorsSource()
    result = source._classify(
        {
            "coretemp-isa-0000": {
                "Package id 0": {"temp1_input": 58},
                "Core 0": {"temp2_input": 51},
            },
            "amdgpu-pci-0300": {
                "edge": {"temp1_input": 48},
                "junction": {"temp2_input": 65},
            },
        }
    )
    assert result["cpu.temp"][0] == 58
    assert result["gpu.temp"][0] == 48
    assert result["temp.amdgpu-pci-0300.junction"][0] == 65
    assert result["temp.coretemp-isa-0000.Core 0"][0] == 51


def test_nvidia_multiple_gpus_and_failure():
    with patch("lumi.sensors.shutil.which", return_value="/bin/nvidia-smi"):
        source = sensors.NvidiaSource()
    with patch(
        "lumi.sensors.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="GPU-aaa, 45\nGPU-bbb, 62\n"),
    ):
        result = source.poll()
    assert result["gpu.temp"][0] == 45
    assert result["gpu.temp.GPU-bbb"][0] == 62
    with patch("lumi.sensors.subprocess.run", side_effect=OSError):
        assert source.poll() == {}


def test_invalid_nvidia_reading_does_not_hide_other_gpus():
    with patch("lumi.sensors.shutil.which", return_value="/bin/nvidia-smi"):
        source = sensors.NvidiaSource()
    with patch(
        "lumi.sensors.subprocess.run",
        return_value=SimpleNamespace(
            returncode=0, stdout="GPU-a, N/A\nmalformed\nGPU-b, nan\nGPU-c, 62\n"
        ),
    ):
        readings = source.poll()
    assert set(readings) == {"gpu.temp", "gpu.temp.GPU-c"}
    assert readings["gpu.temp"][0] == 62


def test_metric_snapshots_do_not_change_when_the_source_is_polled_again():
    values = iter([50, 60])
    source = sensors.Source()
    source.poll = lambda: {"cpu.temp": (next(values), "°C", 0.5)}
    metrics = sensors.Metrics([source])
    metrics.poll()
    snapshot = metrics.snapshot()
    metrics.poll()
    assert snapshot["cpu.temp"].value == 50
    assert list(snapshot["cpu.temp"].history) == [50]
    assert metrics.get("cpu.temp").value == 60
    assert list(metrics.get("cpu.temp").history) == [50, 60]


def test_first_run_returns_normalized_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "STATE_DIR", tmp_path)
    monkeypatch.setattr(overlay, "CONFIG_FILE", tmp_path / "overlay.json")
    cfg = overlay.load_config()
    assert cfg["stats"] == overlay.stat_settings({})
    assert cfg["layout"] == cfg["preset"] == "clock"
    assert overlay.CONFIG_FILE.exists()


def test_psutil_frequency_and_temperature_channels():
    source = sensors.PsutilSource()
    source._psutil = SimpleNamespace(
        cpu_freq=lambda percpu=False: (
            [SimpleNamespace(current=4100), SimpleNamespace(current=4300)]
            if percpu
            else SimpleNamespace(current=4200)
        ),
        sensors_temperatures=lambda: {
            "k10temp": [SimpleNamespace(label="Tctl", current=57)]
        },
    )
    result = source.poll()
    assert result["cpu.freq"][:2] == (4300, "MHz")
    assert result["cpu.freq.core1"][0] == 4300
    assert result["temp.k10temp.Tctl"][0] == 57
    assert result["cpu.temp"][0] == 57


def test_frequency_falls_back_when_per_core_readings_are_unavailable():
    source = sensors.PsutilSource()
    source._psutil = SimpleNamespace(
        cpu_freq=lambda percpu=False: [] if percpu else SimpleNamespace(current=3900)
    )
    assert source.poll()["cpu.freq"][0] == 3900


def test_frequency_ignores_invalid_cores_and_selects_fastest_current_core():
    source = sensors.PsutilSource()
    source._psutil = SimpleNamespace(
        cpu_freq=lambda percpu=False: [
            SimpleNamespace(current=value)
            for value in (0, float("nan"), 3500, 5200, 4700)
        ]
    )
    result = source.poll()
    assert result["cpu.freq"][0] == 5200
    assert "cpu.freq.core0" not in result
    assert "cpu.freq.core1" not in result


def test_background_settings_roundtrip_and_render_alpha(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "STATE_DIR", tmp_path)
    monkeypatch.setattr(overlay, "CONFIG_FILE", tmp_path / "overlay.json")
    cfg = {
        "layout": "sensors",
        "stats": {
            "cpu.freq": {"background_color": "#123456", "background_opacity": 50},
            "cpu.temp": {"enabled": False},
            "gpu.temp": {"enabled": False},
        },
    }
    overlay.save_config(cfg)
    loaded = overlay.load_config()
    settings = loaded["stats"]["cpu.freq"]
    assert (settings["background_color"], settings["background_opacity"]) == (
        "#123456",
        50,
    )
    metrics = sensors.Metrics([])
    ctx = DrawContext(480, datetime.now(), metrics)
    image = overlay.get_layout(loaded).render(ctx)
    box = image.getbbox()
    point = (box[0] + 1, (box[1] + box[3]) // 2)
    assert image.getpixel(point) == (18, 52, 86, 128)
    assert image.getchannel("A").getextrema() == (0, 255)
    settings["background_opacity"] = 0
    transparent = overlay.get_layout(loaded).render(ctx)
    assert transparent.getpixel(point)[3] == 0
    assert transparent.getbbox() is not None  # text remains visible


def test_background_settings_validate_and_preserve_existing_defaults():
    defaults = overlay.stat_settings({})["cpu.freq"]
    assert defaults["background_color"] == "#0C0E16"
    assert defaults["background_opacity"] == 100
    settings = overlay.stat_settings(
        {
            "stats": {
                "cpu.freq": {"background_color": "#BADHEX", "background_opacity": 200}
            }
        }
    )["cpu.freq"]
    assert settings["background_color"] == "#0C0E16"
    assert settings["background_opacity"] == 100


def test_clear_keeps_nonempty_mask_for_later_overlays():
    device = Device.__new__(Device)
    device.lcd = object()
    with (
        patch("lumi.media.send_cmd") as command,
        patch("lumi.media.bulk_write") as bulk,
        patch("lumi.media.read_text", return_value=""),
    ):
        device.clear_overlay()
    command.assert_called_once_with(device.lcd, 0xD0, 7, b"", 0)
    bulk.assert_called_once_with(device.lcd, b"\x00\x00\x00\x00\x01\xef\x69")


def test_all_readouts_disabled_still_sends_nonempty_mask():
    device = Device.__new__(Device)
    device.lcd = SimpleNamespace(serial_flush_input=Mock(), serial_flush_output=Mock())
    device.width = device.height = 480
    with (
        patch("lumi.media.send_cmd") as command,
        patch("lumi.media.bulk_write") as bulk,
        patch("lumi.media.read_text", return_value="") as read,
    ):
        device.push_overlay_frame(Image.new("RGBA", (480, 480)))
    assert command.call_args_list[-1].args == (device.lcd, 0xD0, 7, b"", 0)
    assert bulk.call_args_list[-1].args[-1] == b"\x00\x00\x00\x00\x01\xef\x69"
    assert [call.args[1] for call in read.call_args_list] == [2.0, 2.0, 0.3, 3.0]
    assert all(call.kwargs == {"flush_input": False} for call in read.call_args_list)
    assert device.lcd.serial_flush_output.call_count == 2


def test_gui_enable_does_not_clear_panel_first():
    import pytest

    qt = pytest.importorskip("PySide6.QtCore")
    from lumi.gui import DeviceWorker

    app = qt.QCoreApplication.instance() or qt.QCoreApplication([])
    worker = DeviceWorker(size='2.1"')
    worker.dev = SimpleNamespace(width=480, clear_overlay=Mock())
    with (
        patch.object(overlay, "set_enabled"),
        patch.object(overlay, "load_config", return_value={"layout": "sensors"}),
        patch.object(overlay, "OverlayRenderer"),
    ):
        worker.set_overlay_enabled(True)
        worker.dev.clear_overlay.assert_not_called()
        assert worker._overlay_timer.isActive()
        assert worker._overlay_timer.isSingleShot()
        worker.set_overlay_enabled(False)
        worker.dev.clear_overlay.assert_called_once()
        assert not worker._overlay_timer.isActive()
    assert app is not None


def test_gui_waits_after_each_completed_transfer():
    import pytest

    qt = pytest.importorskip("PySide6.QtCore")
    from lumi.gui import DeviceWorker

    app = qt.QCoreApplication.instance() or qt.QCoreApplication([])
    worker = DeviceWorker(size='2.1"')
    worker.dev = object()
    worker._overlay_renderer = Mock(interval=2.0)
    worker._overlay_timer = Mock()
    worker._overlay_tick()
    worker._overlay_renderer.tick.assert_called_once_with(worker.dev)
    worker._overlay_timer.start.assert_called_once_with(2000)
    assert app is not None


def test_overlay_read_preserves_an_already_queued_reply():
    lcd = SimpleNamespace(
        serial_flush_input=Mock(),
        serial_readall=Mock(return_value=b"seq_png_init_sucess\x00"),
    )
    with patch("lumi.media.time.time", side_effect=[0, 0, 0.05, 1]):
        reply = read_text(lcd, 0.1, flush_input=False)
    assert reply == "seq_png_init_sucess"
    lcd.serial_flush_input.assert_not_called()


def test_serial_poll_uses_nonblocking_pyserial_read():
    transport = SimpleNamespace(lcd_serial=Mock())
    transport.lcd_serial.read_all.return_value = b"reply"
    assert LcdComm.serial_readall(transport) == b"reply"
    transport.lcd_serial.read_all.assert_called_once_with()
    transport.lcd_serial.readall.assert_not_called()


def test_explicit_apply_leaves_playback_undisturbed():
    import pytest

    qt = pytest.importorskip("PySide6.QtCore")
    from lumi.gui import DeviceWorker

    app = qt.QCoreApplication.instance() or qt.QCoreApplication([])
    worker = DeviceWorker(size='2.1"')
    worker.dev = SimpleNamespace(width=480, height=480, lcd=object())
    events = []
    renderer = Mock()
    renderer.tick.side_effect = lambda dev: events.append("frame") or True
    cfg = {"layout": "sensors"}
    with (
        patch.object(overlay, "set_enabled"),
        patch.object(overlay, "load_config", return_value=cfg),
        patch.object(overlay, "OverlayRenderer", return_value=renderer),
        patch(
            "lumi.state.load_last",
            return_value={"kind": "video", "device_name": "/clip.mp4"},
        ),
        patch(
            "lumi.media.play_stored",
            side_effect=lambda *a, **kw: events.append("play") or True,
        ) as play,
    ):
        worker.apply_overlay()
        play.assert_not_called()
        assert events == ["frame"]
        worker._overlay_timer.stop()
    assert app is not None


def test_media_change_queues_overlay_restore_only_when_enabled():
    import pytest

    qt = pytest.importorskip("PySide6.QtCore")
    from lumi.gui import DeviceWorker

    app = qt.QCoreApplication.instance() or qt.QCoreApplication([])
    worker = DeviceWorker(size='2.1"')
    worker._overlay_renderer = Mock()
    with (
        patch.object(overlay, "load_config", return_value={"enabled": True}),
        patch.object(qt.QMetaObject, "invokeMethod") as invoke,
    ):
        worker._set_playing("/clip.mp4")
        invoke.assert_called_once_with(
            worker, "apply_overlay", qt.Qt.ConnectionType.QueuedConnection
        )
    with (
        patch.object(overlay, "load_config", return_value={"enabled": False}),
        patch.object(qt.QMetaObject, "invokeMethod") as invoke,
    ):
        worker._set_playing("/other.mp4")
        invoke.assert_not_called()
    assert app is not None


def test_overlay_stop_waits_for_transfer_completion():
    with patch.object(overlay, "OverlayRenderer") as renderer:
        renderer.return_value.interval = 2
        loop = overlay.OverlayLoop(object(), {})
    thread = Mock()
    thread.is_alive.side_effect = [True, False]
    loop._thread = thread
    loop.stop()
    thread.join.assert_called_once_with(timeout=None)
    assert loop._thread is None


def test_overlay_stop_timeout_keeps_thread_reference():
    import pytest

    with patch.object(overlay, "OverlayRenderer") as renderer:
        renderer.return_value.interval = 2
        loop = overlay.OverlayLoop(object(), {})
    thread = Mock()
    thread.is_alive.return_value = True
    loop._thread = thread
    with pytest.raises(TimeoutError):
        loop.stop(timeout=0.1)
    assert loop._thread is thread


def test_gui_releases_serial_port_before_daemon_handoff():
    import pytest

    qt = pytest.importorskip("PySide6.QtCore")
    from lumi.gui import DeviceWorker

    app = qt.QCoreApplication.instance() or qt.QCoreApplication([])
    worker = DeviceWorker(size='2.1"')
    dev = Mock()
    worker.dev = dev
    worker._overlay_timer = Mock()
    worker.release_panel()
    worker._overlay_timer.stop.assert_called_once()
    dev.close.assert_called_once()
    assert worker.dev is None
    assert app is not None


def test_serial_open_is_exclusive_and_write_has_timeout():
    transport = SimpleNamespace(com_port="/dev/test", lcd_serial=None)
    with patch("lumi.lcd.lcd_comm.serial.Serial") as serial_port:
        LcdComm.openSerial(transport)
    serial_port.assert_called_once_with(
        "/dev/test", 115200, timeout=1, write_timeout=3, rtscts=True, exclusive=True
    )


def test_partial_write_timeout_propagates_instead_of_silently_continuing():
    import pytest
    import serial

    transport = SimpleNamespace(
        serial_write=Mock(side_effect=serial.SerialTimeoutException())
    )
    with pytest.raises(serial.SerialTimeoutException):
        LcdComm.WriteLine(transport, b"partial frame")


def test_output_drain_has_a_deadline():
    import pytest
    import serial

    transport = SimpleNamespace(lcd_serial=SimpleNamespace(out_waiting=1))
    with patch("lumi.lcd.lcd_comm.time.monotonic", side_effect=[0, 4]):
        with pytest.raises(serial.SerialTimeoutException):
            LcdComm.serial_flush_output(transport)


def test_overlay_connection_loss_is_reported_to_daemon():
    with patch.object(overlay, "OverlayRenderer") as renderer:
        renderer.return_value.interval = 2
        renderer.return_value.tick.side_effect = OSError("USB stalled")
        loop = overlay.OverlayLoop(object(), {})
        loop._run()
    assert isinstance(loop.error, OSError)
    assert loop._stop.is_set()


def test_failed_device_handshake_closes_serial_connection():
    import pytest

    with patch("lumi.device.LcdCommRevC") as driver:
        driver.return_value.InitializeComm.side_effect = OSError("USB disconnected")
        with pytest.raises(OSError, match="USB disconnected"):
            Device(com_port="/dev/test")
    driver.return_value.closeSerial.assert_called_once()
