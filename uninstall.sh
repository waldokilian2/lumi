#!/usr/bin/env bash
# Lumi — uninstaller (Linux). Reverses install.sh.
#
# Removes: the systemd user service, the persistent venv, the `lumi` symlink,
# and (with --purge) your settings/last-media state. The udev rule and linger
# require sudo and are removed with confirmation.
#
#   ./uninstall.sh           # remove app; keep your settings
#   ./uninstall.sh --purge   # also remove ~/.config/lumi + ~/.local/share/lumi
set -e

VENV="$HOME/.local/share/lumi/venv"
BIN="$HOME/.local/bin/lumi"
UNIT="$HOME/.config/systemd/user/lumi.service"
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

echo "==> Stopping + removing the systemd user service..."
if systemctl --user is-active -q lumi; then
  systemctl --user stop lumi || true
fi
systemctl --user disable lumi 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload

echo "==> Removing the 'lumi' command + venv..."
rm -f "$BIN"
rm -rf "$VENV"

echo "==> Removing the udev rule (sudo)..."
if [[ -f /etc/udev/rules.d/99-lumi.rules ]]; then
  sudo rm -f /etc/udev/rules.d/99-lumi.rules
  sudo udevadm control --reload-rules
  echo "    Removed. The panel will revert to root-only serial access."
fi

if [[ "$PURGE" -eq 1 ]]; then
  echo "==> Purging settings + media cache (~/.config/lumi, ~/.local/share/lumi)..."
  rm -rf "$HOME/.config/lumi" "$HOME/.local/share/lumi"
else
  echo "==> Keeping your settings (~/.config/lumi). Reinstall to resume where you left off."
  echo "    To wipe them too: ./uninstall.sh --purge"
fi

# Linger is harmless to leave enabled, but offer to clear it.
if loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
  read -r -p "Disable user linger (was enabled for boot autostart)? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    sudo loginctl disable-linger "$USER"
  fi
fi

echo
echo "Lumi uninstalled."
