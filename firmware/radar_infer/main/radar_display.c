/* See radar_display.h. Layout of the 480x480 panel:
 *
 *   y    0.. 99   status: PEDESTRIAN / CLEAR, and the probability as a bar
 *   y  100..419   spectrogram: 64 Doppler bins x 5 px, time scrolling left to right
 *   y  420..479   velocity axis and a legend
 *
 * The spectrogram writes one column per frame and wraps, with a bright cursor
 * at the write position -- a radar-sweep scroll. That costs one 640-byte
 * transfer per frame instead of shifting a 460 KB framebuffer 25 times a second.
 */

#include "radar_display.h"

#include <stdio.h>
#include <string.h>

#include "bsp/display.h"
#include "bsp/esp32_s3_lcd_ev_board.h"
#include "esp_err.h"
#include "esp_lcd_panel_ops.h"
#include "esp_log.h"

#include "radar_features_config.h"
#include "radar_font.h"

static const char *TAG = "display";

#define SCREEN_W 480
#define SCREEN_H 480

#define STATUS_Y    0
#define STATUS_H    100
#define SPECTRO_Y   100
#define BIN_PX      5                                  /* pixels per Doppler bin */
#define SPECTRO_H   (RADAR_N_DOPPLER * BIN_PX)         /* 320 */
#define AXIS_Y      (SPECTRO_Y + SPECTRO_H)            /* 420 */
#define AXIS_H      (SCREEN_H - AXIS_Y)                /* 60 */

#define RGB(r, g, b) ((uint16_t)((((r) & 0xf8) << 8) | (((g) & 0xfc) << 3) | ((b) >> 3)))

#define COL_BG     RGB(8, 10, 16)
#define COL_TEXT   RGB(200, 210, 225)
#define COL_ALERT  RGB(255, 64, 48)
#define COL_CLEAR  RGB(64, 200, 110)
#define COL_CURSOR RGB(255, 255, 255)
#define COL_GRID   RGB(60, 66, 80)

static esp_lcd_panel_handle_t panel;
static bool                   ready;
static int                    cursor_x;

/* One column of the spectrogram, plus a scratch row for text and bars. */
static uint16_t column[SPECTRO_H];
static uint16_t scratch[SCREEN_W];

/* ------------------------------------------------------------------ paint */

static void fill(int x, int y, int w, int h, uint16_t colour)
{
	for (int i = 0; i < w && i < SCREEN_W; i++)
	{
		scratch[i] = colour;
	}

	for (int row = 0; row < h; row++)
	{
		esp_lcd_panel_draw_bitmap(panel, x, y + row, x + w, y + row + 1, scratch);
	}
}

/* Draw one glyph scaled by `scale`, into a temporary buffer and out in one go. */
static void draw_char(int x, int y, char c, int scale, uint16_t fg, uint16_t bg)
{
	if (c < FONT_FIRST || c > FONT_LAST)
	{
		c = ' ';
	}

	const uint8_t *glyph = font5x7[c - FONT_FIRST];
	const int      w     = FONT_W * scale;

	for (int row = 0; row < FONT_H * scale; row++)
	{
		const int bit = row / scale;

		for (int col = 0; col < w; col++)
		{
			const int byte = col / scale;

			scratch[col] = (glyph[byte] >> bit) & 1 ? fg : bg;
		}

		esp_lcd_panel_draw_bitmap(panel, x, y + row, x + w, y + row + 1, scratch);
	}
}

static void draw_text(int x, int y, const char *s, int scale, uint16_t fg, uint16_t bg)
{
	for (const char *p = s; *p; p++)
	{
		draw_char(x, y, *p, scale, fg, bg);
		x += (FONT_W + 1) * scale;
	}
}

/* Black -> blue -> green -> yellow -> red, over the model's own 0..255 input
 * range, so what the screen shows is exactly what the network is fed. */
static uint16_t heat(uint8_t v)
{
	if (v < 64)
	{
		return RGB(0, 0, v * 3);
	}

	if (v < 128)
	{
		return RGB(0, (v - 64) * 4, 192 - (v - 64) * 3);
	}

	if (v < 192)
	{
		return RGB((v - 128) * 4, 255, 0);
	}

	return RGB(255, 255 - (v - 192) * 4, 0);
}

/* ------------------------------------------------------------------- public */

