#!/usr/bin/env python3
"""
FTP Polaroid Snapshot — Home Assistant Add-on
- Polls an FTP server every N minutes (default 5)
- Downloads the newest .mov / .MOV file
- Extracts all frames with ffmpeg
- Picks 4 evenly-spaced frames (index 0 of each quarter)
- Renders a 2×2 polaroid contact sheet and saves it to /media/polaroid/
"""

import ftplib
import json
import logging
import os
import shutil
import subprocess
import sys
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
# Config  (Home Assistant passes options.json)
# ──────────────────────────────────────────────
OPTIONS_PATH = "/data/options.json"

def load_options() -> dict:
    try:
        with open(OPTIONS_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("options.json not found – using environment / defaults")
        return {}

def get_cfg(opts: dict, key: str, default):
    return opts.get(key, os.environ.get(key.upper(), default))


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
        # MLSD gives us timestamps; fall back to NLST if unavailable
        for name, facts in ftp.mlsd(facts=["type", "modify"]):
            if facts.get("type") == "file" and name.lower().endswith(".mov"):
                entries.append((facts.get("modify", ""), name))
    except ftplib.error_perm:
        # Server doesn't support MLSD – use NLST and pick alphabetically last
        log.warning("MLSD not supported; falling back to NLST")
        files = [n for n in ftp.nlst() if n.lower().endswith(".mov")]
        entries = [("", f) for f in files]

    if not entries:
        log.warning("No .mov files found in %s", remote_path)
        return None

    entries.sort(key=lambda x: x[0], reverse=True)
    return entries[0][1]


def ftp_download(ftp: ftplib.FTP, filename: str, local_path: str) -> bool:
    """Download filename from current FTP directory to local_path."""
    try:
        with open(local_path, "wb") as f:
            ftp.retrbinary(f"RETR {filename}", f.write)
        log.info("Downloaded '%s' → %s", filename, local_path)
        return True
    except ftplib.error_perm as exc:
        log.error("Download failed: %s", exc)
        return False


# ──────────────────────────────────────────────
# Frame extraction
# ──────────────────────────────────────────────
def extract_frames(video_path: str, frames_dir: str) -> list[str]:
    """
    Use ffmpeg to dump every frame as a PNG.
    Returns sorted list of frame file paths.
    """
    os.makedirs(frames_dir, exist_ok=True)
    pattern = os.path.join(frames_dir, "frame_%06d.png")

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vsync", "0",          # keep every frame exactly once
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


def pick_4_frames(frame_paths: list[str]) -> list[str]:
    """
    Split frames into 4 equal groups.
    Return the first frame of each group.
    """
    total = len(frame_paths)
    if total == 0:
        return []
    if total < 4:
        # Pad by repeating last frame
        frame_paths = (frame_paths * 4)[:4]
        total = 4

    group_size = total // 4
    chosen = []
    for i in range(4):
        idx = i * group_size          # first frame of each quarter
        chosen.append(frame_paths[idx])
        log.info("Group %d → frame index %d (%s)", i + 1, idx,
                 os.path.basename(frame_paths[idx]))
    return chosen


# ──────────────────────────────────────────────
# Polaroid rendering
# ──────────────────────────────────────────────
POLAROID_BG         = (255, 255, 255)   # polaroid white
SHEET_BG            = (235, 228, 215)   # warm aged paper
CAPTION_COLOR       = (80, 80, 80)
SEPARATOR           = 2                 # px gap between cells
BORDER_SIDE_RATIO   = 0.04             # ~4 % of thumb width each side
BORDER_BOTTOM_RATIO = 0.16             # wider bottom caption strip


def make_polaroid_cell(thumb: Image.Image, label: str) -> Image.Image:
    """Wrap a 25%-scaled frame in a polaroid border."""
    tw, th = thumb.size

    border_side   = max(4, round(tw * BORDER_SIDE_RATIO))
    border_bottom = max(12, round(th * BORDER_BOTTOM_RATIO))

    cell_w = tw + border_side * 2
    cell_h = th + border_side + border_bottom

    cell = Image.new("RGB", (cell_w, cell_h), POLAROID_BG)
    cell.paste(thumb, (border_side, border_side))

    draw = ImageDraw.Draw(cell)

    font_size = max(8, round(tw * 0.045))
    try:
        font = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), label, font=font)
    lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (cell_w - lw) // 2
    ty = th + border_side + (border_bottom - lh) // 2
    draw.text((tx, ty), label, fill=CAPTION_COLOR, font=font)

    return cell


