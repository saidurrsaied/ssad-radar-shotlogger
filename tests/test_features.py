#!/usr/bin/env python3
"""Tests for training/features.py, the reference feature extraction.

That module is the specification the embedded C port is proved against, so the
properties that matter are pinned here with synthetic frames whose correct
answer is known in advance: a target moving at a known Doppler lands in a known
bin, static clutter vanishes, and the range selection follows the mover rather
than the loudest static reflector.

    .venv/bin/python -m unittest tests.test_features -v
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "training"))

import features  # noqa: E402

RNG = np.random.default_rng(0)


def noise_frames(n=4, sigma=1.0):
    shape = (n, features.N_SWEEPS, features.N_RANGE)
    return (RNG.normal(0, sigma, shape) + 1j * RNG.normal(0, sigma, shape)).astype(np.complex64)


def tone(doppler_bins, amplitude, n=4):
    """A target whose phase advances doppler_bins * 2*pi/64 per sweep."""
    k = np.arange(features.N_SWEEPS)
    return (amplitude * np.exp(2j * np.pi * doppler_bins * k / features.N_SWEEPS))[None, :].repeat(n, 0)


class TestShapes(unittest.TestCase):
    def test_one_column_per_frame(self):
        out = features.spectrogram_db(noise_frames(7))
        self.assertEqual(out.shape, (7, features.N_DOPPLER))
        self.assertEqual(out.dtype, np.float32)

    def test_frame_at_a_time_matches_batch(self):
        # The device computes one frame at a time; batching must not change the answer.
        frames = noise_frames(5)
        batch = features.spectrogram_db(frames)
        single = np.stack([features.frame_column_db(f) for f in frames])
        np.testing.assert_allclose(batch, single, rtol=0, atol=1e-5)

    def test_window_is_symmetric_hann(self):
        w = features.HANN
        self.assertAlmostEqual(float(w[0]), 0.0, places=6)
        self.assertAlmostEqual(float(w[-1]), 0.0, places=6)
        np.testing.assert_allclose(w, w[::-1], atol=1e-6)


class TestDoppler(unittest.TestCase):
    def test_moving_target_lands_in_its_bin(self):
        for shift in (-10, -4, 5, 12):
            frames = noise_frames(sigma=0.01)
            frames[:, :, 9] += tone(shift, 100.0)
            col = features.spectrogram_db(frames)[0]
            self.assertEqual(int(np.argmax(col)), features.ZERO_BIN + shift, f"shift {shift}")

    def test_static_clutter_is_removed(self):
        # Same scene with and without a huge reflector that does not move, sharing
        # a range point with a walker -- the case clutter removal exists for.
        # Two movers fix which range points TOP_K selects, so the comparison is exact.
        scene = noise_frames(sigma=1.0)
        scene[:, :, 5] += tone(-7, 200.0)
        scene[:, :, 17] += tone(4, 150.0)
        cluttered = scene.copy()
        cluttered[:, :, 5] += 5000.0 + 2000.0j
        # 0.05 dB, not 0: subtracting a ~5000 mean in float32 leaves ~1e-4 of rounding,
        # which is ~0.01 dB in the quietest near-zero-Doppler bin. The C port runs in
        # float32 too. Without clutter removal the error here is tens of dB.
        np.testing.assert_allclose(
            features.spectrogram_db(cluttered), features.spectrogram_db(scene), atol=0.05
        )


class TestRangeSelection(unittest.TestCase):
    def test_follows_the_mover_not_the_loudest_static_point(self):
        frames = noise_frames(sigma=0.01)
        frames[:, :, 2] += 10000.0                   # loud but static
        frames[:, :, 15] += tone(-6, 50.0)           # quiet but moving
        col = features.spectrogram_db(frames)[0]
        self.assertEqual(int(np.argmax(col)), features.ZERO_BIN - 6)
        # and it is well above the noise-only level
        self.assertGreater(float(col.max()), float(np.median(col)) + 30.0)

    def test_weak_mover_beats_summing_every_range_point(self):
        # The reason for TOP_K: summing all 22 points would bury a target that
        # sits in one point under the noise of the other 21.
        frames = noise_frames(n=200, sigma=1.0)
        frames[:, :, 11] += tone(-5, 3.0, n=200)
        col = features.spectrogram_db(frames).mean(axis=0)
        p = features._power_spectra(frames).sum(axis=2).mean(axis=0)
        naive = 10 * np.log10(p)
        contrast_top_k = col[features.ZERO_BIN - 5] - np.median(col)
        contrast_naive = naive[features.ZERO_BIN - 5] - np.median(naive)
        self.assertGreater(contrast_top_k, contrast_naive + 6.0)


class TestNormalise(unittest.TestCase):
    def test_maps_and_clips(self):
        x = np.array([10.0, 20.0, 30.0, 40.0], dtype=np.float32)
        np.testing.assert_allclose(features.normalise(x, 20.0, 30.0), [0.0, 0.0, 1.0, 1.0])
        self.assertAlmostEqual(float(features.normalise(np.float32(25.0), 20.0, 30.0)), 0.5)


if __name__ == "__main__":
    unittest.main()
