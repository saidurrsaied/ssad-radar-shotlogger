#!/usr/bin/env bash
# Build (and optionally flash) the XM125 frame streamer.
#
# The firmware source lives here, in the project repo. The Acconeer SDK lives
# outside it at ~/acconeer/xm125, because its licence forbids publishing it.
# This script bridges the two WITHOUT modifying the SDK: it symlinks our source
# into the SDK's Src/applications (which is already on the makefile's vpath)
# and passes the target and its source list on the command line, which the
# SDK's own generic target rule picks up.
#
#   ./build.sh              build
#   ./build.sh flash        build, then flash over the UART bootloader
#
# Requires GNU_INSTALL_ROOT and STM32CUBE_FW_L4_ROOT -- see TOOLCHAIN_SETUP.md
# section 5. Nothing here needs the ESP32 side.

set -euo pipefail

SDK="${ACCONEER_XM125_SDK:-$HOME/acconeer/xm125}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="xm125_frame_streamer.c"
TARGET="xm125_frame_streamer"
PORT="${XM125_PORT:-/dev/ttyUSB0}"
JOBS="${JOBS:-4}"          # ninja/make defaults saturate an 8-core laptop

for var in GNU_INSTALL_ROOT STM32CUBE_FW_L4_ROOT; do
    if [ -z "${!var:-}" ]; then
        echo "error: $var is not set (see TOOLCHAIN_SETUP.md section 5)" >&2
        exit 1
    fi
done

if [ ! -d "$SDK" ]; then
    echo "error: Acconeer XM125 SDK not found at $SDK" >&2
    echo "       set ACCONEER_XM125_SDK if it lives elsewhere" >&2
    exit 1
fi

# Symlink rather than copy, so the repo stays the single source of truth and an
# edit here cannot be shadowed by a stale copy inside the SDK.
ln -sf "$HERE/$SOURCE" "$SDK/Src/applications/$SOURCE"

echo "building $TARGET from $HERE/$SOURCE"
make -C "$SDK" -j "$JOBS" \
    TARGETS="$TARGET" \
    "SOURCES_$(echo "$TARGET" | tr '[:lower:]' '[:upper:]')=$SOURCE" \
    "$TARGET"

BIN="$SDK/out/$TARGET.bin"
"$GNU_INSTALL_ROOT/arm-none-eabi-size" "$SDK/out/$TARGET.elf"
echo "binary: $BIN ($(stat -c %s "$BIN") bytes of 131072)"

if [ "${1:-}" != "flash" ]; then
    echo
    echo "to flash: put the module in its bootloader (hold DFU, tap RESET,"
    echo "          release RESET, release DFU) then run: $0 flash"
    exit 0
fi

echo
echo "flashing $BIN to $PORT"
echo "the module must already be in bootloader mode"
STM32_Programmer_CLI -c port="$PORT" br=115200 -w "$BIN" 0x08000000 -v

echo
echo "done. Tap RESET on its own to run it."
echo "NOTE: the exploration server is gone until you restore it --"
echo "      see ~/acconeer/xm125_backup/README.txt"
