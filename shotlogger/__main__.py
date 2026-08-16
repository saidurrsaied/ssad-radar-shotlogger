"""Entry point:  python -m shotlogger

The Exploration Tool must be running separately -- it owns the sensor and does
the recording. This watches where it writes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def default_temp_dir() -> Path:
    """Where the Exploration Tool puts not-yet-saved recordings.

    Resolved through the Exploration Tool's own storage module, which is built
    on platformdirs, so this is correct on Linux, WSL, macOS and Windows
    without a hardcoded path.
    """
    from acconeer.exptool.app.new.storage import get_temp_dir

    return Path(get_temp_dir())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        help=(
            "Where the Exploration Tool writes recordings. Only needed if it was "
            "started with its own --data-dir."
        ),
    )
    args = parser.parse_args()

    try:
        temp_dir = args.temp_dir or default_temp_dir()
    except Exception as exc:
        print(f"ERROR: could not locate the Exploration Tool's data directory: {exc}")
        print("Pass --temp-dir explicitly.")
        return 1

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Shot Logger")

    window = MainWindow(temp_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
