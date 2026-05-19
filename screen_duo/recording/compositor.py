import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from screen_duo.recording.sync import compute_offset, build_trim_args, has_audio_stream


@dataclass
class OverlayBox:
    x: int
    y: int
    width: int
    height: int


def _trim_segment(input_path: str, trim_seconds: float, output_path: str):
    """Copy or trim a single segment to output_path."""
    if trim_seconds > 0:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(trim_seconds), "-i", input_path,
             "-c", "copy", output_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-c", "copy", output_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )


def _concat_segments(paths: list[str], output_path: str):
    """Losslessly concatenate segments via ffmpeg concat demuxer."""
    fd, tmp_list = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            for p in paths:
                f.write(f"file '{p}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", tmp_list,
             "-c", "copy", output_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
    finally:
        if os.path.exists(tmp_list):
            os.remove(tmp_list)


def composite(
    screen_segments: list[str],
    phone_segments: list[str],
    box: OverlayBox,
    output_path: str,
    mic_segments: list[str] | None = None,
    phone_audio_segments: list[str | None] | None = None,
    progress_callback=None,
) -> str:
    """
    Sync segments, overlay phone feed on screen, write output.

    Audio source priority: phone_audio_segments (WebRTC mic) > mic_segments
    (GNOME laptop mic) > embedded screen audio.

    On GNOME Wayland, phone video and mic both start before the D-Bus handshake,
    so they record T_gnome extra content compared to screen. The mic offset measures
    T_gnome; we apply it to phone video too (they start at the same wall-clock time).
    When phone audio is available it measures the same offset directly, so no proxy
    needed.

    Returns the output path.
    """
    if len(screen_segments) != len(phone_segments):
        raise ValueError(
            f"segment count mismatch: {len(screen_segments)} screen vs {len(phone_segments)} phone"
        )
    tmp_dir = tempfile.mkdtemp()
    try:
        n = len(screen_segments)

        screen_trimmed: list[str] = []
        phone_trimmed: list[str] = []
        mic_trimmed: list[str | None] = []
        phone_audio_trimmed: list[str | None] = []

        for i, (screen_seg, phone_seg) in enumerate(zip(screen_segments, phone_segments)):
            if progress_callback:
                progress_callback(f"Syncing segment {i + 1} of {n}…")

            mic_path = (mic_segments or [])[i] if mic_segments and i < len(mic_segments) else None
            mic_valid = bool(mic_path and os.path.exists(mic_path) and os.path.getsize(mic_path) > 0)

            phone_audio_path = (
                (phone_audio_segments or [])[i]
                if phone_audio_segments and i < len(phone_audio_segments)
                else None
            )
            phone_audio_valid = bool(
                phone_audio_path
                and os.path.exists(phone_audio_path)
                and os.path.getsize(phone_audio_path) > 0
                and has_audio_stream(phone_audio_path)
            )

            screen_out = os.path.join(tmp_dir, f"screen_trimmed_{i}.mp4")
            phone_out = os.path.join(tmp_dir, f"phone_trimmed_{i}.mp4")

            # Compute mic offset once — measures the GNOME handshake delay (T_gnome)
            # because mic and phone both start before the D-Bus handshake completes.
            mic_sync = 0.0
            if mic_valid:
                mic_sync = max(0.0, compute_offset(screen_seg, mic_path))

            # Phone video sync: prefer phone audio (direct clapper detection); fall
            # back to mic_sync as a proxy when phone has no audio stream.
            if phone_audio_valid:
                raw = compute_offset(screen_seg, phone_audio_path)
                screen_trim, phone_trim = build_trim_args(raw)
            else:
                offset = compute_offset(screen_seg, phone_seg)  # 0.0 if no audio in phone
                screen_trim, phone_trim = build_trim_args(offset)
                if phone_trim == 0.0 and mic_sync > 0.0:
                    phone_trim = mic_sync

            _trim_segment(screen_seg, screen_trim, screen_out)
            _trim_segment(phone_seg, phone_trim, phone_out)
            screen_trimmed.append(screen_out)
            phone_trimmed.append(phone_out)

            # Mic trim
            if mic_valid:
                mic_out = os.path.join(tmp_dir, f"mic_trimmed_{i}.m4a")
                _trim_segment(mic_path, mic_sync, mic_out)
                mic_trimmed.append(mic_out)
            else:
                mic_trimmed.append(None)

            # Phone audio trim (same offset as phone video to stay in sync)
            if phone_audio_valid:
                phone_audio_out = os.path.join(tmp_dir, f"phone_audio_trimmed_{i}.m4a")
                _trim_segment(phone_audio_path, phone_trim, phone_audio_out)
                phone_audio_trimmed.append(phone_audio_out)
            else:
                phone_audio_trimmed.append(None)

        if progress_callback:
            progress_callback("Concatenating screen segments…")
        screen_full = os.path.join(tmp_dir, "screen_full.mp4")
        _concat_segments(screen_trimmed, screen_full)

        if progress_callback:
            progress_callback("Concatenating phone segments…")
        phone_full = os.path.join(tmp_dir, "phone_full.mp4")
        _concat_segments(phone_trimmed, phone_full)

        valid_phone_audios = [m for m in phone_audio_trimmed if m]
        phone_audio_full = None
        if len(valid_phone_audios) == n and n > 0:
            phone_audio_full = os.path.join(tmp_dir, "phone_audio_full.m4a")
            _concat_segments(valid_phone_audios, phone_audio_full)

        valid_mics = [m for m in mic_trimmed if m]
        mic_full = None
        if len(valid_mics) == n and n > 0:
            mic_full = os.path.join(tmp_dir, "mic_full.m4a")
            _concat_segments(valid_mics, mic_full)

        if progress_callback:
            progress_callback("Compositing final video…")

        # Audio priority: phone mic > laptop mic > embedded screen audio
        audio_source = phone_audio_full or mic_full

        bw, bh = box.width, box.height
        cmd = ["ffmpeg", "-y", "-i", screen_full, "-i", phone_full]
        if audio_source:
            cmd += ["-i", audio_source]
        cmd += [
            "-filter_complex",
            f"[1:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh}[phone];[0:v][phone]overlay={box.x}:{box.y}[out]",
            "-map", "[out]",
            "-map", f"{'2:a' if audio_source else '0:a?'}",
            "-c:v", "libx264", "-preset", "slow", "-crf", "17",
            "-c:a", "copy",
            output_path,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        return output_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
