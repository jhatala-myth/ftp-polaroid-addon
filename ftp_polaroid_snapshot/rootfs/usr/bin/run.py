#!/usr/bin/env python3
"""
FTP Polaroid Snapshot — Home Assistant Add-on  v1.3.0
- Polls an FTP server every N minutes (configurable, default 5)
- Downloads the newest .mov / .MOV file
- Extracts all frames with ffmpeg
- Frame count < 4  → single polaroid from the middle frame (25% size)
- Frame count >= 4 → 2×2 polaroid matrix, one frame per quarter (25% size)
- 2 px separator between cells in matrix mode
- Configurable background colour (sheet + polaroid border) and caption text colour
"""

import ftplib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ftp_polaroid")

# ──────────────────────────────────────────────
# Config  (Home Assistant injects /data/options.json)
# ──────────────────────────────────────────────
OPTIONS_PATH = "/data/options.json"


def load_options() -> dict:
    try:
        with open(OPTIONS_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("options.json not found – using defaults")
        return {}


def get_cfg(opts: dict, key: str, default):
    return opts.get(key, os.environ.get(key.upper(), default))


def hex_to_rgb(hex_color: str, fallback: tuple) -> tuple:
    """
    Parse a CSS hex colour string (#RRGGBB or #RGB) into an (R, G, B) tuple.
    Returns fallback on any parse error so a bad config value never crashes the add-on.
    """
    try:
        h = hex_color.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise ValueError(f"unexpected length {len(h)}")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception as exc:
        log.warning("Cannot parse colour '%s' (%s) – using default %s",
                    hex_color, exc, fallback)
        return fallback


# ──────────────────────────────────────────────
# FTP helpers
# ──────────────────────────────────────────────
def ftp_connect(host: str, port: int, user: str, password: str) -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=30)
    ftp.login(user, password)
    ftp.set_pasv(True)
    return ftp


def ftp_latest_mov(ftp: ftplib.FTP, remote_path: str) -> str | None:
    """Return the filename of the newest .mov/.MOV file in remote_path."""
    try:
        ftp.cwd(remote_path)
    except ftplib.error_perm as exc:
        log.error("Cannot CWD to %s: %s", remote_path, exc)
        return None

    entries = []
    try:
        for name, facts in ftp.mlsd(facts=["type", "modify"]):
            if facts.get("type") == "file" and name.lower().endswith(".mov"):
                entries.append((facts.get("modify", ""), name))
    except ftplib.error_perm:
        log.warning("MLSD not supported; falling back to NLST")
        files = [n for n in ftp.nlst() if n.lower().endswith(".mov")]
        entries = [("", f) for f in files]

    if not entries:
        log.warning("No .mov files found in %s", remote_path)
        return None

    entries.sort(key=lambda x: x[0], reverse=True)
    return entries[0][1]


def ftp_download(ftp: ftplib.FTP, filename: str, local_path: str) -> bool:
    try:
        with open(local_path, "wb") as f:
            ftp.retrbinary(f"RETR {filename}", f.write)
        log.info("Downloaded '%s' → %s", filename, local_path)
        return True
    except ftplib.error_perm as exc:
        log.error("Download failed: %s", exc)
        return False


# ──────────────────────────────────────────────
# Frame extraction & selection
# ──────────────────────────────────────────────
def extract_frames(video_path: str, frames_dir: str) -> list[str]:
    """Dump every frame as PNG via ffmpeg. Returns sorted list of paths."""
    os.makedirs(frames_dir, exist_ok=True)
    pattern = os.path.join(frames_dir, "frame_%06d.png")

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vsync", "0",
        "-q:v", "2",
        pattern,
    ]
    log.info("Extracting frames …")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("ffmpeg error:\n%s", result.stderr)
        return []

    frames = sorted(Path(frames_dir).glob("frame_*.png"))
    log.info("Extracted %d frames", len(frames))
    return [str(f) for f in frames]


def select_frames(frame_paths: list[str]) -> list[str]:
    """
    < 4 frames → [middle frame]               (single-image mode)
    >= 4 frames → first frame of each quarter (2×2 matrix mode)
    """
    total = len(frame_paths)
    if total == 0:
        return []

    if total < 4:
        mid = total // 2
        log.info("Only %d frame(s) — single-image mode (frame index %d)", total, mid)
        return [frame_paths[mid]]

    group_size = total // 4
    chosen = []
    for i in range(4):
        idx = i * group_size
        chosen.append(frame_paths[idx])
        log.info("Group %d → frame index %d (%s)", i + 1, idx,
                 os.path.basename(frame_paths[idx]))
    return chosen


# ──────────────────────────────────────────────
# Polaroid rendering
# ──────────────────────────────────────────────
SEPARATOR           = 2     # px between cells in matrix mode
BORDER_SIDE_RATIO   = 0.04  # fraction of thumb width  → left/right/top border
BORDER_BOTTOM_RATIO = 0.16  # fraction of thumb height → caption strip


def scale_thumb(src: Image.Image) -> Image.Image:
    """Return src resized to 25 % of its original pixel dimensions."""
    w, h = src.size
    tw, th = max(1, round(w * 0.25)), max(1, round(h * 0.25))
    log.info("Thumb: %dx%d → %dx%d (25%%)", w, h, tw, th)
    return src.resize((tw, th), Image.LANCZOS)


def make_polaroid_cell(
    thumb: Image.Image,
    label: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """Wrap a thumb in a polaroid border using the given colours."""
    tw, th = thumb.size
    bs = max(4, round(tw * BORDER_SIDE_RATIO))
    bb = max(12, round(th * BORDER_BOTTOM_RATIO))

    cell = Image.new("RGB", (tw + bs * 2, th + bs + bb), bg_color)
    cell.paste(thumb, (bs, bs))

    draw = ImageDraw.Draw(cell)
    font_size = max(8, round(tw * 0.045))
    try:
        font = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), label, font=font)
    lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((cell.width - lw) // 2, th + bs + (bb - lh) // 2),
        label, fill=text_color, font=font,
    )
    return cell


