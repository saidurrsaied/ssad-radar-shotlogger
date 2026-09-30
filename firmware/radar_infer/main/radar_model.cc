/* See radar_model.h. The resolver lists exactly the four ops the exported
 * model uses -- CONV_2D, MEAN, FULLY_CONNECTED, LOGISTIC. Adding the full
 * resolver instead would pull in every kernel and waste flash. */

#include "radar_model.h"

#include "esp_log.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"

#include "pedestrian_binary_int8.h"
#include "radar_features_config.h"

namespace {

const char *TAG = "model";

/* 19,009 parameters, three strided convolutions and a global mean. The
 * interpreter reports 48,668 B in use on esp-tflite-micro 1.4.1, which is
 * deterministic for a fixed model -- 56 KB leaves room for a component update
 * to shift it without a silent AllocateTensors failure. The startup log prints
 * the real figure; trust that, not this comment. */
constexpr size_t kArenaSize = 56 * 1024;

alignas(16) uint8_t arena[kArenaSize];

tflite::MicroInterpreter *interpreter = nullptr;
TfLiteTensor             *input       = nullptr;
TfLiteTensor             *output      = nullptr;

}  // namespace

bool radar_model_init(void)
{
	const tflite::Model *model = tflite::GetModel(g_pedestrian_model);

	if (model->version() != TFLITE_SCHEMA_VERSION)
	{
		ESP_LOGE(TAG, "schema %lu, expected %d",
		         (unsigned long)model->version(), TFLITE_SCHEMA_VERSION);
		return false;
	}

	static tflite::MicroMutableOpResolver<4> resolver;

	if (resolver.AddConv2D() != kTfLiteOk ||
	    resolver.AddMean() != kTfLiteOk ||
	    resolver.AddFullyConnected() != kTfLiteOk ||
	    resolver.AddLogistic() != kTfLiteOk)
	{
		ESP_LOGE(TAG, "could not register the ops");
		return false;
	}

	static tflite::MicroInterpreter static_interpreter(model, resolver, arena, kArenaSize);

	interpreter = &static_interpreter;

	if (interpreter->AllocateTensors() != kTfLiteOk)
	{
		ESP_LOGE(TAG, "AllocateTensors failed -- arena too small?");
		return false;
	}

	input  = interpreter->input(0);
	output = interpreter->output(0);

	const size_t expected = (size_t)RADAR_PATCH_FRAMES * RADAR_N_DOPPLER;

	if (input->bytes != expected || input->type != kTfLiteInt8)
	{
		ESP_LOGE(TAG, "input is %u bytes type %d, expected %u int8",
		         (unsigned)input->bytes, (int)input->type, (unsigned)expected);
		return false;
	}

	/* The quantisation the model carries must match the constants the feature
	 * code was built with, or the two halves disagree silently. */
	ESP_LOGI(TAG, "arena used %u of %u B, in scale %.9f zp %d, out scale %.9f zp %d",
	         (unsigned)interpreter->arena_used_bytes(), (unsigned)kArenaSize,
	         (double)input->params.scale, (int)input->params.zero_point,
	         (double)output->params.scale, (int)output->params.zero_point);

	return true;
}

int8_t *radar_model_input(void)
{
	return input->data.int8;
}

bool radar_model_invoke(int8_t *q_out)
{
	if (interpreter->Invoke() != kTfLiteOk)
	{
		return false;
	}

	*q_out = output->data.int8[0];

	return true;
}

size_t radar_model_arena_used(void)
{
	return interpreter ? interpreter->arena_used_bytes() : 0;
}
