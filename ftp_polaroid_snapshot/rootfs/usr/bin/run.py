#!/usr/bin/env python3
"""
FTP Polaroid Snapshot — Home Assistant Add-on  v1.4.0
- Polls an FTP server every N minutes (configurable, default 5)
- Downloads the newest .mov / .MOV file
- Timestamp shown on each image is taken from the MOV file's FTP modification
  time (MLSD "modify" field), not from the current wall clock
- The timestamp is burned directly onto each frame image (bottom-right corner)
  AND shown in the polaroid caption strip below
- Frame count < 4  → single polaroid from the middle frame (25% size)
- Frame count >= 4 → 2×2 polaroid matrix, one frame per quarter (25% size)
- 2 px separator between cells in matrix mode
- Configurable background colour and caption text colour
"""

import ftplib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
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
    """Parse #RRGGBB or #RGB into (R, G, B). Returns fallback on error."""
    try:
        h = hex_color.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise ValueError(f"unexpected length {len(h)}")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
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


def parse_mlsd_time(modify: str) -> datetime | None:
    """
    Parse MLSD 'modify' fact (YYYYMMDDHHmmSS or YYYYMMDDHHmmSS.sss) into a
    UTC-aware datetime. Returns None if parsing fails.
    """
    try:
        # Strip sub-seconds if present
        ts = modify.split(".")[0]
        return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def ftp_latest_mov(ftp: ftplib.FTP, remote_path: str) -> tuple[str, datetime | None] | tuple[None, None]:
    """
    Return (filename, file_datetime) for the newest .mov file in remote_path.
    file_datetime is a UTC-aware datetime parsed from MLSD, or None if
    the server doesn't support MLSD / the field is absent.
    """
    try:
        ftp.cwd(remote_path)
    except ftplib.error_perm as exc:
        log.error("Cannot CWD to %s: %s", remote_path, exc)
        return None, None

    entries = []
    try:
        for name, facts in ftp.mlsd(facts=["type", "modify"]):
            if facts.get("type") == "file" and name.lower().endswith(".mov"):
                modify_str = facts.get("modify", "")
                entries.append((modify_str, name))
    except ftplib.error_perm:
        log.warning("MLSD not supported; falling back to NLST (no file timestamp available)")
        files = [n for n in ftp.nlst() if n.lower().endswith(".mov")]
        entries = [("", f) for f in files]

    if not entries:
        log.warning("No .mov files found in %s", remote_path)
        return None, None

    entries.sort(key=lambda x: x[0], reverse=True)
    modify_str, filename = entries[0]

    file_dt = parse_mlsd_time(modify_str) if modify_str else None
    if file_dt:
        log.info("Newest file: '%s'  modified: %s (UTC)", filename,
                 file_dt.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        log.info("Newest file: '%s'  (modification time unavailable)", filename)

    return filename, file_dt


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
# Timestamp overlay helpers
# ──────────────────────────────────────────────
TIMESTAMP_FMT = "%Y-%m-%d  %H:%M:%S UTC"

# Overlay box is semi-transparent black; text is always white for contrast
OVERLAY_BG    = (0, 0, 0, 160)   # RGBA
OVERLAY_TEXT  = (255, 255, 255)


def burn_timestamp(img: Image.Image, ts_text: str) -> Image.Image:
    """
    Burn ts_text into the bottom-right corner of img.
    Uses a semi-transparent dark pill/box so the text is readable on any frame.
    Works on RGB images (converts to RGBA for compositing, returns RGB).
    """
    rgba  = img.convert("RGBA")
    w, h  = rgba.size

    # Choose font size proportional to image width
    font_size = max(10, round(w * 0.035))
    try:
        font = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                                  font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    # Measure text
    dummy_draw = ImageDraw.Draw(rgba)
    bbox = dummy_draw.textbbox((0, 0), ts_text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    pad   = max(4, round(font_size * 0.4))
    box_w = tw + pad * 2
    box_h = th + pad * 2
    margin = max(6, round(w * 0.012))

    # Position: bottom-right
    bx = w - box_w - margin
    by = h - box_h - margin

    # Draw semi-transparent background on a separate overlay layer
    overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    ov_draw.rectangle([bx, by, bx + box_w, by + box_h], fill=OVERLAY_BG)
    rgba = Image.alpha_composite(rgba, overlay)

    # Draw text
    final_draw = ImageDraw.Draw(rgba)
    final_draw.text((bx + pad, by + pad), ts_text, fill=OVERLAY_TEXT, font=font)

    return rgba.convert("RGB")


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
    caption: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """Wrap a thumb in a polaroid border with a caption strip."""
    tw, th = thumb.size
    bs = max(4, round(tw * BORDER_SIDE_RATIO))
    bb = max(12, round(th * BORDER_BOTTOM_RATIO))

    cell = Image.new("RGB", (tw + bs * 2, th + bs + bb), bg_color)
    cell.paste(thumb, (bs, bs))

    draw = ImageDraw.Draw(cell)
    font_size = max(8, round(tw * 0.038))
    try:
        font = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), caption, font=font)
    lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((cell.width - lw) // 2, th + bs + (bb - lh) // 2),
        caption, fill=text_color, font=font,
    )
    return cell


