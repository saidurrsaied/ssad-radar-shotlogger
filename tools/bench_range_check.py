#!/usr/bin/env python3
"""Find the longest range this sensor can actually record at, and recommend a config.

    tools/bench_range_check.py                  # full check, ~15 minutes
    tools/bench_range_check.py --quick          # stages 1-2 only, no walking

Run this before the outdoor session. It answers the one question the outdoor
measurement plan deliberately left open: how far can we usefully see?

Why it cannot be worked out on paper. Two limits only the hardware knows:

  * The sensor rejects some configurations the Python validator accepts.
    step_length=128 passes client-side validation and is refused by the server.
  * Maximum sweep rate falls as points, HWAAS and PRF change, and a config that
    validates can still be unattainable. We measured a 2791 Hz ceiling where
    6000 Hz was wanted.

And one limit only a person can measure: whether a human still returns usable
signal at the far edge. Configured range is not detection range.

Stages
  1  link and sensor identity
  2  candidate sweep -- which ranges the sensor accepts and sustains cleanly
  3  noise floor, empty scene
  4  walk-in test -- where a person disappears into the noise
  5  recommendation, ready to paste

Nothing is written. This only measures and prints.
"""

import argparse
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from acconeer.exptool import a121

BASE_STEP_M = 0.0025
LAMBDA_M = 3e8 / 60.5e9

# Held fixed across every candidate: these set the velocity and time axes of the
# spectrogram, and they must not vary between locations or the recordings cannot
# be compared. Only the range parameters are under test.
SWEEPS_PER_FRAME = 64
SWEEP_RATE = 5500.0
FRAME_RATE = 25.0
HWAAS = 4
PROFILE = a121.Profile.PROFILE_3
START_POINT = 400

# Throughput ceiling measured on the XM125's 2 Mbaud link: 161 kB/s was clean,
# 187 kB/s dropped 77 frames of 280. Stay well below it.
SAFE_BYTES_PER_S = 150e3

SNR_THRESHOLD_DB = 10.0


@dataclass
class Candidate:
    label: str
    step_length: int
    num_points: int
    prf: a121.PRF

    @property
    def end_m(self) -> float:
        return (START_POINT + (self.num_points - 1) * self.step_length) * BASE_STEP_M

    @property
    def bin_m(self) -> float:
        return self.step_length * BASE_STEP_M

    @property
    def bytes_per_s(self) -> float:
        return SWEEPS_PER_FRAME * self.num_points * 4 * FRAME_RATE

    def sensor_config(self) -> a121.SensorConfig:
        return a121.SensorConfig(
            sweeps_per_frame=SWEEPS_PER_FRAME,
            sweep_rate=SWEEP_RATE,
            frame_rate=FRAME_RATE,
            continuous_sweep_mode=False,
            start_point=START_POINT,
            num_points=self.num_points,
            step_length=self.step_length,
            profile=PROFILE,
            hwaas=HWAAS,
            prf=self.prf,
            receiver_gain=16,
        )


# step_length is restricted to multiples of 24 beyond 24 -- 128 is refused by the
# server even though the validator allows it, so only safe values appear here.
CANDIDATES = [
    Candidate("~6 m  (current)", 96, 22, a121.PRF.PRF_13_0_MHz),
    Candidate("~9 m", 192, 18, a121.PRF.PRF_8_7_MHz),
    Candidate("~12 m", 192, 23, a121.PRF.PRF_8_7_MHz),
    Candidate("~17 m", 288, 23, a121.PRF.PRF_6_5_MHz),
    Candidate("~22 m", 384, 23, a121.PRF.PRF_5_2_MHz),
]


@dataclass
class Measurement:
    candidate: Candidate
    ok: bool
    note: str
    max_sweep_rate: Optional[float] = None
    fps: Optional[float] = None
    delayed: Optional[int] = None
    frames: Optional[int] = None


def moving_energy(frame: np.ndarray) -> np.ndarray:
    """Energy per range bin from moving targets only.

    Subtracting the mean across sweeps is a DC notch on slow time, which removes
    the static scene -- ground, parked cars, walls -- by construction. What
    survives is motion. Taking the peak across the remaining Doppler bins gives
    one number per range bin.
    """
    centred = frame - frame.mean(axis=0, keepdims=True)
    spectrum = np.abs(np.fft.fft(centred, axis=0))
    # Drop the residual zero-Doppler bin; clutter removal leaves a little there.
    spectrum[0, :] = 0.0
    return spectrum.max(axis=0)


