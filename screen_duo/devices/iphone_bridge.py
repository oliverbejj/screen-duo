import glob
import os
import subprocess


def find_loopback_device() -> str | None:
    """Return the first v4l2loopback device path, or None if none is loaded."""
    for dev in sorted(glob.glob("/dev/video*")):
        name_file = f"/sys/class/video4linux/{os.path.basename(dev)}/name"
        try:
            with open(name_file) as f:
                name = f.read().lower()
            if "dummy" in name or "loopback" in name:
                return dev
        except OSError:
            pass
    return None


def start_bridge(source_url: str, v4l2_device: str) -> subprocess.Popen:
    """
    Pipe any ffmpeg-readable source (HTTP MJPEG, RTSP, RTMP) into a v4l2loopback device.
    The process runs until stop_bridge() is called.
    """
    cmd = [
        "ffmpeg",
        "-i", source_url,
        "-vf", "format=yuv420p",
        "-f", "v4l2", v4l2_device,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def stop_bridge(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait()
