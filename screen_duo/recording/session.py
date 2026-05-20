import os
import threading
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from screen_duo.devices import screen_capture, phone_capture, wifi_powersave
from screen_duo.recording import clapper, compositor
from screen_duo.recording.compositor import OverlayBox


class State(Enum):
    IDLE = auto()
    RECORDING = auto()
    PAUSED = auto()
    COMPOSITING = auto()


class RecordingSession:
    def __init__(self, display, v4l2_device: str, webrtc_server=None):
        self.display = display
        self.v4l2_device = v4l2_device
        self.state = State.IDLE

        self._session_dir = self._make_session_dir()
        self._screen_segments: list[str] = []
        self._phone_segments: list[str] = []
        self._mic_segments: list[str] = []
        self._phone_audio_segments: list[str | None] = []
        self._segment_index = 0
        self._webrtc = webrtc_server

        self._screen_proc = None
        self._phone_proc = None

        self.overlay_box: compositor.OverlayBox | None = None
        self.on_state_change = None
        self.on_progress = None

    def _make_session_dir(self) -> str:
        base = Path.home() / ".screen-duo" / "sessions" / datetime.now().strftime("%Y%m%d_%H%M%S")
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    def _segment_paths(self, index: int) -> tuple[str, str]:
        screen = os.path.join(self._session_dir, f"screen_{index}.mp4")
        phone = os.path.join(self._session_dir, f"phone_{index}.mp4")
        return screen, phone

    def _start_segment(self, flash_callback=None):
        screen_path, phone_path = self._segment_paths(self._segment_index)
        phone_audio_path = os.path.join(
            self._session_dir, f"phone_{self._segment_index}_audio.m4a"
        )

        # Start both as close together as possible
        started = threading.Barrier(2)

        def start_screen():
            started.wait()
            self._screen_proc = screen_capture.start_recording(self.display, screen_path)

        def start_phone():
            started.wait()
            self._phone_proc = phone_capture.start_recording(self.v4l2_device, phone_path)
            if self._webrtc:
                self._webrtc.record_audio_to(phone_audio_path)

        t1 = threading.Thread(target=start_screen)
        t2 = threading.Thread(target=start_phone)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Allow encoders to initialize before the clapper marker
        time.sleep(0.4)
        clapper.trigger(flash_callback=flash_callback)

        self._screen_segments.append(screen_path)
        self._phone_segments.append(phone_path)
        self._mic_segments.append(screen_path.replace(".mp4", "_mic.m4a"))
        self._phone_audio_segments.append(phone_audio_path if self._webrtc else None)

    def _stop_segment(self):
        def stop_screen():
            if self._screen_proc:
                screen_capture.stop_recording(self._screen_proc)
                self._screen_proc = None

        def stop_phone():
            if self._phone_proc:
                phone_capture.stop_recording(self._phone_proc)
                self._phone_proc = None
            if self._webrtc:
                self._webrtc.stop_audio_recording()

        t1 = threading.Thread(target=stop_screen)
        t2 = threading.Thread(target=stop_phone)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    def start(self, flash_callback=None):
        assert self.state == State.IDLE
        assert self.overlay_box is not None
        wifi_powersave.disable()
        self._start_segment(flash_callback)
        self.state = State.RECORDING
        if self.on_state_change:
            self.on_state_change(self.state)

    def pause(self):
        assert self.state == State.RECORDING
        self._stop_segment()
        self._segment_index += 1
        self.state = State.PAUSED
        if self.on_state_change:
            self.on_state_change(self.state)

    def resume(self, flash_callback=None):
        assert self.state == State.PAUSED
        self._start_segment(flash_callback)
        self.state = State.RECORDING
        if self.on_state_change:
            self.on_state_change(self.state)

    def stop(self) -> str:
        assert self.state in (State.RECORDING, State.PAUSED)
        if self.state == State.RECORDING:
            self._stop_segment()

        self.state = State.COMPOSITING
        if self.on_state_change:
            self.on_state_change(self.state)

        tmp_path = os.path.join(self._session_dir, "output.mp4")
        compositor.composite(
            self._screen_segments,
            self._phone_segments,
            self.overlay_box,
            tmp_path,
            mic_segments=self._mic_segments,
            phone_audio_segments=self._phone_audio_segments,
            progress_callback=self.on_progress,
        )

        wifi_powersave.enable()
        self.state = State.IDLE
        if self.on_state_change:
            self.on_state_change(self.state)

        return tmp_path

    def suggested_save_path(self) -> str:
        out_dir = Path.home() / "Videos" / "screen-duo"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(out_dir / f"{ts}.mp4")
