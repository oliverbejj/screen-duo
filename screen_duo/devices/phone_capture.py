import contextlib
import glob
import os
import subprocess
import threading
import time

import cv2


@contextlib.contextmanager
def _suppress_av_log():
    """Redirect C-level fd 2 to /dev/null to silence libav/v4l2 ioctl noise."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


def list_v4l2_devices() -> list[str]:
    """Return available /dev/videoX device paths."""
    return sorted(glob.glob("/dev/video*"))


def check_device_live(v4l2_device: str) -> bool:
    """Return True if OpenCV can read a frame from the device (fed by WebRTC/ffmpeg or similar)."""
    with _suppress_av_log():
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
    """Block until the device has frames (start camera on phone / feed loopback first)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check_device_live(v4l2_device):
            return True
        time.sleep(0.5)
    return False



def start_recording(v4l2_device: str, output_path: str, framerate: int = 30) -> subprocess.Popen:
    """Capture from the v4l2 device to a file via OpenCV → raw pipe → ffmpeg.

    OpenCV is used for all devices (real cameras and v4l2loopback alike) because
    it is the same path the overlay preview uses and is confirmed to deliver frames
    at the right rate. For real cameras, cap.set(FPS) also disables auto-exposure
    priority, preventing the driver from throttling below 30 fps.
    """
    return _start_recording_opencv(v4l2_device, output_path, framerate)


def _start_recording_opencv(v4l2_device: str, output_path: str, framerate: int) -> subprocess.Popen:
    """Record via OpenCV → raw pipe → ffmpeg, encoding on the GPU (VAAPI).

    Encoding runs on the iGPU's dedicated H.264 block, not libx264 on the CPU.
    The CPU governor (intel_pstate/powersave) keeps cores parked at ~400 MHz
    during a recording because the bursty capture threads never look like
    sustained load; at that clock libx264 only manages ~3 fps, which is what
    made saved phone footage choppy unless something kept the screen busy.
    A hardware encoder is immune to CPU clock state.
    """
    with _suppress_av_log():
        cap = cv2.VideoCapture(v4l2_device)
    cap.set(cv2.CAP_PROP_FPS, framerate)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}", "-r", str(framerate),
            "-i", "pipe:0",
            "-vaapi_device", "/dev/dri/renderD128",
            "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-qp", "18",
            output_path,
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    stop = threading.Event()

    def _pump():
        try:
            while not stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    continue
                try:
                    proc.stdin.write(frame.tobytes())
                except (BrokenPipeError, OSError):
                    break
        finally:
            cap.release()

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    proc._stop_event = stop
    proc._pump_thread = t
    return proc



def stop_recording(proc: subprocess.Popen) -> None:
    stop: threading.Event | None = getattr(proc, "_stop_event", None)
    if stop:
        # OpenCV path: signal pump thread to exit, then close stdin so ffmpeg finalises
        stop.set()
        thread: threading.Thread | None = getattr(proc, "_pump_thread", None)
        if thread:
            thread.join(timeout=2.0)
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    else:
        # Loopback/direct ffmpeg path: send interactive quit
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait()
