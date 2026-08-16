#!/usr/bin/env python3
"""Expand a YAML plan definition into a shot list CSV.

One row per planned recording, in the order the session should be run, using
the naming protocol from section 6 of the getting-started guide:

    <class>_<geometry>_<distance>_<speed>_<subject>_take<n>.h5

Background classes reuse the geometry slot for the disturbance source and set
speed to 'na' and subject to 'p0', so their filenames parse on the same split.

Each environment gets its own plan file under plans/. The location travels in
the CSV so the Shot Logger can refuse to file an outdoor take into the indoor
set.

    tools/make_shotlist.py --plan plans/locA_indoor_3wall.yaml
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import yaml


FIELDS = [
    "seq",
    "location",
    "class",
    "geometry",
    "distance",
    "speed",
    "subject",
    "take",
    "filename",
    "seconds",
    "note",
]


class PlanError(Exception):
    """The plan file is not usable. The message is shown to the user as-is."""


def _require(mapping: Dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise PlanError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _expand_subject_class(
    cls: Dict[str, Any], location: str, default_subjects: List[str], durations: Dict[str, int]
) -> Iterator[Dict[str, Any]]:
    name = cls["name"]
    subjects = cls.get("subjects", default_subjects)
    if not subjects:
        raise PlanError(f"class {name!r}: no subjects, and no top-level 'subjects' to fall back on")

    for subject in subjects:
        # Take numbers restart per subject, so p1 and p2 both have a take1.
        takes: Dict[tuple, int] = {}
        for i, block in enumerate(cls.get("blocks", [])):
            where = f"class {name!r} block {i}"
            geometry = _require(block, "geometry", where)
            distance = _require(block, "distance", where)
            speed = _require(block, "speed", where)

            if "seconds" in block:
                seconds = int(block["seconds"])
            elif speed in durations:
                seconds = int(durations[speed])
            else:
                raise PlanError(
                    f"{where}: speed {speed!r} is not in 'durations' and the block "
                    f"sets no explicit 'seconds'"
                )

            key = (geometry, distance, speed)
            for _ in range(int(_require(block, "repeats", where))):
                takes[key] = takes.get(key, 0) + 1
                take = takes[key]
                yield {
                    "location": location,
                    "class": name,
                    "geometry": geometry,
                    "distance": distance,
                    "speed": speed,
                    "subject": subject,
                    "take": take,
                    "filename": f"{name}_{geometry}_{distance}_{speed}_{subject}_take{take}.h5",
                    "seconds": seconds,
                    "note": block.get("note", ""),
                }


def _expand_background_class(cls: Dict[str, Any], location: str) -> Iterator[Dict[str, Any]]:
    name = cls["name"]
    takes: Dict[tuple, int] = {}
    for i, block in enumerate(cls.get("blocks", [])):
        where = f"class {name!r} block {i}"
        source = _require(block, "source", where)
        distance = _require(block, "distance", where)
        seconds = int(_require(block, "seconds", where))

        key = (source, distance)
        for _ in range(int(_require(block, "repeats", where))):
            takes[key] = takes.get(key, 0) + 1
            take = takes[key]
            yield {
                "location": location,
                "class": name,
                "geometry": source,
                "distance": distance,
                "speed": "na",
                "subject": "p0",
                "take": take,
                "filename": f"{name}_{source}_{distance}_na_p0_take{take}.h5",
                "seconds": seconds,
                "note": block.get("note", ""),
            }


def expand_plan(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    location = _require(plan, "location", "plan")
    default_subjects = plan.get("subjects", [])
    durations = plan.get("durations", {})

    rows: List[Dict[str, Any]] = []
    for cls in _require(plan, "classes", "plan"):
        name = _require(cls, "name", "class")
        kind = cls.get("kind", "subject")
        if kind == "background":
            rows.extend(_expand_background_class(cls, location))
        elif kind == "subject":
            rows.extend(_expand_subject_class(cls, location, default_subjects, durations))
        else:
            raise PlanError(f"class {name!r}: unknown kind {kind!r}, expected 'subject' or 'background'")

    for i, row in enumerate(rows, 1):
        row["seq"] = i
    return rows


def load_plan(path: Path) -> Dict[str, Any]:
    try:
        plan = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PlanError(f"{path}: not valid YAML\n{exc}") from exc
    if not isinstance(plan, dict):
        raise PlanError(f"{path}: expected a mapping at the top level")
    return plan


def write_csv(rows: List[Dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def summarise(rows: List[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    secs: Dict[str, int] = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
        secs[row["class"]] = secs.get(row["class"], 0) + row["seconds"]

    lines = ["%-8s%7s%14s" % ("class", "files", "beam-on min")]
    for cls in counts:
        lines.append("%-8s%7d%14.1f" % (cls, counts[cls], secs[cls] / 60))
    lines.append("%-8s%7d%14.1f" % ("TOTAL", len(rows), sum(secs.values()) / 60))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=Path, required=True, help="YAML plan definition")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output CSV (default: plans/<location>.csv)",
    )
    args = parser.parse_args()

    try:
        plan = load_plan(args.plan)
        rows = expand_plan(plan)
    except PlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("ERROR: plan expanded to zero rows", file=sys.stderr)
        return 1

    # Duplicate filenames are the failure this protocol is most prone to, and
    # they silently overwrite good recordings. There is no row-count check --
    # every environment has a different size.
    names = [r["filename"] for r in rows]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        print(f"ERROR: duplicate filenames: {sorted(duplicates)[:5]}", file=sys.stderr)
        return 1

    out = args.out or args.plan.parent / f"{plan['location']}.csv"
    write_csv(rows, out)

    print(f"wrote {out}")
    print(f"location: {plan['location']}")
    print(summarise(rows))
    print(f"\nall {len(rows)} filenames unique")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
