/* Pedestrian detector, ESP32-S3 side.
 *
 * Reads Sparse IQ frames from UART0, runs the features.py port frame by frame,
 * and invokes the int8 CNN every RADAR_HOP_FRAMES frames over the last
 * RADAR_PATCH_FRAMES columns. Prints one line per decision.
 *
 * During the prototype the frames come from tools/replay_frames.py on the PC.
 * On the finished rig they come from the XM125's UART_TX (J2 pin 11) with no
 * firmware change -- see agentContext/embeddedlink.md.
 *
 * Console output goes to USB Serial/JTAG (the other Type-C port), which is why
 * sdkconfig.defaults moves it there: UART0's pins carry data, not logs.
 */

#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "radar_features.h"
#include "radar_model.h"
#include "radar_selftest.h"

static const char *TAG = "radar";

/* ------------------------------------------------------------------- link */

/* UART1 on GPIO4, routed through the GPIO matrix.
 *
 * The board is an ESP32-S3-LCD-EV-Board, whose EXT_IO header (J5) exposes only
 * six real GPIOs; the rest of its pins drive the I/O expander. Of those six,
 * IO4 is the only one that can safely take a radar stream:
 *
 *   IO19 / IO20  native USB D-/D+ -- taking them kills the console and flashing
 *   IO0          BOOT strapping pin -- held low at reset the chip enters the
 *                download loader instead of running, an ugly intermittent fault
 *   IO47 / IO48  I2C for the touch panel and I/O expander, level-shifted on v1.5
 *   IO4          drives the RGB status LED only. Not a strapping pin, and the
 *                LED merely shows noise while frames arrive.
 *
 * UART0 (GPIO43/44) stays wired to the board's CP2102N and is left alone, so
 * the USB-to-UART port remains usable for anything else.
 */
#define RADAR_UART_NUM  UART_NUM_1
#define RADAR_UART_RX   4                    /* EXT_IO J5 pin 11 */
#define RADAR_UART_TX   UART_PIN_NO_CHANGE   /* the link is listen-only by design */
#define RADAR_BAUD      2000000
#define RADAR_RX_BUFFER (16 * 1024)

/* Frame framing. The sensor has no natural delimiter, so the sender prefixes
 * every frame with a magic word and we resync on it after any error. */
static const uint8_t FRAME_MAGIC[4] = {0xA1, 0x21, 0xF0, 0x0D};

/* ---------------------------------------------------------------- pipeline */

#define RADAR_HOP_FRAMES 4 /* one decision per 4 frames = 6.25 Hz at 25 fps */

/* Columns are quantised on arrival, so the ring holds int8 and one window is a
 * plain memcpy into the model's input tensor. 64 x 64 = 4 KB. */
static int8_t   ring[RADAR_PATCH_FRAMES][RADAR_N_DOPPLER];
static uint32_t ring_head;  /* next row to write */
static uint32_t ring_count; /* frames seen, saturating at PATCH_FRAMES */

static uint32_t positive_run;
static uint32_t frames_seen;
static uint32_t windows_seen;
static uint32_t events;

static void ring_push(const int8_t *column)
{
	memcpy(ring[ring_head], column, RADAR_N_DOPPLER);
	ring_head = (ring_head + 1) % RADAR_PATCH_FRAMES;

	if (ring_count < RADAR_PATCH_FRAMES)
	{
		ring_count++;
	}
}

/* Copy the ring into the input tensor oldest row first. */
static void ring_to_tensor(int8_t *dst)
{
	const uint32_t oldest = ring_head; /* the slot just past the newest */

	for (uint32_t r = 0; r < RADAR_PATCH_FRAMES; r++)
	{
		memcpy(dst + r * RADAR_N_DOPPLER,
		       ring[(oldest + r) % RADAR_PATCH_FRAMES],
		       RADAR_N_DOPPLER);
	}
}

