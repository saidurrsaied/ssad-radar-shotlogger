#!/usr/bin/env python3
"""Train, evaluate and export the binary pedestrian detector (pedestrian / no pedestrian).

Runs on Colab, where TensorFlow is preinstalled. Input is the compact dataset
built locally by build_dataset.py; nothing here reads raw recordings.

    colab upload ...  binary_dataset.npz, test_vectors_raw.npz, features.py, this file
    colab exec -s <session> -f train_binary.py

`--dry-run` builds the windows and folds without TensorFlow, so the data side
can be checked locally first.

Evaluation is leave-one-subject-out: train on two people, test on the third,
three times. A pedestrian detector that has met the person before is not the
product. Two scores come out of each fold:

  window level   -- the clean windows only: walker clearly present, or background.
  recording level -- every window of every test recording, as the device would
                     see it, including the walks too faint to label. A walk is
                     detected if the model fires on 3 consecutive windows
                     (~0.5 s); a false alarm is the same event in a background
                     recording.

Both the float model and the int8 model that actually ships are scored.
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

_here = Path(__file__).resolve().parent if "__file__" in globals() else Path("/content")
sys.path.insert(0, str(_here))
import features  # noqa: E402

PATCH = features.PATCH_FRAMES
HOP_POS = 4             # 0.16 s between pedestrian windows
HOP_NEG = 8             # 0.32 s between background windows
HOP_EVAL = 4            # recording-level evaluation stride
MIN_ACTIVE = 25         # a pedestrian window holds >= 1 s of detectable walking
EVENT_RUN = 3           # consecutive positive windows that make one detection
THRESHOLD = 0.5
SEED = 42
FOLD_SUBJECTS = ["p1", "p2", "p3"]


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ data
def load_dataset(path):
    z = np.load(path, allow_pickle=False)
    return (z["spec_db"].astype(np.float32), z["active"].astype(bool),
            z["offsets"].astype(np.int64), json.loads(str(z["meta"])))


def windows_for(rec_ids, spec, active, offsets, meta, mode):
    """(rec, start) pairs and labels.

    mode 'train': clean windows only -- pedestrian windows need MIN_ACTIVE frames
                  of detectable walking; windows of walk recordings without it
                  are dropped, not called background.
    mode 'all'  : every window at HOP_EVAL, labelled by its recording.
    """
    idx, y = [], []
    for r in rec_ids:
        a, n = offsets[r], offsets[r + 1] - offsets[r]
        if n < PATCH:
            continue
        lab = meta[r]["label"]
        if mode == "all":
            for s in range(0, n - PATCH + 1, HOP_EVAL):
                idx.append((r, s)); y.append(lab)
        elif lab == 0:
            for s in range(0, n - PATCH + 1, HOP_NEG):
                idx.append((r, s)); y.append(0)
        else:
            cs = np.concatenate([[0], np.cumsum(active[a:a + n])])
            for s in range(0, n - PATCH + 1, HOP_POS):
                if cs[s + PATCH] - cs[s] >= MIN_ACTIVE:
                    idx.append((r, s)); y.append(1)
    return np.array(idx, dtype=np.int64).reshape(-1, 2), np.array(y, dtype=np.int64)


def materialise(idx, spec, offsets):
    out = np.empty((len(idx), PATCH, features.N_DOPPLER), dtype=np.float32)
    for i, (r, s) in enumerate(idx):
        out[i] = spec[offsets[r] + s: offsets[r] + s + PATCH]
    return out


def make_folds(meta):
    """Walks: held out by subject. Background (no subject): spread round-robin
    within each disturbance source, so every fold tests a mix of sources."""
    fold = {}
    for i, r in enumerate(meta):
        if r["label"] == 1:
            fold[i] = FOLD_SUBJECTS.index(r["subject"])
    by_source = defaultdict(list)
    for i, r in enumerate(meta):
        if r["label"] == 0:
            by_source[r["geometry"]].append(i)
    k = 0
    for src in sorted(by_source):
        for i in sorted(by_source[src], key=lambda i: (meta[i]["session"], meta[i]["file"])):
            fold[i] = k % 3
            k += 1
    return fold


def split_val(rec_ids, meta, rng, frac=0.15):
    """Recording-level validation split, stratified by label. Never window-level:
    overlapping windows of one recording on both sides would leak."""
    train, val = [], []
    for lab in (0, 1):
        ids = [r for r in rec_ids if meta[r]["label"] == lab]
        rng.shuffle(ids)
        n_val = max(1, int(round(frac * len(ids))))
        val += ids[:n_val]; train += ids[n_val:]
    return train, val


def norm_constants(X, rng):
    sample = X.reshape(-1)
    if sample.size > 4_000_000:
        sample = rng.choice(sample, 4_000_000, replace=False)
    return float(np.percentile(sample, 1.0)), float(np.percentile(sample, 99.5))


# ------------------------------------------------------------------ model
def build_model():
    import tensorflow as tf
    from tensorflow.keras import layers

    inp = layers.Input((PATCH, features.N_DOPPLER, 1))
    x = inp
    for filters in (16, 32, 48):
        # Conv -> BN -> ReLU folds into one int8 CONV_2D with fused activation.
        # momentum=0.9, not the default 0.99: with ~80 steps per epoch the default
        # moving statistics lag the batch statistics for ~7 epochs, and the model
        # calls everything background at inference while training accuracy reads
        # 98% -- long enough for early stopping to lock in the broken weights.
        x = layers.Conv2D(filters, 3, strides=2, padding="same", use_bias=False)(x)
        x = layers.BatchNormalization(momentum=0.9)(x)
        x = layers.ReLU()(x)
    # Global pooling instead of Flatten: fixed shapes, no SHAPE/PACK ops on the MCU.
    x = layers.GlobalAveragePooling2D()(x)
    x = layers.Dropout(0.3)(x)
    out = layers.Dense(1, activation="sigmoid")(x)
    model = tf.keras.Model(inp, out, name="pedestrian_binary")
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss="binary_crossentropy",
                  metrics=["accuracy"])
    return model


def train_model(Xtr, ytr, Xva, yva, gain_db_norm):
    import tensorflow as tf

    tf.keras.utils.set_random_seed(SEED)
    model = build_model()

    def augment(x, y):
        # Approach and depart are both pedestrians and noise is noise either way,
        # so flipping the Doppler axis preserves the label. A +/-1 dB level shift
        # stands in for gain differences between sessions and units.
        x = tf.cond(tf.random.uniform(()) < 0.5, lambda: tf.reverse(x, axis=[1]), lambda: x)
        x = tf.clip_by_value(x + tf.random.uniform((), -gain_db_norm, gain_db_norm), 0.0, 1.0)
        return x, y

    ds = (tf.data.Dataset.from_tensor_slices((Xtr[..., None], ytr[:, None].astype(np.float32)))
          .shuffle(len(Xtr), seed=SEED).map(augment).batch(64).prefetch(2))
    n0, n1 = int((ytr == 0).sum()), int((ytr == 1).sum())
    cw = {0: len(ytr) / (2 * n0), 1: len(ytr) / (2 * n1)}
    cb = [tf.keras.callbacks.EarlyStopping(monitor="val_loss", patience=8, restore_best_weights=True),
          tf.keras.callbacks.ReduceLROnPlateau(monitor="val_loss", factor=0.5, patience=4, min_lr=1e-5)]
    h = model.fit(ds, validation_data=(Xva[..., None], yva[:, None].astype(np.float32)), epochs=60,
                  class_weight=cw, callbacks=cb, verbose=2)
    best = int(np.argmin(h.history["val_loss"])) + 1
    return model, best


def to_int8(model, rep):
    import tensorflow as tf

    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]
    conv.representative_dataset = lambda: ([rep[i:i + 1, ..., None]] for i in range(len(rep)))
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    conv.inference_input_type = tf.int8
    conv.inference_output_type = tf.int8
    return conv.convert()


class Int8Runner:
    def __init__(self, model_bytes):
        try:
            from ai_edge_litert.interpreter import Interpreter
        except ImportError:
            import tensorflow as tf
            Interpreter = tf.lite.Interpreter
        self.it = Interpreter(model_content=model_bytes)
        self.it.allocate_tensors()
        self.inp = self.it.get_input_details()[0]
        self.out = self.it.get_output_details()[0]
        self.s_in, self.z_in = self.inp["quantization"]
        self.s_out, self.z_out = self.out["quantization"]

    def quantise(self, x):
        return np.clip(np.round(x / self.s_in) + self.z_in, -128, 127).astype(np.int8)

    def raw(self, q):
        self.it.set_tensor(self.inp["index"], q[None, ..., None])
        self.it.invoke()
        return int(self.it.get_tensor(self.out["index"])[0, 0])

    def predict(self, X):
        return np.array([(self.raw(self.quantise(x)) - self.z_out) * self.s_out for x in X], dtype=np.float32)


# ------------------------------------------------------------------ metrics
def window_metrics(y, p, idx, meta):
    from sklearn.metrics import roc_auc_score

    yhat = (p >= THRESHOLD).astype(int)
    tp = int(((yhat == 1) & (y == 1)).sum()); tn = int(((yhat == 0) & (y == 0)).sum())
    fp = int(((yhat == 1) & (y == 0)).sum()); fn = int(((yhat == 0) & (y == 1)).sum())
    m = dict(windows=len(y), accuracy=(tp + tn) / len(y),
             recall=tp / max(tp + fn, 1), precision=tp / max(tp + fp, 1),
             false_positive_rate=fp / max(fp + tn, 1),
             auc=float(roc_auc_score(y, p)) if len(set(y)) == 2 else None)
    groups = defaultdict(list)
    for (r, _), yy, yh in zip(idx, y, yhat):
        key = f"walk:{meta[r]['cls']}" if yy == 1 else f"bg:{meta[r]['geometry']}"
        groups[key].append(yh == yy)
        groups[f"session:{meta[r]['session']}:{'walk' if yy else 'bg'}"].append(yh == yy)
    m["by_group_correct"] = {k: round(float(np.mean(v)), 4) for k, v in sorted(groups.items())}
    return m


def recording_metrics(idx, p, meta, active_frames):
    """Events = runs of EVENT_RUN consecutive positive windows."""
    per_rec = defaultdict(list)
    for (r, s), pp in zip(idx, p):
        per_rec[r].append((s, pp >= THRESHOLD))
    walks, faint, alarms, bg_minutes = [], [], 0, 0.0
    for r, seq in per_rec.items():
        hits = [h for _, h in sorted(seq)]
        runs, run = 0, 0
        for h in hits:
            run = run + 1 if h else 0
            if run == EVENT_RUN:
                runs += 1
        if meta[r]["label"] == 1:
            (walks if active_frames[r] >= features.FRAME_RATE else faint).append((r, runs > 0))
        else:
            alarms += runs
            bg_minutes += meta[r]["n_frames"] / features.FRAME_RATE / 60
    by_session = defaultdict(list)
    for r, d in walks:
        by_session[meta[r]["session"]].append(d)
    return dict(
        walks_detected=sum(d for _, d in walks), walks=len(walks),
        faint_walks_detected=sum(d for _, d in faint), faint_walks=len(faint),
        detection_by_session={k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_session.items())},
        missed=[meta[r]["file"] for r, d in walks if not d],
        false_alarms=alarms, background_minutes=round(bg_minutes, 2),
        false_alarms_per_minute=round(alarms / max(bg_minutes, 1e-9), 3),
    )


# ------------------------------------------------------------------ export
def c_array(name, data):
    body = ",".join(("\n  " if i % 16 == 0 else "") + f"0x{b:02x}" for i, b in enumerate(data))
    return (f"// Generated by training/train_binary.py. Do not edit.\n#pragma once\n#include <stdint.h>\n\n"
            f"alignas(16) const uint8_t {name}[] = {{{body}\n}};\nconst unsigned int {name}_len = {len(data)};\n")


def features_header(floor, ceil, runner):
    f = features
    return f"""// Generated by training/train_binary.py from training/features.py. Do not edit.