def prepare_frame(
    frame_path: str,
    ts_text: str,
    caption: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """
    Load a frame, scale to 25 %, burn the timestamp overlay onto the image,
    then wrap it in a polaroid cell.
    """
    src   = Image.open(frame_path).convert("RGB")
    thumb = scale_thumb(src)
    thumb = burn_timestamp(thumb, ts_text)          # timestamp on the photo
    return make_polaroid_cell(thumb, caption, bg_color, text_color)


def render_single(
    frame_path: str,
    ts_text: str,
    caption: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """Single-image mode: one polaroid at 25% size."""
    cell = prepare_frame(frame_path, ts_text, caption, bg_color, text_color)
    log.info("Single-image output: %dx%d", cell.width, cell.height)
    return cell


def render_matrix(
    frame_paths: list[str],
    ts_text: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """
    2×2 matrix: four polaroid cells separated by SEPARATOR px.
    Sheet size = 2*cell_w + SEPARATOR  ×  2*cell_h + SEPARATOR  (no outer padding).
    """
    cells = []
    for i, fp in enumerate(frame_paths):
        caption = f"Frame {i + 1} of 4"
        cells.append(prepare_frame(fp, ts_text, caption, bg_color, text_color))

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
    ts_text: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """Dispatch to single or matrix renderer based on frame count."""
    if len(frame_paths) == 1:
        caption = "Single frame"
        return render_single(frame_paths[0], ts_text, caption, bg_color, text_color)
    return render_matrix(frame_paths, ts_text, bg_color, text_color)


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

    bg_color   = hex_to_rgb(get_cfg(opts, "background_color", "#FFFFFF"),
                             fallback=(255, 255, 255))
    text_color = hex_to_rgb(get_cfg(opts, "text_color", "#505050"),
                             fallback=(80, 80, 80))

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
        filename, file_dt = ftp_latest_mov(ftp, remote_path)
        if not filename:
            ftp.quit()
            return

        # ── Build the timestamp string that will be burned onto every frame ──
        if file_dt:
            ts_text = file_dt.strftime(TIMESTAMP_FMT)
        else:
            # FTP server doesn't expose modification time — fall back to now
            ts_text = datetime.now(timezone.utc).strftime(TIMESTAMP_FMT) + " (approx)"
        log.info("Timestamp burned onto frames: %s", ts_text)

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
            log.info("Rendering mode: %s (%d frame(s) from %d total)",
                     mode, len(chosen), len(all_frames))

            output = build_output(chosen, ts_text, bg_color, text_color)

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
    log.info("  FTP Polaroid Snapshot  v1.4.0")
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
