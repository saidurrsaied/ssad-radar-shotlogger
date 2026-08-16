"""Dialog for exporting a session to another folder."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from .. import export


class ExportDialog(QDialog):
    """Pick a destination, choose copy or move, and run the transfer."""

    def __init__(self, source: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save session to…")
        self.setMinimumWidth(560)

        self._source = source
        self._plan: Optional[export.ExportPlan] = None
        self.result_info: Optional[export.ExportResult] = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"Session folder:\n{source}"))

        row = QHBoxLayout()
        self._dest = QLineEdit()
        self._dest.setPlaceholderText("destination folder…")
        self._dest.textChanged.connect(self._refresh)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(QLabel("Save to:"))
        row.addWidget(self._dest, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        self._copy = QRadioButton("Copy — leave the session here as well")
        self._copy.setChecked(True)
        self._move = QRadioButton("Move — relocate the session and continue there")
        layout.addWidget(self._copy)
        layout.addWidget(self._move)

        self._summary = QLabel("Choose a destination.")
        self._summary.setWordWrap(True)
        self._summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._summary)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        self._buttons.accepted.connect(self._run)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._set_enabled(False)

    # ------------------------------------------------------------------ input

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Save session to", self._dest.text())
        if path:
            self._dest.setText(path)

    def destination(self) -> Optional[Path]:
        text = self._dest.text().strip()
        return Path(text).expanduser() if text else None

    def moved(self) -> bool:
        return self._move.isChecked()

    def _set_enabled(self, ok: bool) -> None:
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def _refresh(self) -> None:
        destination = self.destination()
        if destination is None:
            self._summary.setText("Choose a destination.")
            self._set_enabled(False)
            self._plan = None
            return

        try:
            self._plan = export.prepare(self._source, destination)
        except export.ExportError as exc:
            self._plan = None
            self._summary.setText(str(exc))
            self._set_enabled(False)
            return

        self._summary.setText("Will transfer:\n" + self._plan.describe())
        self._set_enabled(True)

    # -------------------------------------------------------------- execution

    def _run(self) -> None:
        # Re-check rather than trusting the preview: the destination may have
        # gained files while the dialog sat open.
        try:
            plan = export.prepare(self._source, self.destination())
        except export.ExportError as exc:
            self._summary.setText(str(exc))
            self._set_enabled(False)
            return

        self._progress.setVisible(True)
        self._progress.setMaximum(plan.file_count)
        self._set_enabled(False)

        def tick(done: int, total: int, name: str) -> None:
            self._progress.setValue(done)
            self._summary.setText(f"Copying {name}…")
            self._progress.repaint()

        try:
            self.result_info = export.run(plan, move=self.moved(), progress=tick)
        except OSError as exc:
            self._summary.setText(f"Export failed: {exc}")
            self._progress.setVisible(False)
            self._set_enabled(True)
            return

        self.accept()
