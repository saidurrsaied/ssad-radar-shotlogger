# XM125 frame streamer

Firmware for the radar module. It configures the A121 with the campaign's
frozen configuration and pushes every frame out of USART2 untouched — no signal
processing on the module. The ESP32-S3 (`../radar_infer`) does the DSP and the
inference.

```
A121 ──SPI── XM125 (Cortex-M4, this firmware) ──UART 2 Mbaud── ESP32-S3
                                                 141 kB/s, one direction
```

## The link is one-directional

Only `UART_TX` (J2 pin 11) and `GND` (pin 10) are wired. **The ESP32's TX is
deliberately not connected**, because the XE125's on-board CP2105 already drives
that net whenever USB is plugged in and two transmitters would contend.

So this firmware never reads the UART, never accepts a command and never
negotiates a baud rate — it starts streaming as soon as the sensor is ready.
That is also why it must *not* copy the exploration server's startup wait on the
RX pin: nothing will ever release that line, and the board would look dead.

## Wire format

Repeated once per frame, little-endian:

```
A1 21 F0 0D | 64 sweeps × 22 range points of { int16 real, int16 imag }
            = 4 + 5632 bytes, 25 per second
```

The magic word exists so a receiver that loses sync recovers on the next frame.
`tests/test_firmware_protocol.py` checks that this firmware, the ESP32 reader
and `tools/replay_frames.py` all agree on it, and that the C configuration still
matches `config/session_config.json`.

## Licensing

This firmware is ours, but parts of it derive from the Acconeer SDK's **example
sources, which are BSD 3-Clause** — the sensor lifecycle from
`example_service.c` and the DMA transmit from `acc_exploration_server_stm32.c`.
The copyright notice and licence text are retained in
`LICENSE-Acconeer-BSD-3-Clause.txt`, as that licence requires.

That is a different licence from the one covering the SDK's **prebuilt
libraries, binaries and documents**, which is restrictive and forbids
redistribution. Those are not in this repository and must not be added; they
live at `~/acconeer/`.

## Building

Needs `GNU_INSTALL_ROOT` and `STM32CUBE_FW_L4_ROOT` — see `TOOLCHAIN_SETUP.md`
section 5. The Acconeer SDK is **not** in this repo; its licence forbids
publishing it. `build.sh` symlinks this source into the SDK's
`Src/applications/` (already on the makefile's vpath) and passes the target on
the command line, so **the SDK itself is never modified**.

```bash
./build.sh              # build
./build.sh flash        # build, then flash (module must be in bootloader mode)
```

Set `ACCONEER_XM125_SDK` if the SDK is not at `~/acconeer/xm125`, `XM125_PORT`
to override the port (otherwise the XE125 is found by identity under
`/dev/serial/by-id/`, which matters once the ESP32 is plugged in too), and
`JOBS` to change the `-j 4` default.

### Hardware flow control is off by default

The stock UART config uses RTS/CTS and the module pauses when CTS is
de-asserted. The ESP32 never drives that pin — the XE125's own CP2105 does, and
it stays powered because the board needs its 1.8 V rail. If nothing holds the
bridge's port open, its driver may leave RTS de-asserted and **the module would
stop transmitting**. So the default build ignores CTS and the receiver must keep
up, which the ESP32 can.

For PC captures, build with it on, or the cp210x driver drops whole 256-byte
URBs and about a fifth of every frame:

```bash
FLOW_CONTROL=1 ./build.sh          # and use `stty ... crtscts` when capturing
```

Current size: **64,896 bytes of the module's 131,072** — 49.5 %, against the
stock exploration server's 83 %.

## Flashing, and getting back

Flashing replaces the exploration server, which is what the Exploration Tool and
Shot Logger talk to. **The recording setup stops working until it is restored.**

1. Bootloader mode: hold **DFU**, tap **RESET**, release RESET, release DFU.
   (The button is named DFU but drives BOOT0; the transport is the STM32 UART
   bootloader, not USB DFU.)
2. `./build.sh flash`
3. Tap **RESET** on its own to run it.

To restore the recording setup, see `~/acconeer/xm125_backup/README.txt`. That
directory holds a verified read-out of the module's flash taken before the first
custom firmware, byte-identical to Acconeer's shipped image. Afterwards
`tools/check_install.py` must again report `sensor found: XE125`.

## What is not proved yet

Nothing here has run against the sensor. Specifically unverified:

- **Frame layout.** The ESP32 expects sweep-major ordering, matching the
  recordings. The first live frames must be checked against that assumption
  before any accuracy number is believed.
- **Timing.** Whether the module sustains 25 fps while transmitting, and whether
  the receiver keeps up without dropping frames.
- **Calibration drift.** `calibration_needed` drops a frame and recalibrates;
  how often that happens in practice is unknown.
