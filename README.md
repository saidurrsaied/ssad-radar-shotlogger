# Shot Logger

Auto-labelling for the A121 radar recording campaign. It names, files and logs
every recording, so nobody types a filename during a session.

You keep using the **Acconeer Exploration Tool** exactly as before — it owns the
sensor and shows the live plots. Shot Logger runs alongside it, watches where it
writes recordings, and moves each finished take to its planned name.

The full user guide is **built into the app**: press **F1** or use **Help → User
guide**. This README only covers getting it installed.

---

## Requirements

| | |
| --- | --- |
| Python | 3.9 or newer (3.12 is what this was built and tested on) |
| OS | Linux, or Windows with WSL2 |
| Packages | `acconeer-exptool[app]` — nothing else |
| Hardware | Acconeer XE125 / XM125 on USB, for recording |

**There are no dependencies beyond the Exploration Tool.** Shot Logger imports
only `acconeer.exptool`, `PySide6`, `h5py`, `numpy` and `PyYAML`, and every one
of those arrives with `acconeer-exptool[app]`. If you can already run the
Exploration Tool, you can run this.

Please do not add packages. Teammates should be able to set up with one `pip`
command.

---

## Install on Linux

Tested on Linux Mint 22.3 (Ubuntu 24.04 base). Adjust the package manager for
other distributions.

**1. System packages.** Qt needs one library that most distributions do not ship
by default, and without it the window silently refuses to open:

```bash
sudo apt update
sudo apt install python3-venv libxcb-cursor0
```

**2. Get the project.**

```bash
git clone https://github.com/saidurrsaied/ssad-radar-shotlogger.git
cd ssad-radar-shotlogger
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install "acconeer-exptool[app]"
```

This pulls PySide6 and friends and takes a few minutes.

**3. Serial port access.** Your user must be in the `dialout` group to reach the
sensor:

```bash
sudo usermod -aG dialout $USER
```

**Log out and back in.** Group membership is fixed when a session starts, so a
terminal opened before this command still will not see the change — `getent group
dialout` will list you while `groups` does not. That mismatch is the symptom.

To test without logging out: `sg dialout -c 'python -m shotlogger'`

**4. Check it.**

```bash
tools/check_install.py
```

---

## Install on WSL

WSL2 works, with two extra considerations. I could not test these on the machine
this was built on — it runs native Linux — so treat this section as directions
rather than verified output, and tell the team if anything differs.

**Run both tools inside WSL.** Shot Logger finds recordings through the
Exploration Tool's own data directory, which resolves to a different path on
Windows than in WSL. Running the Exploration Tool as a Windows application and
Shot Logger inside WSL will not work without manually bridging the path. Install
both in WSL.

**1. WSLg for the GUI.** Windows 11, or Windows 10 with a current WSL, includes
WSLg and Qt windows just appear. On an older setup, `wsl --update` from
PowerShell. Without a display the app cannot start; `tools/check_install.py`
reports this.

**2. Follow the Linux steps above** inside your WSL distribution. They apply
unchanged.

