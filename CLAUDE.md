# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

screen-duo is a Linux desktop tool for recording your screen and phone camera simultaneously, syncing them, and compositing the phone feed (head cam) as an overlay on the screen recording.

**User flow:** Launch app → select display → phone camera starts automatically via ADB → preview overlay position → record → pause/resume → stop → auto-composite to MP4.

## Architecture

The project is split into three layers:

**1. Device layer** (`devices/`)
- `screen_capture.py` — captures the selected display. Auto-detects X11 (ffmpeg `x11grab`) vs Wayland (PipeWire via `xdg-desktop-portal`). Detection: check `$WAYLAND_DISPLAY`.
- `phone_capture.py` — controls the phone camera over USB via ADB + `scrcpy --video-source=camera`. Exposes the feed as a V4L2 device via `v4l2loopback`. ADB is used to start/stop recording programmatically.

**2. Recording layer** (`recording/`)
- `session.py` — orchestrates a recording session. Manages segments (pause = stop both feeds simultaneously, resume = start new segment pair). Uses simultaneous ADB + ffmpeg triggers for sync.
- `sync.py` — post-recording sync correction. At the start of each segment, a clapper flash is shown on screen and a beep plays through the phone speaker. `sync.py` finds these markers in each segment pair to compute drift and align them.
- `compositor.py` — post-recording ffmpeg compositing. Concatenates segments (lossless via `concat` demuxer), then overlays the phone feed on the screen recording. Output: H.264/MP4.

**3. UI layer** (`ui/`)
- Built with PySide6.
- `main_window.py` — display selector, record/pause/stop controls, live preview.
- `overlay_widget.py` — draggable box the user positions on the preview (default: top-right). Within that box, OpenCV face tracking crops the phone feed to follow the head.
- Preview during recording is a lightweight OpenCV overlay, not the final composited render.

## Key constraints

- **Pause is segment-based**: pause hard-stops both recordings; resume starts new segments. `ffmpeg concat` stitches segments losslessly at the end. Sync is preserved because each segment pair starts simultaneously.
- **Compositing is post-recording only**: ffmpeg runs at full speed after stop, not in real-time during recording (avoids dropped frames).
- **Face tracking scope**: OpenCV tracks the face *within* the user-positioned overlay box only — it crops/pans the phone feed inside the box, it does not move the box itself.
- **Phone connection**: iPhone via OBS virtual camera. OBS runs with `obs-ios-camera-source` plugin and outputs to a v4l2loopback device. screen-duo reads from that device. The user must have OBS running with virtual camera enabled before hitting Record. `phone_capture.py` does not launch OBS — it just detects a live v4l2 feed.

## Tech stack

- Python 3.10+
- PySide6 (UI)
- OpenCV (`opencv-python`) — face tracking within overlay box
- ffmpeg via subprocess — screen capture (x11grab/pipewire), video concat, compositing
- OBS + obs-ios-camera-source — iPhone camera bridge (user-managed, not launched by app)
- v4l2loopback — OBS virtual camera output device

## Development setup

```bash
pip install -r requirements.txt
# System deps
sudo apt install v4l2loopback-dkms ffmpeg obs-studio
sudo modprobe v4l2loopback
```

**iPhone setup (one-time):**
1. Install [obs-ios-camera-source](https://github.com/wtsnz/obs-ios-camera-source) OBS plugin
2. Install "Camera for OBS Studio" app on your iPhone (free, App Store)
3. In OBS: add iOS Camera source → enable Virtual Camera
4. Launch screen-duo — it auto-detects the live v4l2 feed

Run the app:
```bash
python -m screen_duo
```

## Git workflow

After completing any meaningful unit of work — a new file, a bug fix, a refactor — commit and push soon after so work and status are never stranded only on one machine. Prefer one logical change per commit rather than mixing unrelated edits.

```bash
git add <specific files>
git commit -m "short description of what and why"
git push
```

Commit message rules:
- Imperative mood: "add compositor face tracking" not "added" or "adds"
- One line, under 72 chars
- No generic messages like "update files" or "fix stuff"

Push after every commit. This is a solo project with no PR review process — push directly to `main`.

**Documentation:** When you change architecture, setup, or constraints above, update this file (or the doc that owns that information) in the same cadence — ideally the same commit — so humans and coding agents stay aligned. Project agent skills live under `.cursor/skills/` and `.claude/skills/` (`my-init`, `git-commit-and-push`, `keep-context-docs-current`): use `my-init` for `/init`-style onboarding that ties into the other two workflows.
