"""Append-only record of every take attempt.

One row per *attempt*, including rejected and failed ones. A log that only
records successes tells you nothing about why a session went badly, and it is
the only place a rejected take survives -- rejected recordings are deleted from
disk, so if the reason is not written here it is gone.

`config_sha256` on every row is the auditable proof of the configuration
freeze. After the campaign, across every location:

    cut -d, -f15 _logbook.csv | sort -u

must yield exactly one value. tools/campaign_report.py checks this.
"""

from __future__ import annotations

import csv
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

LOGBOOK_NAME = "_logbook.csv"

FIELDS = [
    "ts_iso",
    "location",
    "seq",
    "class",
    "filename",
    "status",
    "frames",
    "duration_s",
    "planned_s",
    "delayed",
    "saturated",
    "calibration_needed",
    "temp_min",
    "temp_max",
    "config_sha256",
    "operator",
    "note",
    "error",
]


@dataclass
class LogRow:
    location: str
    seq: int
    cls: str
    filename: str
    status: str
    config_sha256: str
    ts_iso: str = ""
    frames: Optional[int] = None
    duration_s: Optional[float] = None
    planned_s: Optional[int] = None
    delayed: Optional[int] = None
    saturated: Optional[int] = None
    calibration_needed: Optional[int] = None
    temp_min: Optional[int] = None
    temp_max: Optional[int] = None
    operator: str = ""
    note: str = ""
    error: str = ""


class Logbook:
    def __init__(self, output_dir: Path) -> None:
        self.path = output_dir / LOGBOOK_NAME

    def append(self, row: LogRow) -> None:
        """Add one row, creating the file with a header if needed.

        fsync'd per row. Takes are seconds apart so the cost is irrelevant, and
        it means an abrupt shutdown cannot lose the record of what was just
        recorded.
        """
        row.ts_iso = row.ts_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")

        data = asdict(row)
        data["class"] = data.pop("cls")
        payload = {k: ("" if data.get(k) is None else data.get(k, "")) for k in FIELDS}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0

        with self.path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                writer.writeheader()
            writer.writerow(payload)
            fh.flush()
            os.fsync(fh.fileno())

    def read(self) -> List[dict]:
        if not self.path.exists():
            return []
        with self.path.open(newline="") as fh:
            return list(csv.DictReader(fh))

    def config_hashes(self) -> set:
        """Distinct config hashes seen. More than one means a split dataset."""
        return {r["config_sha256"] for r in self.read() if r.get("config_sha256")}
