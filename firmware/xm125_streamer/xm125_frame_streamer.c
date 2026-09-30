/* XM125 raw Sparse IQ frame streamer.
 *
 * Portions derived from the Acconeer A121 SDK for XM125 (a121-v1.13.0):
 * the sensor lifecycle follows Src/examples/getting_started/example_service.c
 * and the DMA transmit path follows Src/applications/acc_exploration_server_stm32.c.
 * Those files are Copyright (c) 2020-2024 Acconeer AB, licensed BSD 3-Clause;
 * see LICENSE-Acconeer-BSD-3-Clause.txt beside this file. The SDK's prebuilt
 * libraries and documents are under a separate, restrictive licence and are
 * deliberately not part of this repository.
 *
 * Configures the A121 with the campaign's frozen configuration, then pushes
 * every frame out of USART2 exactly as it leaves acc_processing_execute(). The
 * ESP32-S3 (firmware/radar_infer) does the DSP and the inference; this module
 * does no signal processing at all. Reasoning: agentContext/embeddedlink.md.
 *
 * The link is LISTEN-ONLY. The ESP32's TX is not connected, so this firmware
 * never reads the UART, never accepts a command, and never negotiates a baud
 * rate. It starts streaming as soon as the sensor is ready and does not wait
 * for anything on the RX line -- see "Deliberately not copied" below.
 *
 * Wire format, repeated once per frame, little-endian:
 *
 *     A1 21 F0 0D | 64 sweeps x 22 range points of { int16 real, int16 imag }
 *                 = 4 + 5632 bytes, 25 times a second = 141 kB/s
 *
 * Built against the Acconeer XM125 SDK a121-v1.13.0. This file lives in the
 * project repo, not in the SDK tree: see build.sh.
 *
 * Deliberately not copied from acc_exploration_server_stm32.c: its
 * acconeer_main() spins until the host releases the UART RX line. Nothing
 * drives this module's RX pin once the ESP32 is wired in, so that loop would
 * never exit and the board would look dead.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "acc_config.h"
#include "acc_definitions_a121.h"
#include "acc_definitions_common.h"
#include "acc_hal_definitions_a121.h"
#include "acc_hal_integration_a121.h"
#include "acc_integration.h"
#include "acc_processing.h"
#include "acc_rss_a121.h"
#include "acc_sensor.h"
#include "acc_version.h"

#include "main.h"

#define SENSOR_ID         (1U)
#define SENSOR_TIMEOUT_MS (1000U)

/* 2 Mbaud is the module's ceiling and the campaign needs 141 kB/s of it.
 * There is no negotiation: both ends are fixed at this rate. */
#define STREAM_BAUDRATE (2000000U)

/* Frames carry no natural delimiter, so each is prefixed with a magic word.
 * It lets the receiver resynchronise on the next frame after any error rather
 * than emitting garbage for ever. Must match RADAR_FRAME_MAGIC in
 * firmware/radar_infer/main/main.c. */
static const uint8_t FRAME_MAGIC[4] = {0xA1, 0x21, 0xF0, 0x0D};

/* Expected frame geometry, from the frozen configuration. Checked at runtime:
 * if RSS ever hands back a different size, the ESP32's fixed-size reader would
 * silently desynchronise, so refuse to stream instead. */
#define EXPECTED_SWEEPS      (64U)
#define EXPECTED_RANGE       (22U)
#define EXPECTED_FRAME_POINT (EXPECTED_SWEEPS * EXPECTED_RANGE)

extern UART_HandleTypeDef EXPLORATION_SERVER_UART_HANDLE;

/* ------------------------------------------------------------------ UART */

/* Transmit path copied from acc_exploration_server_stm32.c, which already
 * solves chunking a buffer larger than one DMA transfer and waiting for
 * completion. A frame is 5632 B, so it fits one chunk, but the loop costs
 * nothing and keeps the code honest if the geometry ever changes. */
#define UART_DMA_BUFFER_SIZE (8192)

static volatile bool uart_tx_active = false;
static uint8_t       uart_dma_buffer[UART_DMA_BUFFER_SIZE];

static void uart_wait_for_tx_done(void)
{
	while (true)
	{
		__disable_irq();
		bool active = uart_tx_active;
		__enable_irq();

		if (active)
		{
			__WFI();
		}
		else
		{
			break;
		}
	}
}

static void uart_write(const void *data, uint32_t size)
{
	const uint8_t *data8 = (const uint8_t *)data;
	size_t         pos   = 0;

	while (pos < size)
	{
		uart_wait_for_tx_done();

		size_t this_size = ((size - pos) < UART_DMA_BUFFER_SIZE) ? (size - pos) : UART_DMA_BUFFER_SIZE;
		memcpy(uart_dma_buffer, &data8[pos], this_size);

		uart_tx_active = true;
		HAL_UART_Transmit_DMA(&EXPLORATION_SERVER_UART_HANDLE, uart_dma_buffer, this_size);
		pos += this_size;
	}
}

