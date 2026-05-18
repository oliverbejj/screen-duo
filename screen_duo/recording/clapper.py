import subprocess
import tempfile
import threading
import wave
import struct
import math
import time


def _generate_beep_wav(path: str, freq: int = 1000, duration: float = 0.3, sample_rate: int = 44100):
    n_samples = int(sample_rate * duration)
    with wave.open(path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        for i in range(n_samples):
            val = int(32767 * math.sin(2 * math.pi * freq * i / sample_rate))
            f.writeframes(struct.pack("<h", val))


def trigger(flash_callback=None) -> float:
    """
    Play a beep through the system speaker and optionally call flash_callback()
    to show a white flash on screen. Returns the wall-clock timestamp of the trigger.

    The beep is captured by the phone microphone → used by sync.py to align segments.
    The flash appears in the screen recording → used as a secondary alignment marker.
    """
    beep_file = tempfile.mktemp(suffix=".wav")
    _generate_beep_wav(beep_file)

    ts = time.time()

    def _play():
        for player in [["paplay", beep_file], ["aplay", beep_file]]:
            try:
                subprocess.run(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue

    beep_thread = threading.Thread(target=_play, daemon=True)
    beep_thread.start()

    if flash_callback:
        flash_callback()

    return ts
