"""Generate mmap negative features from my own voice reading non-wakeword text.

Source: data/negative_audio_my/*.wav  (~5.6 min of my speech: near-miss words like
соль/солнце/солнышко/моё/со мной + casual speech + assistant commands; 5 s chunks,
16 kHz mono). Purpose: counter false positives on MY voice introduced by positive_my.

Output: precomputed_features/negative_my/{training,validation,testing}/negative_my_mmap
Reuses prepare_v2.mmap_split (augment=False, slide_frames=1; 1.5 s window taken at train
time via truncation_strategy=random). Run inside the project .venv.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_v2 as p  # noqa: E402

ROOT = p.ROOT
WAV = ROOT / "data" / "negative_audio_my"
OUT = ROOT / "precomputed_features" / "negative_my"


def main():
    n = len(list(WAV.glob("*.wav")))
    print(f"my-voice negatives: {n} chunks in {WAV} -> {OUT}", flush=True)
    p.mmap_split("negative_my", WAV, OUT, augment=False, slide_frames_train=1)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
