#!/usr/bin/env python3
"""Turn the recorded campaign into a compact spectrogram dataset for training.

Runs locally, in the recording venv -- it needs only numpy and h5py. Every
recording is passed through features.py (the same code the embedded port is
specified by) and stored as one spectrogram column per frame, plus a per-frame
flag saying whether a walker is detectably moving in the beam.

    .venv/bin/python training/build_dataset.py \\
        --root data/SSAD_DATA_RECORDINGS-main --out training/out

Why the per-frame flag. A walk recording is ~20 s long but the walk itself is a
few seconds; the rest is an empty beam. Labelling every window of a walk
recording "pedestrian" teaches the model that noise is a pedestrian. So a
window only counts as pedestrian if the walker is visibly moving in it, and
that is decided here, from the signal, once, with a threshold set from the
background recordings' own noise statistics.

Labels: pedestrian = every walking class, with or without a carried object
(ped, bag, umbup, umbfwd, umbback, canehand). Background = every bg recording,
including people standing still, which is background by the project's class
definition.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import features  # noqa: E402

SESSIONS = {
    "SSAD_RADAR_DATA_RECORDINGS_FINAL": "S1",
    "SSAD-RADAR_DATA_RECORDINGS_2_FINAL": "S2",
    "SSAD-RADAR_DATA_RECORDINGS_AP": "AP",
    "SSAD-RADAR_DATA_RECORDINGS_BW": "BW",
}

SMOOTH_FRAMES = 25          # 1 s
THRESHOLD_MARGIN = 2.0      # threshold = margin x the loudest background frame, in dB


def parse_name(path: Path):
    cls, geometry, distance, speed, subject, take = path.stem.split("_")
    return dict(cls=cls, geometry=geometry, distance=distance, speed=speed, subject=subject, take=take)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    files = sorted(args.root.rglob("*.h5"))
    if not files:
        print(f"ERROR: no .h5 files under {args.root}", file=sys.stderr)
        return 1

    recs, specs, scores = [], [], []
    for i, path in enumerate(files, 1):
        session = SESSIONS.get(path.parent.parent.name)
        if session is None:
            print(f"ERROR: {path} is not in a known session folder", file=sys.stderr)
            return 1
        meta = parse_name(path)
        frames = features.load_frames(path)
        specs.append(features.spectrogram_db(frames))
        score, _ = features.activity_track(frames, SMOOTH_FRAMES)
        scores.append(score)
        recs.append(dict(meta, session=session, file=f"{session}/{meta['cls']}/{path.name}",
                         label=int(meta["cls"] != "bg"), n_frames=len(frames)))
        if i % 25 == 0:
            print(f"  {i}/{len(files)}")

    # Threshold from the background recordings' own noise. The convolution edges
    # are excluded: zero padding pulls them low, which would flatter the margin.
    edge = SMOOTH_FRAMES
    bg_max = max(float(s[edge:-edge].max()) for s, r in zip(scores, recs) if r["label"] == 0)
    threshold = THRESHOLD_MARGIN * bg_max
    active = [s > threshold for s in scores]
    for r, a, s in zip(recs, active, scores):
        r["active_frames"] = int(a.sum())
        r["peak_score_db"] = round(float(s.max()), 3)

    offsets = np.cumsum([0] + [len(s) for s in specs])
    args.out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        args.out / "binary_dataset.npz",
        spec_db=np.concatenate(specs).astype(np.float16),
        active=np.concatenate(active),
        offsets=offsets,
        meta=np.array(json.dumps(recs)),
    )

    # Two raw windows for the embedded port to be proved against: the raw int16
    # IQ goes in, and features.py's spectrogram is what must come out.
    walk_i = max((i for i, r in enumerate(recs) if r["label"] == 1 and r["session"] == "S2"),
                 key=lambda i: recs[i]["active_frames"])
    bg_i = next(i for i, r in enumerate(recs) if r["cls"] == "bg" and r["geometry"] == "empty" and r["session"] == "S2")
    a = np.flatnonzero(active[walk_i])
    walk_start = int(np.clip(a.mean() - features.PATCH_FRAMES // 2, 0, recs[walk_i]["n_frames"] - features.PATCH_FRAMES))
    picks = [(walk_i, walk_start), (bg_i, 100)]
    raw_re, raw_im, tv_spec, tv_names = [], [], [], []
    import h5py
    for i, start in picks:
        path = args.root / [p for p in files if p.name == recs[i]["file"].split("/")[-1] and SESSIONS[p.parent.parent.name] == recs[i]["session"]][0].relative_to(args.root)
        with h5py.File(path, "r") as f:
            raw = f["sessions/session_0/group_0/entry_0/result/frame"][start:start + features.PATCH_FRAMES]
        raw_re.append(raw["real"]); raw_im.append(raw["imag"])
        tv_spec.append(specs[i][start:start + features.PATCH_FRAMES])
        tv_names.append(f"{recs[i]['file']}[{start}:{start + features.PATCH_FRAMES}]")
    np.savez_compressed(
        args.out / "test_vectors_raw.npz",
        raw_real=np.stack(raw_re).astype(np.int16), raw_imag=np.stack(raw_im).astype(np.int16),
        spec_db=np.stack(tv_spec).astype(np.float32), names=np.array(tv_names), label=np.array([1, 0]),
    )

    walks = [r for r in recs if r["label"] == 1]
    summary = dict(
        recordings=len(recs), frames=int(offsets[-1]),
        by_session=dict(Counter(r["session"] for r in recs)),
        by_class=dict(Counter(r["cls"] for r in recs)),
        smooth_frames=SMOOTH_FRAMES, background_max_score_db=round(bg_max, 3),
        threshold_db=round(threshold, 3),
        background_frames_over_threshold=int(sum(int(a.sum()) for a, r in zip(active, recs) if r["label"] == 0)),
        walks_with_1s_active=sum(r["active_frames"] >= features.FRAME_RATE for r in walks),
        walks_total=len(walks),
        walks_undetectable=[r["file"] for r in walks if r["active_frames"] < features.FRAME_RATE],
        test_vectors=tv_names,
    )
    (args.out / "binary_dataset_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "walks_undetectable"}, indent=2))
    print(f"\n{len(summary['walks_undetectable'])} walks too faint to label (fewer than 1 s above threshold) -- listed in the summary JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
