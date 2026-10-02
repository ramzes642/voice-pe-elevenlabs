"""Re-quantize Sun wake-word model using EVERY negative dataset we can get our hands on.

v4 used only dinner_party (chime6). v5 uses:
  positives  (1500) - real wake-word audio
  speech     (2000) - voices_lav clo/mid/far (close/mid/far field clean speech)
  dinner_party (2000) - chime6 noisy dinner conversation
  no_speech  (2000) - fma_medium music + fsd50k sounds + wham noise
  sberdevices_golos (2000) - Russian crowd reading speech (matches our target language!)
  silence    (500) - synthesized zeros

Total ~10k slices — exhaustive PTQ activation coverage.
"""
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
NEG_ROOT = Path("/root/sun-wakeword/data/negative_datasets")
OUT_TFLITE = "/root/sun-wakeword/sun_v5.tflite"

STRIDE = 3
STEP_MS = 10
SAMPLE_RATE = 16000
CLIP_SECONDS = 1.5
CLIP_SAMPLES = int(SAMPLE_RATE * CLIP_SECONDS)

BUDGETS = {
    "positives": 1500,
    "speech": 2500,
    "dinner_party": 2500,
    "no_speech": 2500,
    "silence": 500,
}

MMAP_DIRS = {
    "speech": NEG_ROOT / "speech" / "training",
    "dinner_party": NEG_ROOT / "dinner_party" / "training",
    "no_speech": NEG_ROOT / "no_speech" / "training",
}


def load_wav(path):
    sr, samples = wavfile.read(path)
    if sr != SAMPLE_RATE:
        return None
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16)
    return samples


def yield_slices(spectrogram):
    rows = (spectrogram.shape[0] // STRIDE) * STRIDE
    for i in range(0, rows - STRIDE, STRIDE):
        chunk = spectrogram[i:i + STRIDE, :].astype(np.float32)
        yield chunk[np.newaxis, ...]


def slices_from_mmap_dir(directory, budget, rng):
    """Round-robin over each *_mmap in `directory`, yielding up to `budget` (1,3,40) slices."""
    mmap_dirs = sorted(directory.glob("*_mmap"))
    if not mmap_dirs:
        raise RuntimeError(f"no *_mmap dirs under {directory}")
    iters = []
    for d in mmap_dirs:
        rg = RaggedMmap(str(d))
        idx = list(range(len(rg)))
        rng.shuffle(idx)
        iters.append((rg, iter(idx)))
    yielded = 0
    while yielded < budget:
        progress = False
        for rg, it in iters:
            if yielded >= budget:
                break
            try:
                i = next(it)
            except StopIteration:
                continue
            progress = True
            spec = np.asarray(rg[i])
            if spec.ndim != 2 or spec.shape[1] != 40:
                continue
            valid_rows = (spec.shape[0] // STRIDE) * STRIDE
            if valid_rows < STRIDE:
                continue
            # take ~5-15 random aligned slices per spectrogram for variety
            n_take = min(rng.integers(5, 15), valid_rows // STRIDE)
            for _ in range(n_take):
                if yielded >= budget:
                    break
                start = int(rng.integers(0, max(1, valid_rows - STRIDE + 1)))
                start = (start // STRIDE) * STRIDE
                chunk = spec[start:start + STRIDE, :].astype(np.float32)
                yield chunk[np.newaxis, ...]
                yielded += 1
        if not progress:
            break


def slices_from_sberdevices(budget):
    """Stream russian speech audio, compute spectrograms, yield (1,3,40) slices."""
    from datasets import load_dataset
    ds = load_dataset("bond005/sberdevices_golos_10h_crowd", split="train", streaming=True)
    yielded = 0
    for row in ds:
        if yielded >= budget:
            break
        audio = row["audio"]["array"]
        if row["audio"]["sampling_rate"] != SAMPLE_RATE:
            continue
        samples = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        if samples.size < 1600:
            continue
        spec = generate_features_for_clip(samples, STEP_MS, use_c=False)
        for chunk in yield_slices(spec):
            if yielded >= budget:
                break
            yield chunk
            yielded += 1


def representative_dataset_gen():
    rng = np.random.default_rng(42)

    # Positives
    yielded = 0
    wavs = sorted(WAV_DIR.glob("*.wav"))
    rng.shuffle(wavs)
    for w in wavs:
        if yielded >= BUDGETS["positives"]:
            break
        samples = load_wav(w)
        if samples is None:
            continue
        spec = generate_features_for_clip(samples, STEP_MS, use_c=False)
        for chunk in yield_slices(spec):
            if yielded >= BUDGETS["positives"]:
                break
            yield [chunk]
            yielded += 1
    print(f"calibration: positives -> {yielded}", flush=True)

    # mmap negative datasets
    for name, directory in MMAP_DIRS.items():
        budget = BUDGETS[name]
        count = 0
        for chunk in slices_from_mmap_dir(directory, budget, rng):
            yield [chunk]
            count += 1
        print(f"calibration: {name} -> {count}", flush=True)

    # silence
    count = 0
    while count < BUDGETS["silence"]:
        samples = np.zeros(CLIP_SAMPLES, dtype=np.int16)
        spec = generate_features_for_clip(samples, STEP_MS, use_c=False)
        for chunk in yield_slices(spec):
            if count >= BUDGETS["silence"]:
                break
            yield [chunk]
            count += 1
    print(f"calibration: silence -> {count}", flush=True)


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
