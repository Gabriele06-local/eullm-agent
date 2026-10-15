#!/bin/sh
# EuLLM Agent, local installation (Linux and macOS): the Core binary and
# Editor Mode in one folder, nothing else to install by hand. uv installs
# the Python that Editor Mode needs; the data stays in SQLite files.
#
#   curl -LsSf https://raw.githubusercontent.com/eullm/eullm-agent/main/install/local.sh | sh
#   sh install/local.sh --source .          # from a checkout (builds the binary with cargo)
#
# Options: --source DIR, --version vX.Y.Z (default: the latest release),
# --no-editor (the Core only). EULLM_HOME sets the folder (default ~/.eullm-agent).
set -eu

REPO=eullm/eullm-agent
HOME_DIR=${EULLM_HOME:-$HOME/.eullm-agent}
BIN_DIR=$HOME/.local/bin
VERSION=latest
SOURCE=
EDITOR=yes

while [ $# -gt 0 ]; do
    case $1 in
        --source) SOURCE=$(cd "$2" && pwd); shift 2 ;;
        --version) VERSION=$2; shift 2 ;;
        --no-editor) EDITOR=no; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "$1 is needed and not installed"; }

need curl
need tar
mkdir -p "$HOME_DIR/bin" "$HOME_DIR/workspace" "$BIN_DIR"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# --- the Core binary --------------------------------------------------------
if [ -n "$SOURCE" ]; then
    say "Building the Core in $SOURCE"
    need cargo
    (cd "$SOURCE" && cargo build --release --locked)
    cp "$SOURCE/target/release/eullm-agent" "$HOME_DIR/bin/eullm-agent"
else
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) ASSET=eullm-agent-linux-x64 ;;
        Linux-aarch64 | Linux-arm64) ASSET=eullm-agent-linux-arm64 ;;
        Darwin-arm64) ASSET=eullm-agent-macos-arm64 ;;
        *) fail "no ready binary for $(uname -s) $(uname -m): use --source with a checkout" ;;
    esac
    if [ "$VERSION" = latest ]; then
        VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" |
            sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
        [ -n "$VERSION" ] || fail "cannot find the latest release"
    fi
    say "Downloading EuLLM Agent $VERSION"
    BASE="https://github.com/$REPO/releases/download/$VERSION"
    curl -fsSL -o "$TMP/$ASSET" "$BASE/$ASSET"
    curl -fsSL -o "$TMP/$ASSET.sha256" "$BASE/$ASSET.sha256"
    WANT=$(cut -d ' ' -f 1 "$TMP/$ASSET.sha256")
    if command -v sha256sum >/dev/null 2>&1; then
        GOT=$(sha256sum "$TMP/$ASSET" | cut -d ' ' -f 1)
    else
        GOT=$(shasum -a 256 "$TMP/$ASSET" | cut -d ' ' -f 1)
    fi
    [ "$WANT" = "$GOT" ] || fail "the download does not match its checksum"
    install -m 755 "$TMP/$ASSET" "$HOME_DIR/bin/eullm-agent"
fi

# --- Editor Mode ------------------------------------------------------------
if [ "$EDITOR" = yes ]; then
    if ! command -v uv >/dev/null 2>&1 && [ ! -x "$BIN_DIR/uv" ]; then
        say "Installing uv (it installs Python for Editor Mode)"
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    fi
    UV=$(command -v uv || echo "$BIN_DIR/uv")

    say "Installing Editor Mode"
    if [ -n "$SOURCE" ]; then
        (cd "$SOURCE" && tar -cf - --exclude .venv --exclude __pycache__ --exclude .pytest_cache editor-mode) |
            (cd "$TMP" && tar -xf -)
    else
        curl -fsSL "https://github.com/$REPO/archive/refs/tags/$VERSION.tar.gz" | (cd "$TMP" && tar -xzf -)
        mv "$TMP"/eullm-agent-*/editor-mode "$TMP/editor-mode"
    fi
    rm -rf "$HOME_DIR/editor-mode"
    mv "$TMP/editor-mode" "$HOME_DIR/editor-mode"
    (cd "$HOME_DIR/editor-mode" && "$UV" sync --locked --no-dev --quiet)
fi

# --- the command ------------------------------------------------------------
cat > "$BIN_DIR/eullm-local" <<LAUNCHER
#!/bin/sh
# Starts EuLLM Agent on this computer (installed by install/local.sh).
cd "$HOME_DIR" && exec "$HOME_DIR/bin/eullm-agent" -c "$HOME_DIR/config.yaml" start "\$@"
LAUNCHER
chmod 755 "$BIN_DIR/eullm-local"

say "Done. Start it with:"
echo
echo "    eullm-local"
echo
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "($BIN_DIR is not in PATH: run $BIN_DIR/eullm-local, or add it to PATH)" ;;
esac
echo "Data, configuration and logs: $HOME_DIR"
echo "The first start looks for an EuLLM Engine on localhost:11434, or asks which provider to use."
