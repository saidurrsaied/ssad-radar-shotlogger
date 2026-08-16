"""Qt worker that spots finished recordings and inspects them off the GUI thread.

Division of labour, and the reason for it:

  worker thread   detection + inspection. Both are read-only and need nothing
                  but the frozen config, which is immutable and safe to share.
                  Opening an HDF5 file is the only part that can stall.
  GUI thread      the move, the working-CSV write-back and the logbook. The
                  move is a rename within one filesystem, so it is fast, and
                  keeping all mutation on one thread means the plan needs no
                  locking at all.

Nothing here blocks on hardware -- the Exploration Tool owns the sensor -- so a
plain `requestInterruption()` is enough to stop cleanly. There is no need for
the threading.Event machinery a sensor-owning design would require.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .filing import TempScanner
from .inspection import InspectionError, inspect_record

POLL_INTERVAL_MS = 500


class Watcher(QObject):
    """Lives on a worker thread. Owns the scanner and the poll timer."""

    #: path, TakeInfo or None, error message ("" when the inspection succeeded)
    inspected = Signal(object, object, str)
    #: emitted once after the first poll, carrying any pre-existing files
    started = Signal(object)
    failed = Signal(str)

    def __init__(self, temp_dir: Path) -> None:
        super().__init__()
        self._temp_dir = Path(temp_dir)
        self._scanner: Optional[TempScanner] = None
        self._timer: Optional[QTimer] = None

    @Slot()
    def start(self) -> None:
        """Begin watching. Called via a queued connection once the thread runs."""
        if not self._temp_dir.is_dir():
            self.failed.emit(
                f"The Exploration Tool's recording directory does not exist:\n"
                f"{self._temp_dir}\n\n"
                f"It is created the first time the Exploration Tool records. Start it, "
                f"record once, then start watching."
            )
            return

        self._scanner = TempScanner(self._temp_dir)
        self.started.emit(self._scanner.preexisting)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._scanner = None

    @Slot(object)
    def release(self, path: object) -> None:
        """Let a file be offered again after the GUI declined to file it."""
        if self._scanner is not None:
            self._scanner.release(Path(str(path)))

    @Slot()
    def _poll(self) -> None:
        if self._scanner is None:
            return
        try:
            settled = self._scanner.poll()
        except OSError as exc:
            self.failed.emit(f"Could not read {self._temp_dir}: {exc}")
            return

        for path in settled:
            try:
                info = inspect_record(path)
            except InspectionError as exc:
                self.inspected.emit(path, None, str(exc))
                continue
            self.inspected.emit(path, info, "")