void HAL_UART_TxCpltCallback(UART_HandleTypeDef *h_uart)
{
	if (h_uart == &EXPLORATION_SERVER_UART_HANDLE)
	{
		uart_tx_active = false;
	}
}

/* Re-initialise the UART at the streaming rate. The deinit/init pair also
 * clears any pending garbage and error flags on the line -- without it a
 * previously-low RX line can trigger HAL_UART_ErrorCallback() and an extra
 * reset cycle. (Same reasoning as the exploration server's own startup.) */
static bool uart_set_stream_baudrate(void)
{
	uart_wait_for_tx_done();

	HAL_UART_DeInit(&EXPLORATION_SERVER_UART_HANDLE);
	EXPLORATION_SERVER_UART_HANDLE.Init.BaudRate = STREAM_BAUDRATE;

	return HAL_UART_Init(&EXPLORATION_SERVER_UART_HANDLE) == HAL_OK;
}

/* ---------------------------------------------------------------- sensor */

/* The campaign's frozen configuration, hash cce7ed611cae. Every one of these
 * values is load-bearing: 230 recordings and the trained model assume them.
 * Do not "improve" anything here -- changing a single value invalidates the
 * dataset and the model's dB constants alike. Mirrors
 * config/session_config.json in the project repo. */
static void set_config(acc_config_t *config)
{
	acc_config_sweeps_per_frame_set(config, EXPECTED_SWEEPS);
	acc_config_sweep_rate_set(config, 5500.0f);
	acc_config_frame_rate_set(config, 25.0f);
	acc_config_continuous_sweep_mode_set(config, false);
	acc_config_double_buffering_set(config, false);
	acc_config_inter_frame_idle_state_set(config, ACC_CONFIG_IDLE_STATE_DEEP_SLEEP);
	acc_config_inter_sweep_idle_state_set(config, ACC_CONFIG_IDLE_STATE_READY);

	acc_config_start_point_set(config, 400);
	acc_config_num_points_set(config, EXPECTED_RANGE);
	acc_config_step_length_set(config, 96);
	acc_config_profile_set(config, ACC_CONFIG_PROFILE_3);
	acc_config_hwaas_set(config, 4);
	acc_config_receiver_gain_set(config, 16);
	acc_config_enable_tx_set(config, true);
	acc_config_enable_loopback_set(config, false);
	acc_config_phase_enhancement_set(config, false);
	acc_config_iq_imbalance_compensation_set(config, false);
	acc_config_prf_set(config, ACC_CONFIG_PRF_13_0_MHZ);
}

static bool calibrate_and_prepare(acc_sensor_t *sensor, acc_config_t *config, void *buffer, uint32_t buffer_size)
{
	bool             status       = false;
	bool             cal_complete = false;
	acc_cal_result_t cal_result;
	const uint16_t   calibration_retries = 1U;

	/* Random disturbances can fail a calibration; retry at least once. */
	for (uint16_t i = 0; !status && (i <= calibration_retries); i++)
	{
		acc_hal_integration_sensor_disable(SENSOR_ID);
		acc_hal_integration_sensor_enable(SENSOR_ID);

		do
		{
			status = acc_sensor_calibrate(sensor, &cal_complete, &cal_result, buffer, buffer_size);

			if (status && !cal_complete)
			{
				status = acc_hal_integration_wait_for_sensor_interrupt(SENSOR_ID, SENSOR_TIMEOUT_MS);
			}
		} while (status && !cal_complete);
	}

	if (status)
	{
		acc_hal_integration_sensor_disable(SENSOR_ID);
		acc_hal_integration_sensor_enable(SENSOR_ID);

		status = acc_sensor_prepare(sensor, config, &cal_result, buffer, buffer_size);
	}

	return status;
}

static void cleanup(acc_config_t *config, acc_processing_t *processing, acc_sensor_t *sensor, void *buffer)
{
	acc_hal_integration_sensor_disable(SENSOR_ID);
	acc_hal_integration_sensor_supply_off(SENSOR_ID);

	if (sensor != NULL)
	{
		acc_sensor_destroy(sensor);
	}

	if (processing != NULL)
	{
		acc_processing_destroy(processing);
	}

	if (config != NULL)
	{
		acc_config_destroy(config);
	}

	if (buffer != NULL)
	{
		acc_integration_mem_free(buffer);
	}
}

/* ------------------------------------------------------------------ main */

int acconeer_main(int argc, char *argv[]);

