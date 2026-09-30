#!/usr/bin/env python3
"""Play recorded Sparse IQ frames into the ESP32-S3 over a serial port.

The PC stands in for the XM125: it sends exactly the bytes the module will
send, at the same 25 fps, so the firmware can be written and proved before any
XM125 firmware exists. See agentContext/embeddedlink.md.

Two cables, two ports:

    --data-port   the board's UART Type-C  (bridge -> GPIO43/44)   frames go in
    --log-port    the board's USB Type-C   (native, /dev/ttyACM0)  lines come out

Verify the whole chain against the committed test vectors:

    tools/replay_frames.py --vectors --data-port /dev/ttyUSB0 --log-port /dev/ttyACM0

Or push a real recording through it:

    tools/replay_frames.py --recording data/.../ped_approach_6m_normal_p1_take1.h5 \\
        --data-port /dev/ttyUSB0 --log-port /dev/ttyACM0
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "training"))

MAGIC = bytes((0xA1, 0x21, 0xF0, 0x0D))
DEFAULT_VECTORS = REPO / "training" / "out" / "final" / "test_vectors.npz"

WIN_RE = re.compile(
    r"WIN (\d+) frame=(\d+) q=(-?\d+) p=([\d.]+) pos=(\d) run=(\d+) det=(\d) "
    r"dsp_us=(\d+) inf_us=(\d+)"
)


class LogReader(threading.Thread):
    """Collects the firmware's output on the log port while frames are sent."""

    def __init__(self, port, baud=115200, echo=True):
        super().__init__(daemon=True)
        import serial

        self.serial = serial.Serial(port, baud, timeout=0.2)
        self.echo = echo
        self.lines: list[str] = []
        self.windows: dict[int, dict] = {}   # frame index -> parsed fields
        self.stop_flag = threading.Event()

    def run(self):
        buf = b""
        while not self.stop_flag.is_set():
            buf += self.serial.read(4096)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                self.lines.append(line)
                if self.echo:
                    print(f"    | {line}", flush=True)
                m = WIN_RE.search(line)
                if m:
                    self.windows[int(m.group(2))] = {
                        "window": int(m.group(1)),
                        "frame": int(m.group(2)),
                        "q": int(m.group(3)),
                        "p": float(m.group(4)),
                        "positive": bool(int(m.group(5))),
                        "run": int(m.group(6)),
                        "detection": bool(int(m.group(7))),
                        "dsp_us": int(m.group(8)),
                        "inf_us": int(m.group(9)),
                    }

    def close(self):
        self.stop_flag.set()
        self.join(timeout=2)
        self.serial.close()


def reset_target(port: str, settle: float = 1.8) -> None:
    """Pulse the board's auto-reset circuit through the UART bridge.

    --vectors needs the firmware to have seen no frames, because it identifies
    the two windows by absolute frame number. On the dev board RTS drives EN and
    DTR drives IO0, so RTS low-then-high restarts the app without touching IO0.

    The native-USB console is on the chip itself, so it disappears and
    re-enumerates across the reset -- hence the settle time before the log port
    is opened.
    """
    import serial

    with serial.Serial(port, 115200, timeout=1) as link:
        link.dtr = False   # IO0 high: boot the application, not the loader
        link.rts = True    # EN low: hold in reset
        time.sleep(0.1)
        link.rts = False   # EN high: run
    print(f"  reset the board via {port}, waiting {settle}s for USB to re-enumerate",
          flush=True)
    time.sleep(settle)


def frames_to_bytes(real: np.ndarray, imag: np.ndarray) -> bytes:
    """One frame (sweeps, range) int16 -> the wire format: magic + interleaved re,im."""
    inter = np.empty(real.size * 2, dtype="<i2")
    inter[0::2] = real.reshape(-1)
    inter[1::2] = imag.reshape(-1)
    return MAGIC + inter.tobytes()


def send(port, baud, payloads, fps, label):
    import serial

    period = 1.0 / fps if fps > 0 else 0.0
    with serial.Serial(port, baud, timeout=1) as link:
        link.reset_output_buffer()
        print(f"  sending {len(payloads)} frames of {label} at {fps} fps "
              f"({len(payloads[0])} B each)", flush=True)
        t_start = time.monotonic()
        for i, payload in enumerate(payloads):
            link.write(payload)
            link.flush()
            target = t_start + (i + 1) * period
            while time.monotonic() < target:
                time.sleep(0.0005)
        elapsed = time.monotonic() - t_start
    rate = len(payloads) * len(payloads[0]) / elapsed / 1000.0
    print(f"  sent in {elapsed:.2f} s ({rate:.1f} kB/s)", flush=True)


