# FTP Polaroid Snapshot — Home Assistant Add-on

**v1.4.0**

Polls an FTP server on a configurable schedule (default every 5 minutes),
downloads the newest `.mov` file, extracts frames with ffmpeg, and renders
a polaroid-style JPEG saved under `/media/polaroid/`.

### Key features

- **MOV file timestamp** — the modification time of the video file (read from
  the FTP server via MLSD) is burned directly onto every frame image in the
  bottom-right corner. This means the timestamp reflects *when the recording
  was made*, not when the add-on ran.
- **< 4 frames** → single polaroid from the middle frame
- **≥ 4 frames** → 2 × 2 polaroid matrix (first frame of each quarter)
- Each cell scaled to **25 % of the original video resolution**
- **2 px separator** between matrix cells
- Configurable **background colour** and **caption text colour**

---

## How the timestamp works

The timestamp shown on each image is taken from the FTP `MLSD` `modify` fact —
the server-side modification time of the `.mov` file — formatted as:

```
2024-03-15  09:42:17 UTC
```

The timestamp is rendered as a **semi-transparent dark overlay in the
bottom-right corner** of each frame so it remains legible on any background.

If the FTP server does not support `MLSD` (falls back to `NLST`), the current
UTC time is used instead and the label is suffixed with `(approx)`.

---

## How frame selection works

```
total_frames = 120
group_size   = 120 // 4 = 30

Matrix mode (≥ 4 frames):
  Group 1 → index   0   (frame_000001.png)
  Group 2 → index  30   (frame_000031.png)
  Group 3 → index  60   (frame_000061.png)
  Group 4 → index  90   (frame_000091.png)

Single mode (< 4 frames):
  total_frames = 2  →  middle frame (index 1)
```

---

## Repository structure

For Home Assistant to discover the add-on your GitHub repository **must**
follow this layout:

```
your-repo/
├── repository.yaml                        ← required at repo root
└── ftp_polaroid_snapshot/
    ├── config.yaml
    ├── Dockerfile
    ├── README.md
    └── rootfs/usr/bin/run.py
```

---

## Installation

1. In Home Assistant open **Settings → Add-ons → Add-on Store**.
2. Click the three-dot menu **⋮ → Repositories**.
3. Paste your GitHub repository URL and click **Add**.
4. Find **FTP Polaroid Snapshot** at the bottom of the store and click **Install**.

### Local / manual install

Copy the `ftp_polaroid_snapshot/` folder into your HA config directory:

```
/config/addons/ftp_polaroid_snapshot/
```

Then go to **Settings → Add-ons → Local add-ons** and install from there.

---

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `ftp_host` | string | `""` | FTP server hostname or IP address |
| `ftp_port` | int | `21` | FTP port |
| `ftp_user` | string | `"anonymous"` | FTP username |
| `ftp_password` | string | `""` | FTP password |
| `ftp_path` | string | `"/"` | Remote directory to scan for `.mov` files |
| `output_dir` | string | `"/media/polaroid"` | Where output images are saved |
| `interval_minutes` | int (1–1440) | `5` | How often to check the FTP server |
| `background_color` | hex string | `"#FFFFFF"` | Polaroid border and separator colour |
| `text_color` | hex string | `"#505050"` | Caption text colour below each frame |

Colours accept standard CSS hex notation — `#RRGGBB` or shorthand `#RGB`.
HA validates the format at save time.

> **Note:** `text_color` controls only the caption strip below each polaroid.
> The timestamp overlay burned onto the photo itself always uses white text on
> a semi-transparent dark background for maximum legibility.

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
text_color: "#505050"
```

### Colour presets

| Style | `background_color` | `text_color` |
|---|---|---|
| Classic white (default) | `#FFFFFF` | `#505050` |
| Aged paper | `#EBE4D7` | `#4A3F35` |
| Dark / night | `#1A1A1A` | `#E0E0E0` |
| Slate blue | `#2D3A4A` | `#BDD4E7` |
| Soft green | `#D4E8D0` | `#2A4A2E` |

---

## Output files

Every successful run writes two files inside `output_dir`:

| File | Description |
|---|---|
| `polaroid_YYYYMMDD_HHMMSS.jpg` | Timestamped archive copy |
| `latest.jpg` | Overwritten each run — use this in your dashboard |

---

## Home Assistant dashboard card

### Basic picture card

```yaml
type: picture
image: /media/polaroid/latest.jpg
```

### With auto-refresh

```yaml
type: picture
image: /media/polaroid/latest.jpg
refresh_interval: 300   # seconds — requires Picture Entity Card
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Add-on not visible in store | Wrong repo structure | Ensure `repository.yaml` is at the repo root and the add-on is in a subdirectory |
| `FTP connection failed` | Network or credentials | Check host, port, credentials; confirm HA can reach the FTP server |
| `No .mov files found` | Wrong path or extension | Verify `ftp_path`; filenames must end in `.mov` (case-insensitive) |
| `No frames extracted` | Corrupt or incomplete file | Camera may still be writing; check ffmpeg lines in the log |
| Timestamp shows `(approx)` | FTP server doesn't support MLSD | Current UTC time is used as fallback — no action needed |
| Single image instead of matrix | Fewer than 4 frames | Short clips produce a single polaroid — expected behaviour |
| Invalid colour rejected by HA | Bad hex string | Use `#RRGGBB` format, e.g. `#FF8800`; no spaces |
| Image looks blank / all black | All-black video frames | Inspect the archived `polaroid_*.jpg` files |

Open the add-on **Log** tab in HA for full output including the timestamp
value, frame counts, thumb dimensions, colour values, and output file paths.

---

## Changelog

### v1.4.0
- Timestamp is now sourced from the MOV file's FTP modification time (MLSD
  `modify` field) rather than the current wall clock
- Timestamp is burned directly onto each frame image (bottom-right corner,
  semi-transparent dark overlay with white text) in addition to the caption strip
- Falls back to current UTC time (labelled `approx`) if MLSD is unavailable

### v1.3.0
- Added `background_color` option — polaroid border and matrix separator colour
- Added `text_color` option — caption strip text colour
- Hex colours validated by HA schema at save time; parse errors fall back gracefully

### v1.2.0
- Smart rendering: single polaroid when < 4 frames, 2×2 matrix when ≥ 4 frames
- `interval_minutes` now configurable (1–1440), validated by HA schema

### v1.1.0
- Fixed repository structure so the add-on is discoverable by the HA store
- Removed stray `image:` line from `config.yaml` (was preventing local builds)
- Added `--break-system-packages` to pip install in Dockerfile

### v1.0.0
- Initial release: FTP poll, ffmpeg frame extraction, 2×2 polaroid matrix at 25% scale
