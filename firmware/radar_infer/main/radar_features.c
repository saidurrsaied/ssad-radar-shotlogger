/* See radar_features.h. The step numbers below are the ones in features.py's docstring. */

#include "radar_features.h"

#include <math.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ tables */

/* Hann, written out rather than taken from a library so C and Python cannot
 * disagree about symmetric vs periodic: w[n] = 0.5 - 0.5 cos(2 pi n / 63). */
static float hann[RADAR_N_SWEEPS];

/* Twiddles for a 64-point radix-2 FFT, and the bit-reversal permutation. */
static float tw_re[RADAR_N_DOPPLER / 2];
static float tw_im[RADAR_N_DOPPLER / 2];
static uint8_t bitrev[RADAR_N_DOPPLER];

static bool tables_ready = false;

static void build_tables(void)
{
	for (int n = 0; n < RADAR_N_SWEEPS; n++)
	{
		hann[n] = 0.5f - 0.5f * cosf(2.0f * (float)M_PI * (float)n / (float)(RADAR_N_SWEEPS - 1));
	}

	for (int k = 0; k < RADAR_N_DOPPLER / 2; k++)
	{
		/* Forward transform, numpy's sign convention: exp(-2 pi i k / N). */
		tw_re[k] = cosf(-2.0f * (float)M_PI * (float)k / (float)RADAR_N_DOPPLER);
		tw_im[k] = sinf(-2.0f * (float)M_PI * (float)k / (float)RADAR_N_DOPPLER);
	}

	for (int i = 0; i < RADAR_N_DOPPLER; i++)
	{
		int r = 0;
		for (int b = 0; b < 6; b++) /* 64 = 2^6 */
		{
			r |= ((i >> b) & 1) << (5 - b);
		}

		bitrev[i] = (uint8_t)r;
	}

	tables_ready = true;
}

/* ---------------------------------------------------------------- 64-pt FFT */

/* In-place decimation-in-time radix-2. Input must already be bit-reversed.
 * No 1/N scaling -- numpy's forward transform does not scale either, and
 * scaling here would shift every dB value by -36.1 dB. */
static void fft64(float *re, float *im)
{
	for (int len = 2; len <= RADAR_N_DOPPLER; len <<= 1)
	{
		const int half = len / 2;
		const int step = RADAR_N_DOPPLER / len;

		for (int i = 0; i < RADAR_N_DOPPLER; i += len)
		{
			for (int j = 0; j < half; j++)
			{
				const float wr = tw_re[j * step];
				const float wi = tw_im[j * step];

				const int a = i + j;
				const int b = a + half;

				const float xr = re[b] * wr - im[b] * wi;
				const float xi = re[b] * wi + im[b] * wr;

				re[b] = re[a] - xr;
				im[b] = im[a] - xi;
				re[a] = re[a] + xr;
				im[a] = im[a] + xi;
			}
		}
	}
}

/* ------------------------------------------------------------------- steps */

void radar_frame_to_column(const radar_sample_t *frame, float *out_db)
{
	if (!tables_ready)
	{
		build_tables();
	}

	/* Power spectrum per range point, fftshifted: power[range][doppler]. */
	static float power[RADAR_N_RANGE][RADAR_N_DOPPLER];
	float        moving[RADAR_N_RANGE];

	for (int p = 0; p < RADAR_N_RANGE; p++)
	{
		float re[RADAR_N_DOPPLER];
		float im[RADAR_N_DOPPLER];

		/* Step 1: subtract the mean over the 64 sweeps for this range point.
		 * Anything stationary during the 11.6 ms burst sits at zero Doppler
		 * and leaves with the mean. */
		float sum_re = 0.0f;
		float sum_im = 0.0f;

		for (int s = 0; s < RADAR_N_SWEEPS; s++)
		{
			sum_re += (float)frame[s * RADAR_N_RANGE + p].real;
			sum_im += (float)frame[s * RADAR_N_RANGE + p].imag;
		}

		const float mean_re = sum_re / (float)RADAR_N_SWEEPS;
		const float mean_im = sum_im / (float)RADAR_N_SWEEPS;

		/* Step 2: window, and write in bit-reversed order ready for the FFT. */
		for (int s = 0; s < RADAR_N_SWEEPS; s++)
		{
			const int   dst = bitrev[s];
			const float w   = hann[s];

			re[dst] = ((float)frame[s * RADAR_N_RANGE + p].real - mean_re) * w;
			im[dst] = ((float)frame[s * RADAR_N_RANGE + p].imag - mean_im) * w;
		}

		/* Step 3: 64-point Doppler FFT. */
		fft64(re, im);

		/* Step 3 (cont.): fftshift, so zero velocity lands at RADAR_ZERO_BIN.
		 * Step 4: power. */
		float move_sum = 0.0f;

		for (int k = 0; k < RADAR_N_DOPPLER; k++)
		{
			const int shifted = (k + RADAR_ZERO_BIN) % RADAR_N_DOPPLER;
			const float pw    = re[k] * re[k] + im[k] * im[k];

			power[p][shifted] = pw;

			if (abs(shifted - RADAR_ZERO_BIN) >= RADAR_MOTION_MIN_BIN)
			{
				move_sum += pw;
			}
		}

		moving[p] = move_sum;
	}

	/* Step 5: keep the TOP_K range points with the most moving power and add
	 * their spectra. Summing all 22 would bury a walker that stands only
	 * ~10 dB above the noise in its own range point. */
	int top[RADAR_TOP_K];

	for (int i = 0; i < RADAR_TOP_K; i++)
	{
		int best = -1;

		for (int p = 0; p < RADAR_N_RANGE; p++)
		{
			bool taken = false;

			for (int j = 0; j < i; j++)
			{
				taken = taken || (top[j] == p);
			}

			if (!taken && (best < 0 || moving[p] > moving[best]))
			{
				best = p;
			}
		}

		top[i] = best;
	}

	/* Step 6: 10 log10(power + EPS). */
	for (int k = 0; k < RADAR_N_DOPPLER; k++)
	{
		float s = 0.0f;

		for (int i = 0; i < RADAR_TOP_K; i++)
		{
			s += power[top[i]][k];
		}

		out_db[k] = 10.0f * log10f(s + RADAR_LOG_EPS);
	}
}

int8_t radar_quantise_db(float db)
{
	float x = (db - RADAR_DB_FLOOR) / (RADAR_DB_CEIL - RADAR_DB_FLOOR);

	x = (x < 0.0f) ? 0.0f : (x > 1.0f ? 1.0f : x);

	/* Round half to even, matching numpy's rint -- lroundf rounds half away
	 * from zero and disagrees on exact .5 values. */
	int q = (int)nearbyintf(x / RADAR_IN_SCALE) + RADAR_IN_ZERO_POINT;

	return (int8_t)((q < -128) ? -128 : (q > 127 ? 127 : q));
}
