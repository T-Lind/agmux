#!/usr/bin/env bash
# Install agmux by symlinking the single script onto your PATH.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bindir="${1:-$HOME/.local/bin}"

mkdir -p "$bindir"
chmod +x "$here/agmux.py"
ln -sf "$here/agmux.py" "$bindir/agmux"
# Keep the historical `cx` command working.
ln -sf "$here/agmux.py" "$bindir/cx"

echo "Linked $bindir/agmux (and $bindir/cx)"
echo
echo "Add to ~/.bashrc:"
echo "    source \"$here/completion.bash\""
echo
echo "Add to ~/.tmux.conf, then run: tmux source-file ~/.tmux.conf"
echo "    source-file \"$here/tmux.conf\""
echo
echo "Make sure $bindir is on PATH (and on the tmux server's PATH)."
