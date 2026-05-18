import subprocess
import time

import cv2


def list_v4l2_devices() -> list[str]:
    """Return available /dev/videoX device paths."""
    import glob
    return sorted(glob.glob("/dev/video*"))


def check_device_live(v4l2_device: str) -> bool:
    """Return True if the device has an active feed (i.e. OBS virtual cam is running)."""
    cap = cv2.VideoCapture(v4l2_device)
    ok, _ = cap.read()
    cap.release()
    return ok


def find_live_device() -> str | None:
    """Return the first v4l2 device with a live feed, or None."""
    for dev in list_v4l2_devices():
        if check_device_live(dev):
            return dev
    return None


def wait_for_device(v4l2_device: str, timeout: float = 10.0) -> bool:
    """Block until the device has a live feed (user may be starting OBS)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check_device_live(v4l2_device):
            return True
        time.sleep(0.5)
    return False


def start_recording(v4l2_device: str, output_path: str, framerate: int = 30) -> subprocess.Popen:
    """Capture from the v4l2 device (OBS virtual cam) to a file via ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "v4l2",
        "-framerate", str(framerate),
        "-i", v4l2_device,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        output_path,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)


def stop_recording(proc: subprocess.Popen) -> None:
    proc.stdin.write(b"q")
    proc.stdin.flush()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait()
