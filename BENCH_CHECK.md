# Bench check — before the outdoor session

**You need: the sensor, a laptop, an open space of at least 25 m, and one person
willing to walk up and down for half a minute. Budget 30 minutes.**

The outdoor measurement plan is written but one number in it is blank: how far the
sensor can usefully see. Everything else is ready and waiting on this.

Do this **before** anyone records anything. Once the first recording exists, the
sensor configuration is locked for the whole campaign, and changing it afterwards
throws away every recording made before the change.

---

## Why this cannot be worked out on paper

Three things only the hardware knows.

**The sensor rejects configurations the software accepts.** `step_length=128`
passes the Python validator and is refused by the sensor's own firmware. The only
way to know which settings are legal is to offer them to the board.

**Maximum sweep rate falls as range grows, and not predictably.** A longer range
needs a lower PRF, and a lower PRF may drag the maximum sweep rate below the
5500 Hz we need. We already hit this once: 6000 Hz was wanted, the sensor
answered 2791 Hz.

**Configured range is not detection range.** The sensor can be *set* to measure
to 24 m. Whether a walking person still returns usable signal at 24 m is a
completely different question, and the only way to answer it is to have someone
walk.

The mock client cannot substitute for any of this. It accepts every configuration
and reports a fictional 100 kHz sweep rate.

---

## Why the range matters so much

Dwell time — how long a target stays in the beam — is set by the range span. It
is the single thing that decides whether a cyclist is recognisable at all.

| Range end | Pedestrian 1.4 m/s | Cyclist 5 m/s | E-scooter 5.5 m/s |
| --- | --- | --- | --- |
| 6.04 m (current) | 3.6 s | **1.0 s** | 0.9 s |
| 12 m | 7.9 s | 2.2 s | 2.0 s |
| 17 m | 11 s | 3.2 s | 2.9 s |

A cyclist's pedal cadence is roughly 1–1.5 Hz, so one pedal cycle takes about
0.7–1 second. At the current 6 m range you capture **one** cycle. The pedalling
pattern is the entire feature that separates a cyclist from anything else, and one
cycle is not a pattern.

The good news: extending range is nearly free. We sum across range to build the
spectrogram, so coarse range bins cost us nothing, and coarser bins mean fewer
bins mean the same data rate over a longer distance.

---

## Before you start

```bash
cd SSAD
source .venv/bin/activate
tools/check_install.py
```

Everything must be `ok` except possibly the sensor line, which this check is
about to fix. If Qt or the packages fail, sort those out first — see `README.md`.

**Confirm nothing has been recorded yet.** Session folders can be anywhere the
operator pointed the app, not just `data/`, so search rather than assuming:

```bash
find . -name "*.h5" -not -path "./.venv/*"
```

If that lists anything, **stop and ask the team** whether those recordings matter.
Re-freezing the configuration after real recordings exist makes them unmixable
with everything recorded afterwards, and no amount of re-running fixes it.

Throwaway takes from testing the app are fine to discard — just be sure that is
what they are before you continue.

---

## Running it

Set the sensor on its tripod at 1.0 m, pointing down the longest clear line you
have — 25 m if possible, so the far candidates can be tested honestly. Nothing
within a couple of metres either side of the beam. Cables behind the sensor.

```bash
tools/bench_range_check.py
```

It runs in five stages and prompts you when it needs a human.

**Stage 1 — sensor identity.** Confirms the link and prints the RSS version.

**Stage 2 — candidate sweep.** Offers five configurations from ~6 m to ~22 m and
records a few seconds on each, reporting what the sensor accepted, the maximum
sweep rate it reported, and whether any frames were dropped. No people needed.
Takes about two minutes.

**Stage 3 — noise floor.** Ten seconds of the empty scene. It will ask you to
clear everyone out of the beam. Do that properly: someone standing at 15 m still
breathes, and that is signal.

**Stage 4 — walk-in.** Thirty seconds while one person walks slowly from beyond
the far edge straight down the centre line to the sensor and back out. Walk the
whole time, repeatedly, so every range bin gets visited. Ordinary clothing,
ordinary pace.

**Stage 5 — recommendation.** Prints a per-range-bin table of signal-to-noise and
the range at which a person fades into the noise, then the exact edits to make.

