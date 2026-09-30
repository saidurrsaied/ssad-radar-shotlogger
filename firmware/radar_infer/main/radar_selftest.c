#include "radar_selftest.h"

#include <math.h>

#include "esp_log.h"
#include "esp_timer.h"

#include "radar_features.h"
#include "radar_selftest_data.h"

static const char *TAG = "selftest";

/* The reference port of these listings agreed with features.py to 8e-6 dB, so
 * anything above a thousandth of a dB means a real difference, not float
 * noise. The int8 comparison below is the one that has to be exact. */
#define DB_TOLERANCE 1.0e-3f

bool radar_selftest_run(void)
{
	float column_db[RADAR_N_DOPPLER];

	const int64_t t0 = esp_timer_get_time();

	radar_frame_to_column((const radar_sample_t *)radar_selftest_frame, column_db);

	const int64_t elapsed = esp_timer_get_time() - t0;

	float worst_db  = 0.0f;
	int   worst_bin = -1;
	int   q_errors  = 0;

	for (int k = 0; k < RADAR_N_DOPPLER; k++)
	{
		const float err = fabsf(column_db[k] - radar_selftest_expected_db[k]);

		if (err > worst_db)
		{
			worst_db  = err;
			worst_bin = k;
		}

		if (radar_quantise_db(column_db[k]) != radar_selftest_expected_q[k])
		{
			q_errors++;
		}
	}

	const bool ok = (worst_db <= DB_TOLERANCE) && (q_errors == 0);

	ESP_LOGI(TAG, "%s: worst %.6f dB at bin %d, int8 mismatches %d/%d, %lld us/frame",
	         ok ? "PASS" : "FAIL", worst_db, worst_bin, q_errors, RADAR_N_DOPPLER,
	         (long long)elapsed);

	if (!ok)
	{
		ESP_LOGE(TAG, "bin %d: got %.6f dB, expected %.6f dB",
		         worst_bin, column_db[worst_bin < 0 ? 0 : worst_bin],
		         radar_selftest_expected_db[worst_bin < 0 ? 0 : worst_bin]);
	}

	/* 25 frames a second is the budget; this number says how much of it the
	 * DSP costs before inference is added. */
	ESP_LOGI(TAG, "DSP is %.1f%% of the 40 ms frame budget", (double)elapsed / 400.0);

	return ok;
}
