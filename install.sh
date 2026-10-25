#!/usr/bin/env bash
#
# One-command installer for agmux (Linux).
#
#   ./install.sh                     # link agmux + wire up ~/.bashrc and ~/.tmux.conf
#   ./install.sh --bin ~/bin         # choose where the command is linked
#   ./install.sh --no-integration    # only link the command
#   ./install.sh --uninstall         # remove links and the managed blocks
#
# The tmux block installs Ctrl-b Ctrl-s (popup), Alt-s (detach and open agmux),
# and Alt-x (kill the current session after confirmation). Everything is
# idempotent: the integration lives between markers and is replaced in place,
# so running it twice is safe.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bindir="${AGMUX_BIN_DIR:-$HOME/.local/bin}"
integration=1
uninstall=0

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

  --bin DIR           where to link the command (default: ~/.local/bin)
  --no-integration    only link the command; do not edit dotfiles
  --uninstall         remove the links and the managed dotfile blocks
  -h, --help          show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --bin) bindir="$2"; shift 2 ;;
        --bin=*) bindir="${1#*=}"; shift ;;
        --no-integration) integration=0; shift ;;
        --uninstall) uninstall=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

begin="# >>> agmux >>>"
end="# <<< agmux <<<"

strip_block() {
    # Remove a previously installed, marker-delimited block from a file.
    local file="$1"
    [ -f "$file" ] || return 0
    if ! grep -qF "$begin" "$file"; then
        return 0
    fi
    local tmp
    tmp="$(mktemp)"
    awk -v b="$begin" -v e="$end" '
        $0 == b { skip = 1; next }
        $0 == e { skip = 0; next }
        !skip { print }
    ' "$file" >"$tmp"
    cat "$tmp" >"$file"
    rm -f "$tmp"
}

write_block() {
    # Replace (or append) the managed block in a file with stdin.
    local file="$1"
    strip_block "$file"
    mkdir -p "$(dirname "$file")"
    {
        printf '%s\n' "$begin"
        cat
        printf '%s\n' "$end"
    } >>"$file"
}

if [ "$uninstall" = 1 ]; then
    rm -f "$bindir/agmux" "$bindir/cx"
    mandir="$(dirname "$bindir")/share/man/man1"
    rm -f "$mandir/agmux.1"
    strip_block "$HOME/.bashrc"
    strip_block "$HOME/.tmux.conf"
    echo "Removed $bindir/agmux and $bindir/cx, the installed man page, and the"
    echo "agmux blocks from ~/.bashrc and ~/.tmux.conf. Your tmux sessions are untouched."
    exit 0
fi

# ---------------------------------------------------------------------------
# Link the command
# ---------------------------------------------------------------------------
chmod +x "$here/agmux.py"
mkdir -p "$bindir"
ln -sf "$here/agmux.py" "$bindir/agmux"
ln -sf "$here/agmux.py" "$bindir/cx"   # historical name still works
echo "Linked $bindir/agmux (and $bindir/cx)"

# Optional man page, next to the bindir so no root is needed.
mandir="$(dirname "$bindir")/share/man/man1"
if [ -f "$here/agmux.1" ]; then
    if mkdir -p "$mandir" 2>/dev/null && cp "$here/agmux.1" "$mandir/agmux.1" 2>/dev/null; then
        echo "Installed man page: $mandir/agmux.1 (try: man agmux)"
    else
        echo "Note: could not install the man page in $mandir (continuing)." >&2
    fi
fi

if ! command -v tmux >/dev/null 2>&1; then
    echo "Note: tmux is not installed. agmux needs tmux to run:" >&2
    echo "      sudo apt install tmux   # Debian/Ubuntu" >&2
    echo "      sudo dnf install tmux   # Fedora" >&2
fi

# ---------------------------------------------------------------------------
# Wire up the shell and tmux integration
# ---------------------------------------------------------------------------
if [ "$integration" = 1 ]; then
    if [ -f "$HOME/.bashrc" ]; then
        write_block "$HOME/.bashrc" <<EOF
# agmux command completion (bash)
[ -f "$here/completion.bash" ] && source "$here/completion.bash"
EOF
        echo "Updated ~/.bashrc (reload with: source ~/.bashrc)"
    else
        echo "No ~/.bashrc found; add this yourself to your shell startup:"
        echo "    source \"$here/completion.bash\""
    fi

    tmux_block() {
        printf 'source-file "%s/tmux.conf"\n' "$here"
        if ! printf '%s' "$PATH" | tr ':' '\n' | grep -qxF "$bindir"; then
            # The tmux server may not see ~/.local/bin; pin the absolute path.
            printf 'set-environment -g AGMUX_BIN "%s/agmux"\n' "$bindir"
        fi
    }
    write_block "$HOME/.tmux.conf" < <(tmux_block)
    echo "Updated ~/.tmux.conf (reload with: tmux source-file ~/.tmux.conf)"
fi

# ---------------------------------------------------------------------------
# PATH hint
# ---------------------------------------------------------------------------
case ":$PATH:" in
    *":$bindir:"*) ;;
    *)
        echo
        echo "Add $bindir to your PATH, e.g. in ~/.bashrc:"
        echo "    export PATH=\"$bindir:\$PATH\""
        ;;
esac

echo
echo "Done. Run 'agmux' to start, or Ctrl-b then Ctrl-s inside tmux."
echo "Optional config file: ~/.config/agmux/config (see the README)"
echo "Try 'agmux doctor' to check your setup."
