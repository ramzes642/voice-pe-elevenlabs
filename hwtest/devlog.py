"""Voice PE ESPHome log capture over USB serial, with event parsing.

The Voice PE enumerates as Espressif "USB JTAG_serial debug unit" (/dev/cu.usbmodem*).
ESPHome's logger streams there at 115200. We timestamp every line against the test
clock (time.monotonic) so device events line up with the laptop's audio timeline.
"""
from __future__ import annotations

import glob
import re
import threading
import time
from dataclasses import dataclass, field

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# (regex, event name) — first match wins. Group 1 (if any) becomes `detail`.
PATTERNS = [
    (re.compile(r"micro_wake_word:\d+\]: Detected '([^']+)'"), "wake_detected"),
    (re.compile(r"Speech recognised as: \"(.*)\"$"), "stt_text"),
    (re.compile(r"voice_assistant:\d+\]: Response: \"(.*)$"), "response_text"),
    (re.compile(r"voice_assistant:\d+\]: STT started"), "stt_started"),
    (re.compile(r"Starting STT by VAD"), "stt_vad_start"),
    (re.compile(r"STT by VAD end"), "stt_vad_end"),
    (re.compile(r"Intent started"), "intent_started"),
    (re.compile(r"Assist Pipeline ended"), "pipeline_ended"),
    (re.compile(r"voice_assistant:\d+\]: State changed from (\w+ to \w+)"), "va_state"),
    (re.compile(r"speaker_source_media_player:\d+\]: State changed to (\w+)"), "player_state"),
    (re.compile(r"\[E\]\[(.*)$"), "error"),
]


@dataclass
class Event:
    t: float          # seconds since test t0
    name: str
    detail: str = ""


@dataclass
class DeviceLog:
    port: str | None = None
    t0: float = field(default_factory=time.monotonic)
    lines: list[tuple[float, str]] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    _stop: bool = False
    _thread: threading.Thread | None = None
    _ser: object = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def autodetect() -> str | None:
        ports = sorted(glob.glob("/dev/cu.usbmodem*"))
        return ports[0] if ports else None

    def start(self) -> "DeviceLog":
        import serial
        if self.port in (None, ""):
            self.port = self.autodetect()
        if self.port is None:
            raise RuntimeError("no /dev/cu.usbmodem* found — is the Voice PE plugged in via USB?")
        self._ser = serial.Serial(self.port, 115200, timeout=0.2)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        buf = b""
        while not self._stop:
            try:
                chunk = self._ser.read(4096)
            except Exception as e:  # device unplugged mid-test
                self._append(time.monotonic() - self.t0, f"[devlog] serial read failed: {e}")
                return
            if not chunk:
                continue
            now = round(time.monotonic() - self.t0, 3)
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = ANSI.sub("", raw.decode("utf-8", "replace")).rstrip("\r")
                if line.strip():
                    self._append(now, line)

    def _append(self, t: float, line: str):
        with self._lock:
            self.lines.append((t, line))
            for rx, name in PATTERNS:
                m = rx.search(line)
                if m:
                    detail = m.group(1) if m.groups() else ""
                    self.events.append(Event(t, name, detail.rstrip('"')))
                    break

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=1)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass

    def snapshot(self) -> list[Event]:
        with self._lock:
            return list(self.events)

    def has(self, name: str, after: float = -1.0) -> Event | None:
        for e in self.snapshot():
            if e.name == name and e.t >= after:
                return e
        return None

    def dump(self, path):
        with open(path, "w") as f:
            for t, line in self.lines:
                f.write(f"{t:8.3f}  {line}\n")


if __name__ == "__main__":
    import sys
    log = DeviceLog(sys.argv[1] if len(sys.argv) > 1 else None).start()
    print(f"reading {log.port} — Ctrl-C to stop")
    seen = 0
    try:
        while True:
            time.sleep(0.2)
            with log._lock:
                new = log.lines[seen:]
                seen = len(log.lines)
            for t, line in new:
                print(f"{t:8.3f}  {line}")
    except KeyboardInterrupt:
        log.stop()
