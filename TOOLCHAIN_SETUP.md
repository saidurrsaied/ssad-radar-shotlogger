# Toolchain setup — from recorded data to the embedded demo

**You need: a Linux machine, two free vendor accounts, and about two hours of
downloading. No sensor and no ESP32 board required for most of it.**

`README.md` covers the recording half of the project: the Exploration Tool, the
Shot Logger, and the frozen configuration. This covers everything after that —
training a model on the recordings, quantising it, and running it on the
ESP32-S3 with the display.

Nothing here is a free choice. The target platform, the runtime and the
conversion path are all fixed by the project brief (sections 2–4 of
`radar_classification_getting_started.pdf`), and the tools below are the ones
those choices require.

---

## What runs where

The pipeline is eight stages. Five run on a PC, three run on the target.

| Stage | Where | Tool |
| --- | --- | --- |
| Sensing | XE125 | A121 sensor, Sparse IQ service |
| Streaming | XE125 → PC | exploration server firmware + Exploration Tool |
| Recording | PC | Shot Logger — see `README.md` |
| Feature extraction | PC | our own Python: FFT across sweeps → spectrogram |
| Training | PC | Keras |
| Quantisation | PC | TensorFlow Lite converter, float32 → int8 |
| **Inference** | **ESP32-S3** | **our C feature extraction + TFLite Micro** |
| Display | ESP32-S3 | LCD-EV-Board: class, confidence, scrolling spectrogram |

The FFT and spectrogram code on the target is our own signal processing work.
The ML runtime does not provide it. That matters for how long this takes.

---

## Start the two accounts today

Both are free, neither is instant, and both block work that cannot begin without
them. Do this before any of the installs below.

**Acconeer developer account** — `developer.acconeer.com`. Gives access to the
A121 SDK: the C libraries for the XM125 and the exploration server firmware
binary.

**ST account** — for STM32CubeProgrammer, which is how the XM125 gets flashed.
Despite the board's button being labelled "DFU", the transport is the STM32's
**UART bootloader** over the CP2105, not USB DFU: the XM125's own USB is not
exposed on the XE125. The button drives BOOT0; CubeProgrammer then connects to
`/dev/ttyUSB0`.

---

## 1. Training environment

A **separate** virtual environment from the recording one.

```bash
cd SSAD
python3 -m venv .venv-ml
source .venv-ml/bin/activate
pip install --upgrade pip
pip install tensorflow scikit-learn jupyterlab matplotlib h5py numpy scipy
```

Then add `.venv-ml/` to `.gitignore` — it currently ignores `.venv/` by exact
name, so a second environment would otherwise be offered up for commit.

**Why separate.** `README.md` promises teammates that recording needs one `pip`
command and nothing else. TensorFlow is several hundred megabytes and is useless
to anyone who only records. Keeping it out of `.venv` keeps that promise true.

**Why TensorFlow and not PyTorch.** The target runtime is TensorFlow Lite Micro,
so the shipping path is Keras → TFLite converter → int8 → `esp-tflite-micro`.
PyTorch models do not convert cleanly onto that path; PyTorch → ONNX → TF →
TFLite is fragile and usually ends in rewriting the architecture in Keras
anyway. If a model has already been trained in PyTorch, budget an afternoon to
re-express it in Keras and retrain on the same spectrograms. For the "few tens
of kilobytes" CNN this project needs, that is a short job — but do it before
building the embedded side, not after.

Verify:

```bash
python -c "import tensorflow as tf; print(tf.__version__)"
```

---

## 2. ESP-IDF — the ESP32-S3 toolchain

System packages first. On Linux Mint 22.3 / Ubuntu 24.04, `ninja`, `ccache`,
`dfu-util`, `flex`, `bison` and `gperf` are the ones typically missing:

```bash
sudo apt update
sudo apt install git wget flex bison gperf python3 python3-venv python3-pip \
    cmake ninja-build ccache libffi-dev libssl-dev dfu-util libusb-1.0-0
```

