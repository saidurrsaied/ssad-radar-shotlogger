"""The two firmwares and the replay tool must agree with each other.

The XM125 streams frames, the ESP32-S3 reads them, and tools/replay_frames.py
impersonates the XM125. Nothing at runtime checks that the three agree: a
mismatched magic word or frame size produces a receiver that resynchronises
for ever and looks like broken radar data, not like a constant that drifted.

The XM125 firmware also carries the campaign's frozen configuration in C. If
that ever diverges from config/session_config.json, the module would record
under a configuration the model was not trained on -- the same class of error
the frozen-config invariant exists to prevent, one layer further down.

No hardware and no toolchain needed; these are source-text checks.
"""

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

XM125 = REPO / "firmware" / "xm125_streamer" / "xm125_frame_streamer.c"
ESP32 = REPO / "firmware" / "radar_infer" / "main" / "main.c"
REPLAY = REPO / "tools" / "replay_frames.py"
FEATURES_CONFIG = REPO / "training" / "out" / "final" / "radar_features_config.h"
SESSION_CONFIG = REPO / "config" / "session_config.json"

# How each frozen-config field is spelled in the C source. Values that are not
# plain numbers (idle states, profile, PRF) map to their enum names.
C_SETTERS = {
    "sweeps_per_frame": ("acc_config_sweeps_per_frame_set", str),
    # floats are compared numerically below, not as text
    "sweep_rate": ("acc_config_sweep_rate_set", float),
    "frame_rate": ("acc_config_frame_rate_set", float),
    "continuous_sweep_mode": ("acc_config_continuous_sweep_mode_set", lambda v: str(v).lower()),
    "double_buffering": ("acc_config_double_buffering_set", lambda v: str(v).lower()),
    "start_point": ("acc_config_start_point_set", str),
    "num_points": ("acc_config_num_points_set", str),
    "step_length": ("acc_config_step_length_set", str),
    "hwaas": ("acc_config_hwaas_set", str),
    "receiver_gain": ("acc_config_receiver_gain_set", str),
    "enable_tx": ("acc_config_enable_tx_set", lambda v: str(v).lower()),
    "enable_loopback": ("acc_config_enable_loopback_set", lambda v: str(v).lower()),
    "phase_enhancement": ("acc_config_phase_enhancement_set", lambda v: str(v).lower()),
    "iq_imbalance_compensation": ("acc_config_iq_imbalance_compensation_set", lambda v: str(v).lower()),
}

ENUM_SETTERS = {
    "profile": ("acc_config_profile_set", {"PROFILE_3": "ACC_CONFIG_PROFILE_3"}),
    "prf": ("acc_config_prf_set", {"PRF_13_0_MHz": "ACC_CONFIG_PRF_13_0_MHZ"}),
    "inter_frame_idle_state": (
        "acc_config_inter_frame_idle_state_set",
        {"DEEP_SLEEP": "ACC_CONFIG_IDLE_STATE_DEEP_SLEEP"},
    ),
    "inter_sweep_idle_state": (
        "acc_config_inter_sweep_idle_state_set",
        {"READY": "ACC_CONFIG_IDLE_STATE_READY"},
    ),
}


def c_call_argument(source: str, function: str) -> str:
    """The second argument of a single-call `function(config, <arg>);`."""
    matches = re.findall(rf"{re.escape(function)}\(\s*config\s*,\s*([^)]+?)\s*\)", source)
    assert len(matches) == 1, f"expected exactly one call to {function}, found {len(matches)}"
    return matches[0]


