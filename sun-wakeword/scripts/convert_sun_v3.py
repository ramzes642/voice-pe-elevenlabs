"""Re-quantize Sun wake-word model with MIXED representative dataset.

Issue with v2: representative dataset was only positive samples, so the PTQ
calibrated activation ranges for "wake word" inputs only. Result: model fires
on any input (post-quantization scales clip background activations to 1.0).

Fix: feed positive + silence + noise spectrograms so the PTQ sees the full
activation distribution.
"""
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
OUT_TFLITE = "/root/sun-wakeword/sun_v3.tflite"

STRIDE = 3
STEP_MS = 10
SAMPLE_RATE = 16000
CLIP_SECONDS = 1.5
CLIP_SAMPLES = int(SAMPLE_RATE * CLIP_SECONDS)

# slice budgets — converter sees roughly equal counts of each class
SLICES_FROM_POSITIVES = 800
SILENCE_CLIPS = 80              # ~24000 slices not needed; per clip yields ~50 slices
NOISE_CLIPS = 80
MIXED_NOISE_LEVELS = (50, 200, 800, 3000)


def load_wav(path):
    sr, samples = wavfile.read(path)
    if sr != SAMPLE_RATE:
        return None
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16)
    return samples


def yield_slices(spectrogram):
    rows = spectrogram.shape[0]
    rows -= rows % STRIDE
    for i in range(0, rows - STRIDE, STRIDE):
        chunk = spectrogram[i:i + STRIDE, :].astype(np.float32)
        yield chunk[np.newaxis, ...]


def representative_dataset_gen():
    rng = np.random.default_rng(42)
    yielded_pos = 0
    yielded_silence = 0
    yielded_noise = 0

    # 1) Positives — random wavs
    wavs = sorted(WAV_DIR.glob("*.wav"))
    rng.shuffle(wavs)
    for w in wavs:
        if yielded_pos >= SLICES_FROM_POSITIVES:
            break
        samples = load_wav(w)
        if samples is None:
            continue
        spec = generate_features_for_clip(samples, STEP_MS, use_c=False)
        for chunk in yield_slices(spec):
            if yielded_pos >= SLICES_FROM_POSITIVES:
                break
            yield [chunk]
            yielded_pos += 1
    print(f"calibration: positives -> {yielded_pos} slices", flush=True)

    # 2) Silence
    for _ in range(SILENCE_CLIPS):
        samples = np.zeros(CLIP_SAMPLES, dtype=np.int16)
        spec = generate_features_for_clip(samples, STEP_MS, use_c=False)
        for chunk in yield_slices(spec):
            yield [chunk]
            yielded_silence += 1
    print(f"calibration: silence -> {yielded_silence} slices", flush=True)

    # 3) Noise at varying amplitudes (covers typical room / fan / music activation levels)
    for _ in range(NOISE_CLIPS):
        amp = float(rng.choice(MIXED_NOISE_LEVELS))
        samples = (rng.standard_normal(CLIP_SAMPLES) * amp).clip(-32768, 32767).astype(np.int16)
        spec = generate_features_for_clip(samples, STEP_MS, use_c=False)
        for chunk in yield_slices(spec):
            yield [chunk]
            yielded_noise += 1
    print(f"calibration: noise -> {yielded_noise} slices", flush=True)
    print(f"calibration: total {yielded_pos + yielded_silence + yielded_noise} slices", flush=True)


def main():
    print("Loading SavedModel from", SAVED_MODEL, flush=True)
    converter = tf.lite.TFLiteConverter.from_saved_model(SAVED_MODEL)
    converter.optimizations = {tf.lite.Optimize.DEFAULT}
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
