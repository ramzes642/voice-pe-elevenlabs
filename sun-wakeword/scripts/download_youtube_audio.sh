#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/.venv/bin/activate"

OUT_DIR="${OUT_DIR:-$ROOT_DIR/data/background_speech}"
MAX_ITEMS="${MAX_ITEMS:-10}"
START_AT="${START_AT:-30}"   # seconds
CLIP_LEN="${CLIP_LEN:-180}"  # seconds

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <youtube_url_or_playlist_or_ytsearch_query>"
  echo "Examples:"
  echo "  $0 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'"
  echo "  $0 'https://www.youtube.com/playlist?list=...'"
  echo "  MAX_ITEMS=5 $0 'ytsearch20:russian podcast interview'"
  exit 1
fi

TARGET="$1"
mkdir -p "$OUT_DIR"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

python -m yt_dlp \
  --ignore-errors \
  --yes-playlist \
  --max-downloads "$MAX_ITEMS" \
  --extract-audio \
  --audio-format wav \
  --audio-quality 0 \
  --postprocessor-args "ffmpeg:-ac 1 -ar 16000 -sample_fmt s16" \
  -o "$tmp_dir/%(title).120B_%(id)s.%(ext)s" \
  "$TARGET"

count=0
while IFS= read -r -d '' src; do
  base="$(basename "$src" .wav)"
  dst="$OUT_DIR/${base}.wav"
  if ! /usr/bin/ffmpeg -y -hide_banner -loglevel error \
      -ss "$START_AT" -t "$CLIP_LEN" -i "$src" \
      -ac 1 -ar 16000 -sample_fmt s16 "$dst"; then
    # Fallback for short clips: keep full audio.
    /usr/bin/ffmpeg -y -hide_banner -loglevel error \
      -i "$src" -ac 1 -ar 16000 -sample_fmt s16 "$dst"
  fi
  # ffmpeg can succeed with near-empty output when -ss is beyond clip duration.
  if [[ ! -s "$dst" || "$(stat -c%s "$dst")" -lt 4096 ]]; then
    /usr/bin/ffmpeg -y -hide_banner -loglevel error \
      -i "$src" -ac 1 -ar 16000 -sample_fmt s16 "$dst"
  fi
  count=$((count + 1))
done < <(find "$tmp_dir" -type f -name "*.wav" -print0)

echo "Saved $count files to: $OUT_DIR"
