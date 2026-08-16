#!/usr/bin/env python3
"""Headless tests for the Shot Logger. No hardware, no GUI.

    .venv/bin/python -m unittest discover -s tests -v

Recordings are produced with a121.Client.open(mock=True), which is the same
code path the Exploration Tool uses minus the sensor.
"""

import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acconeer.exptool import a121  # noqa: E402

from shotlogger import filing, frozen_config, inspection, logbook, plan  # noqa: E402

CONFIG_PATH = ROOT / "config" / "session_config.json"
PLAN_CSV = ROOT / "plans" / "locA_indoor_3wall.csv"


def make_recording(path: Path, session_config: a121.SessionConfig, frames: int = 20) -> Path:
    """Write a real A121 HDF5 recording using the mock client."""
    client = a121.Client.open(mock=True)
    try:
        client.setup_session(session_config)
        recorder = a121.H5Recorder(path, client)
        client.start_session()
        for _ in range(frames):
            client.get_next()
        client.stop_session()
        recorder.close()
    finally:
        client.close()
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shotlogger-test-"))
        self.out = self.tmp / "out"
        self.temp_dir = self.tmp / "ettemp"
        self.temp_dir.mkdir(parents=True)
        self.frozen = frozen_config.load(CONFIG_PATH)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load_plan(self):
        return plan.WorkingPlan.load(PLAN_CSV, self.out)


class TestFrozenConfig(Base):
    def test_loads_and_hashes(self):
        self.assertTrue(self.frozen.sha256)
        self.assertEqual(frozen_config.expected_frame_rate(self.frozen), 25.0)

    def test_rejects_missing(self):
        with self.assertRaises(frozen_config.FrozenConfigError):
            frozen_config.load(self.tmp / "nope.json")

    def test_rejects_hand_edited_file(self):
        # Reordering keys parses fine but no longer round-trips, which is
        # exactly the silent-drift case the guard exists for.
        bad = self.tmp / "bad.json"
        bad.write_text(" " + CONFIG_PATH.read_text())
        with self.assertRaises(frozen_config.FrozenConfigError):
            frozen_config.load(bad)


class TestPlan(Base):
    def test_creates_working_copy(self):
        p = self.load_plan()
        self.assertTrue((self.out / plan.WORKING_CSV_NAME).exists())
        self.assertEqual(len(p), 100)
        self.assertEqual(p.location, "locA_indoor_3wall")
        self.assertEqual(p.counts()["pending"], 100)

    def test_source_plan_not_mutated(self):
        before = PLAN_CSV.read_bytes()
        p = self.load_plan()
        p.mark_recorded(1, actual_seconds=60, frames=1500, delayed=0, saturated=0)
        self.assertEqual(PLAN_CSV.read_bytes(), before)

    def test_next_pending_advances_forward(self):
        p = self.load_plan()
        self.assertEqual(p.next_pending().shot.seq, 1)
        p.mark_recorded(1, actual_seconds=60, frames=1500, delayed=0, saturated=0)
        self.assertEqual(p.next_pending(after=1).shot.seq, 2)
        # A shot reopened earlier in the list must NOT yank the operator
        # backwards mid-session; they are working forwards and can click the
        # row explicitly to redo it.
        p.mark_rejected(1)
        self.assertEqual(p.next_pending(after=50).shot.seq, 51)

    def test_next_pending_wraps_when_tail_is_done(self):
        p = self.load_plan()
        for state in list(p)[1:]:
            p.mark_recorded(
                state.shot.seq, actual_seconds=state.shot.seconds,
                frames=10, delayed=0, saturated=0,
            )
        # Only seq 1 is left, behind the cursor: it must still be offered.
        self.assertEqual(p.next_pending(after=99).shot.seq, 1)

    def test_progress_survives_reload(self):
        p = self.load_plan()
        p.mark_recorded(3, actual_seconds=31.0, frames=775, delayed=2, saturated=0)
        # A recorded row needs its file present or reconcile resets it.
        dest = p.final_path(p.by_seq(3).shot)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")

        again = self.load_plan()
        state = again.by_seq(3)
        self.assertIs(state.status, plan.Status.RECORDED)
        self.assertEqual(state.frames, 775)
        self.assertEqual(state.delayed, 2)

    def test_reconcile_resets_rows_whose_file_vanished(self):
        p = self.load_plan()
        p.mark_recorded(5, actual_seconds=30, frames=750, delayed=0, saturated=0)
        again = self.load_plan()  # file was never created
        self.assertIs(again.by_seq(5).status, plan.Status.PENDING)

    def test_duplicate_filenames_rejected(self):
        working = self.out
        working.mkdir(parents=True)
        with PLAN_CSV.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        rows[1]["filename"] = rows[0]["filename"]
        with (working / plan.WORKING_CSV_NAME).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        with self.assertRaises(plan.PlanError):
            self.load_plan()

    def test_save_is_atomic(self):
        p = self.load_plan()
        p.mark_recorded(1, actual_seconds=60, frames=1500, delayed=0, saturated=0)
        self.assertFalse(list(self.out.glob("*.tmp")), "temp file left behind")
        # Still parseable after many rewrites.
        for seq in range(2, 12):
            p.mark_recorded(seq, actual_seconds=60, frames=1500, delayed=0, saturated=0)
        reread = plan.WorkingPlan._read(p.path, self.out)
        self.assertEqual(len(reread), 100)

    def test_duration_tolerance(self):
        p = self.load_plan()
        state = p.by_seq(1)  # planned 60 s
        state.actual_seconds = 58.0
        self.assertTrue(state.duration_ok)
        state.actual_seconds = 20.0
        self.assertFalse(state.duration_ok)


