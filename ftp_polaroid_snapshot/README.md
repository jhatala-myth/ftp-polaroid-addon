# FTP Polaroid Snapshot — Home Assistant Add-on

Polls an FTP server every **N minutes** (default 5), downloads the newest
`.mov` file, extracts every frame with ffmpeg, selects **4 evenly-spaced
frames** (first frame of each quarter), and renders a **2 × 2 polaroid-style
contact sheet** saved under `/media/polaroid/`.

---

## How frame selection works

```
total_frames = 120

group_size   = 120 // 4  =  30

chosen frames:
  Group 1 → index   0   (frame_000001.png)
  Group 2 → index  30   (frame_000031.png)
  Group 3 → index  60   (frame_000061.png)
  Group 4 → index  90   (frame_000091.png)
```

---

## Installation

1. In Home Assistant open **Settings → Add-ons → Add-on Store**.
2. Click the three-dot menu → **Repositories** and add the URL of this repo.
3. Find **FTP Polaroid Snapshot** and click **Install**.

### Manual / local install

Copy the whole `ftp-polaroid-addon/` folder into your HA config directory:

```
/config/addons/ftp_polaroid_snapshot/
```

Then go to **Settings → Add-ons → Local add-ons** and install from there.

---

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `ftp_host` | string | `""` | FTP server hostname or IP |
| `ftp_port` | int | `21` | FTP port |
| `ftp_user` | string | `"anonymous"` | FTP username |
| `ftp_password` | string | `""` | FTP password |
| `ftp_path` | string | `"/"` | Remote directory to scan |
| `output_dir` | string | `"/media/polaroid"` | Where to save images |
| `interval_minutes` | int | `5` | Poll interval in minutes |

Example `options` block in the add-on UI:

```yaml
ftp_host: "192.168.1.50"
ftp_port: 21
ftp_user: "camera"
ftp_password: "secret"
ftp_path: "/recordings"
output_dir: "/media/polaroid"
interval_minutes: 5
```

---

## Output files

Every successful run produces two files inside `output_dir`:

| File | Description |
|---|---|
| `polaroid_YYYYMMDD_HHMMSS.jpg` | Timestamped archive copy |
| `latest.jpg` | Overwritten each run – use this in your dashboard |

---

## Home Assistant Dashboard card

Add a **Picture** card pointing at the local media path:

```yaml
type: picture
image: /media/polaroid/latest.jpg
```

Or use a **Picture Glance** card. The image auto-refreshes on next page load
because the filename changes with each run.

For live auto-refresh every 5 minutes add a browser-mod or use:

```yaml
type: picture
image: /media/polaroid/latest.jpg
refresh_interval: 300   # seconds (requires Picture Entity Card)
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `FTP connection failed` | Check host/port/credentials; verify the HA host can reach the FTP server |
| `No .mov files found` | Check `ftp_path`; filenames must end in `.mov` (case-insensitive) |
| `No frames extracted` | The downloaded file may be corrupt or still being written by the camera; check ffmpeg logs |
| Image looks blank | The video may be all-black; inspect the archived `polaroid_*.jpg` files |

Enable **Show in sidebar → Log** in the add-on page to see full output.
