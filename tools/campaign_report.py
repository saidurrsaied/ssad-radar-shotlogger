#!/usr/bin/env python3
"""Aggregate every location's recordings and check them against the plan targets.

    tools/campaign_report.py data/

Section 6 of the guide asks for at least 100 passes per class, from at least
three subjects, in at least two locations, with one location held back entirely
as a test set. None of that is visible from inside a single session, which is
why this exists.

The check that matters most is the configuration hash. Every recording in the
campaign must share one frozen configuration -- recordings made with different
sweep or range settings cannot be mixed into one training set. If two hashes
turn up, the dataset is silently split in two and the report says so loudly.
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shotlogger.logbook import LOGBOOK_NAME, Logbook  # noqa: E402
from shotlogger.plan import WORKING_CSV_NAME, WorkingPlan  # noqa: E402

TARGET_PER_CLASS = 100
TARGET_SUBJECTS = 3
TARGET_LOCATIONS = 2


def find_locations(root: Path):
    """Every directory under `root` that looks like a session output folder."""
    found = []
    for path in sorted(root.rglob(WORKING_CSV_NAME)):
        found.append(path.parent)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, nargs="?", default=Path("data"))
    args = parser.parse_args()

    if not args.root.is_dir():
        print(f"ERROR: {args.root} is not a directory", file=sys.stderr)
        return 1

    locations = find_locations(args.root)
    if not locations:
        print(f"No session folders found under {args.root}.")
        print(f"A session folder is one containing {WORKING_CSV_NAME}.")
        return 1

    per_class: Dict[str, int] = defaultdict(int)
    per_class_subjects: Dict[str, Set[str]] = defaultdict(set)
    hashes: Set[str] = set()
    problems = []

    print(f"{'location':<28}{'recorded':>10}{'of':>6}{'remaining min':>15}")
    print("-" * 59)

    for folder in locations:
        try:
            # Passing the working copy as the source is safe: load() prefers an
            # existing working copy, so nothing is overwritten.
            plan = WorkingPlan.load(folder / WORKING_CSV_NAME, folder)
        except Exception as exc:
            problems.append(f"{folder}: could not read plan ({exc})")
            continue

        counts = plan.counts()
        print(
            f"{plan.location:<28}{counts['recorded']:>10}{counts['total']:>6}"
            f"{plan.remaining_seconds() / 60:>15.0f}"
        )

        for state in plan:
            if state.status.value == "recorded":
                per_class[state.shot.cls] += 1
                if state.shot.subject != "p0":
                    per_class_subjects[state.shot.cls].add(state.shot.subject)

        book = Logbook(folder)
        if book.path.exists():
            hashes |= book.config_hashes()
        else:
            problems.append(f"{folder}: no {LOGBOOK_NAME}")

    print()
    print(f"{'class':<10}{'passes':>9}{'target':>9}{'subjects':>10}{'':>4}")
    print("-" * 42)
    for cls in sorted(per_class):
        n = per_class[cls]
        subjects = len(per_class_subjects[cls])
        ok = "ok" if n >= TARGET_PER_CLASS else "short"
        if cls != "bg" and subjects < TARGET_SUBJECTS:
            ok = "few subjects"
        print(f"{cls:<10}{n:>9}{TARGET_PER_CLASS:>9}{subjects:>10}   {ok}")

    print()
    print("Campaign checks")
    print("-" * 42)

    _check(
        len(locations) >= TARGET_LOCATIONS,
        f"locations: {len(locations)} (need {TARGET_LOCATIONS}, one held out as test)",
    )

    for cls in sorted(per_class):
        _check(per_class[cls] >= TARGET_PER_CLASS, f"class {cls}: {per_class[cls]} passes")

    if not hashes:
        print("  ??  no configuration hashes recorded yet")
    elif len(hashes) == 1:
        print(f"  ok  one frozen configuration across the campaign ({sorted(hashes)[0][:12]})")
    else:
        print(f"  !!  {len(hashes)} DIFFERENT configurations in use:")
        for h in sorted(hashes):
            print(f"        {h[:12]}")
        print(
            "      Recordings made under different configurations cannot be mixed "
            "into one training set. Decide which is authoritative and re-record "
            "the rest."
        )

    if problems:
        print()
        print("Problems")
        print("-" * 42)
        for problem in problems:
            print(f"  {problem}")

    return 0 if len(hashes) <= 1 else 2


def _check(ok: bool, message: str) -> None:
    print(f"  {'ok' if ok else '!!'}  {message}")


if __name__ == "__main__":
    raise SystemExit(main())