Then the framework itself:

```bash
mkdir -p ~/esp && cd ~/esp
git clone -b release/v5.4 --recursive https://github.com/espressif/esp-idf.git
cd ~/esp/esp-idf
./install.sh esp32s3
. ./export.sh
```

**Pin the branch to the ESP-IDF version `esp-tflite-micro` currently supports,
not to the newest release.** Exported and generated ESP projects have
historically lagged behind new IDF releases, and discovering a version mismatch
at link time costs a day.

`release/v5.4` above is confirmed good, not a guess: `esp-tflite-micro` 1.4.1
lists `release/v5.1` through `release/v6.0` as supported and covers v5.4 in CI
(checked 2026-09-29; it also needs `esp-nn` >= 1.3.0, which comes with it).
Installed here: **v5.4.4-1375-g47fded9f196** on `release/v5.4`. Re-check the
component's README before moving either version.

`export.sh` only affects the current shell. Add a shortcut to `~/.bashrc` rather
than retyping it:

```bash
alias get_idf='. $HOME/esp/esp-idf/export.sh'
```

**Verify before going near radar code.** Build, flash and monitor the plain
`hello_world` example on the S3 first. If that does not work, nothing built on
top of it will:

```bash
get_idf
cp -r $IDF_PATH/examples/get-started/hello_world ~/esp/hello_world
cd ~/esp/hello_world
idf.py set-target esp32s3
idf.py build
idf.py -p /dev/ttyACM0 flash monitor      # check the actual port first
```

**Verified on the S3 here, 2026-09-29:** builds (190 KB binary, 82 % of the app
partition free), flashes at ~1.1 Mbit/s with all three images hash-verified, and
boots to `Hello world!` at 160 MHz reporting **388 KB free internal heap and no
PSRAM** — ample for a 25 KB model and a ~30 KB tensor arena.

One warning from that boot log to carry into the real project: the default
sdkconfig declares 2 MB of flash while this chip has 16 MB (`Detected
size(16384k) larger than the size in the binary image header(2048k)`). Harmless
for `hello_world`; set `CONFIG_ESPTOOLPY_FLASHSIZE_16MB` rather than stranding
14 MB.

The board's native USB appears as **`/dev/ttyACM0`** — USB ID `303a:1001`,
"USB JTAG/serial debug unit" — not `ttyUSB0`, which would be a separate UART
bridge chip this board does not have. Flashing and the console share that one
port because IDF mirrors the console to USB Serial/JTAG by default; the primary
console stays on UART0 (GPIO43/44), so **those pins can be freed for the radar
link** by making USB Serial/JTAG the only console.

**Plug the board straight into the PC, never into a hub or extender.** Through a
USB extender shared with other devices it did not enumerate at all — `device
descriptor read/64, error -32`, then `unable to enumerate USB device`, and no
`/dev/ttyACM*` to flash. Direct into a PC port it came up first try. The symptom
looks exactly like a dead board and is not one; check `dmesg -T | grep -i usb`
before suspecting the hardware.

Serial access needs the same `dialout` group membership as the sensor, and the
same log-out-and-back-in caveat applies — see `README.md`.

---

## 3. esp-tflite-micro

Not a system install. It is an ESP-IDF component, added per project:

```bash
idf.py add-dependency "espressif/esp-tflite-micro"
```

This brings the TensorFlow Lite Micro runtime together with the ESP-NN kernels,
which use the S3's vector instructions and are the difference between inference
that fits the frame budget and inference that does not.

Its `hello_world` example is the gentlest demonstration of how a model actually
executes on a microcontroller. Run it before integrating our own.

**Verified 2026-09-29.** Resolved `esp-tflite-micro` 1.4.1 with `esp-nn` 1.4.1
against ESP-IDF v5.4.4, built clean (215 KB binary, 89 % of the app partition
free) and ran on the S3, printing a sine reproduced to ~0.1 by an int8 model:

