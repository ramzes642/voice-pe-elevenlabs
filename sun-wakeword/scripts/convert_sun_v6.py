"""v6: like v5 but adds Russian speech (sberdevices_golos) via direct parquet read.

datasets+torchcodec segfaults on this venv; bypass it by downloading parquet directly,
reading audio bytes with pyarrow, and decoding with soundfile (no torch).
"""
import io
import sys
from pathlib import Path

import numpy as np
import scipy.io.wavfile as wavfile
import soundfile as sf
import tensorflow as tf
from mmap_ninja.ragged import RaggedMmap

sys.path.insert(0, "/root/sun-wakeword/micro-wake-word")
from microwakeword.audio.audio_utils import generate_features_for_clip

SAVED_MODEL = "/root/sun-wakeword/saved_model"
WAV_DIR = Path("/root/sun-wakeword/calibration_wavs")
NEG_ROOT = Path("/root/sun-wakeword/data/negative_datasets")
OUT_TFLITE = "/root/sun-wakeword/sun_v6.tflite"

STRIDE = 3
STEP_MS = 10
SAMPLE_RATE = 16000
CLIP_SECONDS = 1.5
CLIP_SAMPLES = int(SAMPLE_RATE * CLIP_SECONDS)

BUDGETS = {
    "positives": 1500,
    "speech": 2000,
    "dinner_party": 2000,
    "no_speech": 2000,
    "sberdevices_ru": 2000,
    "silence": 500,
}

MMAP_DIRS = {
    "speech": NEG_ROOT / "speech" / "training",
    "dinner_party": NEG_ROOT / "dinner_party" / "training",
    "no_speech": NEG_ROOT / "no_speech" / "training",
}

SBERDEVICES_PARQUETS = [
    "data/train-00000-of-00003-85be0fb4b3de87ab.parquet",
    "data/train-00001-of-00003-fcf1367484d8316d.parquet",
    "data/train-00002-of-00003-8e44cef5e8ee83d1.parquet",
]


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
    mmap_dirs = sorted(directory.glob("*_mmap"))
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
            n_take = min(int(rng.integers(5, 15)), valid_rows // STRIDE)
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


def slices_from_sberdevices(budget, rng):
    """Stream sberdevices Russian speech via direct parquet (no torchcodec)."""
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    yielded = 0
    for parquet_rel in SBERDEVICES_PARQUETS:
        if yielded >= budget:
            break
        path = hf_hub_download("bond005/sberdevices_golos_10h_crowd", parquet_rel, repo_type="dataset")
        pf = pq.ParquetFile(path)
        # iterate row groups, then rows
        for rg_idx in range(pf.num_row_groups):
            if yielded >= budget:
                break
            tbl = pf.read_row_group(rg_idx, columns=["audio"])
            audio_col = tbl.column("audio")
            n = len(audio_col)
            order = list(range(n))
            rng.shuffle(order)
            for i in order:
                if yielded >= budget:
                    break
                struct = audio_col[i].as_py()
                bb = struct.get("bytes")
                if not bb:
                    continue
                try:
                    audio, sr = sf.read(io.BytesIO(bb))
                except Exception:
                    continue
                if sr != SAMPLE_RATE or audio.size < 1600:
                    continue
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                samples = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
                spec = generate_features_for_clip(samples, STEP_MS, use_c=False)
                for chunk in yield_slices(spec):
                    if yielded >= budget:
                        break
                    yield chunk
                    yielded += 1


def representative_dataset_gen():
    rng = np.random.default_rng(42)

    # 1) Positives
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

    # 2) mmap datasets (kahrendt)
    for name, directory in MMAP_DIRS.items():
        count = 0
        for chunk in slices_from_mmap_dir(directory, BUDGETS[name], rng):
            yield [chunk]
            count += 1
        print(f"calibration: {name} -> {count}", flush=True)

    # 3) sberdevices Russian speech (direct parquet)
    count = 0
    for chunk in slices_from_sberdevices(BUDGETS["sberdevices_ru"], rng):
        yield [chunk]
        count += 1
    print(f"calibration: sberdevices_ru -> {count}", flush=True)

    # 4) Silence
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
