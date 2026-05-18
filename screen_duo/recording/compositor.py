import os
import subprocess
import tempfile
from dataclasses import dataclass

from screen_duo.recording.sync import compute_offset, build_trim_args


@dataclass
class OverlayBox:
    x: int
    y: int
    width: int
    height: int


def _trim_and_concat(paths: list[str], trim_seconds: float, output_path: str):
    """Concat video segments with optional leading trim, losslessly via ffmpeg concat."""
    tmp_list = tempfile.mktemp(suffix=".txt")
    with open(tmp_list, "w") as f:
        for p in paths:
            f.write(f"file '{p}'\n")

    if trim_seconds > 0:
        trimmed = tempfile.mktemp(suffix=".mp4")
        # Concat first, then trim
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", tmp_list,
             "-c", "copy", trimmed],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(trim_seconds), "-i", trimmed,
             "-c", "copy", output_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        os.remove(trimmed)
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", tmp_list,
             "-c", "copy", output_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

    os.remove(tmp_list)


def composite(
    screen_segments: list[str],
    phone_segments: list[str],
    box: OverlayBox,
    output_path: str,
    progress_callback=None,
) -> str:
    """
    Sync segments, apply face-tracked crop to phone feed, overlay on screen, write output.
    Returns the output path.
    """
    tmp_dir = tempfile.mkdtemp()

    if progress_callback:
        progress_callback("Computing sync offset…")

    offset = compute_offset(screen_segments[0], phone_segments[0])
    screen_trim, phone_trim = build_trim_args(offset)

    if progress_callback:
        progress_callback("Concatenating screen segments…")

    screen_full = os.path.join(tmp_dir, "screen_full.mp4")
    _trim_and_concat(screen_segments, screen_trim, screen_full)

    if progress_callback:
        progress_callback("Concatenating phone segments…")

    phone_full = os.path.join(tmp_dir, "phone_full.mp4")
    _trim_and_concat(phone_segments, phone_trim, phone_full)

    if progress_callback:
        progress_callback("Compositing final video…")

    bw, bh = box.width, box.height
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", screen_full,
            "-i", phone_full,
            "-filter_complex",
            f"[1:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh}[phone];[0:v][phone]overlay={box.x}:{box.y}",
            "-c:v", "libx264", "-preset", "slow", "-crf", "17",
            "-c:a", "copy",
            output_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    for f in [screen_full, phone_full]:
        if os.path.exists(f):
            os.remove(f)
    os.rmdir(tmp_dir)

    return output_path
