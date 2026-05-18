import os
import re
import subprocess
from dataclasses import dataclass


@dataclass
class Display:
    name: str
    width: int
    height: int
    x: int
    y: int

    def __str__(self):
        return f"{self.name} ({self.width}x{self.height})"


def is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def list_displays() -> list[Display]:
    if is_wayland():
        return _list_displays_wayland()
    return _list_displays_x11()


def _list_displays_x11() -> list[Display]:
    out = subprocess.check_output(["xrandr", "--query"], text=True)
    displays = []
    for line in out.splitlines():
        match = re.match(r"^(\S+) connected.*?(\d+)x(\d+)\+(\d+)\+(\d+)", line)
        if match:
            name, w, h, x, y = match.groups()
            displays.append(Display(name, int(w), int(h), int(x), int(y)))
    return displays


def _list_displays_wayland() -> list[Display]:
    try:
        out = subprocess.check_output(["wlr-randr"], text=True, stderr=subprocess.DEVNULL)
        displays = []
        current_name = None
        for line in out.splitlines():
            name_match = re.match(r'^(\S+)\s+"', line)
            if name_match:
                current_name = name_match.group(1)
            if current_name and "current" in line:
                mode_match = re.search(r"(\d+)x(\d+) px", line)
                pos_match = re.search(r"(\d+),(\d+) px", line)
                if mode_match and pos_match:
                    w, h = mode_match.groups()
                    x, y = pos_match.groups()
                    displays.append(Display(current_name, int(w), int(h), int(x), int(y)))
        if displays:
            return displays
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # Fall back: portal will show its own picker, use a sentinel
    return [Display("Screen (portal picker)", 0, 0, 0, 0)]


def start_recording(display: Display, output_path: str, framerate: int = 30) -> subprocess.Popen:
    if is_wayland():
        return _start_wayland(output_path, framerate)
    return _start_x11(display, output_path, framerate)


def _start_x11(display: Display, output_path: str, framerate: int) -> subprocess.Popen:
    xdisplay = os.environ.get("DISPLAY", ":0")
    cmd = [
        "ffmpeg", "-y",
        "-f", "x11grab",
        "-framerate", str(framerate),
        "-video_size", f"{display.width}x{display.height}",
        "-i", f"{xdisplay}.0+{display.x},{display.y}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        output_path,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _start_wayland(output_path: str, framerate: int) -> subprocess.Popen:
    # wf-recorder works on wlroots compositors (Sway, Hyprland, GNOME via xdg-desktop-portal)
    cmd = ["wf-recorder", "--codec", "libx264", "-f", output_path]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)


def stop_recording(proc: subprocess.Popen) -> None:
    if is_wayland():
        import signal
        proc.send_signal(signal.SIGINT)
    else:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait()
