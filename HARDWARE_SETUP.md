# Hardware and dev-tool setup — build, flash, run, improve

**Goal: from a fresh PC to a working demo — radar in, `PEDESTRIAN` on the
screen — and then the loop for improving the model.**

`README.md` covers the recording side (Exploration Tool + Shot Logger).
`TOOLCHAIN_SETUP.md` explains *why* each tool is the one it is. This is the
practical path: what to install, what to wire, what to flash, and how to check
each stage actually works before trusting the next one.

If an agent is helping you, point it at this file and at
`firmware/*/README.md`. The **Traps** section at the end exists so the same
mistakes are not repeated; they all produce identical symptoms and cost about an
hour each to diagnose from scratch.

---

## 1. What you need

**Hardware**

| Item | Notes |
| --- | --- |
| XE125 evaluation board (XM125 module) | The radar. USB-C cable for power **and** flashing |
| ESP32-S3-LCD-EV-Board + SUB2 | The 480×480 panel. USB-C to the **USB** port, not the UART port |
| 2 × jumper wires, female–female | Signal and ground between the two headers |

**Accounts** — both free, neither instant, both block work:

- **Acconeer** (`developer.acconeer.com`) — the A121 SDK for the XM125.
- **ST** (`st.com`) — STM32CubeProgrammer, which flashes the XM125.

**Disk**: about 4 GB for the toolchains and SDKs.

---

## 2. One-time tool install

Follow `TOOLCHAIN_SETUP.md` sections 2, 3 and 5. In brief:

```bash
# ESP32 side
git clone -b release/v5.4 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf
~/esp/esp-idf/install.sh esp32s3

# XM125 side — the exact toolchain the SDK was built with, as a standalone tree
cd ~/opt && curl -LO https://developer.arm.com/-/media/Files/downloads/gnu/13.3.rel1/\
binrel/arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi.tar.xz
tar -xJf arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi.tar.xz

# ST's HAL and CMSIS. THE SUBMODULES ARE NOT OPTIONAL.
git clone --depth 1 --branch v1.18.1 \
    https://github.com/STMicroelectronics/STM32CubeL4.git ~/opt/STM32Cube_FW_L4_V1.18.1
cd ~/opt/STM32Cube_FW_L4_V1.18.1
git submodule update --init --depth 1 \
    Drivers/STM32L4xx_HAL_Driver Drivers/CMSIS/Device/ST/STM32L4xx
```

Then STM32CubeProgrammer from st.com (Get Software → **Generic Linux**, not
Linux Arm) and install it to `/opt/st/stm32cubeprogrammer`.

Put these in `~/.bashrc`:

```bash
alias get_idf='. $HOME/esp/esp-idf/export.sh'
export GNU_INSTALL_ROOT="$HOME/opt/arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi/bin"
export STM32CUBE_FW_L4_ROOT="$HOME/opt/STM32Cube_FW_L4_V1.18.1"
export PATH="$GNU_INSTALL_ROOT:/opt/st/stm32cubeprogrammer/bin:$PATH"
```

### The Acconeer SDK

Download the **XM125 SDK** (`acconeer_xm125_a121-v1_13_0.zip`) and the
**exploration server** binary from `developer.acconeer.com`, and unpack them
**outside this repository**:

```
~/acconeer/xm125/                        the SDK
~/acconeer/xm125_exploration_server/bin/ the stock firmware — your way back
```

> **Its licence forbids publishing the materials and this repo is public.**
> Never copy SDK files, headers or long excerpts into the repo, and keep its
> performance figures out of public reports. Our own numbers are ours.

Keep a copy of the exploration server binary somewhere safe before you flash
anything: it is what the Exploration Tool and Shot Logger talk to, and the
recording setup stops working without it.

---

## 3. Build and flash the XM125

```bash
firmware/xm125_streamer/build.sh              # build
# put the module in its bootloader: hold DFU, tap RESET, release RESET, release DFU
firmware/xm125_streamer/build.sh flash
# tap RESET on its own to run it
```

