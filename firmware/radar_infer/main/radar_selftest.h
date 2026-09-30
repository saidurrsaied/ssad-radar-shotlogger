/* Boot-time check that the C DSP reproduces features.py on a known frame. */
#pragma once

#include <stdbool.h>

/* Runs the embedded test frame through radar_frame_to_column() and compares
 * with the expected column. Logs the worst dB error and any int8 mismatch.
 * Returns false if the port is wrong -- the firmware keeps running so the
 * failure can be inspected, but no accuracy number from that build means
 * anything. */
bool radar_selftest_run(void);
