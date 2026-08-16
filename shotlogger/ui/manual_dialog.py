"""The user guide, shown inside the app.

The text lives in `shotlogger/manual.md` and is rendered here with
QTextDocument's Markdown support. Keeping it as one Markdown file means the same
guide is readable in the repository and in the app, so the two cannot drift
apart -- which is what happens whenever in-app help is maintained separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

MANUAL_PATH = Path(__file__).resolve().parent.parent / "manual.md"


def load_markdown() -> str:
    try:
        return MANUAL_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return (
            f"# Manual unavailable\n\nThe guide should be at `{MANUAL_PATH}` "
            f"but could not be read:\n\n    {exc}\n"
        )


def section_titles(markdown: str) -> List[str]:
    """Top-level sections, in document order.

    Only `## ` headings: the `# ` title is the document itself and `### `
    headings are detail inside a section, so listing them would make the
    contents pane longer than it is useful.
    """
    titles = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            titles.append(line[3:].strip())
    return titles


class ManualDialog(QDialog):
    """Contents list on the left, rendered guide on the right, search on top."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Shot Logger — user guide")
        self.resize(980, 720)

        self._markdown = load_markdown()

        outer = QVBoxLayout(self)
        outer.addWidget(self._build_search())

        body = QHBoxLayout()
        body.addWidget(self._build_contents(), 0)
        body.addWidget(self._build_browser(), 1)
        outer.addLayout(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    # ------------------------------------------------------------------ build

    def _build_search(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search the guide…   (Enter for next match)")
        self._search.returnPressed.connect(self._find_next)
        self._search.textChanged.connect(self._on_search_changed)

        self._search_status = QLabel("")
        self._search_status.setStyleSheet("color: palette(mid);")

        layout.addWidget(self._search, 1)
        layout.addWidget(self._search_status)
        return bar

    def _build_contents(self) -> QWidget:
        self._contents = QListWidget()
        self._contents.setMaximumWidth(250)
        self._contents.setAlternatingRowColors(True)
        for title in section_titles(self._markdown):
            self._contents.addItem(QListWidgetItem(title))
        self._contents.currentTextChanged.connect(self._go_to_section)
        return self._contents

    def _build_browser(self) -> QWidget:
        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(True)

        document = QTextDocument(self._browser)
        document.setMarkdown(self._markdown)
        self._browser.setDocument(document)

        font = self._browser.font()
        font.setPointSize(max(font.pointSize(), 11))
        self._browser.setFont(font)
        # Comfortable measure for prose rather than edge-to-edge text.
        self._browser.document().setDocumentMargin(18)
        return self._browser

    # ------------------------------------------------------------- navigation

    def _go_to_section(self, title: str) -> None:
        """Scroll so the chosen heading sits at the top of the view."""
        if not title:
            return
        self._browser.moveCursor(QTextCursor.MoveOperation.Start)
        if self._browser.find(title):
            cursor = self._browser.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            self._browser.setTextCursor(cursor)
            # Push the heading to the top rather than leaving it mid-view.
            bar = self._browser.verticalScrollBar()
            bar.setValue(bar.value() + self._browser.cursorRect().top())

    def _on_search_changed(self, text: str) -> None:
        if not text:
            self._search_status.setText("")
            return
        self._find_next(from_start=True)

    def _find_next(self, from_start: bool = False) -> None:
        needle = self._search.text().strip()
        if not needle:
            return

        if from_start:
            self._browser.moveCursor(QTextCursor.MoveOperation.Start)

        if self._browser.find(needle):
            self._search_status.setText("")
            return

        # Wrap around once before declaring nothing found, so Enter keeps
        # cycling through matches instead of stopping at the last one.
        self._browser.moveCursor(QTextCursor.MoveOperation.Start)
        if self._browser.find(needle):
            self._search_status.setText("wrapped to top")
        else:
            self._search_status.setText("no match")