class TestInspection(Base):
    def test_reads_counts_without_frames(self):
        path = self.tmp / "rec.h5"
        make_recording(path, self.frozen.session_config, frames=25)
        info = inspection.inspect_record(path)
        self.assertEqual(info.num_frames, 25)
        self.assertEqual(info.delayed, 0)
        self.assertTrue(self.frozen.matches(info.session_config))
        self.assertGreater(info.duration_s, 0)

    def test_rejects_non_recording(self):
        junk = self.tmp / "junk.h5"
        junk.write_bytes(b"not an hdf5 file")
        with self.assertRaises(inspection.InspectionError):
            inspection.inspect_record(junk)


class TestFiling(Base):
    def test_files_under_planned_name(self):
        p = self.load_plan()
        state = p.next_pending()
        src = self.temp_dir / "abc.h5"
        make_recording(src, self.frozen.session_config, frames=25)

        result = filing.file_take(src, state, p, self.frozen)
        self.assertIs(result.outcome, filing.Outcome.FILED)
        self.assertTrue(result.destination.exists())
        self.assertEqual(result.destination.name, state.shot.filename)
        self.assertEqual(result.destination.parent.name, state.shot.cls)
        self.assertFalse(src.exists(), "source should have been moved")

    def test_config_mismatch_is_refused_and_file_left_alone(self):
        p = self.load_plan()
        state = p.next_pending()
        # Valid, but not the frozen one: fewer sweeps and a shorter range.
        other = a121.SessionConfig(
            a121.SensorConfig(
                sweeps_per_frame=32, sweep_rate=1000.0, frame_rate=10.0,
                start_point=400, num_points=14, step_length=96,
            ),
            update_rate=None,
        )
        src = self.temp_dir / "wrong.h5"
        make_recording(src, other, frames=10)

        result = filing.file_take(src, state, p, self.frozen)
        self.assertIs(result.outcome, filing.Outcome.CONFIG_MISMATCH)
        self.assertTrue(src.exists(), "refused file must stay recoverable")
        self.assertFalse(p.final_path(state.shot).exists())

    def test_never_overwrites(self):
        p = self.load_plan()
        state = p.next_pending()
        dest = p.final_path(state.shot)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"precious")

        src = self.temp_dir / "new.h5"
        make_recording(src, self.frozen.session_config, frames=10)
        result = filing.file_take(src, state, p, self.frozen)

        self.assertIs(result.outcome, filing.Outcome.TARGET_EXISTS)
        self.assertEqual(dest.read_bytes(), b"precious")
        self.assertTrue(src.exists())

    def test_unreadable_is_refused(self):
        p = self.load_plan()
        src = self.temp_dir / "bad.h5"
        src.write_bytes(b"garbage")
        result = filing.file_take(src, p.next_pending(), p, self.frozen)
        self.assertIs(result.outcome, filing.Outcome.UNREADABLE)

    def test_warnings_flag_short_take_and_delays(self):
        p = self.load_plan()
        state = p.by_seq(1)  # planned 60 s
        src = self.temp_dir / "short.h5"
        make_recording(src, self.frozen.session_config, frames=25)  # ~1 s
        info = inspection.inspect_record(src)
        notes = filing.warnings_for(info, state)
        self.assertTrue(any("Duration" in n for n in notes), notes)