// The embedded feature extraction must reproduce features.py exactly; prove it
// against test_vectors.npz before trusting any on-device accuracy number.
#pragma once

#define RADAR_N_SWEEPS           {f.N_SWEEPS}
#define RADAR_N_RANGE            {f.N_RANGE}
#define RADAR_N_DOPPLER          {f.N_DOPPLER}
#define RADAR_ZERO_BIN           {f.ZERO_BIN}
#define RADAR_MOTION_MIN_BIN     {f.MOTION_MIN_BIN}   /* moving bins: |k - ZERO_BIN| >= this */
#define RADAR_TOP_K              {f.TOP_K}   /* range points kept per frame */
#define RADAR_LOG_EPS            {f.EPS:.1e}f
#define RADAR_PATCH_FRAMES       {f.PATCH_FRAMES}   /* model input: PATCH_FRAMES x N_DOPPLER */

/* normalise: x = clamp((dB - FLOOR) / (CEIL - FLOOR), 0, 1) */
#define RADAR_DB_FLOOR           {floor:.6f}f
#define RADAR_DB_CEIL            {ceil:.6f}f

/* int8 model I/O: q = clamp(round(x / IN_SCALE) + IN_ZERO_POINT, -128, 127) */
#define RADAR_IN_SCALE           {runner.s_in:.9g}f
#define RADAR_IN_ZERO_POINT      {runner.z_in}
/* probability = (q_out - OUT_ZERO_POINT) * OUT_SCALE */
#define RADAR_OUT_SCALE          {runner.s_out:.9g}f
#define RADAR_OUT_ZERO_POINT     {runner.z_out}
#define RADAR_DECISION_THRESHOLD {THRESHOLD:.2f}f
#define RADAR_EVENT_RUN          {EVENT_RUN}   /* consecutive positive windows = one detection */
"""


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="/content/binary_dataset.npz")
    ap.add_argument("--tv", default="/content/test_vectors_raw.npz")
    ap.add_argument("--out", default="/content/out")
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args()      # parse_known: a Colab kernel carries its own argv

    t0 = time.time()
    spec, active, offsets, meta = load_dataset(args.data)
    active_frames = [int(active[offsets[i]:offsets[i + 1]].sum()) for i in range(len(meta))]
    folds = make_folds(meta)
    log(f"{len(meta)} recordings, {len(spec)} frames")

    report = dict(config=dict(patch=PATCH, hop_pos=HOP_POS, hop_neg=HOP_NEG, min_active=MIN_ACTIVE,
                              event_run=EVENT_RUN, threshold=THRESHOLD, seed=SEED), folds=[])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for f in range(3):
        rng = np.random.default_rng(SEED + f)
        test_ids = [i for i in range(len(meta)) if folds[i] == f]
        train_ids, val_ids = split_val([i for i in range(len(meta)) if folds[i] != f], meta, rng)
        tr_idx, ytr = windows_for(train_ids, spec, active, offsets, meta, "train")
        va_idx, yva = windows_for(val_ids, spec, active, offsets, meta, "train")
        te_idx, yte = windows_for(test_ids, spec, active, offsets, meta, "train")
        ev_idx, _ = windows_for(test_ids, spec, active, offsets, meta, "all")
        log(f"\n=== fold {f + 1}: test subject {FOLD_SUBJECTS[f]} ===")
        log(f"  windows  train {len(ytr)} ({int(ytr.sum())} ped)  val {len(yva)} ({int(yva.sum())} ped)  "
            f"test {len(yte)} ({int(yte.sum())} ped)  recording-level {len(ev_idx)}")
        bg_src = sorted({meta[i]["geometry"] for i in test_ids if meta[i]["label"] == 0})
        log(f"  test recordings: {sum(meta[i]['label'] for i in test_ids)} walks, "
            f"{sum(1 - meta[i]['label'] for i in test_ids)} background ({', '.join(bg_src)})")
        if args.dry_run:
            continue

        Xtr = materialise(tr_idx, spec, offsets)
        floor, ceil = norm_constants(Xtr, rng)
        nrm = lambda X: features.normalise(X, floor, ceil)
        Xtr = nrm(Xtr)
        Xva = nrm(materialise(va_idx, spec, offsets))
        Xte = nrm(materialise(te_idx, spec, offsets))
        model, best_epoch = train_model(Xtr, ytr, Xva, yva, 1.0 / (ceil - floor))

        rep_ids = np.concatenate([rng.choice(np.flatnonzero(ytr == c), 200, replace=False) for c in (0, 1)])
        rng.shuffle(rep_ids)
        runner = Int8Runner(to_int8(model, Xtr[rep_ids]))

        p_float = model.predict(Xte[..., None], batch_size=256, verbose=0)[:, 0]
        p_int8 = runner.predict(Xte)
        Xev = nrm(materialise(ev_idx, spec, offsets))
        ev_float = model.predict(Xev[..., None], batch_size=256, verbose=0)[:, 0]
        ev_int8 = runner.predict(Xev)
        fold_rep = dict(
            fold=f + 1, test_subject=FOLD_SUBJECTS[f], best_epoch=best_epoch,
            db_floor=floor, db_ceil=ceil,
            float=dict(window=window_metrics(yte, p_float, te_idx, meta),
                       recording=recording_metrics(ev_idx, ev_float, meta, active_frames)),
            int8=dict(window=window_metrics(yte, p_int8, te_idx, meta),
                      recording=recording_metrics(ev_idx, ev_int8, meta, active_frames)),
            int8_vs_float_agreement=float(np.mean((p_int8 >= THRESHOLD) == (p_float >= THRESHOLD))),
        )
        report["folds"].append(fold_rep)
        for kind in ("float", "int8"):
            w, r = fold_rep[kind]["window"], fold_rep[kind]["recording"]
            log(f"  {kind:<5} window: acc {w['accuracy']:.3f}  recall {w['recall']:.3f}  "
                f"FPR {w['false_positive_rate']:.4f}  AUC {w['auc']:.4f}   |  recording: walks "
                f"{r['walks_detected']}/{r['walks']}, faint {r['faint_walks_detected']}/{r['faint_walks']}, "
                f"false alarms {r['false_alarms']} in {r['background_minutes']} min")
        (out / "report.json").write_text(json.dumps(report, indent=2))

    if args.dry_run:
        log(f"\ndry run done in {time.time() - t0:.0f} s")
        return 0

    # ---------------------------------------------------------- final model, all subjects
    log("\n=== final model: all subjects ===")
    rng = np.random.default_rng(SEED + 99)
    train_ids, val_ids = split_val(list(range(len(meta))), meta, rng)
    tr_idx, ytr = windows_for(train_ids, spec, active, offsets, meta, "train")
    va_idx, yva = windows_for(val_ids, spec, active, offsets, meta, "train")
    Xtr = materialise(tr_idx, spec, offsets)
    floor, ceil = norm_constants(Xtr, rng)
    Xtr = features.normalise(Xtr, floor, ceil)
    Xva = features.normalise(materialise(va_idx, spec, offsets), floor, ceil)
    model, best_epoch = train_model(Xtr, ytr, Xva, yva, 1.0 / (ceil - floor))
    rep_ids = np.concatenate([rng.choice(np.flatnonzero(ytr == c), 200, replace=False) for c in (0, 1)])
    rng.shuffle(rep_ids)
    tfl = to_int8(model, Xtr[rep_ids])
    runner = Int8Runner(tfl)

    final = out / "final"
    final.mkdir(exist_ok=True)
    model.save(final / "pedestrian_binary.keras")
    (final / "pedestrian_binary_int8.tflite").write_bytes(tfl)
    (final / "pedestrian_binary_int8.h").write_text(c_array("g_pedestrian_model", tfl))
    (final / "radar_features_config.h").write_text(features_header(floor, ceil, runner))

    # Test vectors: raw IQ in, every intermediate the C port must reproduce out.
    tv = np.load(args.tv)
    iq = tv["raw_real"].astype(np.float32) + 1j * tv["raw_imag"].astype(np.float32)
    spec_tv = np.stack([features.spectrogram_db(w) for w in iq])
    norm_tv = features.normalise(spec_tv, floor, ceil)
    q_in = np.stack([runner.quantise(x) for x in norm_tv])
    q_out = np.array([runner.raw(q) for q in q_in], dtype=np.int8)
    prob = (q_out.astype(np.float32) - runner.z_out) * runner.s_out
    np.savez_compressed(final / "test_vectors.npz", raw_real=tv["raw_real"], raw_imag=tv["raw_imag"],
                        spec_db=spec_tv, normalised=norm_tv, input_int8=q_in, output_int8=q_out,
                        probability=prob, label=tv["label"], names=tv["names"])
    spec_drift = float(np.abs(spec_tv - tv["spec_db"]).max())

    report["final"] = dict(best_epoch=best_epoch, db_floor=floor, db_ceil=ceil,
                           tflite_bytes=len(tfl), params=int(model.count_params()),
                           input_scale=float(runner.s_in), input_zero_point=int(runner.z_in),
                           output_scale=float(runner.s_out), output_zero_point=int(runner.z_out),
                           test_vector_probabilities=[round(float(p), 4) for p in prob],
                           test_vector_labels=tv["label"].tolist(),
                           test_vector_spec_drift_db=spec_drift)
    (out / "report.json").write_text(json.dumps(report, indent=2))
    log(f"  params {model.count_params()}  int8 tflite {len(tfl)} bytes  best epoch {best_epoch}")
    log(f"  test vectors: labels {tv['label'].tolist()} -> probabilities {[round(float(p), 3) for p in prob]}"
        f"  (spectrogram drift vs local build {spec_drift:.2e} dB)")
    log(f"\ndone in {time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    rc = main()
    if rc:
        raise SystemExit(rc)