```
x_value: 1.570796, y_value: 1.042060     (sin = 1.0)
x_value: 3.141593, y_value: 0.008472     (sin = 0.0)
x_value: 4.712389, y_value: -1.109837    (sin = -1.0)
```

So `invoke()`, the ESP-NN kernels and the arena all work on this chip. Our model
is 25 KB of weights against this example's few hundred bytes, but the same four
ops (`CONV_2D`, `MEAN`, `FULLY_CONNECTED`, `LOGISTIC`) resolve through the same
runtime.

**`create-project-from-example` unpacks into `<cwd>/<example name>` and does not
refuse a directory that already exists.** Run from `~/esp`, where the IDF
`hello_world` lives, it merges the two and leaves a project with two `app_main`
definitions. Give it `-C <some empty parent>` — this one is at
`~/esp/tflm/hello_world`.

---

## 4. XM125 firmware — check before you flash

The XM125 ships with detector firmware. Streaming Sparse IQ needs the
**exploration server** firmware instead, flashed with STM32CubeProgrammer over
the **UART bootloader** — hold **DFU**, tap **RESET**, release RESET, release
DFU, which leaves the STM32 in its built-in bootloader on `/dev/ttyUSB0`.
(The button is named DFU but drives BOOT0; USB DFU is not involved, because the
XM125's USB never reaches the XE125's connector. Software user guide §3.2.)

**If the recording campaign is running, this has already been done** — the
Exploration Tool cannot talk to the board otherwise. Do not reflash a working
recording setup just because this document lists the step. Check first:

```bash
source .venv/bin/activate
tools/check_install.py        # "sensor found" means the exploration server is live
```

Flashing becomes relevant again only if the ESP32-S3 link turns out to need
different firmware from the PC link. Decide that from the SDK contents, not in
advance. Whatever you flash, write down what was on the board first — going back
is the same procedure in reverse, but only if you know what to go back to.

---

## 5. The XM125 side — ARM toolchain, Cube package, programmer

Needed only to build and flash firmware for the radar module itself. The
ESP32-S3 half does not depend on any of it.

**Use the exact toolchain the SDK was built with.** `~/acconeer/xm125/doc/
BUILDINFO.txt` and the XM125 Software User Guide §4.2 both name **Arm GNU
Toolchain 13.3.Rel1**. Install it as a self-contained tree, not from apt:

```bash
mkdir -p ~/opt && cd ~/opt
curl -LO https://developer.arm.com/-/media/Files/downloads/gnu/13.3.rel1/binrel/\
arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi.tar.xz
tar -xJf arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi.tar.xz
```

**Why not `apt install gcc-arm-none-eabi`.** Ubuntu ships 13.2.rel1, and the
SDK's archives are built with fat LTO objects. Its archiver flags locate the
LTO plugin by searching one directory above `$GNU_INSTALL_ROOT` — which, for an
apt install, means searching all of `/usr`, where the host x86 plugin can match
first. With a standalone toolchain tree the search returns exactly one file.
Check it yourself:

```bash
find "$GNU_INSTALL_ROOT/.." -name liblto_plugin.so     # expect exactly one hit
```

**The STM32Cube L4 package** supplies ST's HAL, CMSIS and the startup file; the
Acconeer SDK deliberately ships without them. The guide names **v1.18.1**.
GitHub is the better source — tagged, public, no login:

```bash
cd ~/opt
git clone --depth 1 --branch v1.18.1 \
    https://github.com/STMicroelectronics/STM32CubeL4.git STM32Cube_FW_L4_V1.18.1
cd STM32Cube_FW_L4_V1.18.1
git submodule update --init --depth 1 \
    Drivers/STM32L4xx_HAL_Driver Drivers/CMSIS/Device/ST/STM32L4xx
```

**That submodule step is not optional and is easy to miss.** The HAL and CMSIS
device files live in separate repositories, so a plain clone looks complete
while `Drivers/STM32L4xx_HAL_Driver/Src` is empty, and the build fails with
`No rule to make target 'out/obj/startup_stm32l431xx.o'`. The three BSP
submodules are not needed.

