# FTP Polaroid Snapshot — Home Assistant Add-on

**v1.5.0**

Polls an FTP server on a configurable schedule, downloads the newest `.mov`
file, renders a polaroid-style snapshot with a burned-in MOV timestamp, and
organises everything into date-structured folders. Once per day it stitches
all that day's snapshots into an H.264 MP4 timelapse. Separate retention
periods control how long photos and timelapse files are kept.

---

## How it works

### Every N minutes
1. Connect to FTP and find the newest `.mov` file
2. Download the file to a temporary directory
3. Extract all frames with `ffmpeg`
4. Select frames based on count:
   - **< 4 frames** → use the middle frame at full original resolution
   - **≥ 4 frames** → use the first frame of each quarter, each scaled to 25%,
     assembled into a 2×2 matrix, then the whole sheet is upscaled back to the
     **exact original video resolution** — no size information is lost
5. Burn the MOV file's modification timestamp (from FTP MLSD) into the
   **bottom-right corner** of each frame photo — no frame numbers or captions
6. Wrap each photo in a polaroid-style border (background colour configurable)
7. Save to `output_dir/YYYY-MM-DD/polaroid_HH-MM-SS.jpg`
8. Overwrite `output_dir/latest.jpg` for dashboard use

### Once per day (00:00–00:05)
1. Build an H.264 timelapse MP4 from all JPEGs in yesterday's folder
2. Save to `output_dir/timelapse/YYYY-MM-DD-timelapse.mp4`
3. Delete photo folders older than `keep_photos_days` full days
4. Delete timelapse files older than `keep_timelapse_days` full days

---

## Output structure

```
/media/polaroid/
├── latest.jpg                          ← always the most recent snapshot
├── 2024-03-15/
│   ├── polaroid_08-00-02.jpg
│   ├── polaroid_08-05-01.jpg
│   └── …
├── 2024-03-16/
│   └── …
└── timelapse/
    ├── 2024-03-15-timelapse.mp4
    └── 2024-03-16-timelapse.mp4
```