**3. Attach the sensor from Windows.** WSL2 does not see USB devices by default.
Install [usbipd-win](https://github.com/dorssel/usbipd-win) on the Windows side,
then in an **administrator** PowerShell:

```powershell
usbipd list
usbipd bind   --busid <BUSID>
usbipd attach --wsl --busid <BUSID>
```

`<BUSID>` is the row for the FTDI / XE125 device. Inside WSL the board then shows
up as `/dev/ttyUSB0` and the `dialout` rule applies as on native Linux.

The attachment does not survive unplugging the board or rebooting Windows, so
`usbipd attach` is a per-session step. `tools/check_install.py` prints these
commands when it detects WSL and finds no sensor.

---

## Verifying the install

```bash
source .venv/bin/activate
tools/check_install.py
```

Healthy output looks like this:

```
Shot Logger install check   (linux)

Python
  [ok  ] Python 3.12.3
  [ok  ] virtual environment  /home/you/SSAD/.venv

Packages
  [ok  ] acconeer-exptool 7.18.2 (RSS 1.13.0)

Graphics
  [ok  ] Qt can open a window

Project files
  [ok  ] frozen config  sha256 cce7ed611cae
  [ok  ] 3 plan CSV(s): locA2_indoor_objects.csv, locA_indoor_3wall.csv, locB_outdoor_path.csv
  [ok  ] Exploration Tool temp dir  /home/you/.local/share/acconeer_exptool/plugoneer/temp

Sensor
  [ok  ] sensor found: XE125 /dev/ttyUSB0
```

Every check prints `ok`, `warn` or `FAIL` with a specific next step. Failures
mean the app will not start. Warnings only matter for an actual recording
session — no sensor plugged in is a warning, not an error. It exits non-zero if
anything failed, so paste the whole output into the group chat if you get stuck.

The `frozen config sha256` must be **the same on every machine**. If yours
differs, you have a different configuration and your recordings cannot be mixed
with everyone else's.

The exact value above is illustrative: it changes if the configuration is
re-frozen after the bench check below. What matters is that everyone's matches,
not that it matches this README.

You can also run the test suite, which needs no hardware and no sensor:

```bash
python -m unittest discover -s tests
QT_QPA_PLATFORM=offscreen python -m unittest tests.test_gui tests.test_manual
```

---

## Before the first recording session

The sensor configuration must be settled once, on hardware, before anyone
records. See **[BENCH_CHECK.md](BENCH_CHECK.md)** — 30 minutes with the sensor,
and it is not reversible afterwards.

```bash
tools/bench_range_check.py
```

## Running it

Start the Exploration Tool first, connect to the sensor, and **load the frozen
config** from `config/session_config.json`.

Then:

```bash
source .venv/bin/activate
python -m shotlogger
```

Set the output folder, the plan CSV and the frozen config, press **Start
watching**, and record in the Exploration Tool as normal. Never use its "Save to
file" — Shot Logger takes the recording instead.

Everything else — the workflow, the table, quality warnings, rejecting a take,
saving a session elsewhere, troubleshooting — is in the built-in guide. Press
**F1**.

---

## What is in the project

```
README.md                    this file -- install and first run
BENCH_CHECK.md               hardware procedure, run once before any recording
measurement_plan.html        the indoor measurement plan, with the reasoning
config/session_config.json   the frozen sensor configuration, shared by everyone
plans/<location>.yaml        plan definition, one per environment
plans/<location>.csv         generated shot list
shotlogger/                  the application
shotlogger/manual.md         the user guide (rendered in-app, press F1)
tools/                       config freeze, shot list generator, install and
                             bench checks, campaign report
tests/                       58 tests, no hardware required
```

Recordings are **not** in the repo and should stay that way -- `*.h5`,
`shotlist_working.csv` and `_logbook.csv` are gitignored. A single 60 s take is
tens of megabytes. Use the app's "Save session to..." to move a finished session
somewhere shared.

Useful commands:

```bash
tools/check_install.py                                  # is this machine ready
tools/bench_range_check.py                              # settle the range, once, on hardware
tools/make_shotlist.py --plan plans/<name>.yaml         # generate a shot list
tools/campaign_report.py data/                          # totals + config check
```

---

## Two rules that matter more than the rest

**Everyone uses the same frozen configuration.** Recordings made with different
sweep or range settings cannot be mixed into one training set. `config/session_config.json`
is generated once for the whole campaign and shared unchanged. Do not edit it by
hand — the loader refuses a file that no longer round-trips through the parser,
which is exactly what a hand edit produces. `tools/campaign_report.py` verifies
at the end that one configuration hash covers every recording.

**Never use "Save to file" in the Exploration Tool.** Shot Logger collects each
recording from the Exploration Tool's temporary folder itself. Saving manually as
well leaves duplicates and triggers an "unclaimed recordings" warning.
