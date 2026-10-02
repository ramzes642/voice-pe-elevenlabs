"""Generate mmap features for sberdevices + openstt negatives only.
Lets us start this heavy step in parallel while EL positives are still being generated."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_v2 import mmap_split, V2_FEAT, SBER_WAV, OPENSTT_WAV

V2_FEAT.mkdir(parents=True, exist_ok=True)
mmap_split("sberdevices", SBER_WAV, V2_FEAT / "sberdevices", augment=False, slide_frames_train=10)
mmap_split("openstt", OPENSTT_WAV, V2_FEAT / "openstt", augment=False, slide_frames_train=10)
print("DONE: negatives mmap ready", flush=True)
