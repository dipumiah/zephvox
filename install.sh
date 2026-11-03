#!/bin/sh
# zephvox installer for macOS and Linux. Also updates an existing install.
#
#   curl -LsSf https://raw.githubusercontent.com/dipumiah/zephvox/main/install.sh | sh
#
# Options go after `sh -s --`, e.g. `... | sh -s -- --profile cpu --yes` (see `zephvox setup --help`).
# Environment: ZEPHVOX_DIR (default ~/zephvox), ZEPHVOX_VERSION (a release tag; default latest).
# The SPEAKCHECK_* names from before the rename still work for DIR.
#
# What it does: installs uv if needed, downloads zephvox into one folder, installs the small core,
# then hands over to `zephvox setup`, which detects your hardware and installs the rest.
set -eu

REPO="dipumiah/zephvox"
# An install made under the app's old name (~/speakcheck) is updated in place, not duplicated.
DIR="${ZEPHVOX_DIR:-${SPEAKCHECK_DIR:-}}"
if [ -z "$DIR" ]; then
    if [ -d "$HOME/speakcheck" ] && [ ! -d "$HOME/zephvox" ]; then DIR="$HOME/speakcheck"; else DIR="$HOME/zephvox"; fi
fi

say() { printf '%s\n' "$*"; }
fail() { say "error: $*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v tar >/dev/null 2>&1 || fail "tar is required"
[ -d "$DIR/.git" ] && fail "$DIR is a git checkout: update it with git pull instead"

# 1. uv, which installs Python and every package
if command -v uv >/dev/null 2>&1; then
    UV=$(command -v uv)
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
else
    say "Installing uv (Python package manager)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || fail "uv installation failed"
fi

# 2. zephvox itself: the latest release, or main while there is none
#    (ZEPHVOX_SOURCE: another source archive, e.g. file:///path/to/zephvox.tar.gz, for testing)
VERSION="${ZEPHVOX_VERSION:-}"
if [ -n "${ZEPHVOX_SOURCE:-}" ]; then
    VERSION="${VERSION:-local}"
elif [ -z "$VERSION" ]; then
    VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null |
        sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1) || true
fi
if [ -n "${ZEPHVOX_SOURCE:-}" ]; then
    URL="$ZEPHVOX_SOURCE"
elif [ -n "$VERSION" ]; then
    URL="https://github.com/$REPO/archive/refs/tags/$VERSION.tar.gz"
else
    VERSION=main
    URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
fi
say "Downloading zephvox $VERSION into $DIR…"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$URL" | tar -xz -C "$TMP"
SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)
mkdir -p "$DIR"
# Replace what the release contains; config.toml, .env and .venv aren't in it, so they stay.
for item in "$SRC"/* "$SRC"/.[!.]*; do
    [ -e "$item" ] || continue
    name=$(basename "$item")
    rm -rf "${DIR:?}/$name"
    mv "$item" "$DIR/$name"
done
rm -rf "$TMP"
trap - EXIT

# 3. the core (a few MB), then the setup wizard for everything else
cd "$DIR"
"$UV" sync --no-dev --frozen --quiet
say "Starting setup…"
# Under `curl | sh` the script itself is on stdin: ask the questions on the terminal instead.
if (exec </dev/tty) 2>/dev/null; then
    exec "$UV" run --no-sync python -m zephvox.cli setup --installed-version "$VERSION" "$@" </dev/tty
fi
exec "$UV" run --no-sync python -m zephvox.cli setup --installed-version "$VERSION" --yes "$@"
