#!/usr/bin/env python3
"""Tests for the in-app user guide.

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest tests.test_manual -v

Beyond checking that the window opens, these assert that the guide still
describes the app that actually exists. A manual that has quietly drifted from
the UI is worse than none, because people trust it.
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from shotlogger.ui import manual_dialog  # noqa: E402
from shotlogger.ui.main_window import MainWindow  # noqa: E402

_app = QApplication.instance() or QApplication([])


class TestManualSource(unittest.TestCase):
    def setUp(self):
        self.markdown = manual_dialog.load_markdown()

    def test_ships_with_the_package(self):
        self.assertTrue(
            manual_dialog.MANUAL_PATH.is_file(),
            f"manual.md must sit next to the package at {manual_dialog.MANUAL_PATH}",
        )
        self.assertNotIn("Manual unavailable", self.markdown)
        self.assertGreater(len(self.markdown), 3000)

    def test_has_navigable_sections(self):
        titles = manual_dialog.section_titles(self.markdown)
        self.assertGreater(len(titles), 8)
        self.assertEqual(len(titles), len(set(titles)), "duplicate section titles")
        for title in titles:
            self.assertTrue(title.strip())

    def test_covers_the_things_people_get_stuck_on(self):
        for topic in (
            "Save to file",          # the one habit that breaks the workflow
            "dialout",               # the commonest Linux blocker
            "libxcb-cursor0",        # the window-will-not-open blocker
            "--temp-dir",            # non-default Exploration Tool data dir
            "Reject & delete",
            "Save session to",
            "frozen config",
        ):
            self.assertIn(topic, self.markdown, f"guide never mentions {topic!r}")

    def test_button_labels_match_the_real_ui(self):
        """The guide names controls; those names must still exist."""
        win = MainWindow(ROOT / "does-not-matter")
        try:
            labels = {
                win._watch_button.text(),
                win._export_button.text(),
                win._reject_button.text(),
                win._manual_button.text(),
            }
        finally:
            win.close()

        # Qt uses && to escape a literal ampersand in a label.
        cleaned = {label.replace("&&", "&") for label in labels}
        for label in ("Start watching", "Save session to…", "Reject & delete"):
            self.assertIn(label, cleaned)
            self.assertIn(label, self.markdown, f"guide does not mention the {label!r} button")

    def test_column_headings_match_the_table(self):
        """Every table column must be documented in the guide's column table.

        Searching the whole document is not good enough: a heading like
        "Filename" also occurs in ordinary prose, so a missing row would pass
        unnoticed. Only the markdown table counts.
        """
        from shotlogger.ui.model import COLUMNS

        rows = [
            line for line in self.markdown.splitlines()
            if line.startswith("| ") and "|" in line[2:]
        ]
        documented = {line.split("|")[1].strip() for line in rows}

        for heading, _ in COLUMNS:
            self.assertIn(
                heading,
                documented,
                f"guide's column table omits {heading!r}",
            )


class TestManualDialog(unittest.TestCase):
    def test_opens_and_renders(self):
        dialog = manual_dialog.ManualDialog()
        try:
            text = dialog._browser.toPlainText()
            self.assertIn("Shot Logger", text)
            self.assertNotIn("## ", text, "markdown should be rendered, not shown raw")
            self.assertEqual(
                dialog._contents.count(),
                len(manual_dialog.section_titles(dialog._markdown)),
            )
        finally:
            dialog.close()

    def test_section_click_scrolls(self):
        dialog = manual_dialog.ManualDialog()
        try:
            dialog._browser.verticalScrollBar().setValue(0)
            dialog._contents.setCurrentRow(dialog._contents.count() - 1)
            self.assertGreater(
                dialog._browser.verticalScrollBar().value(),
                0,
                "choosing a late section should scroll the view",
            )
        finally:
            dialog.close()

    def test_search_finds_and_reports_misses(self):
        dialog = manual_dialog.ManualDialog()
        try:
            dialog._search.setText("dialout")
            self.assertEqual(dialog._search_status.text(), "")
            self.assertTrue(dialog._browser.textCursor().hasSelection())

            dialog._search.setText("zzz-not-in-the-guide")
            self.assertEqual(dialog._search_status.text(), "no match")
        finally:
            dialog.close()

    def test_reachable_from_the_window_and_reused(self):
        win = MainWindow(ROOT / "does-not-matter")
        try:
            self.assertIsNone(win._manual)
            win.show_manual()
            first = win._manual
            self.assertIsNotNone(first)
            win.show_manual()
            self.assertIs(win._manual, first, "should reuse the guide window, not stack copies")
        finally:
            if win._manual is not None:
                win._manual.close()
            win.close()

    def test_help_menu_exists(self):
        win = MainWindow(ROOT / "does-not-matter")
        try:
            menus = [a.text() for a in win.menuBar().actions()]
            self.assertIn("&Help", menus)
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
