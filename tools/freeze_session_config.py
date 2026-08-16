#!/usr/bin/env python3
"""Write the frozen A121 session configuration for the whole campaign.

Section 6 of the getting-started guide requires that every recording, in every
environment, share one sensor configuration -- recordings made with different
sweep or range settings cannot be mixed into one training set. This script is
the only place that configuration is constructed. The Shot Logger can load it
but never build one, so there is no code path that can quietly record against
something else.

The values below were measured on an XE125 (XM125, RSS a121-v1.13.0), not
derived on paper. Four published guesses failed on hardware:

  1. continuous_sweep_mode=True requires the inter-frame and inter-sweep idle
     states to be equal; the defaults differ.
  2. The frame buffer is capped at 4095 samples, so sweeps_per_frame *
     num_points must fit. 128 x 42 = 5376 is rejected.
  3. PRF_15_6_MHz reaches only 5.10 m, short of the 6 m end point.
  4. A 6000 Hz sweep rate is unattainable: the sensor reported a 2791 Hz
     ceiling at 28 points / HWAAS 8.

The real blocker was the link. The XM125 exploration server negotiates 2 Mbaud
and reports that as its max_baudrate, giving roughly 200 kB/s. Continuous sweep
mode over that link produced 399 delayed frames out of 400. Burst mode -- a
coherent 64-sweep frame at 25 Hz -- is what the FFT actually needs and fits
comfortably:

    25 fps -> 134 kB/s ->   0/500 delayed   <- chosen, ~20% margin
    30 fps -> 161 kB/s ->   0/240 delayed
    35 fps -> 187 kB/s ->  77/280 delayed   <- link saturates

The +/-6.82 m/s velocity ceiling is sized for the outdoor cyclist rather than
the indoor walker, because the freeze is global and this number has to survive
every environment.
"""

import argparse
import hashlib
import sys
from pathlib import Path

from acconeer.exptool import a121


# Base range step is 2.5 mm, so start_point 400 -> 1.00 m and step_length 96 -> 0.24 m.
BASE_STEP_M = 0.0025

# Wavelength at the A121's 60.5 GHz centre frequency.
LAMBDA_M = 3e8 / 60.5e9

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "config" / "session_config.json"


def build_session_config() -> a121.SessionConfig:
    """The frozen configuration. Change this and the campaign restarts."""
    sensor_config = a121.SensorConfig(
        # A coherent burst of 64 sweeps is one micro-Doppler column.
        sweeps_per_frame=64,
        sweep_rate=5500.0,
        frame_rate=25.0,
        # Burst mode, not continuous: the 2 Mbaud link cannot sustain gapless
        # slow time at any useful range. See the module docstring.
        continuous_sweep_mode=False,
        # 1.00 m to 6.04 m, covering the 6 m floor marker in the measurement plan.
        start_point=400,
        num_points=22,
        step_length=96,
        profile=a121.Profile.PROFILE_3,
        # HWAAS 4 rather than 8: 8 caps the sweep rate at 2791 Hz, which would
        # alias a walker's limbs.
        hwaas=4,
        # PRF_13_0_MHz measures to 7.00 m; PRF_15_6_MHz stops at 5.10 m.
        prf=a121.PRF.PRF_13_0_MHz,
        receiver_gain=16,
    )
    return a121.SessionConfig(sensor_config, update_rate=None)


def describe(session_config: a121.SessionConfig) -> str:
    sc = session_config.sensor_config
    start_m = sc.start_point * BASE_STEP_M
    end_m = (sc.start_point + (sc.num_points - 1) * sc.step_length) * BASE_STEP_M
    v_max = LAMBDA_M * sc.sweep_rate / 4
    v_res = LAMBDA_M * sc.sweep_rate / (2 * sc.sweeps_per_frame)
    bytes_per_s = sc.sweeps_per_frame * sc.num_points * 4 * sc.frame_rate

    return "\n".join(
        [
            f"range          : {start_m:.2f} - {end_m:.2f} m "
            f"(step {sc.step_length * BASE_STEP_M:.2f} m, {sc.num_points} points)",
            f"velocity       : +/- {v_max:.2f} m/s, resolution {v_res:.3f} m/s",
            f"frame          : {sc.sweeps_per_frame} sweeps at {sc.sweep_rate:.0f} Hz "
            f"= {sc.sweeps_per_frame / sc.sweep_rate * 1e3:.1f} ms burst",
            f"frame rate     : {sc.frame_rate:.0f} Hz "
            f"({sc.frame_rate:.0f} spectrogram columns per second)",
            f"buffer         : {sc.sweeps_per_frame * sc.num_points} / 4095 samples",
            f"throughput     : {bytes_per_s / 1e3:.0f} kB/s "
            f"(link ceiling is about 200 kB/s)",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing config (this invalidates every recording already made)",
    )
    args = parser.parse_args()

    session_config = build_session_config()
    session_config.validate()

    raw = session_config.to_json()

    # The loader asserts this same round-trip, so fail here rather than at the
    # start of a recording session.
    if a121.SessionConfig.from_json(raw).to_json() != raw:
        print("ERROR: config does not round-trip through JSON", file=sys.stderr)
        return 1

    if args.out.exists() and not args.force:
        existing = args.out.read_text()
        if existing == raw:
            print(f"unchanged: {args.out}")
            print(describe(session_config))
            return 0
        print(f"ERROR: {args.out} exists and differs.", file=sys.stderr)
        print(
            "Overwriting it splits the dataset: every recording made against the old "
            "config becomes unmixable with everything recorded after. Pass --force only "
            "if the campaign is restarting.",
            file=sys.stderr,
        )
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(raw)

    print(f"wrote {args.out}")
    print(f"sha256         : {hashlib.sha256(raw.encode()).hexdigest()}")
    print(describe(session_config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