bool radar_display_init(void)
{
	const bsp_display_config_t cfg = {.max_transfer_sz = SCREEN_W * 40 * sizeof(uint16_t)};

	if (bsp_display_new(&cfg, &panel, NULL) != ESP_OK)
	{
		ESP_LOGE(TAG, "bsp_display_new() failed -- running headless");
		return false;
	}

	/* No esp_lcd_panel_disp_on_off() here. The BSP initialises the GC9503 over
	 * SPI and then hands the panel to the RGB peripheral with
	 * `.auto_del_panel_io = 1`, so the SPI handle is already gone and the call
	 * fails with "Panel IO is deleted". The panel is on by then anyway. */
	bsp_display_backlight_on();

	ready = true;

	fill(0, 0, SCREEN_W, SCREEN_H, COL_BG);
	draw_text(12, 20, "RADAR PEDESTRIAN", 3, COL_TEXT, COL_BG);
	draw_text(12, 60, "WAITING FOR FRAMES", 2, COL_GRID, COL_BG);

	/* Velocity axis. Zero Doppler sits at RADAR_ZERO_BIN, and an approaching
	 * walker lands below it -- see agentContext/binarymodel.md. */
	fill(0, AXIS_Y, SCREEN_W, AXIS_H, COL_BG);
	draw_text(8, AXIS_Y + 8, "APPROACH", 2, COL_TEXT, COL_BG);
	draw_text(SCREEN_W - 8 - 7 * (FONT_W + 1) * 2, AXIS_Y + 8, "DEPART", 2, COL_TEXT, COL_BG);
	draw_text(8, AXIS_Y + 34, "TIME 19S", 1, COL_GRID, COL_BG);

	/* A line at zero velocity, so the eye has something to read the sweep against. */
	fill(0, SPECTRO_Y + RADAR_ZERO_BIN * BIN_PX, SCREEN_W, 1, COL_GRID);

	ESP_LOGI(TAG, "480x480 panel up: %d px per Doppler bin, %d s of history",
	         BIN_PX, SCREEN_W / 25);

	return true;
}

void radar_display_push_column(const int8_t *column_q)
{
	if (!ready)
	{
		return;
	}

	/* Bin 0 is the most negative velocity. Draw it at the top so approach is
	 * up and departure is down, matching the axis labels. */
	for (int bin = 0; bin < RADAR_N_DOPPLER; bin++)
	{
		const uint16_t colour = heat((uint8_t)(column_q[bin] + 128));

		for (int p = 0; p < BIN_PX; p++)
		{
			column[bin * BIN_PX + p] = colour;
		}
	}

	esp_lcd_panel_draw_bitmap(panel, cursor_x, SPECTRO_Y, cursor_x + 1, SPECTRO_Y + SPECTRO_H,
	                          column);

	cursor_x = (cursor_x + 1) % SCREEN_W;

	/* A white cursor one column ahead marks where the sweep is writing. */
	for (int i = 0; i < SPECTRO_H; i++)
	{
		column[i] = COL_CURSOR;
	}

	esp_lcd_panel_draw_bitmap(panel, cursor_x, SPECTRO_Y, cursor_x + 1, SPECTRO_Y + SPECTRO_H,
	                          column);
}

void radar_display_set_status(float probability, bool positive, bool detection)
{
	if (!ready)
	{
		return;
	}

	static bool     last_state  = false;
	static bool     painted     = false;
	const uint16_t  colour      = detection ? COL_ALERT : (positive ? COL_TEXT : COL_CLEAR);
	const char     *label       = detection ? "PEDESTRIAN" : (positive ? "MAYBE" : "CLEAR");

	/* The label only changes on a state change; repainting it every 160 ms
	 * would flicker for no reason. */
	if (!painted || detection != last_state)
	{
		fill(0, STATUS_Y, SCREEN_W, 52, COL_BG);
		draw_text(12, STATUS_Y + 6, label, 6, colour, COL_BG);
		last_state = detection;
		painted    = true;
	}

	/* The probability bar redraws every decision: it is the live part. */
	const int bar_y = STATUS_Y + 62;
	const int width = (int)(probability * (SCREEN_W - 24));

	fill(12, bar_y, SCREEN_W - 24, 18, COL_BG);
	if (width > 0)
	{
		fill(12, bar_y, width, 18, colour);
	}

	/* The 0.5 threshold, so the bar can be read without arithmetic. */
	fill(12 + (SCREEN_W - 24) / 2, bar_y - 3, 2, 24, COL_TEXT);

	char text[16];
	snprintf(text, sizeof(text), "P %3d%%", (int)(probability * 100.0f + 0.5f));
	draw_text(SCREEN_W - 12 - 6 * (FONT_W + 1) * 2, STATUS_Y + 12, text, 2, COL_TEXT, COL_BG);
}
