import errno
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import serial

from lumi import usb
from lumi.device import Device, find_panel


def test_recovery_only_targets_the_panel_attached_to_this_tty(tmp_path, monkeypatch):
    device = tmp_path / "devices" / "panel"
    interface = device / "interface"
    interface.mkdir(parents=True)
    for name, value in {
        "idVendor": "1d6b",
        "idProduct": "0121",
        "busnum": "5",
        "devnum": "7",
    }.items():
        (device / name).write_text(value)
    tty = tmp_path / "tty" / "ttyTEST"
    tty.mkdir(parents=True)
    (tty / "device").symlink_to(interface)
    monkeypatch.setattr(usb, "SYS_TTY", tmp_path / "tty")
    monkeypatch.setattr(usb, "USB_DEV", tmp_path / "bus")
    assert usb.panel_usb_node("/dev/ttyTEST") == tmp_path / "bus" / "005" / "007"
    (device / "idProduct").write_text("ffff")
    assert usb.panel_usb_node("/dev/ttyTEST") is None


def test_reset_is_throttled_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(usb, "LOCK_DIR", tmp_path)
    with (
        patch.object(usb, "panel_usb_node", return_value=tmp_path / "device"),
        patch.object(usb.os, "open", return_value=17),
        patch.object(usb.os, "close"),
        patch.object(usb.fcntl, "ioctl") as ioctl,
        patch.object(usb.time, "monotonic", return_value=100),
        patch.object(usb.time, "sleep"),
    ):
        assert usb.reset_panel_usb("/dev/ttyTEST") is True
        assert usb.reset_panel_usb("/dev/ttyTEST") is False
    ioctl.assert_called_once_with(17, usb.USBDEVFS_RESET, 0)


def test_reset_handles_missing_permissions_without_attempting_privilege_escalation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(usb, "LOCK_DIR", tmp_path)
    with (
        patch.object(usb, "panel_usb_node", return_value=tmp_path / "device"),
        patch.object(usb.os, "open", side_effect=PermissionError),
        patch.object(usb.fcntl, "ioctl") as ioctl,
    ):
        assert usb.reset_panel_usb("/dev/ttyTEST") is False
    ioctl.assert_not_called()


def test_busy_ports_and_permission_errors_never_trigger_recovery():
    assert usb.recoverable(serial.SerialTimeoutException())
    assert usb.recoverable(OSError(errno.EPIPE, "broken pipe"))
    assert not usb.recoverable(OSError(errno.EAGAIN, "locked"))
    assert not usb.recoverable(PermissionError(errno.EACCES, "access denied"))


def test_device_retries_after_reset_and_redetects_its_port():
    first = Mock()
    first.InitializeComm.side_effect = serial.SerialTimeoutException()
    second = Mock()
    with (
        patch("lumi.device.LcdCommRevC", side_effect=[first, second]) as driver,
        patch.object(usb, "reset_panel_usb", return_value=True) as reset,
        patch.object(Device, "_wake_and_find", return_value="/dev/ttyNEW"),
    ):
        device = Device(com_port="/dev/ttyTEST")
    assert device.lcd is second
    first.closeSerial.assert_called_once()
    reset.assert_called_once_with("/dev/ttyTEST")
    assert driver.call_args_list[-1].kwargs["com_port"] == "/dev/ttyNEW"
    second.InitializeComm.assert_called_once()


def test_gui_stops_daemon_even_between_systemd_restarts():
    pytest.importorskip("PySide6")
    from lumi import gui

    with (
        patch.object(gui, "_daemon_active", return_value=False),
        patch.object(gui, "_daemon_enabled", return_value=True),
        patch.object(gui, "_daemon_stop") as stop,
    ):
        assert gui._pause_daemon() is True
    stop.assert_called_once()


def test_awake_detection_does_not_open_the_wake_interface():
    port = SimpleNamespace(serial_number="20080411", device="/dev/ttyTEST")
    with (
        patch("serial.tools.list_ports.comports", return_value=[port]),
        patch("lumi.device.LcdCommRevC.auto_detect_com_port") as wake,
    ):
        assert find_panel() == "/dev/ttyTEST"
    wake.assert_not_called()
