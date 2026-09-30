/* The 480x480 panel on the ESP32-S3-LCD-EV-Board: what the detector is seeing
 * and what it decided.
 *
 * Deliberately not LVGL. The BSP's own benchmark puts LVGL at ~97 % CPU on this
 * board, and the detector already needs 21 % of a core; drawing straight into
 * the RGB panel costs a 640-byte copy per frame instead. bsp_display_new()
 * gives the panel handle without bringing LVGL up at all.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Brings up the panel and paints the static furniture. Safe to call once,
 * after the model is ready. Returns false if the panel did not initialise --
 * the detector then carries on headless rather than refusing to run. */
bool radar_display_init(void);

/* One spectrogram column, as it goes into the model: RADAR_N_DOPPLER int8
 * values, already normalised and quantised. Call once per frame. */
void radar_display_push_column(const int8_t *column_q);

/* The result of one inference. Call once per decision. */
void radar_display_set_status(float probability, bool positive, bool detection);
