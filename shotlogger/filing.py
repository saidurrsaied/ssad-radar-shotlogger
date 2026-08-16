"""Detect finished recordings in the Exploration Tool's temp directory and file
them under their planned name.

No Qt here, so all of this is testable headlessly by dropping files into a
directory.

Why a temp directory at all: the Exploration Tool writes every take to
`a121.H5Recorder(get_temp_h5_path())` and, on Stop, calls `stop_session()`,
`detach_recorder()` and `recorder.close()` before offering the file for saving.
By the time Stop returns, a complete and closed HDF5 file is sitting in a known
place. "Save to file" in the GUI is only a copy step, and this is what replaces
it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

import h5py

from .frozen_config import FrozenConfig
from .inspection import InspectionError, TakeInfo, inspect_record
from .plan import ShotState, WorkingPlan


class Outcome(str, Enum):
    FILED = "filed"
    CONFIG_MISMATCH = "config_mismatch"
    LOCATION_MISMATCH = "location_mismatch"
    TARGET_EXISTS = "target_exists"
    UNREADABLE = "unreadable"


@dataclass
class FilingResult:
    outcome: Outcome
    message: str
    source: Path
    info: Optional[TakeInfo] = None
    destination: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.FILED


class TempScanner:
    """Watches a directory for new, finished .h5 files.

    Files present at construction are ignored: they predate this session and
    filing them would attach an unknown recording to a planned shot.
    """

    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = temp_dir
        self._ignored: set = set()
        self._probes: Dict[Path, tuple] = {}
        self._claimed: set = set()
        if temp_dir.is_dir():
            self._ignored = {p.resolve() for p in temp_dir.glob("*.h5")}

    @property
    def preexisting(self) -> List[Path]:
        return sorted(self._ignored)

    def poll(self) -> List[Path]:
        """Return newly settled recordings, oldest first.

        Settled means the file's size *and its frame count* are both unchanged
        since the previous poll, and it holds at least one frame.

        Watching the size alone is not enough, and this is not theoretical: the
        chunked writer flushes only every 512 results or every second, so
        between two flushes a file in the middle of a long recording sits at a
        constant size with its header already in place. Polling twice across
        such a gap looks exactly like a finished file. The frame count is what
        actually distinguishes them, because it only stops rising when the
        recording stops.
        """
        if not self.temp_dir.is_dir():
            return []

        settled: List[Path] = []
        current = {p.resolve() for p in self.temp_dir.glob("*.h5")}

        for path in sorted(current, key=lambda p: p.stat().st_mtime):
            if path in self._ignored or path in self._claimed:
                continue

            probe = self._probe(path)
            previous = self._probes.get(path)
            self._probes[path] = probe

            if probe is None or previous is None or previous != probe:
                continue

            size, frames = probe
            if size == 0 or frames == 0:
                continue

            self._claimed.add(path)
            settled.append(path)

        # Forget files that have gone away so the dicts do not grow forever.
        for known in list(self._probes):
            if known not in current:
                self._probes.pop(known, None)

        return settled

    @staticmethod
    def _probe(path: Path) -> Optional[tuple]:
        """(size, frame count), or None while the file is not readable."""
        try:
            size = path.stat().st_size
        except OSError:
            return None
        try:
            with h5py.File(path, "r") as f:
                sessions = f["sessions"]
                frames = 0
                for session_name in sessions:
                    session = sessions[session_name]
                    for group_name in session:
                        if not group_name.startswith("group_"):
                            continue
                        group = session[group_name]
                        for entry_name in group:
                            tick = group[entry_name].get("result", {}).get("tick")
                            if tick is not None:
                                frames = max(frames, len(tick))
                return size, frames
        except (OSError, KeyError, TypeError):
            return None

    def release(self, path: Path) -> None:
        """Allow a file to be considered again (used when filing failed)."""
        self._claimed.discard(path.resolve())


def file_take(
    source: Path,
    state: ShotState,
    plan: WorkingPlan,
    frozen: FrozenConfig,
) -> FilingResult:
    """Verify a finished recording and move it to its planned name.

    Nothing is moved until every gate passes, so a rejected take is left in the
    temp directory where the operator can still recover it by hand.
    """
    try:
        info = inspect_record(source)
    except InspectionError as exc:
        return FilingResult(Outcome.UNREADABLE, str(exc), source)

    # The configuration freeze is the reason this tool exists. A take recorded
    # against a different config cannot be mixed into the training set, and
    # filing it under a planned name would hide that forever.
    if not frozen.matches(info.session_config):
        return FilingResult(
            Outcome.CONFIG_MISMATCH,
            "This recording used a different sensor configuration than the frozen one "
            f"({frozen.path.name}, {frozen.short_sha}). It has not been filed. Load the "
            "frozen config in the Exploration Tool and record this shot again.",
            source,
            info=info,
        )

    if state.shot.location != plan.location:
        return FilingResult(
            Outcome.LOCATION_MISMATCH,
            f"Shot belongs to location {state.shot.location!r} but this output folder "
            f"holds {plan.location!r}.",
            source,
            info=info,
        )

    destination = plan.final_path(state.shot)
    if destination.exists():
        return FilingResult(
            Outcome.TARGET_EXISTS,
            f"{destination.name} already exists. Reject the existing take first if you "
            f"mean to replace it.",
            source,
            info=info,
            destination=destination,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))

    return FilingResult(
        Outcome.FILED,
        f"Filed as {destination.name}",
        source,
        info=info,
        destination=destination,
    )


def warnings_for(info: TakeInfo, state: ShotState) -> List[str]:
    """Non-fatal quality notes shown after a take is filed."""
    notes: List[str] = []

    planned = state.shot.seconds
    if planned and abs(info.duration_s - planned) > 0.25 * planned:
        notes.append(
            f"Duration {info.duration_s:.1f} s against a planned {planned} s "
            f"(more than 25% off)"
        )

    if info.delayed:
        pct = 100.0 * info.delayed_fraction
        notes.append(f"{info.delayed} of {info.num_frames} frames delayed ({pct:.1f}%)")

    if info.saturated:
        notes.append(
            f"{info.saturated} frames saturated -- the target may be too close or a "
            f"wall is reflecting straight back"
        )

    if info.calibration_needed:
        notes.append(f"{info.calibration_needed} frames flagged calibration needed")

    return notes
