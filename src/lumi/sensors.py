"""Sensor metric registry for overlays — lumi's analogue of the official app's
``M_Data`` (``UsbMonitorL/M_Data.cs``).

Backends (``Source``) poll named metrics; widgets bind to a metric *by key*
(e.g. ``cpu.load``). The registry persists ``Sample`` objects across polls so a
metric's ``history`` deque accumulates for sparklines.

Linux-only by design (matches Lumi's positioning as the open-source Linux
replacement for ID-COOLING's Windows app). Backends degrade gracefully: if
``psutil`` or the ``sensors`` binary aren't installed, their keys simply don't
appear and bound widgets render a placeholder — nothing crashes.

Metric contract (mirrors M_Data's Value / ValueWithUnit / Rate / DataQueue):
  * ``value``  — the raw number (e.g. 55.0)
  * ``unit``   — appended to value in text widgets ("%", "°C", "GB", "")
  * ``rate``   — 0..1 normalised, feeds Bar/Arc widgets directly
  * ``text``   — optional preformatted string (e.g. "14:35" for the clock);
                 when set, text widgets show it verbatim instead of value+unit
  * ``history``— recent values, for Spark widgets
"""
from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from lumi.log import logger

# Samples retained for sparklines (~2 min at 1 Hz; longer at lower cadence).
HISTORY = 120


@dataclass
class Sample:
    """A single named metric. Long-lived in the registry: ``poll()`` mutates it
    in place and appends to ``history`` rather than replacing it."""
    key: str
    value: float = 0.0
    unit: str = ""
    rate: float = 0.0
    text: Optional[str] = None
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))


# --------------------------------------------------------------------------- backends

class Source:
    """A backend producing ``{key: (value, unit, rate)}`` each poll."""
    keys: List[str] = []

    def poll(self) -> Dict[str, Tuple[float, str, float]]:
        return {}


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


class TimeSource(Source):
    """Wall-clock metrics for clock widgets (digital + analog)."""
    keys = ["clock.time", "clock.date", "clock.hms",
            "clock.second", "clock.minute", "clock.hour"]

    def poll(self) -> Dict[str, Tuple[float, str, float]]:
        now = datetime.now()
        s = now.second
        m = now.minute
        h = now.hour
        # Sub-second precision so analog hands land at the *exact* current angle
        # each render. The overlay push is comparatively slow (a full BGRA
        # frame), so ticks land ~1s apart; with integer-second rates the second
        # hand would jump to a quantised position and look like it skips. With
        # fractional rates it always points where it should for the wall-clock
        # instant it was drawn.
        frac = now.microsecond / 1_000_000.0
        sec_f = s + frac
        return {
            # Text widgets read ``.text`` via the registry (set after merge),
            # but we also expose a numeric hhmm for completeness.
            "clock.second": (float(s), "", _clamp01(sec_f / 60.0)),
            "clock.minute": (float(m), "", _clamp01((m * 60 + sec_f) / 3600.0)),
            "clock.hour":   (float(h % 12), "", _clamp01(((h % 12) + m / 60.0 + sec_f / 3600.0) / 12.0)),
            "clock.hms":    (float(h * 10000 + m * 100 + s), "", 0.0),
            "clock.time":   (0.0, now.strftime("%H:%M"), 0.0),
            "clock.date":   (0.0, now.strftime("%b %d"), 0.0),
        }


