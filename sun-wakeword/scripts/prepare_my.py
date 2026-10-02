"""Generate a TRAINING-ONLY mmap from my own recorded positives (real dictaphone clips).

Source: data/positive_samples/my_v1/*.wav  (20 real "солнце моё", 16 kHz mono 16-bit,
recorded from different parts of the house).
Output: precomputed_features/positive_my/training/wakeword_my_mmap

All 20 clips go into training (random_split_seed=None → no held-out split; 20 is too few
for a meaningful test set, and the EL set is kept as the consistent metric). Same
augmentation pipeline as the other positives (background = Russian negatives), slide_frames=10.
Run inside the project .venv.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_v2 as p  # noqa: E402  (sets micro-wake-word path + patches)

from mmap_ninja.ragged import RaggedMmap  # noqa: E402
from microwakeword.audio.clips import Clips  # noqa: E402
from microwakeword.audio.spectrograms import SpectrogramGeneration  # noqa: E402
from microwakeword.audio.augmentation import Augmentation  # noqa: E402

ROOT = p.ROOT
WAV = ROOT / "data" / "positive_samples" / "my_v1"
OUT = ROOT / "precomputed_features" / "positive_my" / "training" / "wakeword_my_mmap"


def main():
    n = len(list(WAV.glob("*.wav")))
    print(f"my positives: {n} wavs in {WAV} -> {OUT}", flush=True)

    clips = Clips(
        input_directory=str(WAV),
        file_pattern="*.wav",
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=None,   # no split -> all clips available via split=None
    )

    bg_paths = p.existing_wav_dirs(
        [p.SBER_WAV, p.OPENSTT_WAV, p.SBER_WAV_ARCHIVE, p.OPENSTT_WAV_ARCHIVE,
         p.BG_MUSIC_WAV, p.BG_SPEECH_EXTRA_WAV]
    )
    print(f"background dirs: {len(bg_paths)}", flush=True)
    augmenter = Augmentation(
        augmentation_duration_s=3.2,
        augmentation_probabilities={
            "SevenBandParametricEQ": 0.15, "TanhDistortion": 0.1, "PitchShift": 0.1,
            "BandStopFilter": 0.1, "AddColorNoise": 0.15,
            "AddBackgroundNoise": 0.8 if bg_paths else 0.0, "Gain": 1.0, "RIR": 0.0,
        },
        impulse_paths=[], background_paths=bg_paths,
        background_min_snr_db=-5, background_max_snr_db=12,
        min_jitter_s=0.15, max_jitter_s=0.3,
    )

    sg = SpectrogramGeneration(clips=clips, augmenter=augmenter, slide_frames=10, step_ms=10)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # repeat=4: only 20 clips, give the augmenter more varied passes over them
    RaggedMmap.from_generator(
        out_dir=str(OUT),
        sample_generator=sg.spectrogram_generator(split=None, repeat=4),
        batch_size=100, verbose=True,
    )
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