int acconeer_main(int argc, char *argv[])
{
	(void)argc;
	(void)argv;

	acc_config_t             *config      = NULL;
	acc_processing_t         *processing  = NULL;
	acc_sensor_t             *sensor      = NULL;
	void                     *buffer      = NULL;
	uint32_t                  buffer_size = 0;
	acc_processing_metadata_t proc_meta;
	acc_processing_result_t   proc_result;

	/* Diagnostics go out at the boot baud rate, before the switch to 2 Mbaud,
	 * so a PC terminal can read them. Once streaming starts the line carries
	 * binary frames only -- anything printed after this point would be parsed
	 * as frame data by the receiver. */
	printf("XM125 frame streamer, RSS %s\n", acc_version_get());

	const acc_hal_a121_t *hal = acc_hal_rss_integration_get_implementation();

	if (!acc_rss_hal_register(hal))
	{
		printf("acc_rss_hal_register() failed\n");
		return EXIT_FAILURE;
	}

	config = acc_config_create();
	if (config == NULL)
	{
		printf("acc_config_create() failed\n");
		cleanup(config, processing, sensor, buffer);
		return EXIT_FAILURE;
	}

	set_config(config);
	acc_config_log(config);

	processing = acc_processing_create(config, &proc_meta);
	if (processing == NULL)
	{
		printf("acc_processing_create() failed\n");
		cleanup(config, processing, sensor, buffer);
		return EXIT_FAILURE;
	}

	/* Refuse to stream a geometry the receiver does not expect. Its reader is
	 * fixed at 5632 bytes per frame; a mismatch here would desynchronise it
	 * permanently and look like corrupt radar data rather than a config bug. */
	if (proc_meta.frame_data_length != EXPECTED_FRAME_POINT)
	{
		printf("frame_data_length is %u, expected %u -- refusing to stream\n",
		       (unsigned)proc_meta.frame_data_length, (unsigned)EXPECTED_FRAME_POINT);
		cleanup(config, processing, sensor, buffer);
		return EXIT_FAILURE;
	}

	const uint32_t frame_bytes = proc_meta.frame_data_length * sizeof(acc_int16_complex_t);

	if (!acc_rss_get_buffer_size(config, &buffer_size))
	{
		printf("acc_rss_get_buffer_size() failed\n");
		cleanup(config, processing, sensor, buffer);
		return EXIT_FAILURE;
	}

	buffer = acc_integration_mem_alloc(buffer_size);
	if (buffer == NULL)
	{
		printf("buffer allocation failed\n");
		cleanup(config, processing, sensor, buffer);
		return EXIT_FAILURE;
	}

	acc_hal_integration_sensor_supply_on(SENSOR_ID);
	acc_hal_integration_sensor_enable(SENSOR_ID);

	sensor = acc_sensor_create(SENSOR_ID);
	if (sensor == NULL)
	{
		printf("acc_sensor_create() failed\n");
		cleanup(config, processing, sensor, buffer);
		return EXIT_FAILURE;
	}

	if (!calibrate_and_prepare(sensor, config, buffer, buffer_size))
	{
		printf("calibration/prepare failed\n");
		acc_sensor_status(sensor);
		cleanup(config, processing, sensor, buffer);
		return EXIT_FAILURE;
	}

	printf("streaming %u B/frame at %u baud\n", (unsigned)frame_bytes, (unsigned)STREAM_BAUDRATE);

	if (!uart_set_stream_baudrate())
	{
		/* Nothing can be reported after this: the console is the same UART. */
		Error_Handler();
	}

	/* From here the line is binary frames only, for ever. */
	while (true)
	{
		if (!acc_sensor_measure(sensor))
		{
			acc_sensor_status(sensor);
			continue;
		}

		if (!acc_hal_integration_wait_for_sensor_interrupt(SENSOR_ID, SENSOR_TIMEOUT_MS))
		{
			acc_sensor_status(sensor);
			continue;
		}

		if (!acc_sensor_read(sensor, buffer, buffer_size))
		{
			acc_sensor_status(sensor);
			continue;
		}

		acc_processing_execute(processing, buffer, &proc_result);

		if (proc_result.calibration_needed)
		{
			/* Temperature drift. Recalibrate and drop this frame; the receiver
			 * resynchronises on the next magic word, and a dropped frame costs
			 * one column of a 64-column window. */
			if (!calibrate_and_prepare(sensor, config, buffer, buffer_size))
			{
				acc_sensor_status(sensor);
			}

			continue;
		}

		uart_write(FRAME_MAGIC, sizeof(FRAME_MAGIC));
		uart_write(proc_result.frame, frame_bytes);
	}

	/* Not reached. cleanup() exists for the failure paths above. */
}
