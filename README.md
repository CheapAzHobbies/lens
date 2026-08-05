# Lens

A fast, mobile-first camera app for Linux. Built for tablets and 2-in-1s.

Every other Linux camera app looks like Windows 98. This one doesn't.

## What it looks like

Full-screen black viewfinder, iPhone-style controls:
- Big white shutter, scales with the window
- **PHOTO / VIDEO** pill switcher
- **Camera flip** with a smooth freeze-frame transition
- **Recent shots** deck, fans out on hover, tap to open the gallery
- Top bar: settings, exposure slider, grid, aspect ratio, timer, flash, filters
- Own title bar with minimise / maximise / close

## Features

| Feature | Status |
|---|---|
| Live viewfinder | ✅ |
| Smooth motion: holds the sensor to its rated frame rate in dim light | ✅ |
| Photo capture (JPEG, up to 8MP) | ✅ |
| Video capture (MKV / MP4 / WebM) | ✅ |
| Audio recording (AAC / Opus), unprocessed | ✅ |
| Camera flip (front / rear / any V4L2 device) | ✅ |
| NO SIGNAL static when there is no picture, click to pick a camera | ✅ |
| Self-timer (3s, 10s) | ✅ |
| Grid overlay (rule of thirds, 2x2) | ✅ |
| Aspect ratio (4:3, 16:9, 1:1) | ✅ |
| Digital zoom: pinch, scroll, or the slider up the right edge | ✅ |
| Drag to pan a zoomed view | ✅ |
| Filters (mono, vivid, warm, cool, sepia, x-ray, cross) | ✅ |
| Screen flash for low light | ✅ |
| Tap to meter | ✅ |
| Mirrored preview, per camera, with the file left unmirrored | ✅ |
| Exposure slider in EV, centre is auto | ✅ |
| Camcorder HUD, interactive | ✅ |
| Gallery: browse, crop, rotate, mirror, rename | ✅ |
| Trash with a recycle bin, its own folder, optional auto-purge | ✅ |
| Settings: camera, folders, naming, mic level and rate, mirroring, retention | ✅ |
| Audio diagnostics: shows hardware vs software gain, format, processing | ✅ |
| Touch gestures (swipe = flip, pinch = zoom) | 🚧 planned |
| Subtitles from speech | 🚧 planned |
| Manual focus | ❌ not possible, no focus motor on either camera |

## Recording HUD

A camcorder-style overlay on the video itself, not in the letterbox bars.
Spread across the corners and hidden entirely when the window gets too small
to fit it, rather than squashing the readouts together.

| Readout | Interactive |
|---|---|
| REC / STBY and elapsed time | |
| Battery and clock | |
| Resolution, respects the current aspect ratio | click to change |
| Live FPS | |
| Codec and container | click to change |
| Mic with a level meter | click to mute, right-click to pick an input |

Left-click the viewfinder for a menu of which readouts to show.

## Gallery

Tap the preview deck to open it.

- Arrow keys, on-screen `<` `>`, the scroll wheel, or the filmstrip to move
- Crop with a ratio picker, rotate, mirror, all animated
- Click the filename to rename in place
- Edits save as a new file, never over the original
- Trash goes to Lens's own bin, purged on a schedule you set

## Requirements

- Linux (tested on Ubuntu 24.04)
- Python 3.10+
- GTK 4.10+, libadwaita 1.0+
- GStreamer 1.20+ with `gtk4paintablesink` plugin (from `gst-plugins-rs`)
- A V4L2-compatible camera

## Install

```bash
# 1. Runtime deps
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 v4l-utils \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav

# 2. GTK4 GStreamer sink (not in Ubuntu apt, build from source)
sudo apt install -y cargo libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgtk-4-dev
git clone --depth 1 https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /tmp/gst-plugins-rs
(cd /tmp/gst-plugins-rs && cargo build --release -p gst-plugin-gtk4)
sudo install -m 644 /tmp/gst-plugins-rs/target/release/libgstgtk4.so \
    /usr/lib/x86_64-linux-gnu/gstreamer-1.0/libgstgtk4.so
sudo ldconfig

# 3. Lens
git clone https://github.com/CheapAzHobbies/lens ~/Applications/lens
cp ~/Applications/lens/data/org.cheapaz.Lens.desktop ~/.local/share/applications/
sed -i "s|/home/bao|$HOME|" ~/.local/share/applications/org.cheapaz.Lens.desktop
sed -i "s|/home/bao|$HOME|; s|Documents/lens|Applications/lens|" ~/.local/share/applications/org.cheapaz.Lens.desktop
```

Then search **Lens** in your app menu.

## Run from source

```bash
python3 src/lens.py
```

## Photos & videos

Saved to `~/Pictures/Lens/` and `~/Videos/Lens/` as `Lens_IMG_0001.jpg` and
`Lens_VID_0001.mp4`. Folders and the name prefix are configurable in settings.

Sequence numbers come from scanning what is already there, so deleting a file
never makes the next shot overwrite something.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` / `Return` | Shutter |
| `F` | Flip camera |
| `G` | Toggle grid |
| `←` `→` | Previous / next in the gallery |
| `Esc` | Close the gallery |

## Assets

`assets/penguin_saving.png` is the save indicator: the Club Penguin dance,
141 frames, recoloured black. It runs at 20fps for one 7.05 second loop,
which is the source GIF's own timing read from its frame delays.

Rebuild it from the frames with:

```bash
python3 tools/make_penguin_sheet.py path/to/frames -o assets/penguin_saving.png
```

The recolour is a hue selection rather than a repaint: the body is a flat
cyan, so it is picked by hue and mapped into a narrow dark range that keeps
the shading. Selecting on raw channel values instead leaves a blue rim
wherever the body meets the background, because those pixels are half cyan
and half white. The background cannot be keyed on colour either, since the
belly is also near-white, so the fill starts at the border and stops at the
outline.

## License

GPL-3.0. See [LICENSE](LICENSE).

## Why "Lens"?

Every existing Linux camera app has a bad name (Cheese, Kamoso, Webcamoid...). This one is called Lens because that's what it is.
