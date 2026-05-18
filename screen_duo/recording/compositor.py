import os
import subprocess
import tempfile
from dataclasses import dataclass

import cv2

from screen_duo.recording.sync import compute_offset, build_trim_args


@dataclass
class OverlayBox:
    x: int
    y: int
    width: int
    height: int


def _process_phone_video(phone_path: str, box: OverlayBox, out_path: str):
    cap = cv2.VideoCapture(phone_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_path, fourcc, fps, (box.width, box.height))

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(cv2.resize(frame, (box.width, box.height)))

    cap.release()
    writer.release()


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
        progress_callback("Applying face tracking…")

    phone_cropped = os.path.join(tmp_dir, "phone_cropped.mp4")
    _process_phone_video(phone_full, box, phone_cropped)

    if progress_callback:
        progress_callback("Compositing final video…")

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", screen_full,
            "-i", phone_cropped,
            "-filter_complex",
            f"[1:v]scale={box.width}:{box.height}[phone];[0:v][phone]overlay={box.x}:{box.y}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "copy",
            output_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # Cleanup temp files
    for f in [screen_full, phone_full, phone_cropped]:
        if os.path.exists(f):
            os.remove(f)
    os.rmdir(tmp_dir)

    return output_path