def stage_1_identity(client: a121.Client) -> None:
    info = client.server_info
    print("  RSS version    :", info.rss_version)
    print("  hardware       :", getattr(info, "hardware_name", "?"))
    print("  sensors        :", len(info.sensor_infos))
    print("  max baudrate   :", info.max_baudrate)


def stage_2_sweep(client: a121.Client, seconds: float) -> List[Measurement]:
    results: List[Measurement] = []
    print(
        "\n  %-16s %8s %7s %10s %9s %8s  %s"
        % ("candidate", "end", "bins", "kB/s", "max_sr", "delayed", "verdict")
    )
    print("  " + "-" * 76)

    for cand in CANDIDATES:
        config = a121.SessionConfig(cand.sensor_config(), update_rate=None)

        try:
            config.validate()
        except Exception as exc:
            results.append(Measurement(cand, False, f"invalid: {exc}"))
            print("  %-16s %8.2f %7.2f %10.0f  %s" % (
                cand.label, cand.end_m, cand.bin_m, cand.bytes_per_s / 1e3,
                "REJECTED by validator"))
            continue

        try:
            metadata = client.setup_session(config)
        except Exception as exc:
            results.append(Measurement(cand, False, f"server refused: {exc}"))
            print("  %-16s %8.2f %7.2f %10.0f  %s" % (
                cand.label, cand.end_m, cand.bin_m, cand.bytes_per_s / 1e3,
                f"REFUSED  {str(exc)[:34]}"))
            continue

        max_sr = metadata.max_sweep_rate
        if max_sr and max_sr < SWEEP_RATE:
            results.append(
                Measurement(cand, False, f"sweep rate ceiling {max_sr:.0f} Hz", max_sr)
            )
            print("  %-16s %8.2f %7.2f %10.0f %9.0f  %s" % (
                cand.label, cand.end_m, cand.bin_m, cand.bytes_per_s / 1e3, max_sr,
                "TOO SLOW for 5500 Hz"))
            continue

        client.start_session()
        n = int(FRAME_RATE * seconds)
        delayed = 0
        start = time.monotonic()
        for _ in range(n):
            result = client.get_next()
            delayed += int(result.frame_delayed)
        elapsed = time.monotonic() - start
        client.stop_session()

        fps = n / elapsed
        ok = delayed == 0 and cand.bytes_per_s <= SAFE_BYTES_PER_S
        verdict = "PASS" if ok else ("frames delayed" if delayed else "over budget")
        results.append(
            Measurement(cand, ok, verdict, max_sr, fps, delayed, n)
        )
        print("  %-16s %8.2f %7.2f %10.0f %9.0f %8s  %s" % (
            cand.label, cand.end_m, cand.bin_m, cand.bytes_per_s / 1e3,
            max_sr or 0, f"{delayed}/{n}", verdict))

    return results


def _collect(client: a121.Client, seconds: float) -> np.ndarray:
    """Peak moving-target energy per range bin over a recording."""
    n = int(FRAME_RATE * seconds)
    peak = None
    for i in range(n):
        energy = moving_energy(client.get_next().frame)
        peak = energy if peak is None else np.maximum(peak, energy)
        if i % int(FRAME_RATE) == 0:
            print(f"    {i / FRAME_RATE:4.0f} s / {seconds:.0f} s", end="\r", flush=True)
    print(" " * 30, end="\r")
    return peak


def stage_3_4_walk_in(client: a121.Client, cand: Candidate) -> Optional[float]:
    """Measure where a walking person stops being visible. Returns usable range."""
    config = a121.SessionConfig(cand.sensor_config(), update_rate=None)
    client.setup_session(config)

    ranges = np.array(
        [(START_POINT + i * cand.step_length) * BASE_STEP_M for i in range(cand.num_points)]
    )

    print("\n  Stage 3  noise floor")
    input("    Clear everyone out of the beam, then press Enter... ")
    client.start_session()
    noise = _collect(client, 10.0)
    client.stop_session()
    print("    done")

    print("\n  Stage 4  walk-in")
    print(f"    Have one person walk slowly from beyond {cand.end_m:.1f} m")
    print("    straight down the centre line towards the sensor, then back out.")
    print("    Repeat for the whole 30 s so every range bin is visited.")
    input("    Press Enter when they are ready to start... ")
    client.start_session()
    signal = _collect(client, 30.0)
    client.stop_session()
    print("    done")

    snr_db = 20 * np.log10(np.maximum(signal, 1e-9) / np.maximum(noise, 1e-9))

    print(f"\n  range      SNR      visible (threshold {SNR_THRESHOLD_DB:.0f} dB)")
    print("  " + "-" * 44)
    usable = None
    for r, s in zip(ranges, snr_db):
        mark = "yes" if s >= SNR_THRESHOLD_DB else "no"
        bar = "#" * int(max(0, min(24, s / 2)))
        print(f"  {r:5.2f} m  {s:6.1f} dB  {mark:<4} {bar}")
        if s >= SNR_THRESHOLD_DB:
            usable = r

    return usable


