#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_TEXTS = [
    "Солнце мое",
    "Солнце моё",
    "Солнце мое.",
    "Солнце моё.",
    "Солнце моё!",
    "солнце мое",
    "солнце моё",
]


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "voice"


def api_request(path: str, api_key: str, payload: dict | None = None) -> dict | bytes:
    url = f"{API_BASE}{path}"
    headers = {"xi-api-key": api_key}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=120) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(raw.decode("utf-8"))
        return raw


def fetch_voices(api_key: str) -> list[dict]:
    response = api_request("/voices?show_legacy=true", api_key)
    assert isinstance(response, dict)
    voices = response.get("voices", [])
    # Prefer multilingual voices likely to preserve Russian pronunciation.
    voices.sort(
        key=lambda voice: (
            "multilingual" not in str(voice.get("category", "")).lower()
            and "multilingual" not in json.dumps(voice.get("labels", {})).lower(),
            voice.get("name", ""),
        )
    )
    return voices


def convert_to_wav(input_path: Path, output_path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--voice-limit", type=int, default=8)
    parser.add_argument("--per-text", type=int, default=4)
    parser.add_argument(
        "--model-id",
        default="eleven_multilingual_v2",
        help="ElevenLabs model id to use for all requests",
    )
    parser.add_argument(
        "--texts-file",
        help="Optional UTF-8 text file with one phrase per line",
    )
    parser.add_argument(
        "--voice-ids-file",
        help="Optional file with one voice id per line to pin exact voices",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ELEVENLABS_API_KEY is not set", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.texts_file:
        texts = [line.strip() for line in Path(args.texts_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        texts = DEFAULT_TEXTS

    voices = fetch_voices(api_key)
    if args.voice_ids_file:
        allowed = {
            line.strip()
            for line in Path(args.voice_ids_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        voices = [voice for voice in voices if voice.get("voice_id") in allowed]
    else:
        voices = voices[: args.voice_limit]

    manifest = []
    for voice in voices:
        voice_id = voice["voice_id"]
        voice_name = voice.get("name", voice_id)
        voice_slug = slugify(voice_name)
        for text_index, text in enumerate(texts):
            for variant in range(args.per_text):
                payload = {
                    "text": text,
                    "model_id": args.model_id,
                    "voice_settings": {
                        "stability": max(0.15, min(0.9, 0.35 + variant * 0.1)),
                        "similarity_boost": max(0.4, min(0.95, 0.7 - variant * 0.05)),
                        "style": min(0.4, variant * 0.1),
                        "use_speaker_boost": True,
                    },
                }
                try:
                    audio = api_request(f"/text-to-speech/{voice_id}", api_key, payload)
                    assert isinstance(audio, bytes)
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")
                    print(f"request failed for {voice_name}: {body}", file=sys.stderr)
                    raise

                stem = f"el_{voice_slug}_{text_index:02d}_{variant:02d}"
                tmp_path = output_dir / f"{stem}.mp3"
                wav_path = output_dir / f"{stem}.wav"
                tmp_path.write_bytes(audio)
                convert_to_wav(tmp_path, wav_path)
                tmp_path.unlink(missing_ok=True)

                manifest.append(
                    {
                        "file": wav_path.name,
                        "voice_id": voice_id,
                        "voice_name": voice_name,
                        "text": text,
                        "variant": variant,
                    }
                )
                time.sleep(0.3)

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"generated {len(manifest)} wav files in {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
