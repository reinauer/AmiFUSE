#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# AmiFUSE Unix Installer (macOS + Linux)
# ---------------------------------------------------------------------------

VENV_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/amifuse/venv"

cyan='\033[0;36m'
yellow='\033[0;33m'
green='\033[0;32m'
red='\033[0;31m'
reset='\033[0m'

step()  { printf "${yellow}[*] %s${reset}\n" "$*"; }
ok()    { printf "${green}[+] %s${reset}\n" "$*"; }
err()   { printf "${red}[!] %s${reset}\n" "$*"; }

banner() {
    echo ""
    printf "${cyan}========================================${reset}\n"
    printf "${cyan}  AmiFUSE Unix Installer${reset}\n"
    printf "${cyan}========================================${reset}\n"
    echo ""
}

has() { command -v "$1" &>/dev/null; }

# ---------------------------------------------------------------------------
# 1. Banner
# ---------------------------------------------------------------------------
banner

# ---------------------------------------------------------------------------
# 2. Detect OS
# ---------------------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Darwin) step "Detected macOS" ;;
    Linux)  step "Detected Linux" ;;
    *)      err "Unsupported OS: $OS"; exit 1 ;;
esac

# ---------------------------------------------------------------------------
# 3. Detect Python 3.9+
# ---------------------------------------------------------------------------
step "Detecting Python..."

PYTHON=""
for candidate in python3 python; do
    if has "$candidate"; then
        ver="$("$candidate" --version 2>&1)"
        if [[ "$ver" =~ Python\ ([0-9]+)\.([0-9]+) ]]; then
            major="${BASH_REMATCH[1]}"
            minor="${BASH_REMATCH[2]}"
            if (( major >= 3 && minor >= 9 )); then
                PYTHON="$candidate"
                ok "Found $ver ($candidate)"
                break
            fi
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    err "Python 3.9+ not found."
    echo ""
    if [[ "$OS" == "Darwin" ]]; then
        echo "Install with:  brew install python@3.12"
    elif has apt; then
        echo "Install with:  sudo apt install python3 python3-venv"
    elif has dnf; then
        echo "Install with:  sudo dnf install python3"
    elif has pacman; then
        echo "Install with:  sudo pacman -S python"
    else
        echo "Install Python 3.9+ from https://python.org"
    fi
    echo ""
    echo "Then re-run this script."
    exit 1
fi

# ---------------------------------------------------------------------------
# 4. Detect FUSE
# ---------------------------------------------------------------------------
step "Detecting FUSE..."

if [[ "$OS" == "Darwin" ]]; then
    if [[ -d "/Library/Filesystems/macfuse.fs" ]]; then
        ok "macFUSE found"
    elif has brew && brew list fuse-t &>/dev/null 2>&1; then
        ok "FUSE-T found"
    else
        err "No FUSE implementation found."
        echo ""
        echo "Install macFUSE with:  brew install --cask macfuse"
        echo "  or FUSE-T with:      brew install fuse-t"
        echo ""
        read -rp "Install macFUSE via Homebrew now? (y/n) " reply
        if [[ "$reply" == "y" ]]; then
            step "Installing macFUSE..."
            brew install --cask macfuse
            ok "macFUSE installed (reboot may be required)."
        else
            err "FUSE is required. Install manually and re-run."
            exit 1
        fi
    fi
else
    # Linux
    if has fusermount3 || has fusermount; then
        ok "FUSE found ($(has fusermount3 && echo fusermount3 || echo fusermount))"
    else
        err "FUSE not found."
        echo ""
        if has apt; then
            echo "Install with:  sudo apt install fuse3 libfuse3-dev"
            read -rp "Install now? (y/n) " reply
            if [[ "$reply" == "y" ]]; then
                sudo apt install -y fuse3 libfuse3-dev
            fi
        elif has dnf; then
            echo "Install with:  sudo dnf install fuse3 fuse3-devel"
            read -rp "Install now? (y/n) " reply
            if [[ "$reply" == "y" ]]; then
                sudo dnf install -y fuse3 fuse3-devel
            fi
        elif has pacman; then
            echo "Install with:  sudo pacman -S fuse3"
            read -rp "Install now? (y/n) " reply
            if [[ "$reply" == "y" ]]; then
                sudo pacman -S --noconfirm fuse3
            fi
        else
            echo "Install fuse3 using your package manager and re-run."
            exit 1
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 5. Detect / create venv
# ---------------------------------------------------------------------------
step "Setting up virtual environment..."

if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    ok "Using active venv at $VIRTUAL_ENV"
else
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        ok "Existing venv found at $VENV_DIR"
    else
        step "Creating venv at $VENV_DIR..."
        mkdir -p "$(dirname "$VENV_DIR")"
        "$PYTHON" -m venv "$VENV_DIR"
        ok "Venv created."
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    ok "Venv activated."
fi

# ---------------------------------------------------------------------------
# 6. Install AmiFUSE
# ---------------------------------------------------------------------------
step "Installing AmiFUSE..."

DEV_MODE=false
if [[ -f "pyproject.toml" ]] && grep -q 'name.*=.*"amifuse"' pyproject.toml 2>/dev/null; then
    DEV_MODE=true
fi

if $DEV_MODE; then
    step "Dev checkout detected -- installing in editable mode..."
    pip install -e .
else
    step "Installing from PyPI..."
    pip install amifuse
fi

# ---------------------------------------------------------------------------
# 7. Run doctor --fix
# ---------------------------------------------------------------------------
step "Running amifuse doctor --fix..."
amifuse doctor --fix || {
    err "amifuse doctor failed. You can run 'amifuse doctor --fix' manually later."
}

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
echo ""
printf "${cyan}========================================${reset}\n"
printf "${cyan}  Installation Complete${reset}\n"
printf "${cyan}========================================${reset}\n"
echo ""
echo "  Python:   $(python3 --version 2>/dev/null || python --version)"
echo "  Venv:     ${VIRTUAL_ENV:-$VENV_DIR}"
if $DEV_MODE; then
    echo "  Mode:     editable (dev)"
else
    echo "  Mode:     PyPI release"
fi
echo ""
printf "${yellow}Next steps:${reset}\n"
echo "  amifuse mount <image> <mountpoint>   Mount an Amiga disk image"
echo "  amifuse doctor                       Check system health"
echo ""
