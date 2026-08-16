# Shot Logger — user guide

Shot Logger names, files and logs every radar recording automatically, so nobody
types a filename during a session.

You keep using the Acconeer Exploration Tool exactly as before. It owns the
sensor and shows the live plots. Shot Logger runs alongside it, watches where it
writes recordings, and moves each finished take to its planned name.

---

## Why it exists

The campaign needs several hundred recordings, each named like
`ped_approach_6m_normal_p1_take2.h5`, and every one made with an identical sensor
configuration. Section 6 of the getting-started guide is blunt about the second
point: recordings made with different sweep or range settings cannot be mixed
into one training set.

Typing those names by hand while also watching the subject is how a campaign ends
up with a mistyped class, a repeated take number that overwrote a good file, or a
configuration that drifted with nobody noticing until training day.

Shot Logger makes the filename a consequence of the plan rather than something a
person types, and checks every single recording against the frozen configuration
before filing it.

---

## Before your first session

You need three things ready.

**The Exploration Tool**, installed and able to reach the sensor. If you can run
it and see live data, you are set. Shot Logger adds no dependencies of its own.

**The frozen configuration**, `config/session_config.json`. It is created once
for the whole campaign with `tools/freeze_session_config.py` and then never
touched. You must load this same configuration into the Exploration Tool before
recording — Shot Logger checks every take against it and refuses anything that
does not match.

**A plan CSV**, such as `plans/locA_indoor_3wall.csv`. Generated from a YAML plan
definition with `tools/make_shotlist.py`. It lists every recording to make, in
the order to make them.

---

## Setting up a session

Start the Exploration Tool first. Connect to the sensor, load the frozen config,
and select Sparse IQ.

Then start Shot Logger:

    python -m shotlogger

Fill in the three fields at the top. Each has a lamp that turns green when the
value is usable.

**Output folder** — where recordings will be filed. Pick a fresh folder per
location. It can be anywhere you have space.

**Plan CSV** — the shot list for this location.

**Frozen config** — `config/session_config.json`.

When all three lamps are green, **Start watching** becomes available. Press it.

The first time you use a given output folder, the plan CSV is copied there as
`shotlist_working.csv`. From then on, progress is written to that copy and the
original plan is never modified. Regenerating a plan therefore cannot wipe
recorded progress.

---

## Recording

The card in the middle of the window shows the next shot in large type: class,
geometry, distance, speed, subject, take number, how long to record, and any note
attached to that shot. It is meant to be readable from across the room.

For each take:

1. Press **Start** in the Exploration Tool.
2. The subject does the pass.
3. Press **Stop** in the Exploration Tool.

That is all. Within a second or two the take is verified, named, filed under
`<output folder>/<class>/`, written to the logbook, and the card moves to the
next shot.

**Never use the Exploration Tool's "Save to file".** Shot Logger takes the
recording from the Exploration Tool's own temporary folder. If you also save it
manually you end up with two copies and a warning about unclaimed files.

### Duration is checked, not enforced

Start and Stop belong to the Exploration Tool, so Shot Logger cannot stop a
recording at the planned length. Instead it compares the two afterwards and flags
any take more than 25% off plan. Watch the card for the target duration and keep
roughly to it.

---

## Reading the table

Every row of the plan is listed, with completed rows dimmed and the next row
highlighted in bold.

| Column | Meaning |
| --- | --- |
| # | Sequence number, the order to record in |
| Class | whatever the loaded plan defines — see below |
| Geometry | Trajectory, or for background the disturbance source |
| Dist | Distance marker on the floor |
| Speed | `slow`, `normal`, `fast`, or `na` for background |
| Subj | Subject, `p0` for background |
| Take | Take number within that combination |
| Filename | The name the recording will be filed under |
| Plan s | How long the shot should run |
| Actual s | How long it actually ran |
| Status | `pending` or `recorded` |
| QC | Quality notes, or `ok` |

Hover any row to see its note. Use **Show** to filter by class, or tick
**Remaining only** to hide finished rows.

### Classes you will meet

Classes come from the plan, so they differ between locations. The indoor plan
uses two; the outdoor plan uses nine.

| | |
| --- | --- |
| `bg` | background — no target in the beam |
| `ped` | one person walking |
| `ped2` `ped3` | two or three people together |
| `cane` | one person using a walking cane |
| `canegrp` | cane user with someone alongside |
| `cyc` | bicycle |
| `sct` | e-scooter |
| `cross` | sideways pass, `lr` left-to-right or `rl` right-to-left |

Two of these behave in ways worth expecting rather than being surprised by.

The sensor has one receive antenna and no angle resolution, so it cannot separate
people standing or walking side by side. `ped2` and `ped3` are not "pedestrian,
twice" — they are one superposed signal that looks quite different. That is why
they are separate classes and must not be filed as `ped`.

`cross` passes will look nearly empty on the live plot. That is the physics, not a
fault: the sensor measures radial velocity, and a target moving across the beam
has almost none. They are recorded so the model learns that someone walking past
is not someone approaching. Record them as planned even though they look like
nothing.

### Quality notes

The QC column turns amber for something worth a look and red for something that
probably needs re-recording.