class TestScanner(Base):
    def test_ignores_preexisting(self):
        old = self.temp_dir / "old.h5"
        make_recording(old, self.frozen.session_config, frames=5)
        scanner = filing.TempScanner(self.temp_dir)
        self.assertEqual(scanner.poll(), [])
        self.assertEqual(len(scanner.preexisting), 1)

    def test_requires_two_stable_polls(self):
        scanner = filing.TempScanner(self.temp_dir)
        new = self.temp_dir / "new.h5"
        make_recording(new, self.frozen.session_config, frames=5)
        # First sighting only records the size.
        self.assertEqual(scanner.poll(), [])
        self.assertEqual([p.name for p in scanner.poll()], ["new.h5"])
        # Claimed, so not offered twice.
        self.assertEqual(scanner.poll(), [])

    def test_does_not_grab_recording_still_in_progress(self):
        """Regression: a growing file whose size happens to be stable.

        The chunked writer flushes only every 512 results or every second, so
        a recording in progress can sit at a constant byte size across two
        polls with its header already written. Only the frame count reveals
        that it is still going.
        """
        scanner = filing.TempScanner(self.temp_dir)
        path = self.temp_dir / "growing.h5"

        client = a121.Client.open(mock=True)
        try:
            client.setup_session(self.frozen.session_config)
            recorder = a121.H5Recorder(path, client)
            client.start_session()
            for _ in range(5):
                client.get_next()

            # Poll repeatedly while the session is still open. The file exists
            # and parses, so a size-only check would hand it over here.
            for _ in range(4):
                self.assertEqual(
                    scanner.poll(), [], "grabbed a recording that was still running"
                )

            for _ in range(5):
                client.get_next()
            client.stop_session()
            recorder.close()
        finally:
            client.close()

        scanner.poll()
        self.assertEqual([p.name for p in scanner.poll()], ["growing.h5"])

    def test_does_not_grab_unopenable_file(self):
        scanner = filing.TempScanner(self.temp_dir)
        partial = self.temp_dir / "partial.h5"
        partial.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\x00" * 200)
        scanner.poll()
        self.assertEqual(scanner.poll(), [], "must not accept a non-A121 file")


class TestLogbook(Base):
    def test_appends_with_header_once(self):
        book = logbook.Logbook(self.out)
        self.out.mkdir(parents=True, exist_ok=True)
        for seq in (1, 2):
            book.append(
                logbook.LogRow(
                    location="locA_indoor_3wall",
                    seq=seq,
                    cls="bg",
                    filename=f"f{seq}.h5",
                    status="completed",
                    config_sha256=self.frozen.sha256,
                    frames=1500,
                )
            )
        rows = book.read()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["class"], "bg")
        self.assertEqual(book.config_hashes(), {self.frozen.sha256})

    def test_rejection_survives_file_deletion(self):
        book = logbook.Logbook(self.out)
        self.out.mkdir(parents=True, exist_ok=True)
        book.append(
            logbook.LogRow(
                location="locA_indoor_3wall",
                seq=7,
                cls="ped",
                filename="ped_x.h5",
                status="rejected",
                config_sha256=self.frozen.sha256,
                note="subject tripped",
            )
        )
        rows = book.read()
        self.assertEqual(rows[0]["status"], "rejected")
        self.assertEqual(rows[0]["note"], "subject tripped")


class TestResume(Base):
    def test_resumes_after_partial_session(self):
        p = self.load_plan()
        for _ in range(4):
            state = p.next_pending()
            src = self.temp_dir / f"t{state.shot.seq}.h5"
            make_recording(src, self.frozen.session_config, frames=10)
            result = filing.file_take(src, state, p, self.frozen)
            self.assertTrue(result.ok, result.message)
            p.mark_recorded(
                state.shot.seq,
                actual_seconds=result.info.duration_s,
                frames=result.info.num_frames,
                delayed=result.info.delayed,
                saturated=result.info.saturated,
            )

        resumed = self.load_plan()
        self.assertEqual(resumed.counts()["recorded"], 4)
        self.assertEqual(resumed.next_pending().shot.seq, 5)
        self.assertEqual(resumed.orphan_files(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
