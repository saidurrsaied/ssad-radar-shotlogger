"""Load the campaign's frozen session configuration.

The Shot Logger can load a configuration but never construct one -- that lives
in tools/freeze_session_config.py. Keeping construction out of this package is
what makes it impossible for the tool to record against a configuration nobody
chose.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from acconeer.exptool import a121


class FrozenConfigError(Exception):
    """The frozen config is missing or unusable. Message is shown to the user."""


@dataclass(frozen=True)
class FrozenConfig:
    session_config: a121.SessionConfig
    sha256: str
    path: Path

    @property
    def short_sha(self) -> str:
        return self.sha256[:12]

    def matches(self, other: a121.SessionConfig) -> bool:
        """True when a recording used this exact configuration.

        SessionConfig.__eq__ compares to_dict(), so this is real structural
        equality rather than object identity.
        """
        return bool(other == self.session_config)


def load(path: Path) -> FrozenConfig:
    if not path.is_file():
        raise FrozenConfigError(
            f"No frozen configuration at {path}.\n"
            f"Create it once with:  tools/freeze_session_config.py"
        )

    raw = path.read_text()

    try:
        session_config = a121.SessionConfig.from_json(raw)
    except Exception as exc:
        raise FrozenConfigError(f"{path} is not a valid SessionConfig:\n{exc}") from exc

    # A hand-edited file can parse into something that is not what the JSON
    # says, because the parser silently normalises unknown or reordered fields.
    # Refusing here beats recording a whole campaign against a config that does
    # not match its own file.
    if session_config.to_json() != raw:
        raise FrozenConfigError(
            f"{path} does not round-trip: the parsed configuration differs from the "
            f"file text. It was probably hand-edited. Regenerate it with "
            f"tools/freeze_session_config.py rather than editing it."
        )

    try:
        session_config.validate()
    except Exception as exc:
        raise FrozenConfigError(f"{path} is not a valid configuration:\n{exc}") from exc

    return FrozenConfig(
        session_config=session_config,
        sha256=hashlib.sha256(raw.encode()).hexdigest(),
        path=path,
    )


def expected_frame_rate(frozen: FrozenConfig) -> float:
    """Frames per second the configuration asks for.

    Used to turn a frame count into a duration without opening the recording's
    tick data.
    """
    sensor_config = frozen.session_config.sensor_config
    if sensor_config.frame_rate:
        return float(sensor_config.frame_rate)
    if sensor_config.sweep_rate and sensor_config.sweeps_per_frame:
        # Continuous mode: frames follow directly from the sweep rate.
        return float(sensor_config.sweep_rate) / float(sensor_config.sweeps_per_frame)
    return 0.0


def bytes_per_second(frozen: FrozenConfig) -> float:
    """Uncompressed data rate, for the disk pre-flight check."""
    sc = frozen.session_config.sensor_config
    return sc.sweeps_per_frame * sc.num_points * 4 * expected_frame_rate(frozen)
