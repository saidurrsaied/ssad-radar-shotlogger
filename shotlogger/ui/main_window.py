"""The Shot Logger window.

Workflow this supports:

  1. Pick the output folder, the plan CSV and the frozen config.
  2. Press Start watching.
  3. Record in the Exploration Tool as normal, pressing Start and Stop there.
     Never use its "Save to file" -- this tool takes the file instead.
  4. The take is verified, named from the plan, filed, and logged. The next
     shot appears on the card.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QMetaObject, Qt, QThread, QSettings, Signal, Slot
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import filing, frozen_config, logbook, plan
from ..watcher import Watcher
from .export_dialog import ExportDialog
from .manual_dialog import ManualDialog
from .model import PlanFilterProxy, PlanTableModel

ORG = "SSAD"
APP = "ShotLogger"


class PathRow(QWidget):
    """A label, a path field, a Browse button and a validity lamp."""

    changed = Signal()

    def __init__(self, label: str, directory: bool = False) -> None:
        super().__init__()
        self._directory = directory
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.lamp = QLabel("●")
        self.lamp.setFixedWidth(16)
        self.field = QLineEdit()
        self.field.setPlaceholderText(f"choose {label.lower()}…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)

        layout.addWidget(self.lamp)
        layout.addWidget(QLabel(label))
        layout.addWidget(self.field, 1)
        layout.addWidget(browse)

        self.field.textChanged.connect(self.changed)
        self.set_valid(False)

    def _browse(self) -> None:
        if self._directory:
            path = QFileDialog.getExistingDirectory(self, "Choose folder", self.field.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose file", self.field.text())
        if path:
            self.field.setText(path)

    def path(self) -> Optional[Path]:
        text = self.field.text().strip()
        return Path(text).expanduser() if text else None

    def set_valid(self, ok: bool, tooltip: str = "") -> None:
        self.lamp.setStyleSheet(f"color: {'#2e9e5b' if ok else '#b8503c'};")
        self.lamp.setToolTip(tooltip)


class MainWindow(QMainWindow):
    def __init__(self, temp_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle("Shot Logger")
        self.resize(1180, 800)

        self._temp_dir = temp_dir
        self._settings = QSettings(ORG, APP)
        self._plan: Optional[plan.WorkingPlan] = None
        self._frozen: Optional[frozen_config.FrozenConfig] = None
        self._logbook: Optional[logbook.Logbook] = None
        self._model: Optional[PlanTableModel] = None
        self._thread: Optional[QThread] = None
        self._watcher: Optional[Watcher] = None
        self._current: Optional[plan.ShotState] = None
        self._last_filed: Optional[plan.ShotState] = None
        self._manual: Optional[ManualDialog] = None

        self._build()
        self._restore()
        self._validate()

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        self._build_menus()

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        outer.addWidget(self._build_paths())
        outer.addWidget(self._build_card())
        outer.addWidget(self._build_filters())
        outer.addWidget(self._build_table(), 1)
        outer.addWidget(self._build_footer())

        self._progress = QProgressBar()
        self._progress.setFormat("%v / %m recorded")
        self.statusBar().addPermanentWidget(self._progress)
        self.statusBar().showMessage("Choose an output folder, a plan and the frozen config.")

    def _build_menus(self) -> None:
        session_menu = self.menuBar().addMenu("&Session")
        save_action = session_menu.addAction("&Save session to…")
        save_action.triggered.connect(self._export_session)
        session_menu.addSeparator()
        quit_action = session_menu.addAction("&Quit")
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)

        help_menu = self.menuBar().addMenu("&Help")
        manual_action = help_menu.addAction("&User guide")
        # F1 is where people reach for help without being told.
        manual_action.setShortcut(QKeySequence.StandardKey.HelpContents)
        manual_action.triggered.connect(self.show_manual)

    def show_manual(self) -> None:
        """Open the guide, reusing the window if it is already up."""
        if self._manual is None:
            self._manual = ManualDialog(self)
        self._manual.show()
        self._manual.raise_()
        self._manual.activateWindow()

    def _build_paths(self) -> QWidget:
        box = QGroupBox("Setup")
        grid = QGridLayout(box)

        self._out_row = PathRow("Output folder", directory=True)
        self._plan_row = PathRow("Plan CSV")
        self._cfg_row = PathRow("Frozen config")
        for i, row in enumerate((self._out_row, self._plan_row, self._cfg_row)):
            row.changed.connect(self._validate)
            grid.addWidget(row, i, 0)

        self._watch_button = QPushButton("Start watching")
        self._watch_button.setCheckable(True)
        self._watch_button.setMinimumHeight(64)
        self._watch_button.clicked.connect(self._toggle_watch)
        grid.addWidget(self._watch_button, 0, 1, 2, 1)

        self._export_button = QPushButton("Save session to…")
        self._export_button.setMinimumHeight(30)
        self._export_button.setToolTip(
            "Copy or move this session's recordings, working CSV and logbook "
            "to another folder."
        )
        self._export_button.clicked.connect(self._export_session)
        self._export_button.setEnabled(False)
        grid.addWidget(self._export_button, 2, 1)

        self._temp_label = QLabel(f"Exploration Tool records into:  {self._temp_dir}")
        self._temp_label.setStyleSheet("color: palette(mid);")
        grid.addWidget(self._temp_label, 3, 0)

        self._manual_button = QPushButton("User guide (F1)")
        self._manual_button.clicked.connect(self.show_manual)
        grid.addWidget(self._manual_button, 3, 1)
        return box

    def _build_card(self) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(card)

        eyebrow = QLabel("NEXT RECORDING")
        eyebrow.setStyleSheet("color: palette(mid); letter-spacing: 2px;")

        self._card_title = QLabel("—")
        title_font = QFont()
        title_font.setPointSize(26)
        title_font.setBold(True)
        self._card_title.setFont(title_font)
        self._card_title.setWordWrap(True)

        self._card_detail = QLabel("")
        detail_font = QFont()
        detail_font.setPointSize(13)
        self._card_detail.setFont(detail_font)

        self._card_note = QLabel("")
        self._card_note.setWordWrap(True)
        self._card_note.setStyleSheet("color: palette(mid);")

        layout.addWidget(eyebrow)
        layout.addWidget(self._card_title)
        layout.addWidget(self._card_detail)
        layout.addWidget(self._card_note)
        return card

    def _build_filters(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self._class_filter = QComboBox()
        self._class_filter.addItem("All classes", "")
        self._class_filter.currentIndexChanged.connect(
            lambda: self._proxy.set_class(self._class_filter.currentData())
        )
        self._remaining_only = QCheckBox("Remaining only")
        self._remaining_only.toggled.connect(lambda on: self._proxy.set_remaining_only(on))

        layout.addWidget(QLabel("Show:"))
        layout.addWidget(self._class_filter)
        layout.addWidget(self._remaining_only)
        layout.addStretch(1)
        self._counts_label = QLabel("")
        layout.addWidget(self._counts_label)
        return bar

    def _build_table(self) -> QWidget:
        self._table = QTableView()
        self._proxy = PlanFilterProxy()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._target_selected_row)
        self._table.verticalHeader().setVisible(False)
        return self._table

    def _build_footer(self) -> QWidget:
        box = QGroupBox("Last take")
        layout = QHBoxLayout(box)

        self._last_label = QLabel("Nothing recorded yet.")
        self._last_label.setWordWrap(True)

        self._note_field = QLineEdit()
        self._note_field.setPlaceholderText("operator note (optional)")

        target_button = QPushButton("Record selected row next")
        target_button.clicked.connect(self._target_selected_row)

        self._reject_button = QPushButton("Reject && delete")
        self._reject_button.setEnabled(False)
        self._reject_button.clicked.connect(self._reject_last)

        layout.addWidget(self._last_label, 1)
        layout.addWidget(self._note_field, 1)
        layout.addWidget(target_button)
        layout.addWidget(self._reject_button)
        return box

    # ------------------------------------------------------------- validation

    def _restore(self) -> None:
        for key, row in (
            ("output_dir", self._out_row),
            ("plan_csv", self._plan_row),
            ("config", self._cfg_row),
        ):
            value = self._settings.value(key, "")
            if value:
                row.field.setText(str(value))

    def _validate(self) -> None:
        out = self._out_row.path()
        plan_csv = self._plan_row.path()
        cfg = self._cfg_row.path()

        out_ok = out is not None and (out.is_dir() or not out.exists())
        self._out_row.set_valid(out_ok, "" if out_ok else "Not a usable folder")

        plan_ok = plan_csv is not None and plan_csv.is_file()
        self._plan_row.set_valid(plan_ok, "" if plan_ok else "Plan CSV not found")

        cfg_ok = False
        cfg_msg = "Frozen config not found"
        if cfg is not None and cfg.is_file():
            try:
                self._frozen = frozen_config.load(cfg)
                cfg_ok = True
                cfg_msg = f"sha256 {self._frozen.short_sha}"
            except frozen_config.FrozenConfigError as exc:
                cfg_msg = str(exc)
        self._cfg_row.set_valid(cfg_ok, cfg_msg)

        has_session = bool(
            out_ok and out is not None and (out / plan.WORKING_CSV_NAME).is_file()
        )
        self._export_button.setEnabled(has_session)

        ready = bool(out_ok and plan_ok and cfg_ok)
        self._watch_button.setEnabled(ready)
        if not ready and self._watch_button.isChecked():
            self._watch_button.setChecked(False)
            self._stop_watching()
        return ready

    # ------------------------------------------------------------ export

    def _export_session(self) -> None:
        source = self._out_row.path()
        if source is None or not source.is_dir():
            return

        if self._thread is not None:
            answer = QMessageBox.question(
                self,
                "Stop watching first?",
                "Watching must stop before the session can be saved elsewhere, so a "
                "take cannot arrive mid-transfer.\n\nStop watching now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._watch_button.setChecked(False)
            self._stop_watching()

        dialog = ExportDialog(source, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_info is None:
            return

        result = dialog.result_info
        destination = result.plan.destination

        if result.moved:
            # Continue against the new location rather than leaving the window
            # pointed at a folder whose recordings have gone.
            self._out_row.field.setText(str(destination))
            self._validate()
            try:
                self._plan = plan.WorkingPlan.load(self._plan_row.path(), destination)
                self._logbook = logbook.Logbook(destination)
                self._install_model()
            except plan.PlanError as exc:
                QMessageBox.warning(self, "Session moved, but could not reload", str(exc))

        verb = "Moved" if result.moved else "Copied"
        QMessageBox.information(
            self,
            "Session saved",
            f"{verb} {result.transferred} file(s) to:\n{destination}"
            + ("\n\nThe window now continues against the new folder." if result.moved else ""),
        )
        self.statusBar().showMessage(f"{verb} session to {destination}", 8000)

    # ---------------------------------------------------------------- watching

    def _toggle_watch(self, checked: bool) -> None:
        if checked:
            if not self._start_watching():
                self._watch_button.setChecked(False)
        else:
            self._stop_watching()

    def _start_watching(self) -> bool:
        if not self._validate():
            return False

        out = self._out_row.path()
        try:
            self._plan = plan.WorkingPlan.load(self._plan_row.path(), out)
        except plan.PlanError as exc:
            QMessageBox.critical(self, "Plan could not be loaded", str(exc))
            return False

        notes = self._plan.reconcile_with_disk()
        orphans = self._plan.orphan_files()
        if notes or orphans:
            lines = list(notes)
            if orphans:
                lines.append(
                    f"{len(orphans)} recording(s) in the output folder are not in the "
                    f"plan, e.g. {orphans[0].name}"
                )
            QMessageBox.information(self, "Reconciled with disk", "\n".join(lines))

        self._logbook = logbook.Logbook(out)
        self._install_model()
        self._save_settings()

        self._thread = QThread(self)
        self._watcher = Watcher(self._temp_dir)
        self._watcher.moveToThread(self._thread)
        self._thread.started.connect(self._watcher.start)
        self._watcher.inspected.connect(self._on_inspected)
        self._watcher.started.connect(self._on_watch_started)
        self._watcher.failed.connect(self._on_watch_failed)
        self._thread.start()

        self._watch_button.setText("Stop watching")
        return True

    def _stop_watching(self) -> None:
        if self._watcher is not None and self._thread is not None and self._thread.isRunning():
            # The poll QTimer was created on the worker thread and can only be
            # stopped there. Calling stop() directly from here would leave Qt
            # complaining that timers cannot be stopped from another thread,
            # and the timer would outlive its thread.
            QMetaObject.invokeMethod(
                self._watcher, "stop", Qt.ConnectionType.BlockingQueuedConnection
            )
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = None
        self._watcher = None
        self._watch_button.setText("Start watching")
        self.statusBar().showMessage("Not watching.")

    @Slot(object)
    def _on_watch_started(self, preexisting) -> None:
        self.statusBar().showMessage(f"Watching {self._temp_dir}")
        if preexisting:
            QMessageBox.warning(
                self,
                "Unclaimed recordings already present",
                f"{len(preexisting)} recording(s) were already in the Exploration Tool's "
                f"temp folder before watching started, and will be ignored.\n\n"
                f"That usually means someone used “Save to file” as well, or a "
                f"previous session was not filed. Clear them out if they are not needed.",
            )

    @Slot(str)
    def _on_watch_failed(self, message: str) -> None:
        self._watch_button.setChecked(False)
        self._stop_watching()
        QMessageBox.critical(self, "Cannot watch", message)

    # -------------------------------------------------------------- the model

    def _install_model(self) -> None:
        self._model = PlanTableModel(self._plan)
        self._proxy.setSourceModel(self._model)
        self._table.resizeColumnsToContents()

        self._class_filter.blockSignals(True)
        self._class_filter.clear()
        self._class_filter.addItem("All classes", "")
        for cls in sorted({s.shot.cls for s in self._plan}):
            self._class_filter.addItem(cls, cls)
        self._class_filter.blockSignals(False)

        self._advance()

    def _advance(self, after: Optional[int] = None) -> None:
        if self._plan is None:
            return
        self._current = self._plan.next_pending(after=after)
        self._model.set_next(self._current.shot.seq if self._current else None)
        self._refresh_card()
        self._refresh_counts()

    def _refresh_card(self) -> None:
        if self._current is None:
            self._card_title.setText("All shots recorded")
            self._card_detail.setText("Nothing left in this plan.")
            self._card_note.setText("")
            return

        shot = self._current.shot
        self._card_title.setText(shot.describe())
        self._card_detail.setText(f"{shot.filename}    ·    record for {shot.seconds} s")
        self._card_note.setText(shot.note)

    def _refresh_counts(self) -> None:
        if self._plan is None:
            return
        counts = self._plan.counts()
        self._progress.setMaximum(counts["total"])
        self._progress.setValue(counts["recorded"])
        remaining = self._plan.remaining_seconds() / 60
        self._counts_label.setText(
            f"{counts['recorded']} of {counts['total']} recorded "
            f"· {remaining:.0f} min of beam-on left"
        )

    def _target_selected_row(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows or self._plan is None:
            return
        state = self._model.state_at(self._proxy.mapToSource(rows[0]).row())
        if state.status is plan.Status.RECORDED:
            QMessageBox.information(
                self,
                "Already recorded",
                f"{state.shot.filename} is already recorded. Reject it first if you want "
                f"to record it again.",
            )
            return
        self._current = state
        self._model.set_next(state.shot.seq)
        self._refresh_card()

    # ----------------------------------------------------------- filing takes

    @Slot(object, object, str)
    def _on_inspected(self, path, info, error) -> None:
        path = Path(str(path))

        if self._plan is None or self._frozen is None:
            return

        if self._current is None:
            QMessageBox.information(
                self,
                "Nothing left to record",
                f"A recording appeared ({path.name}) but every shot in this plan is done. "
                f"It has been left where it is.",
            )
            return

        if error:
            self._log(self._current, "failed", None, error=error)
            QMessageBox.warning(self, "Unreadable recording", error)
            return

        state = self._current
        result = filing.file_take(path, state, self._plan, self._frozen)

        if not result.ok:
            self._log(state, "failed", info, error=result.message)
            if result.outcome is filing.Outcome.CONFIG_MISMATCH:
                self._watch_button.setChecked(False)
                self._stop_watching()
                QMessageBox.critical(self, "Wrong sensor configuration", result.message)
            else:
                QMessageBox.warning(self, "Not filed", result.message)
            return

        self._plan.mark_recorded(
            state.shot.seq,
            actual_seconds=info.duration_s,
            frames=info.num_frames,
            delayed=info.delayed,
            saturated=info.saturated,
            operator_note=self._note_field.text().strip(),
        )
        self._log(state, "completed", info)

        warnings = filing.warnings_for(info, state)
        summary = (
            f"{state.shot.filename}  ·  {info.num_frames} frames  ·  "
            f"{info.duration_s:.1f} s (planned {state.shot.seconds} s)"
        )
        if warnings:
            summary += "\n⚠  " + "; ".join(warnings)
        self._last_label.setText(summary)

        self._last_filed = state
        self._reject_button.setEnabled(True)
        self._note_field.clear()
        self._advance(after=state.shot.seq)
        self.statusBar().showMessage(f"Filed {state.shot.filename}", 8000)

    def _reject_last(self) -> None:
        if self._plan is None or self._last_filed is None:
            return
        state = self._last_filed
        destination = self._plan.final_path(state.shot)

        confirm = QMessageBox.question(
            self,
            "Delete this take?",
            f"{destination.name} will be deleted and the shot reopened.\n\n"
            f"The rejection and its reason stay in the logbook, but the recording "
            f"itself cannot be recovered.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        note = self._note_field.text().strip()
        try:
            if destination.exists():
                destination.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "Could not delete", str(exc))
            return

        self._plan.mark_rejected(state.shot.seq, operator_note=note)
        self._log(state, "rejected", None, note=note)

        self._last_filed = None
        self._reject_button.setEnabled(False)
        self._last_label.setText(f"Rejected {state.shot.filename}. Record it again.")
        self._note_field.clear()
        self._current = state
        self._model.set_next(state.shot.seq)
        self._refresh_card()
        self._refresh_counts()

    def _log(self, state, status, info, *, note: str = "", error: str = "") -> None:
        if self._logbook is None or self._frozen is None:
            return
        self._logbook.append(
            logbook.LogRow(
                location=self._plan.location,
                seq=state.shot.seq,
                cls=state.shot.cls,
                filename=state.shot.filename,
                status=status,
                config_sha256=self._frozen.sha256,
                frames=None if info is None else info.num_frames,
                duration_s=None if info is None else round(info.duration_s, 2),
                planned_s=state.shot.seconds,
                delayed=None if info is None else info.delayed,
                saturated=None if info is None else info.saturated,
                calibration_needed=None if info is None else info.calibration_needed,
                temp_min=None if info is None else info.temp_min,
                temp_max=None if info is None else info.temp_max,
                note=note or self._note_field.text().strip(),
                error=error,
            )
        )

    # ------------------------------------------------------------- lifecycle

    def _save_settings(self) -> None:
        self._settings.setValue("output_dir", str(self._out_row.path() or ""))
        self._settings.setValue("plan_csv", str(self._plan_row.path() or ""))
        self._settings.setValue("config", str(self._cfg_row.path() or ""))

    def closeEvent(self, event) -> None:
        self._stop_watching()
        self._save_settings()
        super().closeEvent(event)


def free_space_note(output_dir: Path, bytes_per_second: float, seconds_left: int) -> Optional[str]:
    """Warn when the remaining shots will not fit. None when there is room."""
    try:
        free = shutil.disk_usage(output_dir).free
    except OSError:
        return None
    needed = bytes_per_second * seconds_left
    if needed and free < needed * 1.3:
        return (
            f"About {needed / 1e9:.2f} GB is still to be recorded but only "
            f"{free / 1e9:.2f} GB is free. Free some space before starting."
        )
    return None