def stage_5_recommend(cand: Candidate, usable: Optional[float]) -> None:
    end = usable if usable is not None else cand.end_m
    far = float(int(end))          # round down to a whole metre for taping
    mid = round(far * 0.6 * 2) / 2  # nearest half metre
    near = max(2.0, round(far * 0.25 * 2) / 2)

    sc = cand.sensor_config()
    print("\n" + "=" * 72)
    print("RECOMMENDATION")
    print("=" * 72)
    print(f"\nUsable range measured: {end:.2f} m")
    if usable is not None and usable < cand.end_m - cand.bin_m:
        print(
            f"  Note: the config reaches {cand.end_m:.2f} m but a person fades out at "
            f"{usable:.2f} m.\n  Trust the measured number, not the configured one."
        )

    print("\n1. Paste into tools/freeze_session_config.py, build_session_config():\n")
    print(f"        start_point={sc.start_point},")
    print(f"        num_points={sc.num_points},")
    print(f"        step_length={sc.step_length},")
    print(f"        prf=a121.PRF.{cand.prf.name},")
    print("\n   (leave sweeps_per_frame, sweep_rate, frame_rate, profile and hwaas alone --")
    print("    they set the velocity and time axes and must not vary between locations)")

    print("\n2. Re-freeze -- ONLY if data/ is still empty:\n")
    print("        tools/freeze_session_config.py --force")

    print("\n3. Fill the bands into plans/locB_outdoor_path.yaml:\n")
    print(f"        far  = {far:.1f} m")
    print(f"        mid  = {mid:.1f} m")
    print(f"        near = {near:.1f} m")

    print("\n4. Regenerate and check:\n")
    print("        tools/make_shotlist.py --plan plans/locB_outdoor_path.yaml")
    print("        tools/check_install.py")
    print("        python -m unittest discover -s tests")

    dwell_ped = (end - START_POINT * BASE_STEP_M) / 1.4
    dwell_cyc = (end - START_POINT * BASE_STEP_M) / 5.0
    print(f"\nDwell at this range: pedestrian {dwell_ped:.1f} s, cyclist {dwell_cyc:.1f} s")
    if dwell_cyc < 1.5:
        print("  WARNING: under ~1.5 s a cyclist gives less than two pedal cycles,")
        print("  which is the feature that distinguishes a cyclist at all. Report this.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=None, help="e.g. /dev/ttyUSB0 (autodetected if omitted)")
    parser.add_argument("--quick", action="store_true", help="stages 1-2 only, nobody has to walk")
    parser.add_argument("--seconds", type=float, default=6.0, help="per-candidate soak time")
    args = parser.parse_args()

    port = args.port
    if port is None:
        from acconeer.exptool._core.communication.comm_devices import get_serial_devices

        found = [d for d in get_serial_devices() if d.recognized]
        if not found:
            print("ERROR: no Acconeer sensor found. Plug it in, or pass --port.")
            return 1
        port = found[0].port
        print(f"Using {found[0].display_name()}")

    try:
        client = a121.Client.open(serial_port=port)
    except Exception as exc:
        print(f"ERROR: could not open {port}: {exc}")
        print("If this is a permission error, see the dialout note in README.md.")
        return 1

    try:
        print("\nStage 1  sensor")
        stage_1_identity(client)

        print("\nStage 2  candidate configurations")
        results = stage_2_sweep(client, args.seconds)

        passing = [m for m in results if m.ok]
        if not passing:
            print("\nNo candidate passed. Report the table above -- do not guess a config.")
            return 1

        best = max(passing, key=lambda m: m.candidate.end_m)
        print(f"\n  Longest clean candidate: {best.candidate.label} "
              f"({best.candidate.end_m:.2f} m)")

        usable = None
        if not args.quick:
            usable = stage_3_4_walk_in(client, best.candidate)

        stage_5_recommend(best.candidate, usable)
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
