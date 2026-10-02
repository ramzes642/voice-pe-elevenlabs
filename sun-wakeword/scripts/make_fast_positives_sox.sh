#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IN_DIR="${1:-$ROOT_DIR/audio_data/positives_el_v2}"
OUT_DIR="${2:-$ROOT_DIR/audio_data/positives_el_v2_fast125}"
TEMPO="${3:-1.25}"

if ! command -v sox >/dev/null 2>&1; then
  echo "sox not found. Install it first: apt-get install -y sox" >&2
  exit 1
fi

if [[ ! -d "$IN_DIR" ]]; then
  echo "Input directory not found: $IN_DIR" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

count=0
while IFS= read -r -d '' src; do
  rel="${src#$IN_DIR/}"
  dst="$OUT_DIR/$rel"
  mkdir -p "$(dirname "$dst")"
  # 'tempo' changes speed while preserving pitch.
  sox "$src" "$dst" tempo -s "$TEMPO"
  count=$((count + 1))
done < <(find "$IN_DIR" -type f -name "*.wav" -print0)

echo "Created $count files in: $OUT_DIR"
