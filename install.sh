#!/usr/bin/env bash
# Lumi — one-command installer for end users (Linux).
#
# Installs into a persistent venv at ~/.local/share/lumi/venv and symlinks the
# `lumi` CLI onto ~/.local/bin. This means the project folder can live ANYWHERE
# during install and can be moved or deleted afterwards — the install is
# self-contained under your home directory.
#
#   ./install.sh              # full install + daemon prompt
#   ./install.sh --no-daemon  # skip the systemd daemon
set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

WITH_DAEMON=1
[[ "${1:-}" == "--no-daemon" ]] && WITH_DAEMON=0

VENV="$HOME/.local/share/lumi/venv"
BIN="$HOME/.local/bin"

echo "==> [1/4] Installing Lumi into a persistent venv ($VENV)..."
mkdir -p "$(dirname "$VENV")"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet '.[gui]'   # base + GUI (PySide6) — the GUI is the primary interface
deactivate

echo "==> [2/4] Putting 'lumi' on PATH (~/.local/bin)..."
mkdir -p "$BIN"
ln -sf "$VENV/bin/lumi" "$BIN/lumi"
echo "    Installed: $(readlink "$BIN/lumi")"
if [[ ":$PATH:" != *":$BIN:"* ]]; then
  echo "    NOTE: ~/.local/bin is not in your PATH. Add it to your shell rc."
fi

echo "==> [3/4] Installing udev rule (serial access without root)..."
if [[ ! -f /etc/udev/rules.d/99-lumi.rules ]]; then
  sudo cp "$HERE/99-lumi.rules" /etc/udev/rules.d/
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  echo "    Installed. Unplug/replug the panel (or reboot) if it's already connected."
else
  echo "    Already installed."
fi

if [[ "$WITH_DAEMON" -eq 1 ]]; then
  echo "==> [4/4] Setting up autostart + shutdown-TURNOFF daemon..."
  read -r -p "    Enable the systemd user daemon (autostart at boot, TURNOFF at shutdown)? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    mkdir -p "$HOME/.config/systemd/user"
    sed -e "s|ExecStart=.*|ExecStart=$BIN/lumi daemon|" \
        "$HERE/lumi.service" > "$HOME/.config/systemd/user/lumi.service"
    systemctl --user daemon-reload
    systemctl --user enable lumi.service
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
      echo "    Enabling linger (so the service runs at boot) — needs sudo:"
      sudo loginctl enable-linger "$USER"
    fi
    systemctl --user restart lumi.service || true
    echo "    Daemon enabled. Logs: journalctl --user -u lumi -f"
  else
    echo "    Skipped. You can run 'lumi daemon' manually anytime."
  fi
fi

echo
echo "Done. The project folder can now be moved or deleted."
echo "Try:"
echo "  lumi info        # detect the panel"
echo "  lumi ls video    # list stored videos"
echo "  lumi gui         # open the UI"
echo "  lumi --help"