If you only have ten minutes and no second person, `tools/bench_range_check.py
--quick` runs stages 1 and 2 only. That tells you what the *link* supports but not
what a *person* looks like, so the range will be a guess at the top end. Prefer
the full run.

---

## Reading the result

The script prints a table like this:

```
  range      SNR      visible (threshold 10 dB)
  --------------------------------------------
   1.00 m    34.2 dB  yes  #################
   ...
  11.56 m    12.8 dB  yes  ######
  13.00 m     6.1 dB  no   ###
```

The last row marked `yes` is the honest usable range. **Trust that number over the
configured one.** A config that reaches 22 m is worthless if a person vanishes at
9 m.

If the script warns that cyclist dwell is under about 1.5 s, say so when you report
back. It means the cyclist and e-scooter classes will be hard to separate no matter
how good the model is, and the team should know before spending a day recording
them.

---

## TODO after the run

Work through these in order. Steps 1 and 2 are the ones that matter; the rest is
bookkeeping.

### 1. Apply the recommended configuration

The script prints exactly what to paste. Open
`tools/freeze_session_config.py`, find `build_session_config()`, and change **only
these four lines**:

```python
        start_point=400,
        num_points=<from the script>,
        step_length=<from the script>,
        prf=a121.PRF.<from the script>,
```

**Do not touch** `sweeps_per_frame`, `sweep_rate`, `frame_rate`, `profile` or
`hwaas`. Those set the velocity and time axes of the spectrogram. Range parameters
can differ between what we planned and what we measure; the axes cannot, or
recordings from different sessions stop being comparable.

### 2. Re-freeze

```bash
tools/freeze_session_config.py --force
```

`--force` is needed because a config already exists. It is safe **only** because
nothing has been recorded. Note the new `sha256` it prints — every teammate's
machine must show that same hash, and `tools/campaign_report.py` checks it at the
end of the campaign.

### 3. Fill the distance bands into the plan

Open `plans/locB_outdoor_path.yaml`. Near the top there is a block with three
blanks:

```
#     far  = ___ m
#     mid  = ___ m
#     near = ___ m
```

Fill in the three numbers the script printed. These are labels the filenames use
(`ped_approach_far_normal_p1_take1.h5`), so nothing else in the plan changes.

While you are there, update the `description:` field to name the actual site.

### 4. Regenerate the shot list

```bash
tools/make_shotlist.py --plan plans/locB_outdoor_path.yaml
```

Expect 194 recordings across 9 classes and the message `all 194 filenames unique`.

### 5. Check the indoor plan still makes sense

`plans/locA_indoor_3wall.yaml` uses metric markers (`2m`, `4m`, `6m`) because the
indoor room is only that big. Those stay as they are — a longer configured range
indoors just means the extra bins see the far wall, which is static clutter and
gets removed. **No change needed** unless the room turns out to be smaller than the
markers assume.

### 6. Confirm nothing broke

```bash
tools/check_install.py
python -m unittest discover -s tests
QT_QPA_PLATFORM=offscreen python -m unittest tests.test_gui tests.test_manual
```

All should pass. `check_install.py` should now report the new config hash and the
sensor.

### 7. Report back to the team

Post these four things:

- the Stage 2 table
- the measured usable range from Stage 4
- the new config `sha256`
- the three band values you wrote into the YAML

The hash is the important one. Everybody records against the same configuration or
the dataset cannot be combined, and the hash is how we prove it.

---

## If something goes wrong

**No candidate passes Stage 2.** Report the table and stop. Do not hand-pick a
configuration to make it work — a config that drops frames produces short files
with no error, and you will not notice until training.

**A person is only visible to 6 m or so.** That is a real result, not a failure.
Report it. It means the outdoor plan's cyclist and e-scooter classes need
rethinking rather than more recording.

**Permission denied on `/dev/ttyUSB0`.** You are not in the `dialout` group, or you
are but your login session predates the change. See `README.md`; the short version
is `sudo usermod -aG dialout $USER` then log out and back in.

**The sensor is not found at all.** On WSL you must attach it from Windows first
with `usbipd attach --wsl --busid <BUSID>`. `tools/check_install.py` prints the
exact commands.