def run_vectors(args) -> int:
    """Replay both test windows back to back and check the two decisions.

    Sending window 0 then window 1 with no gap means the firmware's 64-frame
    ring holds exactly window 0 at frame 64 and exactly window 1 at frame 128,
    so no reset is needed between them. Windows in between are mixtures and are
    ignored.
    """
    d = np.load(args.vectors, allow_pickle=True)
    n_windows, n_frames = d["raw_real"].shape[:2]

    payloads = []
    for w in range(n_windows):
        for f in range(n_frames):
            payloads.append(frames_to_bytes(d["raw_real"][w, f], d["raw_imag"][w, f]))

    if args.reset:
        reset_target(args.data_port)

    reader = LogReader(args.log_port, echo=args.echo)
    reader.start()
    time.sleep(0.5)

    send(args.data_port, args.baud, payloads, args.fps, "test vectors")
    time.sleep(1.5)
    reader.close()

    print()
    failures = 0
    for w in range(n_windows):
        at_frame = (w + 1) * n_frames
        got = reader.windows.get(at_frame)
        want_q = int(d["output_int8"][w])
        want_p = float(d["probability"][w])
        name = str(d["names"][w])

        if got is None:
            print(f"  window {w}: NO DECISION at frame {at_frame}  <- {name}")
            failures += 1
            continue

        ok = got["q"] == want_q
        failures += 0 if ok else 1
        print(f"  window {w}: {'MATCH ' if ok else 'DIFFER'} "
              f"device q={got['q']} p={got['p']:.4f} | "
              f"expected q={want_q} p={want_p:.4f}")
        print(f"            {name}")
        # The DSP runs every frame (40 ms each); inference runs once per hop of
        # 4 frames, so its budget is 160 ms. Comparing both with 40 ms would
        # overstate the load by 4x.
        duty = got["dsp_us"] / 40_000.0 + got["inf_us"] / 160_000.0
        print(f"            dsp {got['dsp_us']} us of 40 ms/frame, "
              f"inference {got['inf_us']} us of 160 ms/window "
              f"-> {duty * 100.0:.1f}% of one core")

    print()
    if failures:
        print(f"FAIL: {failures} of {n_windows} windows differ from test_vectors.npz")
    else:
        print(f"PASS: all {n_windows} windows match test_vectors.npz exactly")
    return 1 if failures else 0


def run_recording(args) -> int:
    import features

    frames = features.load_frames(args.recording)
    real = np.round(frames.real).astype(np.int16)
    imag = np.round(frames.imag).astype(np.int16)

    if args.limit:
        real, imag = real[: args.limit], imag[: args.limit]

    payloads = [frames_to_bytes(real[i], imag[i]) for i in range(len(real))]

    reader = LogReader(args.log_port, echo=args.echo)
    reader.start()
    time.sleep(0.5)

    send(args.data_port, args.baud, payloads, args.fps, Path(args.recording).name)
    time.sleep(1.5)
    reader.close()

    decisions = sorted(reader.windows.values(), key=lambda w: w["frame"])
    positives = sum(1 for w in decisions if w["positive"])
    events = sum(1 for w in decisions if w["detection"])

    print()
    print(f"  {len(decisions)} windows, {positives} positive, {events} detection event(s)")
    if decisions:
        dsp = np.array([w["dsp_us"] for w in decisions])
        inf = np.array([w["inf_us"] for w in decisions])
        print(f"  dsp {dsp.mean():.0f} us mean / {dsp.max()} us max")
        print(f"  inference {inf.mean():.0f} us mean / {inf.max()} us max")
        duty = dsp.max() / 40_000.0 + inf.max() / 160_000.0
        print(f"  worst case {duty * 100.0:.1f}% of one core "
              f"(dsp per frame, inference per 4-frame window)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--vectors", nargs="?", const=DEFAULT_VECTORS, type=Path,
                     help="replay training/out/final/test_vectors.npz and check the result")
    src.add_argument("--recording", type=Path, help="replay an Exploration Tool .h5")
    ap.add_argument("--data-port", required=True, help="serial port wired to GPIO44 (U0RXD)")
    ap.add_argument("--log-port", required=True, help="the board's native USB port")
    ap.add_argument("--baud", type=int, default=2000000)
    ap.add_argument("--fps", type=float, default=25.0, help="0 sends as fast as possible")
    ap.add_argument("--limit", type=int, default=0, help="only the first N frames")
    ap.add_argument("--quiet", dest="echo", action="store_false",
                    help="do not echo the firmware's log lines")
    ap.add_argument("--no-reset", dest="reset", action="store_false",
                    help="do not restart the board first (--vectors needs a fresh boot)")
    args = ap.parse_args()

    return run_vectors(args) if args.vectors else run_recording(args)


if __name__ == "__main__":
    raise SystemExit(main())