**STM32CubeProgrammer** comes from st.com behind a login and the SLA0048
licence. On the product page the per-OS rows in the page source are dead markup;
use the **Get Software** button, then the OS dropdown — pick **Generic Linux**,
not Linux Arm. The installer is a Java GUI wizard with a bundled JRE:

```bash
unzip -q SetupSTM32CubeProgrammer_linux_64.zip -d ~/opt/cubeprog-installer
cd ~/opt/cubeprog-installer
sudo -E env DISPLAY="$DISPLAY" XAUTHORITY="$XAUTHORITY" \
    ./SetupSTM32CubeProgrammer-2.23.0.linux     # install to /opt/st/stm32cubeprogrammer
sudo cp /opt/st/stm32cubeprogrammer/Drivers/rules/*.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Those udev rules cover **ST-LINK only**, which matters for SWD work on J6/J2 but
not for flashing the XM125 — that path is the UART bootloader and needs only
`dialout`.

Put both roots in `~/.bashrc`; the SDK reads them by name:

```bash
export GNU_INSTALL_ROOT="$HOME/opt/arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi/bin"
export STM32CUBE_FW_L4_ROOT="$HOME/opt/STM32Cube_FW_L4_V1.18.1"
export PATH="$GNU_INSTALL_ROOT:/opt/st/stm32cubeprogrammer/bin:$PATH"
```

**Verified 2026-09-30:**

```bash
$ cd ~/acconeer/xm125 && make -j 4 example_service
Linking out/example_service.elf
   text 65,616   data 152   bss 6,824