static void handle_frame(const radar_sample_t *frame)
{
	float  column_db[RADAR_N_DOPPLER];
	int8_t column_q[RADAR_N_DOPPLER];

	const int64_t t0 = esp_timer_get_time();

	radar_frame_to_column(frame, column_db);

	for (int k = 0; k < RADAR_N_DOPPLER; k++)
	{
		column_q[k] = radar_quantise_db(column_db[k]);
	}

	const int64_t t_dsp = esp_timer_get_time() - t0;

	ring_push(column_q);
	frames_seen++;

	if (ring_count < RADAR_PATCH_FRAMES || (frames_seen % RADAR_HOP_FRAMES) != 0)
	{
		return;
	}

	ring_to_tensor(radar_model_input());

	const int64_t t1 = esp_timer_get_time();
	int8_t        q_out;

	if (!radar_model_invoke(&q_out))
	{
		ESP_LOGE(TAG, "invoke failed");
		return;
	}

	const int64_t t_inf = esp_timer_get_time() - t1;

	const float probability = ((float)q_out - (float)RADAR_OUT_ZERO_POINT) * RADAR_OUT_SCALE;
	const bool  positive    = probability >= RADAR_DECISION_THRESHOLD;

	positive_run = positive ? (positive_run + 1) : 0;

	const bool detection = (positive_run == RADAR_EVENT_RUN);

	if (detection)
	{
		events++;
	}

	windows_seen++;

	/* One machine-readable line per window; tools/replay_frames.py parses these
	 * and compares them with what features.py + the tflite model say. */
	printf("WIN %lu frame=%lu q=%d p=%.4f pos=%d run=%lu det=%d dsp_us=%lld inf_us=%lld\n",
	       (unsigned long)windows_seen,
	       (unsigned long)frames_seen,
	       (int)q_out,
	       probability,
	       (int)positive,
	       (unsigned long)positive_run,
	       (int)detection,
	       (long long)t_dsp,
	       (long long)t_inf);
	fflush(stdout);
}

/* -------------------------------------------------------------------- link */

/* Probe the receive pin as a plain input before handing it to the UART.
 *
 * Three outcomes, three different faults:
 *   always high  - wire connected to an idle UART line, but no data on it
 *   always low   - wire on ground, or the far end is holding the line low
 *   transitions  - data is present, so any silence afterwards is UART config
 * and "floating" shows up as a low transition count with both levels seen. */
static void probe_rx_pin(void)
{
	const gpio_config_t cfg = {
	    .pin_bit_mask = 1ULL << RADAR_UART_RX,
	    .mode         = GPIO_MODE_INPUT,
	    .pull_up_en   = GPIO_PULLUP_DISABLE,
	    .pull_down_en = GPIO_PULLDOWN_DISABLE,
	    .intr_type    = GPIO_INTR_DISABLE,
	};

	ESP_ERROR_CHECK(gpio_config(&cfg));

	int           last        = gpio_get_level(RADAR_UART_RX);
	uint32_t      transitions = 0;
	uint32_t      high        = 0;
	uint32_t      samples     = 0;
	const int64_t until       = esp_timer_get_time() + 200000; /* 200 ms */

	while (esp_timer_get_time() < until)
	{
		const int now = gpio_get_level(RADAR_UART_RX);

		samples++;
		high += (now != 0);

		if (now != last)
		{
			transitions++;
			last = now;
		}
	}

	ESP_LOGI(TAG, "GPIO%d probe: %lu%% high, %lu transitions in %lu samples over 200 ms",
	         RADAR_UART_RX, (unsigned long)(high * 100 / samples),
	         (unsigned long)transitions, (unsigned long)samples);

	if (transitions == 0)
	{
		ESP_LOGW(TAG, "  pin is static %s -- %s", high ? "HIGH" : "LOW",
		         high ? "line idle: connected but nothing transmitting, or wrong wire"
		              : "wire on GND, or nothing connected and the pin is floating low");
	}
}

