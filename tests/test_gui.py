#!/usr/bin/env python3
"""GUI smoke test. Runs headless under QT_QPA_PLATFORM=offscreen.

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest tests.test_gui -v

Drives the real window against a fake Exploration Tool temp directory: a
recording is produced with the mock client, dropped into the watched folder,
and the window is expected to file it under the planned name with no dialogs.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from shotlogger import plan  # noqa: E402
from shotlogger.ui.export_dialog import ExportDialog  # noqa: E402
from shotlogger.ui.main_window import MainWindow, free_space_note  # noqa: E402
from tests.test_shotlogger import CONFIG_PATH, PLAN_CSV, make_recording  # noqa: E402
from shotlogger import frozen_config  # noqa: E402

_app = QApplication.instance() or QApplication([])


def pump(ms: int) -> None:
    """Run the event loop for a while without blocking the test process."""
    QTimer.singleShot(ms, _app.quit)
    _app.exec()


class TestMainWindow(unittest.TestCase):
    def setUp(self):
        QSettings("SSAD", "ShotLogger").clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="shotlogger-gui-"))
        self.out = self.tmp / "out"
        self.temp_dir = self.tmp / "ettemp"
        self.temp_dir.mkdir(parents=True)
        self.frozen = frozen_config.load(CONFIG_PATH)

        self.win = MainWindow(self.temp_dir)
        self.win._out_row.field.setText(str(self.out))
        self.win._plan_row.field.setText(str(PLAN_CSV))
        self.win._cfg_row.field.setText(str(CONFIG_PATH))

    def tearDown(self):
        self.win._stop_watching()
        self.win.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paths_validate_and_enable_watching(self):
        self.assertTrue(self.win._validate())
        self.assertTrue(self.win._watch_button.isEnabled())

    def test_invalid_config_blocks_watching(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{}")
        self.win._cfg_row.field.setText(str(bad))
        self.assertFalse(self.win._validate())
        self.assertFalse(self.win._watch_button.isEnabled())

    def test_files_a_take_end_to_end(self):
        self.assertTrue(self.win._start_watching())
        self.assertIsNotNone(self.win._current)
        first = self.win._current.shot
        self.assertEqual(first.seq, 1)

        # Let the watcher take its snapshot of the (empty) temp dir.
        pump(300)

        make_recording(self.temp_dir / "take.h5", self.frozen.session_config, frames=30)

        # Two polls to settle, plus slack for the queued signal.
        pump(2500)

        destination = self.out / first.cls / first.filename
        self.assertTrue(destination.exists(), "take was not filed")

        reloaded = plan.WorkingPlan.load(PLAN_CSV, self.out)
        self.assertIs(reloaded.by_seq(1).status, plan.Status.RECORDED)
        self.assertEqual(reloaded.by_seq(1).frames, 30)

        rows = self.win._logbook.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["config_sha256"], self.frozen.sha256)

        # The card must have moved on.
        self.assertEqual(self.win._current.shot.seq, 2)

    def test_thread_exits_cleanly(self):
        self.assertTrue(self.win._start_watching())
        pump(300)
        thread = self.win._thread
        self.win._stop_watching()
        self.assertTrue(thread.isFinished() or thread.wait(2000))
        self.assertIsNone(self.win._thread)


class TestExportDialog(unittest.TestCase):
    def setUp(self):
        QSettings("SSAD", "ShotLogger").clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="shotlogger-exportgui-"))
        self.session = self.tmp / "session"
        self.dest = self.tmp / "archive"
        self.frozen = frozen_config.load(CONFIG_PATH)

        p = plan.WorkingPlan.load(PLAN_CSV, self.session)
        for _ in range(2):
            state = p.next_pending()
            target = p.final_path(state.shot)
            target.parent.mkdir(parents=True, exist_ok=True)
            make_recording(target, self.frozen.session_config, frames=5)
            p.mark_recorded(
                state.shot.seq, actual_seconds=1.0, frames=5, delayed=0, saturated=0
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_button_enables_once_a_session_exists(self):
        win = MainWindow(self.tmp / "ettemp")
        try:
            win._out_row.field.setText(str(self.tmp / "nothing-here"))
            win._validate()
            self.assertFalse(win._export_button.isEnabled())

            win._out_row.field.setText(str(self.session))
            win._validate()
            self.assertTrue(win._export_button.isEnabled())
        finally:
            win.close()

    def test_dialog_previews_then_copies(self):
        dialog = ExportDialog(self.session)
        try:
            ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.assertFalse(ok.isEnabled(), "Save must be off until a destination is set")

            dialog._dest.setText(str(self.dest))
            self.assertTrue(ok.isEnabled())
            self.assertIn("2 recording(s)", dialog._summary.text())

            dialog._run()
            self.assertIsNotNone(dialog.result_info)
            self.assertEqual(len(list(self.dest.rglob("*.h5"))), 2)
            self.assertTrue(list(self.session.rglob("*.h5")), "copy must keep originals")
        finally:
            dialog.close()

    def test_dialog_refuses_nested_destination(self):
        dialog = ExportDialog(self.session)
        try:
            dialog._dest.setText(str(self.session / "inner"))
            ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.assertFalse(ok.isEnabled())
            self.assertIn("inside the session folder", dialog._summary.text())
        finally:
            dialog.close()


class TestDiskCheck(unittest.TestCase):
    def test_reports_when_space_is_short(self):
        note = free_space_note(Path.cwd(), bytes_per_second=1e12, seconds_left=10_000)
        self.assertIsNotNone(note)
        self.assertIn("GB", note)

    def test_silent_when_there_is_room(self):
        self.assertIsNone(free_space_note(Path.cwd(), bytes_per_second=1.0, seconds_left=1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
