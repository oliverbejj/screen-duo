import subprocess
import tempfile
import os
import numpy as np


def _has_audio_stream(video_path: str) -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=5,
        )
        return "audio" in result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _extract_audio(video_path: str, wav_path: str, sample_rate: int = 44100):
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", video_path,
            "-f", "wav", "-ar", str(sample_rate), "-ac", "1",
            wav_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _find_audio_spike(wav_path: str, sample_rate: int = 44100) -> float:
    """Return timestamp (seconds) of first audio spike above threshold."""
    import wave
    import struct

    with wave.open(wav_path, "r") as f:
        n_frames = f.getnframes()
        raw = f.readframes(n_frames)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    samples /= 32768.0

    window = int(sample_rate * 0.01)  # 10ms RMS window
    rms = np.array([
        np.sqrt(np.mean(samples[i:i+window] ** 2))
        for i in range(0, len(samples) - window, window)
    ])

    threshold = np.max(rms) * 0.5
    hits = np.where(rms > threshold)[0]
    if len(hits) == 0:
        return 0.0
    return float(hits[0] * window) / sample_rate


def _find_screen_flash(video_path: str, framerate: int = 30) -> float:
    """Return timestamp (seconds) of first bright flash frame in screen video."""
    result = subprocess.run(
        [
            "ffmpeg", "-i", video_path,
            "-vf", "scale=64:36,geq=lum_expr='lum(X\\,Y)':component_fmt=yuv420p",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    frames = np.frombuffer(result.stdout, dtype=np.uint8)
    frame_size = 64 * 36
    if len(frames) < frame_size:
        return 0.0

    n_frames = len(frames) // frame_size
    frames = frames[:n_frames * frame_size].reshape(n_frames, frame_size)
    means = frames.mean(axis=1)

    threshold = 200.0
    hits = np.where(means > threshold)[0]
    if len(hits) == 0:
        return 0.0
    return float(hits[0]) / framerate


def compute_offset(screen_path: str, phone_path: str) -> float:
    """
    Return trim_phone - trim_screen in seconds.
    Positive means phone recording starts later → trim phone by this amount.
    Negative means screen recording starts later → trim screen by abs(value).
    Returns 0.0 when phone recording has no audio (WebRTC pipeline is video-only).
    """
    if not _has_audio_stream(phone_path):
        return 0.0

    tmp_dir = tempfile.mkdtemp()
    phone_wav = os.path.join(tmp_dir, "phone.wav")

    try:
        _extract_audio(phone_path, phone_wav)
        phone_ts = _find_audio_spike(phone_wav)
        screen_ts = _find_screen_flash(screen_path)
        return phone_ts - screen_ts
    finally:
        if os.path.exists(phone_wav):
            os.remove(phone_wav)
        os.rmdir(tmp_dir)


def build_trim_args(offset: float) -> tuple[float, float]:
    """Return (screen_trim_seconds, phone_trim_seconds) to apply before concatenating."""
    if offset >= 0:
        return 0.0, offset
    return abs(offset), 0.0