**delayed** — frames arrived late, meaning the link could not keep up. A few are
tolerable; more than 20% is red. If this appears repeatedly, something is wrong
with the configuration or the machine, and you should stop and investigate rather
than record a hundred compromised files.

**sat** — the receiver saturated. Usually a target too close to the sensor, or a
wall reflecting straight back into it.

**duration** — the take was more than 25% off its planned length.

---

## Redoing a take

If a pass went wrong — the subject stumbled, someone walked through the beam,
the spectrogram looks empty — type a short reason in the note box and press
**Reject & delete**.

The recording is deleted and the shot returns to pending, so it comes round
again. The rejection and your reason stay in the logbook permanently, which is
the only place they survive, since the file itself is gone.

To redo a shot from earlier in the session, select its row and press **Record
selected row next**. A shot that is already recorded must be rejected first.

---

## Saving a session elsewhere

**Save session to…** hands the whole session — every `.h5`, the updated
`shotlist_working.csv`, and the logbook — to another folder in one step. Use it
for a shared drive, a USB stick, or the machine that will do the training.

**Copy** leaves the session where it is. This is the right choice for an interim
backup partway through a campaign.

**Move** relocates it, and the window carries on against the new folder, so the
session is never left split across two places.

Files are copied first and only removed afterwards, so an interruption cannot
lose data. Watching stops for the duration so a take cannot arrive mid-transfer.

The export refuses to start — before transferring anything — if the destination
already holds a session, if any file would be overwritten, if the two folders are
nested, or if there is not enough space. A half-merged session is worse than no
export, so it is all or nothing.

---

## What Shot Logger refuses to do

These are deliberate. Each one exists because the alternative corrupts the
dataset quietly.

**File a take recorded with the wrong configuration.** Watching stops, the file
is left untouched where it is, and you are told. Load the frozen config in the
Exploration Tool and record the shot again. This is the single most important
check in the tool.

**Overwrite an existing recording.** Reject the old take first if you mean to
replace it.

**File a take into the wrong location's folder.**

**Claim a recording that is still in progress.** It waits until both the file
size and the frame count stop changing. Watching the size alone is not enough:
the writer flushes in bursts, so a recording partway through can sit at a
constant size and look finished.

---

## Files a session produces

    <output folder>/
      shotlist_working.csv    the plan plus your progress
      _logbook.csv            one row per attempt, including rejected and failed
      bg/  ped/  cyc/  ...    recordings, one folder per class in the plan

`shotlist_working.csv` is the live progress file. The original plan CSV is never
modified.

`_logbook.csv` is append-only and records every attempt, including the ones that
were rejected or failed. Each row carries the configuration hash, which is what
proves at the end of the campaign that one configuration covered everything.

If a recording is deleted from disk, the matching row resets to pending the next
time the session is loaded. The files on disk are always the source of truth, not
the CSV.

---

## Checking the whole campaign

    tools/campaign_report.py data/

Totals per class and per subject across every location, measured against the
guide's targets, plus the check that matters most: that a single configuration
hash covers the entire campaign. It exits non-zero if two turn up, because that
means the dataset is silently split in two.

---

## Adding a new environment

Each environment gets a YAML plan under `plans/`. Copy an existing one, edit the
blocks, and generate its CSV:

    tools/make_shotlist.py --plan plans/locB_outdoor_path.yaml

The YAML holds the location, the subjects and a list of blocks per class. No
Python involved. Filenames repeat between environments by design — they are kept
apart by the per-location output folder.

One thing not to trim from the outdoor plans: they record pedestrian and
background too, not only cyclist. If every cyclist recording were outdoors and
every pedestrian recording indoors, "outdoors" would predict "cyclist" perfectly,
and the network would learn the environment instead of the gait — scoring well in
validation having understood nothing.

---

## Troubleshooting

**The Start watching button stays greyed out.** One of the three lamps is red.
Hover it to see why.

**"The Exploration Tool's recording directory does not exist".** That folder is
created the first time the Exploration Tool records anything. Start it, record
once, then start watching. If the Exploration Tool was launched with its own
`--data-dir`, pass the matching path to Shot Logger with `--temp-dir`.

**"Unclaimed recordings already present".** Recordings were sitting in the
Exploration Tool's temp folder before watching began, and will be ignored.
Usually it means someone used "Save to file" as well, or a previous session was
never filed. Clear them out if they are not needed.

**Nothing happens when I press Stop.** Check the path shown at the bottom of the
Setup box actually matches where your Exploration Tool writes. Give it two
seconds — settling needs two polls.

**"This recording used a different sensor configuration".** The Exploration Tool
is not using the frozen config. Load `config/session_config.json` in it and
record the shot again. Do not work around this.

**Permission denied on /dev/ttyUSB0.** Your account needs to be in the `dialout`
group, and the change only takes effect after logging out and back in. If
`getent group dialout` already lists you but the error persists, your session
predates the change.

**The window will not open at all on Linux.** Install `libxcb-cursor0`. Qt cannot
load its platform plugin without it.

**Lots of delayed frames.** The link cannot keep up. Do not adjust the frozen
config to fix it — that splits the dataset. Stop, work out what changed, and if
the configuration genuinely needs revising, the campaign restarts from the freeze.
