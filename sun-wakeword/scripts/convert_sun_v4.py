"""Re-quantize Sun wake-word model with REAL human speech in representative dataset.

v2: positives only -> model fires on everything (background not calibrated).
v3: positives + synthetic silence/noise -> better, but synthetic noise != real speech.
v4: positives + real speech (chime6 dinner party recordings) + silence -> proper coverage.
"""
import random
import sys
from pathlib import Path

import numpy as np
import scipy.io.wavfile as wavfile
import tensorflow as tf
from mmap_ninja.ragged import RaggedMmap

sys.path.insert(0, "/root/sun-wakeword/micro-wake-word")
from microwakeword.audio.audio_utils import generate_features_for_clip

SAVED_MODEL = "/root/sun-wakeword/saved_model"
WAV_DIR = Path("/root/sun-wakeword/calibration_wavs")
NEG_ROOT = Path("/root/sun-wakeword/data/negative_datasets/dinner_party/training")
OUT_TFLITE = "/root/sun-wakeword/sun_v4.tflite"

STRIDE = 3
STEP_MS = 10
SAMPLE_RATE = 16000
CLIP_SECONDS = 1.5
CLIP_SAMPLES = int(SAMPLE_RATE * CLIP_SECONDS)

# Calibration budgets (slices = (1, 3, 40) input chunks)
SLICES_FROM_POSITIVES = 1000
SLICES_FROM_NEGATIVES = 5000
SLICES_FROM_SILENCE = 500


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

    # 1) Positive samples — real wake-word audio
    yielded_pos = 0
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

    # 2) Negative samples — real human speech from chime6 dinner party corpus
    # mmap_ninja format: each subdir holds RaggedMmap of pre-computed spectrograms
    mmap_dirs = sorted(NEG_ROOT.glob("*_mmap"))
    if not mmap_dirs:
        raise RuntimeError(f"no *_mmap dirs found under {NEG_ROOT}")
    print(f"  found {len(mmap_dirs)} negative mmaps", flush=True)
    yielded_neg = 0
    # Round-robin across mmaps for variety; each mmap entry is a (T, 40) spectrogram
    mmap_iters = []
    for d in mmap_dirs:
        ragged = RaggedMmap(str(d))
        idx = list(range(len(ragged)))
        rng.shuffle(idx)
        mmap_iters.append((ragged, iter(idx)))
    while yielded_neg < SLICES_FROM_NEGATIVES:
        progress = False
        for ragged, it in mmap_iters:
            if yielded_neg >= SLICES_FROM_NEGATIVES:
                break
            try:
                i = next(it)
            except StopIteration:
                continue
            progress = True
            spec = np.asarray(ragged[i])
            if spec.ndim != 2 or spec.shape[1] != 40:
                continue
            # take ~10 random slices per spectrogram, otherwise we burn through too few clips
            valid_rows = (spec.shape[0] // STRIDE) * STRIDE
            if valid_rows < STRIDE:
                continue
            start_positions = rng.integers(0, valid_rows - STRIDE + 1, size=min(10, valid_rows // STRIDE))
            for sp in start_positions:
                if yielded_neg >= SLICES_FROM_NEGATIVES:
                    break
                sp_aligned = (sp // STRIDE) * STRIDE
                chunk = spec[sp_aligned:sp_aligned + STRIDE, :].astype(np.float32)
                yield [chunk[np.newaxis, ...]]
                yielded_neg += 1
        if not progress:
            break
    print(f"calibration: negatives -> {yielded_neg} slices", flush=True)

    # 3) Synthetic silence — to cover the very-low-activation region
    yielded_sil = 0
    while yielded_sil < SLICES_FROM_SILENCE:
        samples = np.zeros(CLIP_SAMPLES, dtype=np.int16)
        spec = generate_features_for_clip(samples, STEP_MS, use_c=False)
        for chunk in yield_slices(spec):
            if yielded_sil >= SLICES_FROM_SILENCE:
                break
            yield [chunk]
            yielded_sil += 1
    print(f"calibration: silence -> {yielded_sil} slices", flush=True)
    print(f"calibration: total {yielded_pos + yielded_neg + yielded_sil}", flush=True)


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
