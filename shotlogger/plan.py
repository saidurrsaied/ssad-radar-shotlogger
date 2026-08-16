"""The shot list and its recording progress.

The plan CSV that tools/make_shotlist.py produces is treated as read-only. On
first use with an output folder it is copied there as `shotlist_working.csv`,
and progress is written to the copy. Regenerating a plan therefore cannot wipe
recorded progress, and each location folder stays self-contained and portable
between teammates.

The filesystem is the tiebreaker, not the CSV: a row marked `recorded` whose
file has since been deleted is reset to `pending` on load. A CSV that
disagrees with the recordings on disk is always wrong.
"""

from __future__ import annotations

import csv
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, List, Optional

WORKING_CSV_NAME = "shotlist_working.csv"
SESSION_JSON_NAME = "session.json"

PLAN_FIELDS = [
    "seq",
    "location",
    "class",
    "geometry",
    "distance",
    "speed",
    "subject",
    "take",
    "filename",
    "seconds",
    "note",
]

PROGRESS_FIELDS = [
    "status",
    "recorded_at",
    "actual_seconds",
    "frames",
    "delayed",
    "saturated",
    "operator_note",
]

WORKING_FIELDS = PLAN_FIELDS + PROGRESS_FIELDS


class PlanError(Exception):
    """The plan or working CSV is unusable. Message is shown to the user."""


class Status(str, Enum):
    PENDING = "pending"
    RECORDED = "recorded"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Shot:
    seq: int
    location: str
    cls: str
    geometry: str
    distance: str
    speed: str
    subject: str
    take: int
    filename: str
    seconds: int
    note: str

    def describe(self) -> str:
        """One-line human summary for the next-shot card."""
        if self.speed == "na":
            return f"{self.cls} / {self.geometry} @ {self.distance}"
        return (
            f"{self.cls} / {self.geometry} @ {self.distance} / "
            f"{self.speed} / {self.subject} / take {self.take}"
        )


@dataclass
class ShotState:
    shot: Shot
    status: Status = Status.PENDING
    recorded_at: str = ""
    actual_seconds: Optional[float] = None
    frames: Optional[int] = None
    delayed: Optional[int] = None
    saturated: Optional[int] = None
    operator_note: str = ""

    @property
    def duration_ok(self) -> Optional[bool]:
        """Whether the take's length is within 25% of the plan.

        None when it has not been recorded. The tolerance exists because the
        operator drives Start and Stop in the Exploration Tool, so duration is
        something we verify rather than enforce.
        """
        if self.actual_seconds is None or not self.shot.seconds:
            return None
        return abs(self.actual_seconds - self.shot.seconds) <= 0.25 * self.shot.seconds


def _shot_from_row(row: Dict[str, str], line: int) -> Shot:
    try:
        return Shot(
            seq=int(row["seq"]),
            location=row["location"],
            cls=row["class"],
            geometry=row["geometry"],
            distance=row["distance"],
            speed=row["speed"],
            subject=row["subject"],
            take=int(row["take"]),
            filename=row["filename"],
            seconds=int(row["seconds"]),
            note=row.get("note", "") or "",
        )
    except (KeyError, ValueError) as exc:
        raise PlanError(f"row {line}: {exc}") from exc


