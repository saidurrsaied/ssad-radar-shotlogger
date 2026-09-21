"""Reference feature extraction: raw A121 Sparse IQ frame -> one spectrogram column.

This module is the specification for the embedded port. The ESP32-S3 has to
compute exactly this, frame by frame, and the C implementation is proved
against test vectors exported from it. So everything here is deliberately
something a microcontroller can do in a streaming loop:

  * it works on one frame at a time and keeps no history,
  * it has no data-dependent normalisation -- every constant is fixed at
    training time and exported to C alongside the model,
  * it never looks at a label.

Per frame (64 sweeps x 22 range points, complex):

  1. Static clutter removal. Subtract the mean over the 64 sweeps, separately
     for each range point. Anything that did not move during the 11.6 ms burst
     lands in the zero-Doppler bin and is removed here.
  2. Window. Multiply each range point's 64 sweeps by a symmetric Hann window,
     w[n] = 0.5 - 0.5 * cos(2*pi*n / 63).
  3. Doppler FFT. 64-point complex FFT along the sweeps, for each of the 22
     range points. Re-order so zero velocity sits at index 32 (fftshift).
     Index k maps to velocity (k - 32) * 0.213 m/s. On this campaign's data an
     approaching walker lands at k < 32 (all 111 approach recordings).
  4. Power. |X|^2.
  5. Range selection. For each range point, sum the power over the moving bins
     |k - 32| >= 3 (|v| >= 0.64 m/s). Keep the TOP_K = 2 range points with the
     most moving power and add their power spectra.
     Why not sum all 22: the target occupies one or two range points, and on
     this dataset a walker peaks only ~10 dB above the noise in its own range
     point. Summing all 22 buries that under 20 points of noise (~13 dB).
  6. Log. 10 * log10(power + EPS).

The result is 64 floats per frame. PATCH_FRAMES consecutive columns make one
64 x 64 model input after `normalise`.
"""

from typing import Tuple

import numpy as np

N_SWEEPS = 64          # sweeps per frame  (frozen config: sweeps_per_frame)
N_RANGE = 22           # range points      (frozen config: num_points, 1.00-6.04 m)
N_DOPPLER = 64         # FFT length = N_SWEEPS
ZERO_BIN = N_DOPPLER // 2
MOTION_MIN_BIN = 3     # |k - ZERO_BIN| >= 3  <=>  |v| >= 0.64 m/s
TOP_K = 2              # range points kept per frame
EPS = 1e-6             # log floor; negligible against the measured noise power
PATCH_FRAMES = 64      # 2.56 s at 25 fps
FRAME_RATE = 25.0

HANN = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_SWEEPS) / (N_SWEEPS - 1))).astype(np.float32)
MOVING = np.abs(np.arange(N_DOPPLER) - ZERO_BIN) >= MOTION_MIN_BIN


def load_frames(path) -> np.ndarray:
    """All frames of an Exploration Tool recording as complex64 (frames, 64, 22)."""
    import h5py

    with h5py.File(path, "r") as f:
        raw = f["sessions/session_0/group_0/entry_0/result/frame"][()]
    return raw["real"].astype(np.float32) + 1j * raw["imag"].astype(np.float32)


def _power_spectra(frames: np.ndarray) -> np.ndarray:
    """Steps 1-4 for a batch of frames: (F, 64, 22) complex -> (F, 64 doppler, 22 range) power."""
    d = frames - frames.mean(axis=1, keepdims=True)
    x = np.fft.fft(d * HANN[None, :, None], axis=1)
    x = np.fft.fftshift(x, axes=1)
    return (x.real ** 2 + x.imag ** 2).astype(np.float32)


def spectrogram_db(frames: np.ndarray) -> np.ndarray:
    """Steps 1-6 for every frame: (F, 64, 22) complex -> (F, 64) dB.

    Vectorised over frames for speed, but each output row depends only on its
    own input frame -- exactly what the embedded loop computes one frame at a time.
    """
    p = _power_spectra(frames)
    moving = p[:, MOVING, :].sum(axis=1)                      # (F, 22)
    top = np.argsort(moving, axis=1)[:, -TOP_K:]              # (F, TOP_K)
    rows = np.arange(len(p))[:, None]
    s = p[rows, :, top].sum(axis=1)                           # (F, 64)
    return (10.0 * np.log10(s + EPS)).astype(np.float32)


def frame_column_db(frame: np.ndarray) -> np.ndarray:
    """One frame (64, 22) complex -> one spectrogram column (64,) dB. The embedded unit of work."""
    return spectrogram_db(frame[None])[0]


def normalise(spec_db: np.ndarray, db_floor: float, db_ceil: float) -> np.ndarray:
    """Map dB to [0, 1] with fixed constants chosen at training time. Label-independent."""
    return np.clip((spec_db - db_floor) / (db_ceil - db_floor), 0.0, 1.0).astype(np.float32)


def motion_power(frames: np.ndarray) -> np.ndarray:
    """Per-frame moving power in the TOP_K range points, linear. Used only for offline labelling."""
    moving = _power_spectra(frames)[:, MOVING, :].sum(axis=1)
    return np.sort(moving, axis=1)[:, -TOP_K:].sum(axis=1)


def activity_track(frames: np.ndarray, smooth_frames: int = 25) -> Tuple[np.ndarray, np.ndarray]:
    """Offline labelling aid, not part of the device pipeline.

    Returns the smoothed motion score in dB above the recording's own median,
    and the raw linear motion power. A walk recording is mostly an empty beam,
    so its median is the noise floor and the walk stands out above it.
    """
    m = motion_power(frames)
    k = np.ones(smooth_frames, dtype=np.float64) / smooth_frames
    smooth = np.convolve(m, k, mode="same")
    return (10.0 * np.log10(smooth / np.median(m))).astype(np.float32), m
