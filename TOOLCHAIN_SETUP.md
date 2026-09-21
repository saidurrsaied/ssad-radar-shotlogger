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

**ST account** — for STM32CubeProgrammer, which is the only way to flash the
XM125 over USB DFU.

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
not to the newest release.** Check its README before cloning. Exported and
generated ESP projects have historically lagged behind new IDF releases, and
discovering a version mismatch at link time costs a day. `release/v5.4` above is
an example, not a recommendation — confirm it.

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

---

## 4. XM125 firmware — check before you flash

The XM125 ships with detector firmware. Streaming Sparse IQ needs the
**exploration server** firmware instead, flashed over USB DFU with
STM32CubeProgrammer using the DFU and RESET buttons on the board.

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

## 5. Edge Impulse CLI — only if that track is still live

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
project. The Acconeer SDK targets the XM125's own Cortex-M33; the ESP32 side of
that link is code we write. Confirm what the SDK actually provides for a
non-PC host as soon as the download is available, and get two boards talking
before committing to a schedule around it. The 24 MHz logic analyzer in the lab
kit exists for this exact task.

---

## The checklist

Accounts, in advance:

- [ ] Acconeer developer account registered, A121 SDK downloaded
- [ ] ST account registered, STM32CubeProgrammer installed

PC, training side:

- [ ] `.venv-ml` created, separate from `.venv`
- [ ] `tensorflow scikit-learn jupyterlab matplotlib h5py numpy scipy` installed
- [ ] `.venv-ml/` added to `.gitignore`
- [ ] `python -c "import tensorflow as tf; print(tf.__version__)"` prints a version
- [ ] Framework of the existing trained models confirmed as Keras — or re-expressed in Keras

PC, embedded side:

- [ ] apt prerequisites installed
- [ ] `esp-tflite-micro`'s supported ESP-IDF version checked
- [ ] ESP-IDF cloned at that version and `install.sh esp32s3` run
- [ ] `get_idf` alias in `~/.bashrc`
- [ ] user in `dialout`, logged out and back in
- [ ] ESP-IDF `hello_world` builds, flashes and runs on the S3
- [ ] `esp-tflite-micro` added to a project and its `hello_world` runs

Hardware:

- [ ] XM125 firmware state known and written down
- [ ] `tools/check_install.py` still reports the sensor after any firmware work

Deliberately not installed:

- [ ] STM32CubeIDE / X-CUBE-AI — fallback only
- [ ] Edge Impulse CLI — only if that track is still running
