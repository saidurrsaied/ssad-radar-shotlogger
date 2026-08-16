#!/usr/bin/env python3
"""Tests for exporting a session to another folder.

    .venv/bin/python -m unittest tests.test_export -v

The interesting cases are all the refusals: an export that half-succeeds is
worse than one that never starts, because it leaves recordings split across two
folders with no record of which is authoritative.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shotlogger import export, frozen_config, logbook, plan  # noqa: E402
from tests.test_shotlogger import CONFIG_PATH, PLAN_CSV, make_recording  # noqa: E402


class ExportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shotlogger-export-"))
        self.session = self.tmp / "session"
        self.dest = self.tmp / "archive"
        self.frozen = frozen_config.load(CONFIG_PATH)
        self.plan = plan.WorkingPlan.load(PLAN_CSV, self.session)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, count: int = 3):
        """Produce `count` filed takes plus logbook rows."""
        book = logbook.Logbook(self.session)
        for _ in range(count):
            state = self.plan.next_pending()
            destination = self.plan.final_path(state.shot)
            destination.parent.mkdir(parents=True, exist_ok=True)
            make_recording(destination, self.frozen.session_config, frames=5)
            self.plan.mark_recorded(
                state.shot.seq, actual_seconds=1.0, frames=5, delayed=0, saturated=0
            )
            book.append(
                logbook.LogRow(
                    location=self.plan.location,
                    seq=state.shot.seq,
                    cls=state.shot.cls,
                    filename=state.shot.filename,
                    status="completed",
                    config_sha256=self.frozen.sha256,
                )
            )


class TestPrepare(ExportBase):
    def test_lists_recordings_and_sidecars(self):
        self.record(3)
        p = export.prepare(self.session, self.dest)
        self.assertEqual(len(p.recordings), 3)
        names = {s.name for s in p.sidecars}
        self.assertIn(plan.WORKING_CSV_NAME, names)
        self.assertIn(logbook.LOGBOOK_NAME, names)
        self.assertGreater(p.total_bytes, 0)

    def test_refuses_destination_inside_session(self):
        self.record(1)
        with self.assertRaises(export.ExportError) as ctx:
            export.prepare(self.session, self.session / "inner")
        self.assertIn("inside the session folder", str(ctx.exception))

    def test_refuses_session_inside_destination(self):
        self.record(1)
        with self.assertRaises(export.ExportError):
            export.prepare(self.session, self.tmp)

    def test_refuses_same_folder(self):
        self.record(1)
        with self.assertRaises(export.ExportError):
            export.prepare(self.session, self.session)

    def test_refuses_destination_holding_a_session(self):
        self.record(1)
        self.dest.mkdir(parents=True)
        (self.dest / plan.WORKING_CSV_NAME).write_text("seq\n1\n")
        with self.assertRaises(export.ExportError) as ctx:
            export.prepare(self.session, self.dest)
        self.assertIn("already holds a recording session", str(ctx.exception))

    def test_refuses_when_a_recording_would_be_overwritten(self):
        self.record(2)
        first = next(s for s in self.plan if s.status is plan.Status.RECORDED)
        clash = self.dest / first.shot.cls / first.shot.filename
        clash.parent.mkdir(parents=True)
        clash.write_bytes(b"existing")

        with self.assertRaises(export.ExportError) as ctx:
            export.prepare(self.session, self.dest)
        self.assertIn("already exist", str(ctx.exception))
        self.assertEqual(clash.read_bytes(), b"existing")

    def test_refuses_empty_session(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(export.ExportError):
            export.prepare(empty, self.dest)


class TestRun(ExportBase):
    def test_copy_leaves_originals(self):
        self.record(3)
        p = export.prepare(self.session, self.dest)
        result = export.run(p, move=False)

        self.assertEqual(result.transferred, p.file_count)
        self.assertFalse(result.moved)
        for source_path in p.recordings:
            self.assertTrue(source_path.exists(), "copy must not remove the original")
            relative = source_path.relative_to(self.session)
            self.assertTrue((self.dest / relative).exists())

    def test_copy_preserves_class_folders(self):
        self.record(3)
        export.run(export.prepare(self.session, self.dest), move=False)
        originals = sorted(p.relative_to(self.session) for p in self.session.rglob("*.h5"))
        copies = sorted(p.relative_to(self.dest) for p in self.dest.rglob("*.h5"))
        self.assertEqual(originals, copies)

    def test_copied_recordings_are_still_valid(self):
        self.record(2)
        export.run(export.prepare(self.session, self.dest), move=False)
        from shotlogger import inspection

        for copied in self.dest.rglob("*.h5"):
            info = inspection.inspect_record(copied)
            self.assertEqual(info.num_frames, 5)
            self.assertTrue(self.frozen.matches(info.session_config))

    def test_move_relocates_and_the_plan_reloads(self):
        self.record(4)
        p = export.prepare(self.session, self.dest)
        export.run(p, move=True)

        self.assertEqual(list(self.session.rglob("*.h5")), [])
        self.assertEqual(len(list(self.dest.rglob("*.h5"))), 4)

        # The whole point of moving: work continues against the new folder.
        moved_plan = plan.WorkingPlan.load(PLAN_CSV, self.dest)
        self.assertEqual(moved_plan.counts()["recorded"], 4)
        self.assertEqual(moved_plan.next_pending().shot.seq, 5)

    def test_move_does_not_strand_progress_in_the_old_folder(self):
        self.record(3)
        export.run(export.prepare(self.session, self.dest), move=True)
        # Reloading the emptied folder must not claim recordings still exist.
        stale = plan.WorkingPlan.load(PLAN_CSV, self.session)
        self.assertEqual(stale.counts()["recorded"], 0)

    def test_progress_callback_reports_every_file(self):
        self.record(2)
        p = export.prepare(self.session, self.dest)
        seen = []
        export.run(p, move=False, progress=lambda done, total, name: seen.append(done))
        self.assertEqual(seen, list(range(1, p.file_count + 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
