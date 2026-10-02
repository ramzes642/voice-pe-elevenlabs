"""Stream tar.gz of open_stt public_youtube700, extract first N opus files,
convert to 16 kHz mono int16 wav. No full unpack needed.
"""
import io
import subprocess
import sys
import tarfile
from pathlib import Path

TAR = Path("/root/sun-wakeword/data/open_stt/public_youtube700.tar.gz")
OUT_DIR = Path("/root/sun-wakeword/data/negative_audio/openstt_wav")
N_TARGET = 5000


def opus_bytes_to_wav(opus_bytes: bytes, wav_path: Path) -> bool:
    """ffmpeg opus stdin -> 16kHz mono int16 wav file."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "ogg", "-i", "pipe:0",
             "-ar", "16000", "-ac", "1",
             "-acodec", "pcm_s16le",
             str(wav_path)],
            input=opus_bytes, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        return True
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"  ffmpeg fail: {e.stderr.decode('utf-8', 'replace')[:200]}\n")
        return False


def main():
    if not TAR.exists():
        sys.exit(f"missing {TAR}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    print(f"opening {TAR}", flush=True)
    with tarfile.open(TAR, "r:gz") as tar:
        for member in tar:
            if written >= N_TARGET:
                break
            if not member.isfile() or not member.name.endswith(".opus"):
                continue
            try:
                f = tar.extractfile(member)
                if f is None:
                    continue
                opus_bytes = f.read()
            except Exception as e:
                print(f"  read fail {member.name}: {e}", flush=True)
                continue
            if not opus_bytes or len(opus_bytes) < 1000:
                continue
            wav_path = OUT_DIR / f"yt_{written:05d}.wav"
            if opus_bytes_to_wav(opus_bytes, wav_path):
                written += 1
                if written % 200 == 0:
                    print(f"  wrote {written}", flush=True)
    print(f"DONE: wrote {written} wavs to {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
