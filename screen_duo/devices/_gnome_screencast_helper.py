"""Holds a D-Bus connection open for a GNOME Shell screencast.

GNOME Shell stops a screencast as soon as the caller's D-Bus connection
disconnects, so a one-shot `gdbus call` cannot drive a recording. This script
runs as a long-lived subprocess: it starts the screencast, reports the actual
output filename on stdout, then blocks until the parent writes a line to stdin
(or closes it), at which point it stops the screencast and exits.

It must run on a python3 that has PyGObject (gi) — typically the system
interpreter, not necessarily the one running the app.
"""
import sys

from gi.repository import Gio, GLib

DEST = "org.gnome.Shell.Screencast"
PATH = "/org/gnome/Shell/Screencast"
IFACE = "org.gnome.Shell.Screencast"


def main() -> int:
    template = sys.argv[1]
    framerate = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    options = {
        "framerate": GLib.Variant("u", framerate),
        "draw-cursor": GLib.Variant("b", True),
    }

    try:
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

    # Block here, keeping the bus connection alive, until the parent signals.
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