def render_single(
    frame_path: str,
    timestamp: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """Single-image mode: one polaroid at 25% size."""
    src   = Image.open(frame_path).convert("RGB")
    thumb = scale_thumb(src)
    cell  = make_polaroid_cell(thumb, f"Frame 1  •  {timestamp}", bg_color, text_color)
    log.info("Single-image output: %dx%d", cell.width, cell.height)
    return cell


def render_matrix(
    frame_paths: list[str],
    timestamp: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """
    2×2 matrix: four polaroid cells separated by SEPARATOR px.
    The separator strip uses bg_color so it blends with the polaroid border.
    Sheet size = 2*cell_w + SEPARATOR  ×  2*cell_h + SEPARATOR  (no outer padding).
    """
    cells = []
    for i, fp in enumerate(frame_paths):
        src   = Image.open(fp).convert("RGB")
        thumb = scale_thumb(src)
        label = f"Frame {i + 1}  •  {timestamp}"
        cells.append(make_polaroid_cell(thumb, label, bg_color, text_color))

    cw, ch  = cells[0].width, cells[0].height
    sheet_w = cw * 2 + SEPARATOR
    sheet_h = ch * 2 + SEPARATOR
    log.info("Matrix output: %dx%d  (cell %dx%d, sep %dpx)",
             sheet_w, sheet_h, cw, ch, SEPARATOR)

    sheet = Image.new("RGB", (sheet_w, sheet_h), bg_color)
    for cell, (x, y) in zip(cells, [
        (0,              0),
        (cw + SEPARATOR, 0),
        (0,              ch + SEPARATOR),
        (cw + SEPARATOR, ch + SEPARATOR),
    ]):
        sheet.paste(cell, (x, y))

    return sheet


def build_output(
    frame_paths: list[str],
    timestamp: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """Dispatch to single or matrix renderer based on frame count."""
    if len(frame_paths) == 1:
        return render_single(frame_paths[0], timestamp, bg_color, text_color)
    return render_matrix(frame_paths, timestamp, bg_color, text_color)


# ──────────────────────────────────────────────
# Main processing run
# ──────────────────────────────────────────────
def process(opts: dict):
    host        = get_cfg(opts, "ftp_host",        "")
    port        = int(get_cfg(opts, "ftp_port",    21))
    user        = get_cfg(opts, "ftp_user",        "anonymous")
    password    = get_cfg(opts, "ftp_password",    "")
    remote_path = get_cfg(opts, "ftp_path",        "/")
    output_dir  = get_cfg(opts, "output_dir",      "/media/polaroid")

    bg_color   = hex_to_rgb(
        get_cfg(opts, "background_color", "#FFFFFF"),
        fallback=(255, 255, 255),
    )
    text_color = hex_to_rgb(
        get_cfg(opts, "text_color", "#505050"),
        fallback=(80, 80, 80),
    )

    log.info("Colours — background: #%02X%02X%02X  text: #%02X%02X%02X",
             *bg_color, *text_color)

    if not host:
        log.error("ftp_host is not configured – skipping run")
        return

    os.makedirs(output_dir, exist_ok=True)

    log.info("Connecting to FTP %s:%d as '%s' …", host, port, user)
    try:
        ftp = ftp_connect(host, port, user, password)
    except Exception as exc:
        log.error("FTP connection failed: %s", exc)
        return

    try:
        filename = ftp_latest_mov(ftp, remote_path)
        if not filename:
            ftp.quit()
            return

        with tempfile.TemporaryDirectory(prefix="ftp_polaroid_") as tmp:
            local_video = os.path.join(tmp, filename)
            if not ftp_download(ftp, filename, local_video):
                ftp.quit()
                return
            ftp.quit()

            frames_dir = os.path.join(tmp, "frames")
            all_frames = extract_frames(local_video, frames_dir)
            if not all_frames:
                log.error("No frames extracted – aborting")
                return

            chosen = select_frames(all_frames)
            if not chosen:
                log.error("Frame selection returned nothing – aborting")
                return

            mode = "single" if len(chosen) == 1 else "2×2 matrix"
            log.info("Rendering mode: %s (%d frame(s) selected from %d total)",
                     mode, len(chosen), len(all_frames))

            ts     = datetime.now().strftime("%Y-%m-%d %H:%M")
            output = build_output(chosen, ts, bg_color, text_color)

            safe_name = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path  = os.path.join(output_dir, f"polaroid_{safe_name}.jpg")
            output.save(out_path, "JPEG", quality=92)
            log.info("✔  Saved → %s", out_path)

            latest = os.path.join(output_dir, "latest.jpg")
            shutil.copy2(out_path, latest)
            log.info("✔  latest.jpg updated")

    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        try:
            ftp.quit()
        except Exception:
            pass


# ──────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────
def main():
    opts     = load_options()
    interval = int(get_cfg(opts, "interval_minutes", 5)) * 60

    log.info("════════════════════════════════════════")
    log.info("  FTP Polaroid Snapshot  v1.3.0")
    log.info("  Check interval : %d min (%d s)", interval // 60, interval)
    log.info("════════════════════════════════════════")

    while True:
        try:
            process(opts)
        except Exception as exc:
            log.exception("process() raised: %s", exc)
        log.info("Next check in %d min – sleeping …", interval // 60)
        time.sleep(interval)


if __name__ == "__main__":
    main()
