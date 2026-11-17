#!/usr/bin/env sh
# agents.org installer (macOS / Linux)
#   curl -fsSL https://agentsorg.sahilsidhu.pro/install.sh | sh
# or from a local checkout:  ./install.sh
# Upgrading from 0.7: run it again. Your companies, clients and history are kept.
set -e
VERSION="0.8.4"
SRC="${AGENTSORG_SRC:-https://agentsorg.sahilsidhu.pro/agentsorg-$VERSION-py3-none-any.whl}"
case "$0" in */install.sh) [ -z "$AGENTSORG_SRC" ] && [ -f "$(dirname "$0")/pyproject.toml" ] && SRC="$(cd "$(dirname "$0")" && pwd)";; esac

echo "Installing agents.org $VERSION"
PY=$(command -v python3 || command -v python || true)
[ -z "$PY" ] && { echo "Python 3.11+ is required: https://www.python.org/downloads/"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || { echo "Python 3.11+ is required (found $("$PY" -V 2>&1))"; exit 1; }

HAS_CLAUDE=1
command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ] || HAS_CLAUDE=0

BIN="$HOME/.local/bin"
mkdir -p "$BIN"
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SRC" >/dev/null
else
  # a private venv: works on distros that block `pip install --user` (PEP 668)
  VENV="$HOME/.local/share/agentsorg/venv"
  "$PY" -m venv "$VENV" 2>/dev/null || { echo "The python venv module is missing. Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
  "$VENV/bin/pip" install -q --upgrade --force-reinstall "$SRC"
  ln -sf "$VENV/bin/agentsorg" "$BIN/agentsorg"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to your PATH:  echo 'export PATH=\$HOME/.local/bin:\$PATH' >> ~/.profile";; esac

# a running company picks up the new version
if command -v systemctl >/dev/null 2>&1 && systemctl --user list-units 'agentsorg*' --no-legend 2>/dev/null | grep -q agentsorg; then
  systemctl --user restart 'agentsorg*' 2>/dev/null && echo "Restarted your running company service(s)."
fi

echo
echo "Installed agents.org $VERSION."
if [ "$HAS_CLAUDE" = 0 ]; then
  echo
  echo "1. Install Claude Code and log in once (agents.org runs on your Claude subscription):"
  echo "     curl -fsSL https://claude.ai/install.sh | bash && claude"
fi
echo
echo "Start a company:"
echo "  agentsorg init ~/my-company"
echo "  cd ~/my-company && agentsorg start        # opens the dashboard"
echo
echo "In the dashboard: describe your startup, accept the teams Claude designs, then tell it what to do."
echo "Keep it running in the background:  agentsorg service install"