@dataclass
class WorkingPlan:
    """A shot list bound to one output directory."""

    path: Path
    output_dir: Path
    location: str
    states: List[ShotState] = field(default_factory=list)

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, plan_csv: Path, output_dir: Path) -> "WorkingPlan":
        """Open the working copy for `output_dir`, creating it from `plan_csv`.

        Once a working copy exists it wins: the source plan is not re-read, so
        editing the plan mid-campaign cannot silently reshape a session that is
        already half recorded.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        working = output_dir / WORKING_CSV_NAME

        if not working.exists():
            if not plan_csv.is_file():
                raise PlanError(f"No plan CSV at {plan_csv}")
            shutil.copyfile(plan_csv, working)

        plan = cls._read(working, output_dir)
        plan.reconcile_with_disk()
        return plan

    @classmethod
    def _read(cls, working: Path, output_dir: Path) -> "WorkingPlan":
        with working.open(newline="") as fh:
            rows = list(csv.DictReader(fh))

        if not rows:
            raise PlanError(f"{working} contains no rows")

        states: List[ShotState] = []
        seen: Dict[str, int] = {}
        for i, row in enumerate(rows, start=2):
            shot = _shot_from_row(row, i)
            if shot.filename in seen:
                raise PlanError(
                    f"{working}: duplicate filename {shot.filename!r} "
                    f"(rows {seen[shot.filename]} and {i}). "
                    f"Duplicates silently overwrite recordings."
                )
            seen[shot.filename] = i

            states.append(
                ShotState(
                    shot=shot,
                    status=Status(row.get("status") or Status.PENDING.value),
                    recorded_at=row.get("recorded_at", "") or "",
                    actual_seconds=_opt_float(row.get("actual_seconds")),
                    frames=_opt_int(row.get("frames")),
                    delayed=_opt_int(row.get("delayed")),
                    saturated=_opt_int(row.get("saturated")),
                    operator_note=row.get("operator_note", "") or "",
                )
            )

        locations = {s.shot.location for s in states}
        if len(locations) != 1:
            raise PlanError(f"{working}: rows span several locations: {sorted(locations)}")

        return cls(
            path=working,
            output_dir=output_dir,
            location=locations.pop(),
            states=states,
        )

    def reconcile_with_disk(self) -> List[str]:
        """Reset rows whose recording has gone missing. Returns notes for the UI."""
        notes: List[str] = []
        for state in self.states:
            if state.status is Status.RECORDED and not self.final_path(state.shot).exists():
                state.status = Status.PENDING
                state.recorded_at = ""
                state.actual_seconds = None
                state.frames = state.delayed = state.saturated = None
                notes.append(f"{state.shot.filename} was marked recorded but is missing")
        if notes:
            self.save()
        return notes

    def orphan_files(self) -> List[Path]:
        """Recordings on disk that no row claims. Usually a leftover rename."""
        known = {self.final_path(s.shot) for s in self.states}
        found: List[Path] = []
        for path in sorted(self.output_dir.rglob("*.h5")):
            if path not in known:
                found.append(path)
        return found

    # ----------------------------------------------------------------- paths

    def final_path(self, shot: Shot) -> Path:
        return self.output_dir / shot.cls / shot.filename

    # --------------------------------------------------------------- queries

    def __iter__(self) -> Iterator[ShotState]:
        return iter(self.states)

    def __len__(self) -> int:
        return len(self.states)

    def by_seq(self, seq: int) -> Optional[ShotState]:
        for state in self.states:
            if state.shot.seq == seq:
                return state
        return None

    def next_pending(self, after: Optional[int] = None) -> Optional[ShotState]:
        """First pending shot, optionally continuing past `after`.

        Falls back to the first pending row overall so a re-record earlier in
        the list is picked up rather than skipped.
        """
        if after is not None:
            for state in self.states:
                if state.shot.seq > after and state.status is Status.PENDING:
                    return state
        for state in self.states:
            if state.status is Status.PENDING:
                return state
        return None

    def counts(self) -> Dict[str, int]:
        out = {s.value: 0 for s in Status}
        for state in self.states:
            out[state.status.value] += 1
        out["total"] = len(self.states)
        return out

    def remaining_seconds(self) -> int:
        return sum(s.shot.seconds for s in self.states if s.status is not Status.RECORDED)

    # -------------------------------------------------------------- mutation

    def mark_recorded(
        self,
        seq: int,
        *,
        actual_seconds: float,
        frames: int,
        delayed: int,
        saturated: int,
        operator_note: str = "",
    ) -> None:
        state = self._require(seq)
        state.status = Status.RECORDED
        state.recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state.actual_seconds = round(actual_seconds, 2)
        state.frames = frames
        state.delayed = delayed
        state.saturated = saturated
        state.operator_note = operator_note
        self.save()

    def mark_rejected(self, seq: int, *, operator_note: str = "") -> None:
        """Reopen a shot after its recording was discarded.

        Status goes back to pending, not `rejected`, because the shot still
        needs doing. The rejection itself is preserved in the logbook, which is
        append-only -- so a take rejected three times stays visible there even
        though its files are gone.
        """
        state = self._require(seq)
        state.status = Status.PENDING
        state.recorded_at = ""
        state.actual_seconds = None
        state.frames = state.delayed = state.saturated = None
        state.operator_note = operator_note
        self.save()

    def _require(self, seq: int) -> ShotState:
        state = self.by_seq(seq)
        if state is None:
            raise PlanError(f"no shot with seq {seq}")
        return state

    # ---------------------------------------------------------------- saving

    def save(self) -> None:
        """Rewrite the working CSV atomically.

        Written to a temp file in the same directory and renamed, so a crash
        mid-write leaves the previous version intact rather than a truncated
        file that loses the whole session's progress.
        """
        tmp = self.path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=WORKING_FIELDS)
            writer.writeheader()
            for state in self.states:
                shot = state.shot
                writer.writerow(
                    {
                        "seq": shot.seq,
                        "location": shot.location,
                        "class": shot.cls,
                        "geometry": shot.geometry,
                        "distance": shot.distance,
                        "speed": shot.speed,
                        "subject": shot.subject,
                        "take": shot.take,
                        "filename": shot.filename,
                        "seconds": shot.seconds,
                        "note": shot.note,
                        "status": state.status.value,
                        "recorded_at": state.recorded_at,
                        "actual_seconds": (
                            "" if state.actual_seconds is None else state.actual_seconds
                        ),
                        "frames": "" if state.frames is None else state.frames,
                        "delayed": "" if state.delayed is None else state.delayed,
                        "saturated": "" if state.saturated is None else state.saturated,
                        "operator_note": state.operator_note,
                    }
                )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


def _opt_int(value: Optional[str]) -> Optional[int]:
    return int(value) if value else None


def _opt_float(value: Optional[str]) -> Optional[float]:
    return float(value) if value else None