static void link_init(void)
{
	const uart_config_t cfg = {
	    .baud_rate  = RADAR_BAUD,
	    .data_bits  = UART_DATA_8_BITS,
	    .parity     = UART_PARITY_DISABLE,
	    .stop_bits  = UART_STOP_BITS_1,
	    .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
	    .source_clk = UART_SCLK_DEFAULT,
	};

	ESP_ERROR_CHECK(uart_driver_install(RADAR_UART_NUM, RADAR_RX_BUFFER, 0, 0, NULL, 0));
	ESP_ERROR_CHECK(uart_param_config(RADAR_UART_NUM, &cfg));
	ESP_ERROR_CHECK(uart_set_pin(RADAR_UART_NUM, RADAR_UART_TX, RADAR_UART_RX,
	                             UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
}

/* Block until the magic word has been seen, byte by byte, so a desynchronised
 * stream recovers on the next frame instead of producing garbage for ever.
 *
 * While hunting, report progress every few seconds. Silence on this link has
 * two very different causes that look identical from outside -- no bytes at all
 * (wiring) versus bytes that never sync (baud, inversion, wrong source) -- and
 * the byte count separates them immediately. */
static void wait_for_magic(void)
{
	size_t   matched = 0;
	uint32_t seen    = 0;
	uint8_t  first[8];
	size_t   kept    = 0;
	int64_t  last_report = esp_timer_get_time();

	while (matched < sizeof(FRAME_MAGIC))
	{
		uint8_t b;

		if (uart_read_bytes(RADAR_UART_NUM, &b, 1, pdMS_TO_TICKS(500)) != 1)
		{
			if (esp_timer_get_time() - last_report > 3000000)
			{
				ESP_LOGW(TAG, "no data on GPIO%d: %lu bytes since boot",
				         RADAR_UART_RX, (unsigned long)seen);
				last_report = esp_timer_get_time();
			}

			continue;
		}

		seen++;
		if (kept < sizeof(first))
		{
			first[kept++] = b;
		}

		if (esp_timer_get_time() - last_report > 3000000)
		{
			ESP_LOGW(TAG, "%lu bytes but no frame magic yet; first bytes were "
			              "%02x %02x %02x %02x %02x %02x %02x %02x",
			         (unsigned long)seen, first[0], first[1], first[2], first[3],
			         first[4], first[5], first[6], first[7]);
			last_report = esp_timer_get_time();
		}

		matched = (b == FRAME_MAGIC[matched]) ? (matched + 1)
		                                      : ((b == FRAME_MAGIC[0]) ? 1 : 0);
	}
}

static bool read_frame(radar_sample_t *frame)
{
	uint8_t *dst      = (uint8_t *)frame;
	int      remaining = RADAR_FRAME_BYTES;

	while (remaining > 0)
	{
		const int n = uart_read_bytes(RADAR_UART_NUM, dst, remaining, pdMS_TO_TICKS(2000));

		if (n <= 0)
		{
			ESP_LOGW(TAG, "frame timed out with %d bytes missing, resyncing", remaining);
			return false;
		}

		dst += n;
		remaining -= n;
	}

	return true;
}

/* -------------------------------------------------------------------- main */

void app_main(void)
{
	printf("\n");
	ESP_LOGI(TAG, "pedestrian detector: %d sweeps x %d range, window %d, hop %d",
	         RADAR_N_SWEEPS, RADAR_N_RANGE, RADAR_PATCH_FRAMES, RADAR_HOP_FRAMES);

	/* Prove the DSP against a frame exported from test_vectors.npz before
	 * trusting anything this build says about live data. */
	if (!radar_selftest_run())
	{
		ESP_LOGE(TAG, "SELFTEST FAILED -- the C port does not match features.py");
	}

	if (!radar_model_init())
	{
		ESP_LOGE(TAG, "model init failed");
		return;
	}

	probe_rx_pin();
	link_init();
	ESP_LOGI(TAG, "listening on UART%d rx=GPIO%d (EXT_IO J5 pin 11) at %d baud, %d bytes/frame",
	         RADAR_UART_NUM, RADAR_UART_RX, RADAR_BAUD, RADAR_FRAME_BYTES);

	static radar_sample_t frame[RADAR_FRAME_SAMPLES]; /* 5632 B */

	while (true)
	{
		wait_for_magic();

		if (read_frame(frame))
		{
			handle_frame(frame);
		}
	}
}
