/* TFLite Micro wrapper. C interface so main.c stays C. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Builds the interpreter and allocates the arena. False means the model did
 * not fit or an op is missing from the resolver. */
bool radar_model_init(void);

/* The input tensor's data, RADAR_PATCH_FRAMES * RADAR_N_DOPPLER int8 values,
 * row-major with the oldest column first. Write here, then invoke. */
int8_t *radar_model_input(void);

/* Runs inference and returns the raw int8 output. Convert with
 * (q - RADAR_OUT_ZERO_POINT) * RADAR_OUT_SCALE. */
bool radar_model_invoke(int8_t *q_out);

/* Bytes of arena actually used -- worth logging once so the size is measured
 * rather than guessed. */
size_t radar_model_arena_used(void);

#ifdef __cplusplus
}
#endif