Photo folders are named `YYYY-MM-DD` (from the MOV file timestamp).
Timelapse files are named `YYYY-MM-DD-timelapse.mp4` (yesterday's date).

---

## Image quality

- JPEG saved at **quality 97, no chroma subsampling** — visually lossless
- Single mode: photo is at **100% of original video resolution**
- Matrix mode: each of 4 cells is 25% → assembled → **upscaled back to 100%**
  so the output file always matches the source video dimensions exactly
- Timestamp overlay uses a **semi-transparent dark box** with white bold text,
  rendered proportionally to the image width — legible at any resolution

---

## Timestamp source

The timestamp burned onto each image is the **MOV file's modification time**
from the FTP server's `MLSD` response (`modify` fact), formatted as:

```
2024-03-15  09:42:17 UTC
```

If the FTP server does not support `MLSD`, the current UTC time is used as a
fallback and the label is suffixed with `(approx)`.

---

## How frame selection works

```
total_frames = 120  →  group_size = 30

Matrix mode (≥ 4 frames):
  Quarter 1 → frame index   0
  Quarter 2 → frame index  30
  Quarter 3 → frame index  60
  Quarter 4 → frame index  90

Single mode (< 4 frames, e.g. total = 3):
  Middle frame → index 1
```

---

## Repository structure

```
your-repo/
├── repository.yaml
└── ftp_polaroid_snapshot/
    ├── config.yaml
    ├── Dockerfile
    ├── README.md
    └── rootfs/usr/bin/run.py
```

---

## Installation

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Paste your GitHub repository URL → **Add**
3. Find **FTP Polaroid Snapshot** → **Install**

### Local install

Copy `ftp_polaroid_snapshot/` to `/config/addons/` then install from
**Settings → Add-ons → Local add-ons**.

---

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `ftp_host` | string | `""` | FTP server hostname or IP |
| `ftp_port` | int | `21` | FTP port |
| `ftp_user` | string | `"anonymous"` | FTP username |
| `ftp_password` | string | `""` | FTP password |
| `ftp_path` | string | `"/"` | Remote directory to scan |
| `output_dir` | string | `"/media/polaroid"` | Root output directory |
| `interval_minutes` | int (1–1440) | `5` | FTP poll interval |
| `background_color` | hex | `"#FFFFFF"` | Polaroid border colour |
| `text_color` | hex | `"#505050"` | *(reserved — not used in v1.5.0)* |
| `keep_photos_days` | int (1–365) | `7` | Days to retain photo folders |
| `keep_timelapse_days` | int (1–730) | `30` | Days to retain timelapse MP4s |

> `text_color` is retained in the schema for forward compatibility but the
> polaroid border no longer has a text caption strip — timestamp is burned
> directly onto the photo.

### Example configuration

```yaml
ftp_host: "192.168.1.50"
ftp_port: 21
ftp_user: "camera"
ftp_password: "secret"
ftp_path: "/recordings"
output_dir: "/media/polaroid"
interval_minutes: 5
background_color: "#FFFFFF"
keep_photos_days: 7
keep_timelapse_days: 30
```

### Background colour presets

| Style | `background_color` |
|---|---|
| Classic white (default) | `#FFFFFF` |
| Aged paper | `#EBE4D7` |
| Dark / night | `#1A1A1A` |
| Slate blue | `#2D3A4A` |
| Soft green | `#D4E8D0` |

---

## Home Assistant dashboard

### Latest snapshot

```yaml
type: picture
image: /media/polaroid/latest.jpg
refresh_interval: 300
```

### Timelapse (Media Browser)

Timelapse MP4s appear automatically in **Media → Local Media → polaroid →
timelapse** in the HA Media Browser.

---

## Timelapse details

- Source: all `polaroid_*.jpg` files in yesterday's date folder, sorted by name
- Codec: **H.264 (libx264)**, CRF 18 (high quality), `slow` preset
- Pixel format: `yuv420p` (maximum compatibility)
- FPS: 10 frames per second (one snapshot every 0.1 s of playback)
- Dimensions forced to even numbers for H.264 compatibility
- Built once per day in the 00:00–00:05 window

Example: 288 snapshots (5-minute interval over 24 h) → ~29 seconds of video.

---

## Retention behaviour

| What | Controlled by | What gets deleted |
|---|---|---|
| Photo folders (`YYYY-MM-DD/`) | `keep_photos_days` | Entire dated folder |
| Timelapse files (`*.mp4`) | `keep_timelapse_days` | Individual MP4 files |

Cutoff is calculated as `today - keep_X_days`. Folders/files with a date
**before** the cutoff are removed. The `timelapse/` folder itself is never
removed. Set either value to `0` to disable that retention check (not
recommended for long-running installations).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Add-on not in store | Wrong repo structure | `repository.yaml` must be at repo root; add-on in a subfolder |
| `FTP connection failed` | Network / credentials | Check host, port, user, password |
| `No .mov files found` | Wrong path or extension | Verify `ftp_path`; extension must be `.mov` (case-insensitive) |
| `No frames extracted` | Corrupt / still-writing file | Check ffmpeg lines in the Log tab |
| Timestamp shows `(approx)` | FTP doesn't support MLSD | Current UTC used as fallback — no action needed |
| Single image instead of matrix | Video has < 4 frames | Expected — short clips use single mode |
| No timelapse built | No photos in yesterday's folder | Add-on may not have run that day; check logs |
| Old folders not deleted | `keep_photos_days` too large | Lower the value; deletion runs just after midnight |

Open the add-on **Log** tab for full output.

---

## Changelog

### v1.5.0
- Timestamp only on photo — removed frame numbers and caption strip entirely
- Single mode: full original resolution (no downscaling)
- Matrix mode: 4 cells at 25% each, sheet upscaled back to original video size
- JPEG saved at quality 97 / no chroma subsampling (visually lossless)
- Date-structured storage: `output_dir/YYYY-MM-DD/polaroid_HH-MM-SS.jpg`
- Daily H.264 timelapse from previous day's photos: `timelapse/YYYY-MM-DD-timelapse.mp4`
- `keep_photos_days` retention — removes dated photo folders
- `keep_timelapse_days` retention — removes old timelapse MP4s
- Maintenance runs once per day in the 00:00–00:05 window

### v1.4.0
- Timestamp sourced from MOV file's FTP modification time (MLSD `modify`)
- Timestamp burned onto frame image (bottom-right, semi-transparent overlay)
- Fallback to current UTC + `(approx)` if MLSD unavailable

### v1.3.0
- Added `background_color` and `text_color` options
- Hex colours validated by HA schema at save time

### v1.2.0
- Smart rendering: single polaroid < 4 frames, 2×2 matrix ≥ 4 frames
- `interval_minutes` configurable (1–1440)

### v1.1.0
- Fixed repository structure for HA add-on store discoverability
- Removed stray `image:` line from `config.yaml`

### v1.0.0
- Initial release
