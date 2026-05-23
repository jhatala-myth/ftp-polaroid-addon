# FTP Polaroid Snapshot — Home Assistant Add-on

**v1.3.0**

Polls an FTP server on a configurable schedule (default every 5 minutes),
downloads the newest `.mov` file, extracts every frame with ffmpeg, and
renders a polaroid-style JPEG saved under `/media/polaroid/`.

- **< 4 frames** → single polaroid from the middle frame
- **≥ 4 frames** → 2 × 2 polaroid matrix (first frame of each quarter)
- Each cell is scaled to **25 % of the original video resolution**
- **2 px separator** between cells in matrix mode
- Fully configurable **background colour** and **caption text colour**

---

## How frame selection works

```
total_frames = 120
group_size   = 120 // 4 = 30

chosen frames:
  Group 1 → index   0   (frame_000001.png)
  Group 2 → index  30   (frame_000031.png)
  Group 3 → index  60   (frame_000061.png)
  Group 4 → index  90   (frame_000091.png)

total_frames = 2  →  single mode, middle frame (index 1)
```

---

## Repository structure

For Home Assistant to discover the add-on, your GitHub repository **must**
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
| `text_color` | hex string | `"#505050"` | Caption text colour |

Colours accept standard CSS hex notation — `#RRGGBB` or shorthand `#RGB`.
HA validates the format at save time; an invalid value is rejected before the
add-on starts.

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

### Auto-refresh with browser_mod

```yaml
type: picture
image: /media/polaroid/latest.jpg
tap_action:
  action: none
```

Add a **browser_mod** automation to reload the card every N minutes, or
set the interval to match `interval_minutes`:

```yaml
type: picture
image: /media/polaroid/latest.jpg
refresh_interval: 300   # seconds — requires Picture Entity Card
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Add-on not visible in store | Wrong repo structure | Ensure `repository.yaml` is at the root and the add-on is in a subdirectory |
| `FTP connection failed` | Network / credentials | Check host, port, and credentials; confirm HA can reach the FTP server |
| `No .mov files found` | Wrong path or extension | Verify `ftp_path`; filenames must end in `.mov` (case-insensitive) |
| `No frames extracted` | Corrupt or incomplete file | The camera may still be writing; check the ffmpeg log lines |
| Single image instead of matrix | Fewer than 4 frames extracted | Short clips produce a single polaroid — this is expected behaviour |
| Invalid colour rejected | Bad hex string | Use `#RRGGBB` format, e.g. `#FF8800`; no spaces or other characters |
| Image looks blank / all black | All-black video frames | Inspect the archived `polaroid_*.jpg` files to confirm |

Open the add-on **Log** tab in HA for full output including colour values,
frame counts, thumb dimensions, and output file paths.

---

## Changelog

### v1.3.0
- Added `background_color` option — controls polaroid border and matrix separator colour
- Added `text_color` option — controls caption text colour
- Hex colour values validated by HA schema at save time; parse errors fall back gracefully

### v1.2.0
- Smart rendering mode: single polaroid when < 4 frames, 2×2 matrix when ≥ 4 frames
- `interval_minutes` now configurable (1–1440); validated by HA schema

### v1.1.0
- Fixed repository structure so the add-on is discoverable by the HA store
- Removed stray `image:` line from `config.yaml` (was preventing local builds)
- Added `--break-system-packages` to pip install in Dockerfile

### v1.0.0
- Initial release: FTP poll, ffmpeg frame extraction, 2×2 polaroid matrix at 25% scale
