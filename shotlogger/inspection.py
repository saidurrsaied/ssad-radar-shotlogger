"""Inspect a finished recording without loading its frames.

A 60 s take holds tens of megabytes of IQ data, and none of it is needed to
decide whether the take is good. Everything below reads only the small
per-frame flag arrays and the stored configuration, straight out of the HDF5.
Touching `frame` here would stall the UI for no benefit.

Layout written by a121.H5Recorder (verified against 7.18.2):

    server_info                                          JSON
    sessions/session_0/session_config                    JSON
    sessions/session_0/group_<g>/entry_<e>/result/frame_delayed        bool[n]
                                              /data_saturated          bool[n]
                                              /calibration_needed      bool[n]
                                              /temperature             int64[n]
                                              /tick                    int64[n]
                                              /frame                   <- never read
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import h5py
from acconeer.exptool import a121


class InspectionError(Exception):
    """The file could not be read as an A121 recording."""


@dataclass(frozen=True)
class TakeInfo:
    path: Path
    num_frames: int
    duration_s: float
    delayed: int
    saturated: int
    calibration_needed: int
    temp_min: Optional[int]
    temp_max: Optional[int]
    session_config: a121.SessionConfig

    @property
    def delayed_fraction(self) -> float:
        return self.delayed / self.num_frames if self.num_frames else 0.0


def _first(group: h5py.Group, prefix: str) -> h5py.Group:
    names = sorted(n for n in group if n.startswith(prefix))
    if not names:
        raise InspectionError(f"no {prefix}* group found")
    return group[names[0]]


def inspect_record(path: Path) -> TakeInfo:
    """Read the cheap parts of a recording. Raises InspectionError."""
    try:
        with h5py.File(path, "r") as f:
            sessions = f["sessions"]
            session = _first(sessions, "session_")

            session_config = a121.SessionConfig.from_json(session["session_config"][()])

            try:
                ticks_per_second = int(json.loads(f["server_info"][()])["ticks_per_second"])
            except Exception:
                ticks_per_second = 0

            delayed = saturated = calib = 0
            num_frames = 0
            temp_min: Optional[int] = None
            temp_max: Optional[int] = None
            tick_span = 0

            # Our configuration is single-sensor, single-group, but summing
            # across whatever is present costs nothing and avoids a surprise if
            # a plan ever uses more.
            for group_name in sorted(n for n in session if n.startswith("group_")):
                grp = session[group_name]
                for entry_name in sorted(n for n in grp if n.startswith("entry_")):
                    result = grp[entry_name]["result"]

                    delayed += int(result["frame_delayed"][()].sum())
                    saturated += int(result["data_saturated"][()].sum())
                    calib += int(result["calibration_needed"][()].sum())

                    ticks = result["tick"][()]
                    num_frames = max(num_frames, len(ticks))
                    if len(ticks) >= 2:
                        tick_span = max(tick_span, int(ticks[-1]) - int(ticks[0]))

                    temps = result["temperature"][()]
                    if len(temps):
                        lo, hi = int(temps.min()), int(temps.max())
                        temp_min = lo if temp_min is None else min(temp_min, lo)
                        temp_max = hi if temp_max is None else max(temp_max, hi)
    except InspectionError:
        raise
    except (OSError, KeyError) as exc:
        raise InspectionError(f"{path.name}: not a readable A121 recording ({exc})") from exc

    if num_frames == 0:
        raise InspectionError(f"{path.name}: recording contains no frames")

    # Wall-clock duration from the sensor's own ticks. Falls back to the
    # configured frame rate when server_info is missing, which only happens for
    # files not written by the Exploration Tool.
    if ticks_per_second and tick_span:
        duration_s = tick_span / ticks_per_second
        # One frame period is missing from a span between first and last tick.
        duration_s *= num_frames / max(num_frames - 1, 1)
    else:
        frame_rate = session_config.sensor_config.frame_rate
        duration_s = num_frames / float(frame_rate) if frame_rate else 0.0

    return TakeInfo(
        path=path,
        num_frames=num_frames,
        duration_s=duration_s,
        delayed=delayed,
        saturated=saturated,
        calibration_needed=calib,
        temp_min=temp_min,
        temp_max=temp_max,
        session_config=session_config,
    )