def build_polaroid_sheet(frame_paths: list[str], timestamp: str) -> Image.Image:
    """
    Arrange 4 polaroid cells in a 2x2 grid.

    Each frame is resized to 25% of its original pixel dimensions.
    Cells are separated by SEPARATOR (2 px) lines on SHEET_BG colour.
    Sheet size = 2*cell_w + SEPARATOR  x  2*cell_h + SEPARATOR  (no outer padding).
    """
    cells = []
    for i, fp in enumerate(frame_paths):
        src = Image.open(fp).convert("RGB")
        orig_w, orig_h = src.size

        # ── 25% of original resolution ──
        thumb_w = max(1, round(orig_w * 0.25))
        thumb_h = max(1, round(orig_h * 0.25))
        thumb   = src.resize((thumb_w, thumb_h), Image.LANCZOS)
        log.info("Cell %d: original %dx%d → thumb %dx%d (25%%)",
                 i + 1, orig_w, orig_h, thumb_w, thumb_h)

        label = f"Frame {i + 1}  •  {timestamp}"
        cells.append(make_polaroid_cell(thumb, label))

    cell_w = cells[0].width
    cell_h = cells[0].height

    # Sheet is exactly 2 cells + 1 separator gap in each direction
    sheet_w = cell_w * 2 + SEPARATOR
    sheet_h = cell_h * 2 + SEPARATOR

    log.info("Sheet size: %dx%d  (cell %dx%d, sep %dpx)",
             sheet_w, sheet_h, cell_w, cell_h, SEPARATOR)

    sheet = Image.new("RGB", (sheet_w, sheet_h), SHEET_BG)

    positions = [
        (0,                  0),
        (cell_w + SEPARATOR, 0),
        (0,                  cell_h + SEPARATOR),
        (cell_w + SEPARATOR, cell_h + SEPARATOR),
    ]

    for cell, (x, y) in zip(cells, positions):
        sheet.paste(cell, (x, y))

    return sheet

# ──────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────
def process(opts: dict):
    host         = get_cfg(opts, "ftp_host",        "")
    port         = int(get_cfg(opts, "ftp_port",    21))
    user         = get_cfg(opts, "ftp_user",        "anonymous")
    password     = get_cfg(opts, "ftp_password",    "")
    remote_path  = get_cfg(opts, "ftp_path",        "/")
    output_dir   = get_cfg(opts, "output_dir",      "/media/polaroid")

    if not host:
        log.error("ftp_host is not configured – skipping run")
        return

    os.makedirs(output_dir, exist_ok=True)

    # ── connect & find newest file ──
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

        # ── download to temp dir ──
        with tempfile.TemporaryDirectory(prefix="ftp_polaroid_") as tmp:
            local_video = os.path.join(tmp, filename)
            if not ftp_download(ftp, filename, local_video):
                ftp.quit()
                return
            ftp.quit()

            # ── extract all frames ──
            frames_dir = os.path.join(tmp, "frames")
            all_frames = extract_frames(local_video, frames_dir)
            if not all_frames:
                log.error("No frames extracted – aborting")
                return

            # ── pick 4 representative frames ──
            chosen = pick_4_frames(all_frames)
            if len(chosen) < 4:
                log.error("Could not select 4 frames")
                return

            # ── render polaroid sheet ──
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            sheet = build_polaroid_sheet(chosen, ts)

            safe_name = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path  = os.path.join(output_dir, f"polaroid_{safe_name}.jpg")
            sheet.save(out_path, "JPEG", quality=92)
            log.info("✔  Polaroid saved → %s", out_path)

            # Also overwrite a fixed "latest.jpg" for easy dashboard use
            latest = os.path.join(output_dir, "latest.jpg")
            shutil.copy2(out_path, latest)
            log.info("✔  latest.jpg updated")

    except Exception as exc:
        log.exception("Unexpected error during processing: %s", exc)
        try:
            ftp.quit()
        except Exception:
            pass


def main():
    opts = load_options()
    interval = int(get_cfg(opts, "interval_minutes", 5)) * 60

    log.info("═══════════════════════════════════════")
    log.info("  FTP Polaroid Snapshot add-on starting")
    log.info("  Interval : %d s", interval)
    log.info("═══════════════════════════════════════")

    while True:
        try:
            process(opts)
        except Exception as exc:
            log.exception("process() raised: %s", exc)
        log.info("Sleeping %d s until next run …", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
