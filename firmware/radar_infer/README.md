# Pedestrian detector — ESP32-S3 half

Reads Sparse IQ frames from the XM125 over UART, runs the feature extraction
from `training/features.py` in C, invokes the int8 CNN, and shows the result on
the board's 480×480 panel.

```
XM125 ──UART1, 2 Mbaud, GPIO4── ESP32-S3-LCD-EV-Board
                                  frame → column → 64-column window
                                  → CNN → CLEAR / PEDESTRIAN + spectrogram
```

## Wiring

```
XE125  J2 pin 11  (UART_TX) ──→  LCD-EV-Board  J5/EXT_IO pin 11  (IO4)
XE125  J2 pin 10  (GND)     ──→  LCD-EV-Board  J5/EXT_IO pin 1, 7, 19 or 20
```

Both boards keep their USB cables: the XE125 for power (it needs a 1.8 V rail
the jumper does not carry), the ESP32 for flashing and the console. **The
ESP32's TX is deliberately not connected** — see `agentContext/embeddedlink.md`.

Three traps, all of which produce the same symptom (silence):

- **`EX_IO4` is not `IO4`.** J5 pin 12 is an I/O-expander output driven over
  I2C; pin 11 is the chip pin. Adjacent pins, near-identical labels.
- **2×11 headers number across the rows, not along them.** Pin 11 is the sixth
  position in the odd row; counting eleven along a row lands on `5V_IO`.
- **The XE125 must stay powered.** Unplugging it stops the stream silently.

The firmware diagnoses all three at boot. `GPIO4 probe` reports the pin
electrically — static HIGH means idle or disconnected, static LOW means
grounded, transitions mean data is present — and a byte counter then separates
"no bytes at all" from "bytes that never synchronise".

## Why UART1 on GPIO4

This board is an ESP32-S3-LCD-EV-Board, whose EXT_IO header exposes only six
real GPIOs; everything else drives the RGB panel, the audio codec or the I/O
expander. Of those six, `IO19`/`IO20` are the native USB pins, `IO0` is the BOOT
strapping pin (held low at reset the chip enters the download loader instead of
running), and `IO47`/`IO48` are level-shifted I2C for the touch panel. `IO4`
drives only the status LED, so it is the one safe choice — and the LED
flickering is a free sign that bytes are arriving.

## The display

Not LVGL. The BSP's own benchmark puts LVGL near 97 % CPU on this board, and the
detector needs a quarter of a core; drawing straight into the RGB panel costs a
640-byte transfer per frame instead. `bsp_display_new()` gives the panel handle
without bringing LVGL up.

```
y   0.. 99   CLEAR / PEDESTRIAN, probability bar with a tick at the threshold
y 100..419   spectrogram, 64 Doppler bins × 5 px, 480 columns = 19.2 s
y 420..479   APPROACH / DEPART axis
```

The spectrogram renders `column_q` — the int8 values **after** normalisation and
quantisation, the literal bytes fed to the network. A separately scaled "pretty"
view would hide exactly the failure worth seeing.

The cursor advances one column per frame, so it crosses the screen in 19.2 s.
That is the 25 Hz **frame** rate, not the 5500 Hz **sweep** rate — the sweeps
resolve velocity *within* each frame.

### Panel configuration is not free choice

`CONFIG_BSP_LCD_RGB_BOUNCE_BUFFER_MODE` **with** `ESP32S3_DATA_CACHE_LINE_64B`.
The RGB peripheral streams the framebuffer out of PSRAM at pixel-clock rate, and
any stall shifts every following pixel — the image walks down and across the
panel. Auto-refresh drifted here both with a 64 B and a 32 B cache line; bounce
buffer mode stages scanlines in internal RAM, where latency is deterministic,
and the image is stable. The two settings belong together: 64 B *without* bounce
buffer is the one combination the BSP warns about at boot.

### The detector runs on core 1

Bounce-buffer copying is continuous work on core 0. With the radar loop there
too, the idle task never ran and the task watchdog fired. `radar_task` is
therefore pinned to core 1.

## Decision state versus event edge

Two different things, easily confused — the display showed the wrong one at
first, which made a working detector look indecisive:

- **`detection`** is an *edge*: true only on the window where the positive run
  first reaches `RADAR_EVENT_RUN`. It counts events, and it is what the
  leave-one-subject-out evaluation counted. It is true for one 160 ms window and
  false again while the person is still walking.
- **`detected_state`** is the *latch*, for the screen: set when the run reaches
  `RADAR_EVENT_RUN`, cleared after two consecutive negatives. The release
  hysteresis stops the banner flickering at turns, where radial velocity passes
  through zero and a Doppler detector briefly has nothing to see.

Event counting is unchanged, so reported numbers keep their original meaning.

## Building

```bash
. $HOME/esp/esp-idf/export.sh
CMAKE_BUILD_PARALLEL_LEVEL=4 idf.py build      # idf.py has no -j flag
idf.py -p /dev/ttyACM0 flash monitor
```

`radar_features_config.h` and the model array are included straight from
`training/out/final/`, so retraining cannot leave a stale constant here.
`tests/test_firmware_protocol.py` checks that this firmware, the XM125 firmware
and `tools/replay_frames.py` still agree on the wire format.

## Measured on hardware

| | |
|---|---|
| Boot self-test | worst 8e-6 dB vs `features.py`, 0/64 int8 mismatches |
| DSP | 1.55 ms per frame (40 ms budget) |
| Inference | ~32 ms per window (160 ms budget) |
| Duty cycle | ~24 % of core 1 |
| Arena | 48,668 B of 57,344 |
| Live empty room | 124 windows, 0 false positives, max p 0.035 |
| Live walk | 98 of 110 windows positive, p 0.996 |

Inference was 26.4 ms before the display existed; the RGB panel's PSRAM traffic
accounts for the difference.
