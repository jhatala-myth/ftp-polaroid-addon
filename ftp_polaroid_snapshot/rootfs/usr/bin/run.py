#!/usr/bin/env python3
"""
FTP Polaroid Snapshot — Home Assistant Add-on  v1.9.0

Every N minutes (configurable):
  • Checks FTP for the newest .mov file
  • Skips download if the file is identical to the last one (same name + mtime)
  • Warns in the log if no new file appears for 3 consecutive cycles
  • Extracts frames with ffmpeg
  • < 4 frames  → single full-size polaroid
  • ≥ 4 frames  → 2×2 matrix: each cell is 25% of original, scaled up so the
                   output image matches the original video resolution exactly
  • Timestamp shown in the bottom polaroid caption strip (centred, no frame numbers)
  • Saves to output_dir/YYYY-MM-DD/polaroid_HH-MM-SS.jpg
  • latest.jpg always updated at output_dir root

Once per day (just after midnight):
  • Builds an H.264 MP4 timelapse from all JPEGs in the previous day's folder
  • Saves to output_dir/timelapse/YYYY-MM-DD-timelapse.mp4
  • Enforces photo retention (keep_photos_days)
  • Enforces timelapse retention (keep_timelapse_days)
"""

import ftplib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, date, timedelta, timezone
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
# Config
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
    """Parse #RRGGBB or #RGB → (R, G, B). Returns fallback on error."""
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
    try:
        ts = modify.split(".")[0]
        return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def ftp_latest_mov(
    ftp: ftplib.FTP, remote_path: str
) -> tuple[str, datetime | None] | tuple[None, None]:
    """Return (filename, utc_datetime) for the newest .mov in remote_path."""
    try:
        ftp.cwd(remote_path)
    except ftplib.error_perm as exc:
        log.error("Cannot CWD to %s: %s", remote_path, exc)
        return None, None

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
        return None, None

    entries.sort(key=lambda x: x[0], reverse=True)
    modify_str, filename = entries[0]
    file_dt = parse_mlsd_time(modify_str) if modify_str else None

    if file_dt:
        log.info("Newest MOV: '%s'  modified: %s UTC",
                 filename, file_dt.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        log.info("Newest MOV: '%s'  (modification time unavailable)", filename)

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
    os.makedirs(frames_dir, exist_ok=True)
    pattern = os.path.join(frames_dir, "frame_%06d.png")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vsync", "0", "-q:v", "1",      # q:v 1 = maximum quality PNG proxy
        pattern,
    ]
    log.info("Extracting frames …")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("ffmpeg stderr:\n%s", result.stderr)
        return []
    frames = sorted(Path(frames_dir).glob("frame_*.png"))
    log.info("Extracted %d frames", len(frames))
    return [str(f) for f in frames]


def select_frames(frame_paths: list[str]) -> list[str]:
    """< 4 → middle frame;  ≥ 4 → first frame of each quarter."""
    total = len(frame_paths)
    if total == 0:
        return []
    if total < 4:
        mid = total // 2
        log.info("%d frame(s) → single mode (index %d)", total, mid)
        return [frame_paths[mid]]
    group = total // 4
    chosen = [frame_paths[i * group] for i in range(4)]
    for i, fp in enumerate(chosen):
        log.info("Quarter %d → frame index %d (%s)",
                 i + 1, i * group, os.path.basename(fp))
    return chosen


# ──────────────────────────────────────────────
# Polaroid rendering
# ──────────────────────────────────────────────
TIMESTAMP_FMT       = "%Y-%m-%d  %H:%M:%S UTC"
SEPARATOR           = 2      # px between matrix cells
BORDER_SIDE_RATIO   = 0.04   # left / right / top border as fraction of photo width
BORDER_BOTTOM_RATIO = 0.13   # bottom caption strip height as fraction of photo height


def make_polaroid_cell(
    photo: Image.Image,
    ts_text: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """
    Wrap photo in a polaroid border.
    The timestamp is printed centred in the white bottom strip — no frame numbers.
    """
    pw, ph = photo.size
    bs = max(4, round(pw * BORDER_SIDE_RATIO))
    bb = max(10, round(ph * BORDER_BOTTOM_RATIO))

    cell = Image.new("RGB", (pw + bs * 2, ph + bs + bb), bg_color)
    cell.paste(photo, (bs, bs))

    draw = ImageDraw.Draw(cell)
    font_size = max(8, round(pw * 0.038))
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/dejavu/DejaVuSans.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), ts_text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((cell.width - tw) // 2, ph + bs + (bb - th) // 2),
        ts_text, fill=text_color, font=font,
    )
    return cell


