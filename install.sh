#!/bin/sh
# subcortex installer.
#
#   curl -fsSL https://raw.githubusercontent.com/pavlealeksic/subcortex/main/install.sh | sh
#
# Installs the `subcortex` CLI into its own isolated environment (uv tool,
# pipx, or a private venv — whichever is available), then starts the
# interactive `subcortex setup`. Nothing touches your TUI configs until you
# confirm it in setup.
#
# Environment:
#   SUBCORTEX_SOURCE   what to install (default: subcortex from PyPI;
#                      e.g. git+https://github.com/pavlealeksic/subcortex)
#   SUBCORTEX_NO_SETUP set to 1 to skip launching setup
set -eu

SOURCE="${SUBCORTEX_SOURCE:-subcortex}"
BIN_DIR="${HOME}/.local/bin"
VENV_DIR="${HOME}/.local/share/subcortex/cli"

say() { printf '\033[1m◆\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

case "$(uname -s)" in
    Darwin|Linux) ;;
    *) fail "subcortex supports macOS and Linux" ;;
esac

if command -v uv >/dev/null 2>&1; then
    say "installing subcortex with uv"
    uv tool install --force "$SOURCE"
    SUBCORTEX="$(uv tool dir --bin 2>/dev/null || echo "$BIN_DIR")/subcortex"
elif command -v pipx >/dev/null 2>&1; then
    say "installing subcortex with pipx"
    pipx install --force "$SOURCE"
    SUBCORTEX="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$BIN_DIR")/subcortex"
else
    PYTHON="$(find_python)" || fail "Python 3.11+ is required (install it, or install uv: https://docs.astral.sh/uv/)"
    say "installing subcortex into ${VENV_DIR} (using ${PYTHON})"
    "$PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$SOURCE"
    mkdir -p "$BIN_DIR"
    ln -sf "$VENV_DIR/bin/subcortex" "$BIN_DIR/subcortex"
    ln -sf "$VENV_DIR/bin/subcortex-hook" "$BIN_DIR/subcortex-hook"
    SUBCORTEX="$BIN_DIR/subcortex"
fi

[ -x "$SUBCORTEX" ] || SUBCORTEX="$(command -v subcortex || true)"
[ -n "$SUBCORTEX" ] || fail "installed, but the subcortex command was not found"
say "installed $("$SUBCORTEX" --version)"

case ":${PATH}:" in
    *":$(dirname "$SUBCORTEX"):"*) ;;
    *) say "add $(dirname "$SUBCORTEX") to your PATH to run subcortex directly" ;;
esac

if [ "${SUBCORTEX_NO_SETUP:-0}" = "1" ]; then
    say "next: subcortex setup"
elif [ -r /dev/tty ] && (exec </dev/tty) 2>/dev/null; then
    # Piped through `curl | sh`: give setup the terminal, not the pipe.
    "$SUBCORTEX" setup </dev/tty
else
    say "next: subcortex setup"
fi