class TestWireFormat(unittest.TestCase):
    """Sender, receiver and replay tool must describe the same bytes."""

    def test_magic_word_matches_everywhere(self):
        pattern = r"0x([0-9A-Fa-f]{2})\s*,\s*0x([0-9A-Fa-f]{2})\s*,\s*0x([0-9A-Fa-f]{2})\s*,\s*0x([0-9A-Fa-f]{2})"

        xm125 = re.search(rf"FRAME_MAGIC\[4\]\s*=\s*\{{{pattern}", XM125.read_text())
        esp32 = re.search(rf"FRAME_MAGIC\[4\]\s*=\s*\{{{pattern}", ESP32.read_text())
        self.assertIsNotNone(xm125, "no FRAME_MAGIC in the XM125 firmware")
        self.assertIsNotNone(esp32, "no FRAME_MAGIC in the ESP32 firmware")

        replay = re.search(r"MAGIC\s*=\s*bytes\(\(([^)]+)\)\)", REPLAY.read_text())
        self.assertIsNotNone(replay, "no MAGIC in replay_frames.py")
        replay_bytes = [int(v.strip(), 0) for v in replay.group(1).split(",")]

        xm125_bytes = [int(v, 16) for v in xm125.groups()]
        esp32_bytes = [int(v, 16) for v in esp32.groups()]

        self.assertEqual(xm125_bytes, esp32_bytes, "XM125 and ESP32 magic words differ")
        self.assertEqual(xm125_bytes, replay_bytes, "replay_frames.py magic word differs")

    def test_frame_geometry_matches(self):
        """64 sweeps x 22 range points, agreed by both firmwares and the DSP header."""
        xm125 = XM125.read_text()
        sweeps = int(re.search(r"#define EXPECTED_SWEEPS\s+\((\d+)U\)", xm125).group(1))
        ranges = int(re.search(r"#define EXPECTED_RANGE\s+\((\d+)U\)", xm125).group(1))

        header = FEATURES_CONFIG.read_text()
        n_sweeps = int(re.search(r"#define RADAR_N_SWEEPS\s+(\d+)", header).group(1))
        n_range = int(re.search(r"#define RADAR_N_RANGE\s+(\d+)", header).group(1))

        self.assertEqual(sweeps, n_sweeps, "sweeps per frame differs from radar_features_config.h")
        self.assertEqual(ranges, n_range, "range points differ from radar_features_config.h")

        # The receiver reads a fixed number of bytes per frame; 4 bytes per
        # complex sample is what both sides assume.
        self.assertEqual(sweeps * ranges * 4, 5632)


class TestFrozenConfigInFirmware(unittest.TestCase):
    """The C configuration must equal config/session_config.json."""

    @classmethod
    def setUpClass(cls):
        cls.source = XM125.read_text()
        cfg = json.loads(SESSION_CONFIG.read_text())
        session = cfg["groups"][0]["1"]
        cls.session = session
        cls.subsweep = session["subsweeps"][0]

    def _value(self, key):
        return self.session.get(key, self.subsweep.get(key))

    def test_numeric_and_boolean_fields(self):
        for key, (function, render) in C_SETTERS.items():
            with self.subTest(field=key):
                expected = render(self._value(key))
                actual = c_call_argument(self.source, function)

                # EXPECTED_SWEEPS / EXPECTED_RANGE are passed by name, not literal.
                if actual in ("EXPECTED_SWEEPS", "EXPECTED_RANGE"):
                    actual = re.search(rf"#define {actual}\s+\((\d+)U\)", self.source).group(1)

                if render is float:
                    # 5500.0f and 5500f are the same number; do not compare text.
                    self.assertAlmostEqual(float(actual.rstrip("fF")), expected, places=6,
                                           msg=f"{key}: firmware {actual}, json {expected}")
                else:
                    self.assertEqual(actual, expected,
                                     f"{key}: firmware says {actual}, session_config.json says {expected}")

    def test_enum_fields(self):
        for key, (function, mapping) in ENUM_SETTERS.items():
            with self.subTest(field=key):
                json_value = self._value(key)
                self.assertIn(json_value, mapping,
                              f"{key}={json_value} has no known C enum; update this test deliberately")
                self.assertEqual(c_call_argument(self.source, function), mapping[json_value])

    def test_streams_at_the_rate_the_link_was_budgeted_for(self):
        """141 kB/s of a 2 Mbaud link. Lower the baud and frames start dropping."""
        baud = int(re.search(r"#define STREAM_BAUDRATE\s+\((\d+)U\)", self.source).group(1))
        self.assertEqual(baud, 2_000_000)

        frame_rate = self.session["frame_rate"]
        bytes_per_second = 64 * 22 * 4 * frame_rate
        # 10 bits on the wire per byte with 8N1 framing.
        self.assertLess(bytes_per_second * 10, baud,
                        "the frozen config needs more bandwidth than the link provides")


if __name__ == "__main__":
    unittest.main()