**Read the existing firmware out first**, so you can always go back:

```bash
STM32_Programmer_CLI -c port=/dev/ttyUSB0 br=115200 \
    -u 0x08000000 0x20000 xm125_backup_$(date +%F).bin
```

Expect ~65 KB of the module's 128 KB flash used. It streams autonomously from
boot — no command, no handshake.

**Check it works** before going further:

```bash
stty -F /dev/ttyUSB0 2000000 raw -echo min 0 time 1
timeout 3 cat /dev/ttyUSB0 | wc -c      # expect >300000 bytes
```

---

## 4. Build and flash the ESP32-S3

```bash
get_idf
CMAKE_BUILD_PARALLEL_LEVEL=4 idf.py -C firmware/radar_infer build
idf.py -C firmware/radar_infer -p /dev/ttyACM0 flash monitor
```

The first build takes a while — `esp-tflite-micro` alone is ~1300 files.

At boot it must print:

```
selftest: PASS: worst 0.000008 dB at bin 1, int8 mismatches 0/64
```

**If that fails, stop.** It means the C feature extraction no longer matches
`training/features.py`, and no accuracy number from that build means anything.

---

## 5. Wire the two boards

```
XE125  J2 pin 11  (UART_TX) ──→  LCD-EV-Board  J5/EXT_IO pin 11  (IO4)
XE125  J2 pin 10  (GND)     ──→  LCD-EV-Board  J5/EXT_IO pin 1, 7, 19 or 20
```

Ground first, then signal. **Do not connect the ESP32's TX** — the link is
one-directional by design, and the XE125's own USB bridge already drives that
net.

Both boards keep their USB cables: the XE125 for power (it needs a 1.8 V rail
the jumper does not carry), the ESP32 for flashing and the console.

---

## 6. Verify, stage by stage

```bash
idf.py -C firmware/radar_infer -p /dev/ttyACM0 monitor
```

| What you should see | What it means |
| --- | --- |
| `GPIO4 probe: ~65% high, ~38000 transitions` | Frames are physically arriving |
| `WIN … p=0.0039 … det=0` in a still room | The detector is quiet when it should be |
| `PEDESTRIAN` on screen when you walk | The whole chain works |

If there are **no `WIN` lines at all**, the firmware tells you which fault it is:
`no data on GPIO4: 0 bytes` means nothing is arriving (wiring), while
`N bytes but no frame magic` means bytes arrive but do not synchronise (baud,
wrong source). Do not guess between those two — the message distinguishes them.

Without the boards you can still test everything but the sensor:

```bash
tools/replay_frames.py --vectors --data-port /dev/ttyUSB0 --log-port /dev/ttyACM0
```

This replays the committed test vectors and must report **`PASS: all 2 windows
match test_vectors.npz exactly`**.

---

## 7. Going back to recording

Flashing the XM125 removes the exploration server, so the Exploration Tool and
Shot Logger stop seeing the sensor. To restore:

```bash
# bootloader mode again: hold DFU, tap RESET, release RESET, release DFU
STM32_Programmer_CLI -c port=/dev/ttyUSB0 br=115200 \
    -w ~/acconeer/xm125_exploration_server/bin/acc_exploration_server_a121.bin 0x08000000 -v
# tap RESET, then:
tools/check_install.py        # must report "sensor found: XE125"
```

---

## 8. Improving the model

The loop, end to end:

```bash
python training/build_dataset.py --root data/<recordings> --out training/out
# train on Colab — see agentContext/binarymodel.md for the exact commands
# download the exports into training/out/final/
python tools/gen_selftest.py                       # refresh the embedded test frame
python -m unittest discover -s tests               # 71 tests, no hardware needed
CMAKE_BUILD_PARALLEL_LEVEL=4 idf.py -C firmware/radar_infer build
```

**The firmware includes `radar_features_config.h` and the model array straight
from `training/out/final/`**, so a retrain cannot leave a stale constant behind.
If you change `training/features.py`, you must rebuild the dataset, retrain,
re-export, regenerate the self-test frame and update `tests/test_features.py` —
they are one contract, not five files.

