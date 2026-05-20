"""Toggle WiFi power-save mode during recording.

WiFi PSM (802.11 power management) batches UDP packet delivery to ~300 ms
bursts when the system is idle, causing WebRTC frames to arrive in clumps
and drop the effective phone camera fps from 30 to ~3.

Requires a sudoers drop-in (added by setup) so iw runs without a prompt.
"""

import subprocess


def _wifi_interface() -> str | None:
    try:
        out = subprocess.check_output(
            ["iw", "dev"], stderr=subprocess.DEVNULL, text=True
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Interface "):
                return line.split()[1]
    except Exception:
        pass
    return None


def _set(iface: str, state: str) -> bool:
    try:
        r = subprocess.run(
            ["sudo", "/usr/sbin/iw", "dev", iface, "set", "power_save", state],
            capture_output=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def disable() -> bool:
    iface = _wifi_interface()
    return _set(iface, "off") if iface else False


def enable() -> bool:
    iface = _wifi_interface()
    return _set(iface, "on") if iface else False
