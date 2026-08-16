"""Table model over the working plan.

Colour carries status so the operator can find where they are at a glance from
across the room, rather than reading a column: done rows recede, the next row
is highlighted, and anything with a quality warning is amber.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor, QFont

from ..plan import ShotState, Status, WorkingPlan

COLUMNS = [
    ("#", "seq"),
    ("Class", "cls"),
    ("Geometry", "geometry"),
    ("Dist", "distance"),
    ("Speed", "speed"),
    ("Subj", "subject"),
    ("Take", "take"),
    ("Filename", "filename"),
    ("Plan s", "seconds"),
    ("Actual s", "actual"),
    ("Status", "status"),
    ("QC", "qc"),
]

_DONE_FG = QColor(130, 140, 148)
_NEXT_BG = QColor(46, 108, 164, 60)
_WARN_FG = QColor(190, 130, 20)
_BAD_FG = QColor(180, 60, 45)


class PlanTableModel(QAbstractTableModel):
    def __init__(self, plan: WorkingPlan) -> None:
        super().__init__()
        self._plan = plan
        self._next_seq: Optional[int] = None

    # ------------------------------------------------------------ Qt plumbing

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._plan)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section][0]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        state = self._plan.states[index.row()]
        key = COLUMNS[index.column()][1]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(state, key)

        if role == Qt.ItemDataRole.ForegroundRole:
            if key == "qc" and self._qc_severity(state) == 2:
                return QBrush(_BAD_FG)
            if key == "qc" and self._qc_severity(state) == 1:
                return QBrush(_WARN_FG)
            if state.status is Status.RECORDED:
                return QBrush(_DONE_FG)

        if role == Qt.ItemDataRole.BackgroundRole:
            if state.shot.seq == self._next_seq:
                return QBrush(_NEXT_BG)

        if role == Qt.ItemDataRole.FontRole and state.shot.seq == self._next_seq:
            font = QFont()
            font.setBold(True)
            return font

        if role == Qt.ItemDataRole.TextAlignmentRole and key in {
            "seq", "take", "seconds", "actual",
        }:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ToolTipRole and state.shot.note:
            return state.shot.note

        return None

    # --------------------------------------------------------------- contents

    @staticmethod
    def _text(state: ShotState, key: str) -> str:
        shot = state.shot
        if key == "actual":
            return "" if state.actual_seconds is None else f"{state.actual_seconds:.1f}"
        if key == "status":
            return state.status.value
        if key == "qc":
            return PlanTableModel._qc_text(state)
        value = getattr(shot, key, "")
        return "" if value is None else str(value)

    @staticmethod
    def _qc_text(state: ShotState) -> str:
        if state.status is not Status.RECORDED:
            return ""
        bits: List[str] = []
        if state.delayed:
            bits.append(f"{state.delayed} delayed")
        if state.saturated:
            bits.append(f"{state.saturated} sat")
        if state.duration_ok is False:
            bits.append("duration")
        return ", ".join(bits) or "ok"

    @staticmethod
    def _qc_severity(state: ShotState) -> int:
        """0 fine, 1 worth a look, 2 probably needs re-recording."""
        if state.status is not Status.RECORDED:
            return 0
        if state.frames and state.delayed and state.delayed / state.frames > 0.2:
            return 2
        if state.duration_ok is False:
            return 2
        if state.delayed or state.saturated:
            return 1
        return 0

    # ---------------------------------------------------------------- updates

    def set_next(self, seq: Optional[int]) -> None:
        self._next_seq = seq
        self.refresh()

    def refresh(self) -> None:
        if len(self._plan):
            top = self.index(0, 0)
            bottom = self.index(len(self._plan) - 1, len(COLUMNS) - 1)
            self.dataChanged.emit(top, bottom)

    def state_at(self, row: int) -> ShotState:
        return self._plan.states[row]


class PlanFilterProxy(QSortFilterProxyModel):
    """Filter by class and by 'remaining only'."""

    def __init__(self) -> None:
        super().__init__()
        self._cls = ""
        self._remaining_only = False

    def set_class(self, cls: str) -> None:
        self._cls = cls
        self.invalidateFilter()

    def set_remaining_only(self, on: bool) -> None:
        self._remaining_only = on
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model = self.sourceModel()
        state = model.state_at(row)
        if self._cls and state.shot.cls != self._cls:
            return False
        if self._remaining_only and state.status is Status.RECORDED:
            return False
        return True
