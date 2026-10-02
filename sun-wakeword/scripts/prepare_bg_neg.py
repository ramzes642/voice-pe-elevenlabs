"""Generate mmap negative features from the long background clips.

These same dirs are ALSO used as background-augmentation sources for the positives
(see prepare_v2.py); here we additionally expose them as direct negative classes so the
model explicitly learns that music / YouTube speech is NOT the wake word.

Sources (16 kHz mono):
  data/background_music   (~432 clips, ~10 s)   music  -> precomputed_features/bg_music
  data/background_speech  (~357 clips, ~30 s)   YT ru  -> precomputed_features/bg_speech

slide_frames=1: one spectrogram per clip; the 1.5 s window is taken at train time via
truncation_strategy=random in the config (same approach as long negatives). augment=False.
Run inside the project .venv.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_v2 as p  # noqa: E402

ROOT = p.ROOT
JOBS = [
    ("bg_music", ROOT / "data" / "background_music", ROOT / "precomputed_features" / "bg_music"),
    ("bg_speech", ROOT / "data" / "background_speech", ROOT / "precomputed_features" / "bg_speech"),
]


def main():
    for name, wav_dir, out_root in JOBS:
        n = len(list(wav_dir.glob("*.wav")))
        print(f"\n=== {name}: {n} wavs in {wav_dir} -> {out_root} ===", flush=True)
        p.mmap_split(name, wav_dir, out_root, augment=False, slide_frames_train=1)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