class PsutilSource(Source):
    """CPU / memory / disk / swap metrics via psutil (optional dependency)."""
    keys = ["cpu.load", "mem.used", "mem.load", "disk.used", "disk.load",
            "swap.load", "cpu.freq"]

    def __init__(self) -> None:
        self._psutil = None
        if importlib.util.find_spec("psutil") is not None:
            try:
                import psutil  # type: ignore
                self._psutil = psutil
                # Prime cpu_percent so the first reading is meaningful (psutil
                # returns 0.0 until it has two samples to diff).
                try:
                    psutil.cpu_percent(interval=None)
                except Exception:
                    pass
            except Exception as e:  # pragma: no cover
                logger.warning(f"sensors: psutil import failed: {e}")

    def poll(self) -> Dict[str, Tuple[float, str, float]]:
        if self._psutil is None:
            return {}
        out: Dict[str, Tuple[float, str, float]] = {}
        try:
            load = float(self._psutil.cpu_percent(interval=None))
            out["cpu.load"] = (load, "%", _clamp01(load / 100.0))
        except Exception:
            pass
        try:
            vm = self._psutil.virtual_memory()
            out["mem.load"] = (float(vm.percent), "%", _clamp01(vm.percent / 100.0))
            out["mem.used"] = (vm.used / (1024 ** 3), "GB", _clamp01(vm.percent / 100.0))
        except Exception:
            pass
        try:
            du = self._psutil.disk_usage("/")
            out["disk.load"] = (float(du.percent), "%", _clamp01(du.percent / 100.0))
            out["disk.used"] = (du.used / (1024 ** 3), "GB", _clamp01(du.percent / 100.0))
        except Exception:
            pass
        try:
            out["swap.load"] = (float(self._psutil.swap_memory().percent), "%", 0.0)
        except Exception:
            pass
        core_frequencies = []
        try:
            for i, core in enumerate(self._psutil.cpu_freq(percpu=True) or []):
                current = float(core.current)
                if math.isfinite(current) and current > 0:
                    core_frequencies.append(current)
                    out[f"cpu.freq.core{i}"] = (current, "MHz", 0.0)
        except Exception:
            pass
        if core_frequencies:
            out["cpu.freq"] = (max(core_frequencies), "MHz", 0.0)
        else:
            try:
                freq = self._psutil.cpu_freq()
                if freq and math.isfinite(freq.current) and freq.current > 0:
                    out["cpu.freq"] = (float(freq.current), "MHz", 0.0)
            except Exception:
                pass
        try:
            for chip, entries in self._psutil.sensors_temperatures().items():
                for i, entry in enumerate(entries):
                    key = f"temp.{chip}.{entry.label or i}"
                    value = float(entry.current)
                    if not math.isfinite(value):
                        continue
                    out[key] = (value, "°C", _clamp01(value / 100))
                    alias = ("cpu.temp" if any(c in chip.lower() for c in _CPU_CHIPS)
                             else "gpu.temp" if any(c in chip.lower() for c in _GPU_CHIPS)
                             else None)
                    if alias:
                        out.setdefault(alias, out[key])
        except Exception:
            pass
        return out


# Chip-name substrings used to classify lm-sensors adapters.
_CPU_CHIPS = ("k10temp", "coretemp", "zenpower", "cpu_thermal", "cpu-thermal",
              "acpitz", "scpi_sensors", "via_cputemp", "fam15h_power")
_GPU_CHIPS = ("amdgpu", "nvidia", "nouveau", "radeon", "radeondrm")
# Within a CPU chip, prefer these feature names for the representative temp.
_CPU_TEMP_PREF = ("package", "tctl", "tdie", "cpu", "composite", "temp1")


class LmSensorsSource(Source):
    """Temperature metrics via the ``sensors`` binary (lm-sensors).

    Uses ``sensors -j`` (JSON) when available. Best-effort classification of a
    CPU vs GPU temperature; if anything is ambiguous or the binary is missing,
    the source yields no keys (widgets fall back to placeholders)."""
    keys = ["cpu.temp", "gpu.temp"]

    def __init__(self) -> None:
        self._bin = shutil.which("sensors")

    def poll(self) -> Dict[str, Tuple[float, str, float]]:
        if not self._bin:
            return {}
        try:
            cp = subprocess.run(
                [self._bin, "-j"], capture_output=True, text=True,
                timeout=3, check=False)
            if cp.returncode != 0 or not cp.stdout.strip():
                return {}
            data = json.loads(cp.stdout)
        except Exception as e:
            logger.debug(f"sensors: lm-sensors parse failed: {e}")
            return {}
        return self._classify(data)

    @staticmethod
    def _first_temp(feature) -> Optional[float]:
        """First ``*_input`` temperature value in a feature dict."""
        if not isinstance(feature, dict):
            return None
        for k, v in feature.items():
            if k.endswith("_input") and "temp" in k:
                try:
                    value = float(v)
                    if math.isfinite(value):
                        return value
                except (TypeError, ValueError):
                    continue
        return None

    def _classify(self, data: dict) -> Dict[str, Tuple[float, str, float]]:
        out: Dict[str, Tuple[float, str, float]] = {}
        cpu_best: Optional[float] = None
        gpu_best: Optional[float] = None
        for chip, body in data.items():
            if not isinstance(body, dict):
                continue
            name = str(chip).lower()
            features = {k: v for k, v in body.items() if k != "Adapter"}
            for fname, feature in features.items():
                temp = self._first_temp(feature)
                if temp is not None:
                    out[f"temp.{chip}.{fname}"] = (temp, "°C", _clamp01(temp / 100))
            is_cpu = any(s in name for s in _CPU_CHIPS)
            is_gpu = any(s in name for s in _GPU_CHIPS)
            if not (is_cpu or is_gpu):
                continue
            # Choose the most representative temperature for this chip.
            picked: Optional[float] = None
            for pref in _CPU_TEMP_PREF if is_cpu else ("edge", "junction", "temp1"):
                for fname, feat in features.items():
                    if pref in fname.lower():
                        t = self._first_temp(feat)
                        if t is not None:
                            picked = t
                            break
                if picked is not None:
                    break
            if picked is None:
                for feat in features.values():
                    t = self._first_temp(feat)
                    if t is not None:
                        picked = t
                        break
            if picked is None:
                continue
            if is_cpu and cpu_best is None:
                cpu_best = picked
            elif is_gpu and gpu_best is None:
                gpu_best = picked
        if cpu_best is not None:
            out["cpu.temp"] = (cpu_best, "°C", _clamp01(cpu_best / 100.0))
        if gpu_best is not None:
            out["gpu.temp"] = (gpu_best, "°C", _clamp01(gpu_best / 100.0))
        return out


