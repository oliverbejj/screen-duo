"""Holds a D-Bus connection open for a GNOME Shell screencast.

Usage:
  Full desktop:  python3 _gnome_screencast_helper.py <template> <framerate>
  Single monitor: python3 _gnome_screencast_helper.py <template> <x> <y> <w> <h> <framerate>
"""
import sys

from gi.repository import Gio, GLib

DEST = "org.gnome.Shell.Screencast"
PATH = "/org/gnome/Shell/Screencast"
IFACE = "org.gnome.Shell.Screencast"


def main() -> int:
    template = sys.argv[1]

    # Detect area vs full-desktop mode by argument count
    if len(sys.argv) >= 7:
        x, y, w, h = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
        framerate = int(sys.argv[6])
        area_mode = True
    else:
        framerate = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        area_mode = False

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    options = {
        "framerate": GLib.Variant("u", framerate),
        "draw-cursor": GLib.Variant("b", True),
    }

    try:
        if area_mode:
            result = bus.call_sync(
                DEST, PATH, IFACE, "ScreencastArea",
                GLib.Variant("(iiiisa{sv})", (x, y, w, h, template, options)),
                GLib.VariantType("(bs)"),
                Gio.DBusCallFlags.NONE, 10000, None,
            )
        else:
            result = bus.call_sync(
                DEST, PATH, IFACE, "Screencast",
                GLib.Variant("(sa{sv})", (template, options)),
                GLib.VariantType("(bs)"),
                Gio.DBusCallFlags.NONE, 10000, None,
            )
    except GLib.Error as exc:
        sys.stdout.write(f"ERR {exc.message}\n")
        sys.stdout.flush()
        return 1

    success, filename = result.unpack()
    if not success:
        sys.stdout.write("ERR Screencast returned false\n")
        sys.stdout.flush()
        return 1

    sys.stdout.write(f"OK {filename}\n")
    sys.stdout.flush()

    sys.stdin.readline()

    try:
        bus.call_sync(
            DEST, PATH, IFACE, "StopScreencast",
            None, GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, 15000, None,
        )
    except GLib.Error:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
