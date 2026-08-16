"""Bundle a session's recordings and bookkeeping into another folder.

Recordings already land in the output folder chosen at the top of the window,
so nothing here is a rescue from a temp directory. What this adds is the step
after a batch: hand the whole session -- the .h5 files, the updated working
CSV, the logbook -- to a shared drive, a USB stick, or the machine that will do
the training, in one action instead of a manual copy that is easy to do
half-way.

Copy leaves the session where it is and is the safe default for an interim
backup. Move relocates it, and the window then continues against the new
location, so the session is not left split across two folders.

Nothing is overwritten. If a destination file already exists the export stops
before transferring anything, because a half-merged session is worse than no
export at all.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .logbook import LOGBOOK_NAME
from .plan import SESSION_JSON_NAME, WORKING_CSV_NAME

#: Bookkeeping files carried alongside the recordings.
SIDECARS = (WORKING_CSV_NAME, LOGBOOK_NAME, SESSION_JSON_NAME)


class ExportError(Exception):
    """The export cannot proceed. The message is shown to the user as-is."""


@dataclass
class ExportPlan:
    """What an export would transfer, worked out before anything is touched."""

    source: Path
    destination: Path
    recordings: List[Path] = field(default_factory=list)
    sidecars: List[Path] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def file_count(self) -> int:
        return len(self.recordings) + len(self.sidecars)

    def describe(self) -> str:
        size = f"{self.total_bytes / 1e9:.2f} GB" if self.total_bytes >= 1e9 else (
            f"{self.total_bytes / 1e6:.0f} MB"
        )
        parts = [f"{len(self.recordings)} recording(s), {size}"]
        if self.sidecars:
            parts.append(", ".join(p.name for p in self.sidecars))
        return "\n".join(parts)


@dataclass
class ExportResult:
    plan: ExportPlan
    moved: bool
    transferred: int


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def prepare(source: Path, destination: Path) -> ExportPlan:
    """Work out what would be transferred, and refuse anything unsafe.

    Every check runs before a single byte moves, so a rejected export leaves
    both folders exactly as they were.
    """
    source = Path(source)
    destination = Path(destination)

    if not source.is_dir():
        raise ExportError(f"The session folder does not exist:\n{source}")

    if source.resolve() == destination.resolve():
        raise ExportError("The destination is the session folder itself.")

    # Nesting either way produces a copy that contains itself, or a move that
    # deletes what it is writing into.
    if _is_within(destination, source):
        raise ExportError(
            f"The destination sits inside the session folder:\n{destination}\n\n"
            f"Choose a folder outside {source}."
        )
    if _is_within(source, destination):
        raise ExportError(
            f"The session folder sits inside the destination:\n{source}\n\n"
            f"Choose a different destination."
        )

    if (destination / WORKING_CSV_NAME).exists():
        raise ExportError(
            f"{destination} already holds a recording session ({WORKING_CSV_NAME}).\n\n"
            f"Merging two sessions would combine their progress and their take "
            f"numbering. Export into a new, empty folder instead."
        )

    plan = ExportPlan(source=source, destination=destination)

    for path in sorted(source.rglob("*.h5")):
        plan.recordings.append(path)
        plan.total_bytes += path.stat().st_size

    for name in SIDECARS:
        candidate = source / name
        if candidate.is_file():
            plan.sidecars.append(candidate)
            plan.total_bytes += candidate.stat().st_size

    if not plan.file_count:
        raise ExportError(f"There is nothing to export in {source}.")

    clashes = [
        p.name
        for p in plan.recordings + plan.sidecars
        if (destination / p.relative_to(source)).exists()
    ]
    if clashes:
        raise ExportError(
            f"{len(clashes)} file(s) already exist at the destination, "
            f"e.g. {clashes[0]}.\n\nNothing has been transferred. Export into an "
            f"empty folder so no recording can be overwritten."
        )

    free = shutil.disk_usage(_existing_ancestor(destination)).free
    if free < plan.total_bytes * 1.1:
        raise ExportError(
            f"The destination has {free / 1e9:.2f} GB free but the session needs "
            f"{plan.total_bytes / 1e9:.2f} GB."
        )

    return plan


def _existing_ancestor(path: Path) -> Path:
    path = path.resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def run(plan: ExportPlan, *, move: bool, progress=None) -> ExportResult:
    """Perform a prepared export.

    Files are copied first and only unlinked afterwards when moving, so an
    interruption mid-export leaves the originals intact.
    """
    plan.destination.mkdir(parents=True, exist_ok=True)

    transferred = 0
    copied: List[Path] = []

    for source_path in plan.recordings + plan.sidecars:
        relative = source_path.relative_to(plan.source)
        target = plan.destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
        copied.append(source_path)
        transferred += 1
        if progress is not None:
            progress(transferred, plan.file_count, relative.as_posix())

    if move:
        for source_path in copied:
            try:
                source_path.unlink()
            except OSError:
                # The copy is already safe at the destination; a leftover
                # original is untidy but never data loss.
                pass
        _prune_empty_dirs(plan.source)

    return ExportResult(plan=plan, moved=move, transferred=transferred)


def _prune_empty_dirs(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            try:
                path.rmdir()
            except OSError:
                pass
