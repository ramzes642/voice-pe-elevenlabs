"""Generate mmap spectrogram features for the +25% tempo (fast125) positives.

Source wavs:  audio_data/positives_el_v2_fast125/*.wav  (regenerated via sox tempo 1.25)
Output mmap:  precomputed_features/positive_fast/{training,validation,testing}/wakeword_fast_mmap

Reuses prepare_v2.mmap_split so the split logic (SPLIT_SEED=10, split_count=0.1) and the
augmentation pipeline (background = archive Russian negatives) are identical to the
original positives. Run inside the project .venv.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_v2 as p  # noqa: E402  (also sets up micro-wake-word import path + patches)

ROOT = p.ROOT
FAST_WAV = ROOT / "audio_data" / "positives_el_v2_fast125"
OUT_ROOT = ROOT / "precomputed_features" / "positive_fast"


def main():
    print(f"fast wav dir: {FAST_WAV}  ({len(list(FAST_WAV.glob('*.wav')))} wavs)", flush=True)
    print(f"out root:     {OUT_ROOT}", flush=True)
    p.mmap_split(
        "wakeword_fast",
        FAST_WAV,
        OUT_ROOT,
        augment=True,
        slide_frames_train=10,
    )
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