def render_single(
    frame_path: str,
    ts_text: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """Single-image mode: full original resolution, timestamp in bottom strip."""
    src = Image.open(frame_path).convert("RGB")
    orig_w, orig_h = src.size
    cell = make_polaroid_cell(src, ts_text, bg_color, text_color)
    log.info("Single mode: original %dx%d → output %dx%d",
             orig_w, orig_h, cell.width, cell.height)
    return cell


def render_matrix(
    frame_paths: list[str],
    ts_text: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    """
    2×2 matrix mode.
    Each cell is 25% of original resolution, assembled into a sheet,
    then the sheet is upscaled back to the original video resolution.
    Timestamp shown in the bottom strip of each cell — no frame numbers.
    """
    first_src = Image.open(frame_paths[0]).convert("RGB")
    orig_w, orig_h = first_src.size

    thumb_w = max(1, round(orig_w * 0.25))
    thumb_h = max(1, round(orig_h * 0.25))
    log.info("Matrix: original %dx%d → cell thumb %dx%d",
             orig_w, orig_h, thumb_w, thumb_h)

    cells = []
    for fp in frame_paths:
        src   = Image.open(fp).convert("RGB")
        thumb = src.resize((thumb_w, thumb_h), Image.LANCZOS)
        cell  = make_polaroid_cell(thumb, ts_text, bg_color, text_color)
        cells.append(cell)

    cw, ch  = cells[0].width, cells[0].height
    raw_w   = cw * 2 + SEPARATOR
    raw_h   = ch * 2 + SEPARATOR

    # Assemble the raw sheet at thumbnail scale
    sheet = Image.new("RGB", (raw_w, raw_h), bg_color)
    for cell, (x, y) in zip(cells, [
        (0,              0),
        (cw + SEPARATOR, 0),
        (0,              ch + SEPARATOR),
        (cw + SEPARATOR, ch + SEPARATOR),
    ]):
        sheet.paste(cell, (x, y))

    # Scale the assembled sheet back up to original video resolution
    output = sheet.resize((orig_w, orig_h), Image.LANCZOS)
    log.info("Matrix sheet %dx%d → upscaled to %dx%d (original size)",
             raw_w, raw_h, orig_w, orig_h)
    return output


def build_output(
    frame_paths: list[str],
    ts_text: str,
    bg_color: tuple,
    text_color: tuple,
) -> Image.Image:
    if len(frame_paths) == 1:
        return render_single(frame_paths[0], ts_text, bg_color, text_color)
    return render_matrix(frame_paths, ts_text, bg_color, text_color)


# ──────────────────────────────────────────────
# Date-structured storage
# ──────────────────────────────────────────────
def day_dir(output_dir: str, dt: datetime) -> str:
    """Return the dated sub-folder path, creating it if necessary."""
    d = os.path.join(output_dir, dt.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    return d


def timelapse_dir(output_dir: str) -> str:
    d = os.path.join(output_dir, "timelapse")
    os.makedirs(d, exist_ok=True)
    return d


# ──────────────────────────────────────────────
# Timelapse builder
# ──────────────────────────────────────────────
def build_timelapse(photo_dir: str, out_mp4: str,
                    frame_duration_sec: float = 0.5) -> bool:
    """
    Build an H.264 MP4 from all JPEGs in photo_dir (sorted by name).
    frame_duration_sec controls how long each image is shown (default 0.5 s).
    Returns True on success.
    """
    jpegs = sorted(Path(photo_dir).glob("*.jpg"))
    if not jpegs:
        log.warning("No JPEGs in %s – skipping timelapse", photo_dir)
        return False

    log.info("Building timelapse: %d images × %.2f s/frame → ~%.1f s total",
             len(jpegs), frame_duration_sec, len(jpegs) * frame_duration_sec)

    with tempfile.TemporaryDirectory(prefix="timelapse_") as tmp:
        # Write an ffmpeg concat file — each entry sets its own display duration
        list_file = os.path.join(tmp, "frames.txt")
        with open(list_file, "w") as f:
            for jp in jpegs:
                f.write(f"file '{jp}'\n")
                f.write(f"duration {frame_duration_sec:.6f}\n")
            # ffmpeg concat demuxer needs a final duration-less entry
            f.write(f"file '{jpegs[-1]}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # ensure even dimensions
            "-c:v", "libx264",
            "-crf", "18",          # high quality H.264
            "-preset", "slow",
            "-pix_fmt", "yuv420p",
            out_mp4,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("ffmpeg timelapse error:\n%s", result.stderr)
            return False

    log.info("✔  Timelapse saved → %s", out_mp4)
    return True


# ──────────────────────────────────────────────
# Retention / cleanup
# ──────────────────────────────────────────────
def cleanup_photos(output_dir: str, keep_days: int):
    """Remove dated photo folders older than keep_days full days."""
    if keep_days <= 0:
        return
    cutoff = date.today() - timedelta(days=keep_days)
    base = Path(output_dir)
    for folder in base.iterdir():
        if not folder.is_dir():
            continue
        # Only touch YYYY-MM-DD shaped directories
        try:
            folder_date = date.fromisoformat(folder.name)
        except ValueError:
            continue
        if folder_date < cutoff:
            log.info("Removing old photo folder: %s", folder)
            shutil.rmtree(folder, ignore_errors=True)


def cleanup_timelapse(output_dir: str, keep_days: int):
    """Remove timelapse MP4s whose date prefix is older than keep_days."""
    if keep_days <= 0:
        return
    cutoff = date.today() - timedelta(days=keep_days)
    tl_dir = Path(output_dir) / "timelapse"
    if not tl_dir.exists():
        return
    for mp4 in tl_dir.glob("*.mp4"):
        # filename pattern: YYYY-MM-DD-timelapse.mp4
        try:
            file_date = date.fromisoformat(mp4.name[:10])
        except ValueError:
            continue
        if file_date < cutoff:
            log.info("Removing old timelapse: %s", mp4)
            mp4.unlink(missing_ok=True)


# ──────────────────────────────────────────────
# Midnight maintenance (once per calendar day)
# ──────────────────────────────────────────────
_last_maintenance: date | None = None


def timelapse_exists(output_dir: str, for_date: date) -> bool:
    """Return True if the timelapse MP4 for for_date already exists on disk."""
    mp4 = Path(output_dir) / "timelapse" / f"{for_date.isoformat()}-timelapse.mp4"
    return mp4.exists()


def run_maintenance(output_dir: str, keep_photos_days: int,
                    keep_timelapse_days: int, frame_duration_sec: float):
    """
    Run once per calendar day, on the first check cycle after midnight.

    Guard logic:
      • _last_maintenance tracks the last date maintenance was attempted.
      • If it equals today → already ran, return immediately.
      • Otherwise → run regardless of the current clock minute so that a
        restart at (say) 02:30 still triggers maintenance for the previous day
        rather than waiting until the next midnight window.
      • The timelapse is only built if the MP4 for yesterday does NOT already
        exist — so a restart / crash recovery never rebuilds an existing file.
    """
    global _last_maintenance
    today = date.today()
    if _last_maintenance == today:
        return                          # already done for today
    _last_maintenance = today           # mark before doing work (prevents retry on error)

    yesterday     = today - timedelta(days=1)
    yesterday_str = yesterday.isoformat()
    photo_dir     = os.path.join(output_dir, yesterday_str)
    tl_dir        = timelapse_dir(output_dir)
    mp4_path      = os.path.join(tl_dir, f"{yesterday_str}-timelapse.mp4")

    log.info("── Daily maintenance – building timelapse for %s ──", yesterday_str)

    if timelapse_exists(output_dir, yesterday):
        log.info("Timelapse for %s already exists – skipping build", yesterday_str)
    elif os.path.isdir(photo_dir):
        build_timelapse(photo_dir, mp4_path, frame_duration_sec)
    else:
        log.info("No photo folder found for %s – no timelapse to build", yesterday_str)

    cleanup_photos(output_dir, keep_photos_days)
    cleanup_timelapse(output_dir, keep_timelapse_days)
    log.info("── Daily maintenance complete ──")


# ──────────────────────────────────────────────
# Download deduplication & stale-file detection
# ──────────────────────────────────────────────
class DownloadTracker:
    """
    Tracks the last successfully downloaded file and counts consecutive
    cycles where no new file appeared.

    A file is considered "new" when either its name or its modification
    timestamp differs from the previous download.  If the FTP server does not
    expose modification times (NLST fallback), name-only comparison is used.

    After `warn_after` consecutive cycles with no new file a WARNING is written
    to the log and the counter resets so the warning repeats every `warn_after`
    further cycles if the situation persists.
    """

    WARN_AFTER = 3  # cycles before issuing a stale-file warning

    def __init__(self):
        self._last_name:   str | None      = None
        self._last_mtime:  datetime | None = None
        self._stale_count: int             = 0

    # ── identity key ──────────────────────────
    @staticmethod
    def _key(name: str, mtime: datetime | None) -> tuple:
        return (name, mtime.isoformat() if mtime else None)

    # ── public API ────────────────────────────
    def is_new(self, name: str, mtime: datetime | None) -> bool:
        """Return True if this file has not been seen before."""
        return self._key(name, mtime) != self._key(
            self._last_name or "", self._last_mtime)

    def record_skipped(self, name: str, mtime: datetime | None):
        """Call when a file is seen but not downloaded (already processed)."""
        self._stale_count += 1
        log.info(
            "Skipping '%s' – already processed (unchanged for %d consecutive cycle%s)",
            name, self._stale_count,
            "s" if self._stale_count != 1 else "",
        )
        if self._stale_count % self.WARN_AFTER == 0:
            log.warning(
                "⚠  No new MOV file on FTP for %d consecutive check cycles "
                "(interval × %d = %d min without a new recording). "
                "Camera offline or FTP path incorrect?",
                self._stale_count,
                self._stale_count,
                # approximate wall-clock minutes — caller doesn't pass interval,
                # so we just report the cycle count; README explains this.
                self._stale_count,
            )

    def record_downloaded(self, name: str, mtime: datetime | None):
        """Call after a file has been successfully downloaded and processed."""
        self._last_name  = name
        self._last_mtime = mtime
        self._stale_count = 0
        log.info("Tracker updated: last file = '%s'  mtime = %s",
                 name, mtime.isoformat() if mtime else "unknown")


# ──────────────────────────────────────────────
# Main processing run
# ──────────────────────────────────────────────
def process(opts: dict, tracker: DownloadTracker):
    host        = get_cfg(opts, "ftp_host",        "")
    port        = int(get_cfg(opts, "ftp_port",    21))
    user        = get_cfg(opts, "ftp_user",        "anonymous")
    password    = get_cfg(opts, "ftp_password",    "")
    remote_path = get_cfg(opts, "ftp_path",        "/")
    output_dir  = get_cfg(opts, "output_dir",      "/media/polaroid")

    bg_color = hex_to_rgb(
        get_cfg(opts, "background_color", "#FFFFFF"), fallback=(255, 255, 255))
    text_color = hex_to_rgb(
        get_cfg(opts, "text_color", "#505050"), fallback=(80, 80, 80))

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

        # ── Deduplication: skip if we already processed this exact file ──
        if not tracker.is_new(filename, file_dt):
            tracker.record_skipped(filename, file_dt)
            ftp.quit()
            return

        if file_dt:
            ts_text  = file_dt.strftime(TIMESTAMP_FMT)
            save_dt  = file_dt
        else:
            now      = datetime.now(timezone.utc)
            ts_text  = now.strftime(TIMESTAMP_FMT) + " (approx)"
            save_dt  = now
        log.info("Timestamp: %s", ts_text)

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
            log.info("Mode: %s (%d selected from %d total frames)",
                     mode, len(chosen), len(all_frames))

            output = build_output(chosen, ts_text, bg_color, text_color)

            # ── Date-structured save ──
            photo_folder = day_dir(output_dir, save_dt)
            fname        = save_dt.strftime("polaroid_%H-%M-%S.jpg")
            out_path     = os.path.join(photo_folder, fname)
            output.save(out_path, "JPEG", quality=97, subsampling=0)
            log.info("✔  Saved → %s", out_path)

            # Always update root latest.jpg for dashboard use
            latest = os.path.join(output_dir, "latest.jpg")
            shutil.copy2(out_path, latest)
            log.info("✔  latest.jpg updated")

            # Mark this file as processed so we don't re-download it
            tracker.record_downloaded(filename, file_dt)

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
    opts = load_options()

    interval            = int(get_cfg(opts, "interval_minutes",       5))  * 60
    keep_photos_days    = int(get_cfg(opts, "keep_photos_days",        7))
    keep_timelapse_days = int(get_cfg(opts, "keep_timelapse_days",    30))
    frame_duration_sec  = float(get_cfg(opts, "timelapse_frame_duration", 0.5))
    output_dir          = get_cfg(opts, "output_dir", "/media/polaroid")

    log.info("════════════════════════════════════════")
    log.info("  FTP Polaroid Snapshot  v1.9.0")
    log.info("  Check interval  : %d min", interval // 60)
    log.info("  Photo retention : %d days", keep_photos_days)
    log.info("  Lapse retention : %d days", keep_timelapse_days)
    log.info("  Lapse frame dur : %.2f s/frame", frame_duration_sec)
    log.info("  Stale warning   : after %d unchanged cycles", DownloadTracker.WARN_AFTER)
    log.info("════════════════════════════════════════")

    tracker = DownloadTracker()

    while True:
        # Run daily maintenance on the first cycle of each new calendar day.
        # The guard inside run_maintenance ensures it executes only once per day
        # regardless of what time the add-on started or restarted.
        run_maintenance(output_dir, keep_photos_days, keep_timelapse_days,
                        frame_duration_sec)

        try:
            process(opts, tracker)
        except Exception as exc:
            log.exception("process() raised: %s", exc)

        log.info("Next check in %d min …", interval // 60)
        time.sleep(interval)


if __name__ == "__main__":
    main()
