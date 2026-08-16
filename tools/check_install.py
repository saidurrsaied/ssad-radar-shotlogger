#!/usr/bin/env python3
"""Check that a machine is ready to run Shot Logger.

    tools/check_install.py

Every check prints ok, warn or FAIL with a specific next step. Warnings are
things you only need for an actual recording session (a sensor, a display);
failures mean the app will not start.

Written so a teammate can paste the output into the group chat and get a useful
answer, instead of "it doesn't work".
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "ok  ", "warn", "FAIL"
_counts = {OK: 0, WARN: 0, FAIL: 0}


def report(level: str, title: str, detail: str = "") -> None:
    _counts[level] += 1
    print(f"  [{level}] {title}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def check_python() -> None:
    v = sys.version_info
    if v < (3, 9):
        report(FAIL, f"Python {v.major}.{v.minor}", "acconeer-exptool needs Python 3.9 or newer.")
    else:
        report(OK, f"Python {v.major}.{v.minor}.{v.micro}")

    if sys.prefix == sys.base_prefix:
        report(
            WARN,
            "not running inside a virtual environment",
            "Expected .venv. Run:  source .venv/bin/activate",
        )
    else:
        report(OK, f"virtual environment  {sys.prefix}")


def check_packages() -> None:
    missing = []
    for module, label in (
        ("acconeer.exptool", "acconeer-exptool"),
        ("PySide6", "PySide6 (from the [app] extra)"),
        ("h5py", "h5py"),
        ("yaml", "PyYAML"),
        ("numpy", "numpy"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(label)

    if missing:
        report(
            FAIL,
            "missing packages: " + ", ".join(missing),
            'Run:  pip install "acconeer-exptool[app]"',
        )
        return

    import acconeer.exptool as et
    from acconeer.exptool import a121

    # et.__version__ is the package; a121.SDK_VERSION is the RSS firmware
    # generation, which is a different number and easy to confuse.
    report(
        OK,
        f"acconeer-exptool {getattr(et, '__version__', 'unknown')} "
        f"(RSS {getattr(a121, 'SDK_VERSION', '?')})",
    )


def check_qt() -> None:
    """Can Qt actually open a window? This is the commonest Linux blocker."""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        report(
            WARN,
            "no display detected",
            "Fine over SSH. On WSL you need WSLg (Windows 11, or Windows 10 with\n"
            "a recent WSL update). The GUI cannot open without one.",
        )
        return

    probe = (
        "from PySide6.QtWidgets import QApplication;"
        "import sys; QApplication(sys.argv); print('qt-ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    if "qt-ok" in result.stdout:
        report(OK, "Qt can open a window")
    elif "xcb-cursor0" in result.stderr or "xcb" in result.stderr:
        report(
            FAIL,
            "Qt cannot load its platform plugin",
            "Run:  sudo apt install libxcb-cursor0",
        )
    else:
        report(FAIL, "Qt failed to start", result.stderr.strip()[:400])


def check_project_files() -> None:
    config = ROOT / "config" / "session_config.json"
    if config.is_file():
        try:
            sys.path.insert(0, str(ROOT))
            from shotlogger import frozen_config

            frozen = frozen_config.load(config)
            report(OK, f"frozen config  sha256 {frozen.short_sha}")
        except Exception as exc:
            report(FAIL, "frozen config is unusable", str(exc))
    else:
        report(
            FAIL,
            "no frozen config",
            "Expected config/session_config.json.\n"
            "Run:  tools/freeze_session_config.py",
        )

    plans = sorted((ROOT / "plans").glob("*.csv"))
    if plans:
        report(OK, f"{len(plans)} plan CSV(s): " + ", ".join(p.name for p in plans))
    else:
        report(
            WARN,
            "no plan CSVs",
            "Generate one:  tools/make_shotlist.py --plan plans/<name>.yaml",
        )


def check_temp_dir() -> None:
    try:
        from acconeer.exptool.app.new.storage import get_temp_dir

        temp = Path(get_temp_dir())
    except Exception as exc:
        report(FAIL, "cannot resolve the Exploration Tool's data directory", str(exc))
        return

    if temp.is_dir():
        report(OK, f"Exploration Tool temp dir  {temp}")
    else:
        report(
            WARN,
            f"Exploration Tool temp dir does not exist yet  ({temp})",
            "It is created the first time the Exploration Tool records.\n"
            "Start it, record once, then re-run this check.",
        )


def check_sensor() -> None:
    try:
        from acconeer.exptool._core.communication.comm_devices import (
            get_serial_devices,
            get_usb_devices,
        )

        serial_devices = [d for d in get_serial_devices() if d.recognized]
        usb_devices = get_usb_devices()
    except Exception as exc:
        report(WARN, "could not enumerate devices", str(exc))
        return

    if serial_devices or usb_devices:
        names = [d.display_name() for d in serial_devices] + [
            d.display_name() for d in usb_devices
        ]
        report(OK, "sensor found: " + ", ".join(names))
        return

    detail = "Not needed to start the app, only to record."
    if is_wsl():
        detail += (
            "\nOn WSL the board is not visible until it is attached from Windows:\n"
            "  usbipd list                      (in Windows PowerShell as admin)\n"
            "  usbipd bind --busid <BUSID>\n"
            "  usbipd attach --wsl --busid <BUSID>"
        )
    else:
        detail += (
            "\nIf it is plugged in, check permissions:  groups | grep dialout\n"
            "Adding yourself needs a fresh login to take effect."
        )
    report(WARN, "no Acconeer sensor detected", detail)


def check_serial_permissions() -> None:
    ports = sorted(Path("/dev").glob("ttyUSB*")) + sorted(Path("/dev").glob("ttyACM*"))
    if not ports:
        return  # check_sensor already covered the no-device case

    unreadable = [p for p in ports if not os.access(p, os.R_OK | os.W_OK)]
    if unreadable:
        report(
            FAIL,
            f"no permission to use {', '.join(p.name for p in unreadable)}",
            "Run:  sudo usermod -aG dialout $USER\n"
            "Then log out and back in -- group changes do not apply to a\n"
            "session that started before them. To test without logging out:\n"
            "  sg dialout -c 'python -m shotlogger'",
        )
    else:
        report(OK, f"serial port readable  ({', '.join(p.name for p in ports)})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()

    print(f"Shot Logger install check   ({'WSL' if is_wsl() else sys.platform})\n")

    print("Python")
    check_python()
    print("\nPackages")
    check_packages()
    print("\nGraphics")
    check_qt()
    print("\nProject files")
    check_project_files()
    check_temp_dir()
    print("\nSensor")
    check_sensor()
    check_serial_permissions()

    print(
        f"\n{_counts[OK]} ok, {_counts[WARN]} warning(s), {_counts[FAIL]} failure(s)"
    )
    if _counts[FAIL]:
        print("\nFix the failures above before running the app.")
        return 1
    if _counts[WARN]:
        print("\nThe app will start. Warnings matter only for a real recording session.")
    else:
        print("\nReady.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
