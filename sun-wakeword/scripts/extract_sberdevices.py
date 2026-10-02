"""Stream sberdevices_golos parquet files and write 16 kHz mono int16 wavs.

Used as hard-negative source for retraining: real Russian speech that doesn't contain
'солнце моё' but spans general Russian phonetics — exactly what the current model lacks.
"""
import io
import sys
from pathlib import Path

import numpy as np
import scipy.io.wavfile as wavfile
import soundfile as sf
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

REPO = "bond005/sberdevices_golos_10h_crowd"
PARQUETS = [
    "data/train-00000-of-00003-85be0fb4b3de87ab.parquet",
    "data/train-00001-of-00003-fcf1367484d8316d.parquet",
    "data/train-00002-of-00003-8e44cef5e8ee83d1.parquet",
]
OUT_DIR = Path("/root/sun-wakeword/data/negative_audio/sberdevices_wav")
TARGET_SR = 16000
MAX_FILES = 3000


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for parq_path in PARQUETS:
        if written >= MAX_FILES:
            break
        print(f"Downloading {parq_path}...", flush=True)
        local = hf_hub_download(REPO, parq_path, repo_type="dataset")
        pf = pq.ParquetFile(local)
        for rg_idx in range(pf.num_row_groups):
            if written >= MAX_FILES:
                break
            tbl = pf.read_row_group(rg_idx, columns=["audio"])
            col = tbl.column("audio")
            for i in range(len(col)):
                if written >= MAX_FILES:
                    break
                struct = col[i].as_py()
                bb = struct.get("bytes")
                if not bb:
                    continue
                try:
                    audio, sr = sf.read(io.BytesIO(bb))
                except Exception as e:
                    print(f"  decode fail {i}: {e}", flush=True)
                    continue
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                if sr != TARGET_SR:
                    continue
                pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
                if pcm.size < TARGET_SR * 0.5:   # skip <0.5s clips
                    continue
                wavfile.write(OUT_DIR / f"sber_{written:05d}.wav", TARGET_SR, pcm)
                written += 1
                if written % 200 == 0:
                    print(f"  wrote {written}", flush=True)
    print(f"DONE: wrote {written} wavs to {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
