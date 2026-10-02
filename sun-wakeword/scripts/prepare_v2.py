"""Build mmap spectrogram features for retraining.

Reads:
  - new EL Russian positives:        /root/sun-wakeword/data/positive_samples/el_v2/*.wav
  - faster (+25% tempo) positives:   /root/sun-wakeword/data/positive_samples/el_v2_fast125/*.wav
  - sberdevices Russian speech (neg): /root/sun-wakeword/data/negative_audio/sberdevices_wav/*.wav
  - open_stt YT Russian speech (neg): /root/sun-wakeword/data/negative_audio/openstt_wav/*.wav

Writes:
  - /root/sun-wakeword/data/v2_features/positive/{training,validation,testing}/wakeword_mmap
  - /root/sun-wakeword/data/v2_features/sberdevices/{training,validation,testing}/sberdevices_mmap
  - /root/sun-wakeword/data/v2_features/openstt/{training,validation,testing}/openstt_mmap
  - /root/sun-wakeword/training_parameters_v2.yaml
"""
import sys
from pathlib import Path

import yaml
from mmap_ninja.ragged import RaggedMmap
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "micro-wake-word"))
from microwakeword.audio.augmentation import Augmentation
from microwakeword.audio.clips import Clips
from microwakeword.audio.spectrograms import SpectrogramGeneration

ROOT = REPO_ROOT
DATA = ROOT / "data"
V2_FEAT = DATA / "v2_features"

POS_WAV = DATA / "positive_samples/el_v2"
POS_WAV_FAST = DATA / "positive_samples/el_v2_fast125"
SBER_WAV = DATA / "negative_audio/sberdevices_wav"
OPENSTT_WAV = DATA / "negative_audio/openstt_wav"
SBER_WAV_ARCHIVE = ROOT / "audio_data/negative_audio/sberdevices_wav"
OPENSTT_WAV_ARCHIVE = ROOT / "audio_data/negative_audio/openstt_wav"
BG_MUSIC_WAV = DATA / "background_music"
BG_SPEECH_EXTRA_WAV = DATA / "background_speech"

EXISTING_NEG = DATA / "negative_datasets"  # speech, dinner_party, no_speech (kahrendt)

SPLIT_SEED = 10


def existing_wav_dirs(candidates):
    out = []
    seen = set()
    for p in candidates:
        rp = str(p.resolve()) if p.exists() else str(p)
        if rp in seen:
            continue
        if p.exists() and any(p.glob("*.wav")):
            out.append(str(p))
            seen.add(rp)
    return out


def mmap_split(name: str, wav_dir: Path, out_root: Path, augment: bool, slide_frames_train: int) -> None:
    """Generate mmap features for train/val/test splits from a wav directory."""
    if not wav_dir.exists():
        print(f"SKIP {name}: {wav_dir} missing", flush=True)
        return
    n_wavs = len(list(wav_dir.glob("*.wav")))
    if n_wavs == 0:
        print(f"SKIP {name}: no wavs in {wav_dir}", flush=True)
        return
    print(f"==> {name}: {n_wavs} wavs in {wav_dir}", flush=True)

    clips = Clips(
        input_directory=str(wav_dir),
        file_pattern="*.wav",
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=SPLIT_SEED,
        split_count=0.1,
    )

    augmenter = None
    if augment:
        # Mix positives with speech/music backgrounds.
        # Speech helps robustness in conversation; music/noise reduces false triggers.
        bg_paths = existing_wav_dirs(
            [
                SBER_WAV,
                OPENSTT_WAV,
                SBER_WAV_ARCHIVE,
                OPENSTT_WAV_ARCHIVE,
                BG_MUSIC_WAV,
                BG_SPEECH_EXTRA_WAV,
            ]
        )
        print(f"  background wav dirs: {len(bg_paths)}", flush=True)
        for p in bg_paths:
            print(f"    - {p}", flush=True)
        augmenter = Augmentation(
            augmentation_duration_s=3.2,
            augmentation_probabilities={
                "SevenBandParametricEQ": 0.15,
                "TanhDistortion": 0.1,
                "PitchShift": 0.1,
                "BandStopFilter": 0.1,
                "AddColorNoise": 0.15,
                "AddBackgroundNoise": 0.8 if bg_paths else 0.0,
                "Gain": 1.0,
                "RIR": 0.0,                  # mit_rirs not downloaded
            },
            impulse_paths=[],
            background_paths=bg_paths,
            background_min_snr_db=-5,
            background_max_snr_db=12,
            min_jitter_s=0.15,
            max_jitter_s=0.3,
        )

    for split in ["training", "validation", "testing"]:
        split_dir = out_root / split
        split_dir.mkdir(parents=True, exist_ok=True)

        if split == "training":
            split_name = "train"
            repetition = 2
            slide_frames = slide_frames_train
        elif split == "validation":
            split_name = "validation"
            repetition = 1
            slide_frames = slide_frames_train
        else:
            split_name = "test"
            repetition = 1
            slide_frames = 1

        sg = SpectrogramGeneration(
            clips=clips,
            augmenter=augmenter if split == "training" else None,
            slide_frames=slide_frames,
            step_ms=10,
        )
        out_mmap = split_dir / f"{name}_mmap"
        if out_mmap.exists():
            print(f"  exists, skip: {out_mmap}", flush=True)
            continue
        RaggedMmap.from_generator(
            out_dir=str(out_mmap),
            sample_generator=sg.spectrogram_generator(split=split_name, repeat=repetition),
            batch_size=100,
            verbose=True,
        )
        print(f"  wrote {out_mmap}", flush=True)