### Where the accuracy actually stands

- Leave-one-subject-out on the int8 model: 176/179 walks detected, 2 false
  alarms in 31 minutes.
- **All 230 recordings come from one room.** The brief asks for two locations
  and that requirement is unmet. Claim generalisation to a new *person*, not a
  new *place* — a corridor test showed the detector still works but in a
  geometry it has never seen.
- It is effectively a motion detector at low SNR. It fires on doors. It has
  never seen a fan or trolley it could detect.

The highest-value work is **a second location**, recorded with the same frozen
config, not architecture changes.

---

## Traps

Short list. Each of these produced a symptom that looked like something else.

**Wiring — all three look identical (silence):**

- `EX_IO4` is **not** `IO4`. J5 pin 12 is an I/O-expander output driven over
  I2C; pin 11 is the chip pin. Adjacent pins, near-identical labels.
- 2×11 headers number **across the rows**, not along them. Pin 11 is the sixth
  position in the odd row; counting eleven along one row lands on `5V_IO`.
- The XE125 must stay on USB. Unplugging it silently stops the stream.

**Serial and ports:**

- With both boards plugged in, `/dev/ttyUSB0` is ambiguous. Use
  `/dev/serial/by-id/`; `build.sh` already does.
- One process owns a port. The Exploration Tool, `idf.py monitor` and any script
  all compete. `fuser -v /dev/ttyACM0` says who holds it.
- Plug boards **straight into the PC**. Through a shared USB extender the ESP32
  did not enumerate at all — it looks like a dead board and is not one.
- The XM125 has **no hardware flow control** in this firmware, deliberately, so
  PC captures at 2 Mbaud lose ~20 % of every frame. That is expected. Build with
  `FLOW_CONTROL=1` when you need a complete capture on a PC.

**Flashing:**

- The XM125's "DFU" button drives BOOT0; the transport is the STM32 **UART
  bootloader**, not USB DFU. It needs only `dialout`, no udev rules.
- `idf.py` has **no `-j` flag**. Use `CMAKE_BUILD_PARALLEL_LEVEL=4`.

**Display:**

- RGB screen drift is a **memory-bandwidth** symptom, not a drawing bug: the
  panel streams the framebuffer from PSRAM and any stall shifts every following
  pixel. Only `BSP_LCD_RGB_BOUNCE_BUFFER_MODE` **with**
  `ESP32S3_DATA_CACHE_LINE_64B` is stable. Those two belong together.
- Bounce buffering is continuous work on core 0, so the detector runs pinned to
  **core 1**. Sharing one core triggers the task watchdog.
- Do not call `esp_lcd_panel_disp_on_off()` after `bsp_display_new()`; the BSP
  deletes the SPI handle (`auto_del_panel_io`) once the panel switches to RGB.

**Build system:**

- The STM32Cube L4 HAL and CMSIS are **git submodules**. A plain clone looks
  complete while the driver directory is empty, and the build then fails on a
  missing `startup_stm32l431xx.o`, which reads like a broken SDK.
- Use the standalone Arm toolchain, not apt's. The SDK locates its LTO plugin by
  searching above `$GNU_INSTALL_ROOT`; from `/usr/bin` that sweeps all of `/usr`
  and can match the host x86 plugin.
- `idf.py create-project-from-example` unpacks into `<cwd>/<example name>` and
  will merge into an existing directory. Give it `-C <empty parent>`.

**Logic worth not re-deriving:**

- `detection` is an **edge** (true for one window, used for counting events);
  the display needs the **latched state**. Showing the edge makes a working
  detector look indecisive.
- The display's cursor crosses the screen in 19.2 s. That is the 25 Hz **frame**
  rate, not the 5500 Hz **sweep** rate.
- `config/session_config.json` is **locked**. 230 recordings use hash
  `cce7ed611cae`; re-freezing splits the dataset in two.
