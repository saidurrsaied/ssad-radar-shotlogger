/* Embedded port of training/features.py -- one Sparse IQ frame -> one spectrogram column.
 *
 * features.py is the contract. Change a step or a constant there and this file,
 * radar_features_config.h and test_vectors.npz all have to be regenerated together.
 */
#pragma once

#include <stdint.h>

#include "radar_features_config.h"

/* One frame as it arrives from the sensor: RADAR_N_SWEEPS x RADAR_N_RANGE
 * complex samples, sweep-major (sweep 0 range 0..21, sweep 1 range 0..21, ...).
 * That is the layout acc_processing_execute() hands back, and the layout
 * tools/replay_frames.py sends over UART. */
typedef struct
{
	int16_t real;
	int16_t imag;
} radar_sample_t;

#define RADAR_FRAME_SAMPLES (RADAR_N_SWEEPS * RADAR_N_RANGE)
#define RADAR_FRAME_BYTES   (RADAR_FRAME_SAMPLES * (int)sizeof(radar_sample_t))

/* Steps 1-6 of features.py for a single frame.
 * out_db must hold RADAR_N_DOPPLER floats. Deterministic, no history, no allocation. */
void radar_frame_to_column(const radar_sample_t *frame, float *out_db);

/* normalise() + int8 quantisation, with the constants frozen at training time. */
int8_t radar_quantise_db(float db);