def write_training_config():
    config = {
        "window_step_ms": 10,
        "train_dir": str(ROOT / "trained_models" / "sun_v2"),
        "features": [
            # positives (new EL Russian)
            {
                "features_dir": str(V2_FEAT / "positive"),
                "sampling_weight": 2.5,
                "penalty_weight": 1.0,
                "truth": True,
                "truncation_strategy": "truncate_start",
                "type": "mmap",
            },
            # positives: same phrases with +25% tempo (pitch preserved by sox tempo effect)
            {
                "features_dir": str(V2_FEAT / "positive_fast"),
                "sampling_weight": 2.0,
                "penalty_weight": 1.0,
                "truth": True,
                "truncation_strategy": "truncate_start",
                "type": "mmap",
            },
            # NEW Russian negatives — heavy weight, force model to learn Russian
            {
                "features_dir": str(V2_FEAT / "sberdevices"),
                "sampling_weight": 12.0,
                "penalty_weight": 1.5,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(V2_FEAT / "openstt"),
                "sampling_weight": 12.0,
                "penalty_weight": 1.5,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            # existing English/mixed negatives — keep for variety
            {
                "features_dir": str(EXISTING_NEG / "speech"),
                "sampling_weight": 5.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(EXISTING_NEG / "dinner_party"),
                "sampling_weight": 8.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(EXISTING_NEG / "no_speech"),
                "sampling_weight": 6.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(EXISTING_NEG / "dinner_party_eval"),
                "sampling_weight": 0.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "split",
                "type": "mmap",
            },
        ],
        "training_steps": [8000, 5000],   # was 6000+4000
        "positive_class_weight": [1, 1],
        "negative_class_weight": [22, 26],  # was 18, 22 — push harder on negatives
        "learning_rates": [0.001, 0.0005],
        "batch_size": 128,
        "time_mask_max_size": [0, 2],
        "time_mask_count": [0, 1],
        "freq_mask_max_size": [0, 2],
        "freq_mask_count": [0, 1],
        "eval_step_interval": 500,
        "clip_duration_ms": 1500,
        "target_minimization": 0.9,
        "minimization_metric": None,
        "maximization_metric": "average_viable_recall",
    }
    out = ROOT / "training_parameters_v2.yaml"
    out.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    print(f"==> wrote {out}", flush=True)


def main():
    V2_FEAT.mkdir(parents=True, exist_ok=True)
    mmap_split("wakeword", POS_WAV, V2_FEAT / "positive", augment=True, slide_frames_train=10)
    mmap_split("wakeword_fast", POS_WAV_FAST, V2_FEAT / "positive_fast", augment=True, slide_frames_train=10)
    mmap_split("sberdevices", SBER_WAV, V2_FEAT / "sberdevices", augment=False, slide_frames_train=10)
    mmap_split("openstt", OPENSTT_WAV, V2_FEAT / "openstt", augment=False, slide_frames_train=10)
    write_training_config()


if __name__ == "__main__":
    main()
