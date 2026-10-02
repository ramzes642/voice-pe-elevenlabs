"""Re-quantize trained Sun wake-word model with INT8 state variables (fixes DEQUANTIZE issue).

Loads the existing SavedModel and re-converts to TFLite with:
- _experimental_variable_quantization = True  (keeps streaming state in INT8 — no DEQUANTIZE op)
- input/output dtypes matching the standard mWW format (INT8 in, UINT8 out)
- Representative dataset built from the original wake-word wavs.
"""
import os
import random
import sys
from pathlib import Path

import numpy as np
import scipy.io.wavfile as wavfile
import tensorflow as tf

sys.path.insert(0, "/root/sun-wakeword/micro-wake-word")
from microwakeword.audio.audio_utils import generate_features_for_clip

SAVED_MODEL = "/root/sun-wakeword/saved_model"
WAV_DIR = Path("/root/sun-wakeword/calibration_wavs")
OUT_TFLITE = "/root/sun-wakeword/sun_v2.tflite"

STRIDE = 3              # frames per inference step (matches training stride=3)
STEP_MS = 10            # spectrogram step
N_CALIB_WAVS = 500      # representative samples
SAMPLE_RATE = 16000

def load_wav(path):
    sr, samples = wavfile.read(path)
    if sr != SAMPLE_RATE:
        raise RuntimeError(f"sr={sr} for {path}")
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16)
    return samples


def representative_dataset_gen():
    random.seed(42)
    wavs = list(WAV_DIR.glob("*.wav"))
    random.shuffle(wavs)
    wavs = wavs[:N_CALIB_WAVS]
    print(f"calibration: using {len(wavs)} wavs from {WAV_DIR}", flush=True)
    yielded = 0
    for w in wavs:
        try:
            samples = load_wav(w)
        except Exception as e:
            print(f"  skip {w.name}: {e}", flush=True)
            continue
        spectrogram = generate_features_for_clip(samples, STEP_MS, use_c=False)
        # mimic upstream representative_dataset_gen slicing
        rows = spectrogram.shape[0]
        rows -= rows % STRIDE
        for i in range(0, rows - STRIDE, STRIDE):
            chunk = spectrogram[i:i + STRIDE, :].astype(np.float32)
            # chunk shape is (3, 40); converter expects same as model input (1, 3, 40)
            yield [chunk[np.newaxis, ...]]
            yielded += 1
            if yielded % 1000 == 0:
                print(f"  ... yielded {yielded} slices", flush=True)
    print(f"calibration: total {yielded} slices yielded", flush=True)


def main():
    print("Loading SavedModel from", SAVED_MODEL, flush=True)
    converter = tf.lite.TFLiteConverter.from_saved_model(SAVED_MODEL)
    converter.optimizations = {tf.lite.Optimize.DEFAULT}
    # KEY FIX: keep streaming state variables in INT8
    # (without this the converter inserts DEQUANTIZE/QUANTIZE around every ReadVariable/AssignVariable)
    converter._experimental_variable_quantization = True
    converter.target_spec.supported_ops = {tf.lite.OpsSet.TFLITE_BUILTINS_INT8}
    converter.inference_input_type = tf.int8
    converter.inference_output_type = tf.uint8
    converter.representative_dataset = tf.lite.RepresentativeDataset(representative_dataset_gen)

    print("Converting...", flush=True)
    tflite_bytes = converter.convert()
    Path(OUT_TFLITE).write_bytes(tflite_bytes)
    print(f"Wrote {OUT_TFLITE}: {len(tflite_bytes)} bytes", flush=True)


if __name__ == "__main__":
    main()
