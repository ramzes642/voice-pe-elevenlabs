#!/usr/bin/env bash
# Auto-split a long dictaphone recording of repeated "солнце моё" (separated by pauses)
# into individual 16 kHz mono 16-bit wav clips, ready for training.
#
# Usage:
#   bash scripts/split_recording.sh <input_file> [out_dir] [prefix]
# Defaults: out_dir=audio_data/positives_my, prefix=my2
#
# Tunables via env: SIL_DUR (min silence sec to cut, default 0.4), SIL_THRESH
# (silence threshold, default 2%), MIN_S / MAX_S (keep clips within duration).
set -euo pipefail
cd "$(dirname "$0")/.."

IN="${1:?usage: split_recording.sh <input.wav> [out_dir] [prefix]}"
OUT="${2:-audio_data/positives_my}"
PREFIX="${3:-my2}"
SIL_DUR="${SIL_DUR:-0.4}"
SIL_THRESH="${SIL_THRESH:-2%}"
MIN_S="${MIN_S:-0.5}"
MAX_S="${MAX_S:-2.5}"

mkdir -p "$OUT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1) normalize to 16k mono 16-bit
sox "$IN" -r 16000 -c 1 -b 16 "$TMP/norm.wav"

# 2) split on silence: each utterance -> its own file
#    silence 1 0.1 THRESH : keep leading; 1 SIL_DUR THRESH : cut on trailing silence
sox "$TMP/norm.wav" "$TMP/seg_.wav" \
  silence 1 0.1 "$SIL_THRESH" 1 "$SIL_DUR" "$SIL_THRESH" : newfile : restart

# 3) keep only clips within [MIN_S, MAX_S]; trim edge silence; renumber
i=0; kept=0; skipped=0
for f in "$TMP"/seg_*.wav; do
  [ -e "$f" ] || continue
  dur=$(soxi -D "$f")
  if awk "BEGIN{exit !($dur>=$MIN_S && $dur<=$MAX_S)}"; then
    i=$((i+1))
    sox "$f" "$OUT/${PREFIX}_$(printf '%03d' "$i").wav" silence 1 0.05 "$SIL_THRESH" reverse silence 1 0.05 "$SIL_THRESH" reverse
    kept=$((kept+1))
  else
    skipped=$((skipped+1))
  fi
done

echo "input: $IN"
echo "kept $kept clips -> $OUT/${PREFIX}_*.wav (skipped $skipped out of duration range [$MIN_S,$MAX_S]s)"
echo "durations:"; for f in "$OUT/${PREFIX}"_*.wav; do [ -e "$f" ] && printf '  %5.2fs  %s\n' "$(soxi -D "$f")" "$(basename "$f")"; done | head -20
