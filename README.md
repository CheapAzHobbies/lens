# Lens

A fast, mobile-first camera app for Linux. Built for tablets and 2-in-1s.

Every other Linux camera app looks like Windows 98. This one doesn't.

## What it looks like

Full-screen black viewfinder, iPhone-style controls:
- Big white shutter button
- **PHOTO / VIDEO** pill switcher
- **Camera flip** button (multi-camera aware)
- **Recent photo** thumbnail (tap to open gallery)
- Top pill row: **grid**, **aspect ratio**, **self-timer**

## Features

| Feature | Status |
|---|---|
| Live viewfinder | ✅ |
| Photo capture (JPEG) | ✅ |
| Video capture (H.264/Matroska) | ✅ |
| Camera flip (front / rear / any V4L2 device) | ✅ |
| Self-timer (3s, 10s) | ✅ |
| Rule-of-thirds grid overlay | ✅ |
| Aspect ratio toggle (4:3, 16:9, 1:1) | ✅ |
| Gallery access | ✅ |
| Keyboard shortcuts (space=shutter, F=flip, G=grid) | ✅ |
| Digital zoom | 🚧 planned |
| Filters | 🚧 planned |
| Touch gestures (swipe = flip, pinch = zoom) | 🚧 planned |
| Autofocus / manual focus | 🚧 planned |

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

# 2. GTK4 GStreamer sink (not in Ubuntu apt — build from source)
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

Saved to `~/Pictures/Lens/` and `~/Videos/Lens/`.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` / `Return` | Shutter |
| `F` | Flip camera |
| `G` | Toggle grid |

## License

GPL-3.0. See [LICENSE](LICENSE).

## Why "Lens"?

Every existing Linux camera app has a bad name (Cheese, Kamoso, Webcamoid...). This one is called Lens because that's what it is.
