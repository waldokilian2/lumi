"""Persist the last-shown media so the daemon can resume it on the next boot."""
import json
from pathlib import Path
from typing import Optional

STATE_DIR = Path.home() / ".config" / "lumi"
STATE_FILE = STATE_DIR / "state.json"


def save_last(kind: str, path: str, device_name: Optional[str] = None,
              rotate: int = 0) -> None:
    """kind: 'image' | 'video'. path: local file. device_name: name on panel flash."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"kind": kind, "path": str(path), "device_name": device_name,
                   "rotate": rotate}, f)


def load_last() -> Optional[dict]:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def clear_last() -> None:
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


BRIGHTNESS_FILE = STATE_DIR / "brightness"


def load_brightness(default: int = 50) -> int:
    try:
        return max(0, min(100, int(BRIGHTNESS_FILE.read_text().strip())))
    except Exception:
        return default


def save_brightness(level: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BRIGHTNESS_FILE.write_text(str(int(level)))


# Uploads we've sent to the device. The panel's cmd 101 directory listing is
# capped at 512 bytes, so once the library fills up, newer uploads get truncated
# off the listing even though they're stored + playable. We track them locally
# and merge with the device listing so the app always shows what we uploaded.
UPLOADS_FILE = STATE_DIR / "uploads.json"


def log_upload(device_name: str) -> None:
    """Record a successful upload (deduplicated)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    uploads = load_uploads()
    if device_name not in uploads:
        uploads.append(device_name)
        UPLOADS_FILE.write_text(json.dumps(uploads))


def forget_upload(device_name: str) -> None:
    """Drop a tracked upload (e.g. after deleting it on the device) so the
    merged listing doesn't show a ghost entry."""
    uploads = load_uploads()
    if device_name in uploads:
        uploads.remove(device_name)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        UPLOADS_FILE.write_text(json.dumps(uploads))


def clear_uploads() -> None:
    """Forget all tracked uploads (e.g. after wiping the library)."""
    try:
        UPLOADS_FILE.write_text("[]")
    except Exception:
        pass


def load_uploads() -> list:
    try:
        return json.loads(UPLOADS_FILE.read_text())
    except Exception:
        return []
