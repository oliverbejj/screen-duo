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

    def label(self) -> str:
        """Human-readable label, replacing opaque XWayland names."""
        if self.name.startswith("XWAYLAND"):
            try:
                n = int(self.name[len("XWAYLAND"):]) + 1
            except ValueError:
                n = 1
            tag = f"Screen {n}"
        else:
            tag = self.name
        primary = self.x == 0 and self.y == 0
        suffix = " (primary)" if primary else f" @ {self.x},{self.y}"
        return f"{tag}  {self.width}x{self.height}{suffix}"

    def __str__(self):
        return self.label()


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
    # wlr-randr: works on wlroots compositors (Sway, Hyprland)
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

    # xrandr via XWayland: works on GNOME Wayland sessions
    try:
        out = subprocess.check_output(["xrandr", "--query"], text=True, stderr=subprocess.DEVNULL)
        displays = []
        for line in out.splitlines():
            match = re.match(r"^(\S+) connected.*?(\d+)x(\d+)\+(\d+)\+(\d+)", line)
            if match:
                name, w, h, x, y = match.groups()
                displays.append(Display(name, int(w), int(h), int(x), int(y)))
        if displays:
            return displays
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return [Display("Screen", 0, 0, 0, 0)]


def start_recording(display: Display, output_path: str, framerate: int = 30) -> subprocess.Popen:
    if is_wayland():
        return _start_wayland(display, output_path, framerate)
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


_SCREENCAST_DEST = "org.gnome.Shell.Screencast"
_SCREENCAST_PATH = "/org/gnome/Shell/Screencast"
_HELPER_SCRIPT = os.path.join(os.path.dirname(__file__), "_gnome_screencast_helper.py")


class _GnomeScreencast:
    """Handle for a recording driven by the org.gnome.Shell.Screencast D-Bus
    API. GNOME stops the recording when the caller's D-Bus connection vanishes,
    so a helper subprocess holds that connection open for the segment's life."""

    def __init__(self, proc: subprocess.Popen, requested_path: str, actual_path: str):
        self._proc = proc
        self._requested_path = requested_path
        self._actual_path = actual_path

    def stop(self) -> None:
        try:
            self._proc.stdin.write(b"stop\n")
            self._proc.stdin.flush()
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait()
        if self._actual_path != self._requested_path and os.path.exists(self._actual_path):
            os.replace(self._actual_path, self._requested_path)


_gi_python: str | None | bool = False  # False = not yet probed


def _find_gi_python() -> str | None:
    """Locate a python3 with PyGObject (gi) for the screencast helper. The app
    itself may run on an interpreter without gi, so we probe the system one."""
    global _gi_python
    if _gi_python is not False:
        return _gi_python
    _gi_python = None
    probe = "import gi; gi.require_version('Gio','2.0'); from gi.repository import Gio"
    for exe in ("/usr/bin/python3", "/usr/bin/python3.12", "/usr/bin/python3.11", "python3"):
        try:
            subprocess.check_call(
                [exe, "-c", probe],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
            _gi_python = exe
            break
        except (subprocess.SubprocessError, OSError):
            continue
    return _gi_python


def _gnome_screencast_available() -> bool:
    """GNOME's Mutter does not implement wlr-screencopy (so wf-recorder fails);
    it exposes screen recording over D-Bus instead."""
    if _find_gi_python() is None:
        return False
    try:
        out = subprocess.check_output(
            ["gdbus", "call", "--session",
             "--dest", _SCREENCAST_DEST,
             "--object-path", _SCREENCAST_PATH,
             "--method", "org.freedesktop.DBus.Properties.Get",
             _SCREENCAST_DEST, "ScreencastSupported"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        return "true" in out
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _start_gnome_screencast(display: Display, output_path: str, framerate: int) -> _GnomeScreencast:
    py = _find_gi_python()
    if py is None:
        raise RuntimeError("No python3 with PyGObject (gi) found for GNOME screencast")
    args = [py, _HELPER_SCRIPT, output_path]
    if display.width > 0 and display.height > 0:
        # Area mode: record only the selected monitor's region
        args += [str(display.x), str(display.y), str(display.width), str(display.height)]
    args.append(str(framerate))
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    line = proc.stdout.readline().decode(errors="replace").strip()
    if not line.startswith("OK "):
        proc.terminate()
        raise RuntimeError(f"GNOME screencast failed to start: {line or 'no response'}")
    return _GnomeScreencast(proc, output_path, line[3:])


def _start_wayland(display: Display, output_path: str, framerate: int):
    if _gnome_screencast_available():
        return _start_gnome_screencast(display, output_path, framerate)
    # wf-recorder works on wlroots compositors (Sway, Hyprland), not GNOME.
    cmd = ["wf-recorder", "--codec", "libx264", "-f", output_path]
    # Pass the output name when it's a real connector (not an XWayland alias)
    if display.name and not display.name.startswith("XWAYLAND"):
        cmd += ["--output", display.name]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)


def stop_recording(proc) -> None:
    if isinstance(proc, _GnomeScreencast):
        proc.stop()
        return
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