class NvidiaSource(Source):
    """GPU temperatures from the optional NVIDIA driver utility."""

    def __init__(self):
        self._bin = shutil.which("nvidia-smi")

    def poll(self):
        if not self._bin:
            return {}
        try:
            cp = subprocess.run(
                [self._bin, "--query-gpu=uuid,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, check=False)
            if cp.returncode:
                return {}
            out = {}
            for line in cp.stdout.splitlines():
                try:
                    uuid, raw = line.split(",", 1)
                    value = float(raw.strip())
                except ValueError:
                    continue
                if not uuid.strip() or not math.isfinite(value):
                    continue
                sample = (value, "°C", _clamp01(value / 100))
                out[f"gpu.temp.{uuid.strip()}"] = sample
                out.setdefault("gpu.temp", sample)
            return out
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}


def default_sources() -> List[Source]:
    """The default backend set for a live overlay (all optional / degrading)."""
    return [TimeSource(), PsutilSource(), LmSensorsSource(), NvidiaSource()]


# --------------------------------------------------------------------------- registry

class Metrics:
    """The central metric registry widgets bind against.

    ``Sample`` objects are persistent: each ``poll()`` refreshes their value and
    appends to ``history``. Time/clock ``.text`` is propagated after merge so
    text widgets can read it verbatim.
    """

    def __init__(self, sources: Optional[List[Source]] = None):
        self._sources = sources if sources is not None else default_sources()
        self._samples: Dict[str, Sample] = {}

    def poll(self) -> None:
        snapshot: Dict[str, Tuple[float, str, float]] = {}
        for src in self._sources:
            try:
                snapshot.update(src.poll())
            except Exception as e:  # never let one backend kill the overlay
                logger.warning(f"sensors: {src.__class__.__name__} poll failed: {e}")
        for key, (value, unit, rate) in snapshot.items():
            if not math.isfinite(value):
                continue
            samp = self._samples.get(key)
            if samp is None:
                samp = Sample(key=key)
                self._samples[key] = samp
            samp.value = value
            samp.unit = unit
            samp.rate = rate
            # The clock emits a preformatted string in ``unit``; surface it as
            # ``text`` so text widgets render it verbatim.
            samp.text = unit if key.startswith("clock.") else None
            samp.unit = "" if key.startswith("clock.") else unit
            samp.history.append(value)
        # A disconnected or failed sensor must not keep displaying an old value.
        for key in list(self._samples):
            if key not in snapshot or not math.isfinite(snapshot[key][0]):
                del self._samples[key]

    def get(self, key: Optional[str]) -> Optional[Sample]:
        if not key:
            return None
        return self._samples.get(key)

    def keys(self) -> List[str]:
        return list(self._samples)

    def snapshot(self) -> Dict[str, Sample]:
        """An independent snapshot safe to read while this registry polls again."""
        return {key: replace(sample, history=sample.history.copy())
                for key, sample in self._samples.items()}