$ STM32_Programmer_CLI --version
STM32CubeProgrammer version: 2.23.0
```

A Sparse IQ application is **65.8 KB of the module's 128 KB flash and ~7 KB of
its 64 KB RAM** — half the flash free for streaming logic, where the exploration
server leaves only 17 %. That is what makes custom XM125 firmware practical; see
`agentContext/embeddedlink.md`.

**Why not STM32CubeIDE or CubeMX.** CubeProgrammer flashes; CubeIDE is an
Eclipse IDE with *its own* bundled GCC; CubeMX generates init code. The SDK
already contains the CubeMX output (`xm125.ioc` plus the generated `main.c` and
HAL MSP), and its makefile build is the guide's own §4.2 path. Adding CubeIDE
means a second compiler that does not match the SDK's, and §4.3.1 warns you must
then exclude most SDK sources or the link fails with multiple definitions. Worth
installing only for source-level SWD debugging, or for unrelated STM32 work.

---

## 6. Edge Impulse CLI — only if that track is still live

```bash
npm install -g edge-impulse-cli
```

Node is a prerequisite and is usually already present. The project brief's
two-track plan runs Edge Impulse in parallel with the manual pipeline only until
a fixed decision date, after which exactly one of them continues. **If the
manual pipeline is the one we committed to, skip this entirely** — running both
to the end is the one thing the brief rules out.

---

## Not now: the Nucleo fallback

STM32CubeIDE and X-CUBE-AI are the documented fallback if the ESP32-S3 path
fails, using the STM32 Nucleo-L476RG that is otherwise held in reserve. Do not
install them during setup. A second half-learned toolchain is a cost with no
benefit until the first one has actually failed.

---

## Where this leads

With the checklist done, the firmware lives in `firmware/radar_infer/` and is
built with the same toolchain:

```bash
. $HOME/esp/esp-idf/export.sh
CMAKE_BUILD_PARALLEL_LEVEL=4 idf.py -C firmware/radar_infer build   # idf.py has no -j
idf.py -C firmware/radar_infer -p /dev/ttyACM0 flash monitor
```

How it gets its frames, and why the link is shaped the way it is, is in
`agentContext/embeddedlink.md`.

## Build the thin pipeline before perfecting any stage

The brief's one process rule, and the reason integration eats lab projects:
get a trivial model running end to end — sensor → features → inference →
display — with ugly plots and poor accuracy, **before** improving any single
stage. A dummy end-to-end system early is the insurance. Leave it to the end and
the integration problems arrive with no time left to solve them.

---

## Two things that will cost weeks, and neither is an install

**Feature-extraction parity.** The C spectrogram on the ESP32 must be
numerically identical to the Python spectrogram the model was trained on. Small
differences — window function, scaling, FFT length, clutter removal order — do
not crash anything. They quietly cost accuracy, and the symptom is a model that
scored well in the notebook and performs badly on the board, which is very hard
to diagnose from that end.

Freeze the defence early: take a handful of recorded frames, save the Python
spectrograms next to them as fixtures, and assert the C implementation
reproduces them within tolerance. `tests/` already does exactly this for the
frozen configuration — follow that pattern.

**The XE125 → ESP32-S3 serial link.** This is the integration risk of the
project. The Acconeer SDK targets the XM125's own Cortex-M4 (STM32L431CB,
128 KB flash / 64 KB RAM); the ESP32 side of that link is code we write. Confirm what the SDK actually provides for a
non-PC host as soon as the download is available, and get two boards talking
before committing to a schedule around it. The 24 MHz logic analyzer in the lab
kit exists for this exact task.

---

## The checklist

Accounts, in advance:

- [x] Acconeer developer account registered, A121 SDK downloaded — 2026-09-29,
      unpacked at `~/acconeer/` **outside the repo**: the licence forbids publishing it
- [x] ST account registered, STM32CubeProgrammer installed — 2.23.0, 2026-09-30

PC, training side — **superseded: the model was trained on Google Colab** (the
`colab` CLI, see `agentContext/binarymodel.md`), so no local TensorFlow was ever
needed. The first four are only for someone who wants to train locally:

- [ ] `.venv-ml` created, separate from `.venv`
- [ ] `tensorflow scikit-learn jupyterlab matplotlib h5py numpy scipy` installed
- [x] `.venv-ml/` added to `.gitignore` — done anyway, so the path stays open
- [ ] `python -c "import tensorflow as tf; print(tf.__version__)"` prints a version
- [x] Framework confirmed as Keras — `training/train_binary.py` is Keras end to end

PC, embedded side (all verified 2026-09-29):

- [x] apt prerequisites installed
- [x] `esp-tflite-micro`'s supported ESP-IDF version checked — v5.1 to v6.0
- [x] ESP-IDF cloned at that version and `install.sh esp32s3` run — v5.4.4
- [x] `get_idf` alias in `~/.bashrc`
- [x] user in `dialout`, logged out and back in
- [x] ESP-IDF `hello_world` builds, flashes and runs on the S3 — `/dev/ttyACM0`
- [x] `esp-tflite-micro` added to a project and its `hello_world` runs — 1.4.1

PC, XM125 side (verified 2026-09-30):

- [x] Arm GNU Toolchain 13.3.Rel1 installed standalone — matches the SDK's BUILDINFO
- [x] STM32Cube FW L4 v1.18.1 cloned **with the two submodules initialised**
- [x] `GNU_INSTALL_ROOT`, `STM32CUBE_FW_L4_ROOT` and the programmer on `PATH`
- [x] `make -j 4 example_service` links — 65.8 KB of 128 KB flash
- [x] `STM32_Programmer_CLI --version` runs — 2.23.0
- [ ] XM125 streaming firmware written and flashed

Hardware:

- [x] XM125 firmware state known and written down — read out 2026-09-30 to
      `~/acconeer/xm125_backup/`, and **verified byte-identical to Acconeer's
      stock `acc_exploration_server_a121.bin`** (the rest of the 128 KB is
      erased). The board carries no local modifications
- [ ] `tools/check_install.py` still reports the sensor after any firmware work

Deliberately not installed:

- [ ] STM32CubeIDE / X-CUBE-AI — fallback only
- [ ] Edge Impulse CLI — only if that track is still running
