#!/usr/bin/env python3
"""
Lens — a fast, mobile-first camera app for Linux tablets.
GTK4 + libadwaita + GStreamer.
"""

import gi, os, re, sys, math, json, time, datetime, pathlib, tempfile
import subprocess
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
import cairo
from gi.repository import Gtk, Adw, Gst, GLib, Gdk, Gio, GObject, GdkPixbuf, Graphene, Pango

Gst.init(None)

APP_ID = "org.cheapaz.Lens"
VERSION = "0.9"
CONFIG_DIR = pathlib.Path(
    os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "lens"
SETTINGS = CONFIG_DIR / "settings.json"
# Defaults only. The live values come from settings.json and are held on
# the window, so they can be changed without restarting.
DEF_PICTURES = pathlib.Path.home() / "Pictures" / "Lens"
DEF_VIDEOS   = pathlib.Path.home() / "Videos"  / "Lens"
PICTURES = DEF_PICTURES
VIDEOS   = DEF_VIDEOS
THUMBS = pathlib.Path(
    os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "lens" / "thumbs"
VIDEO_EXTS = (".mkv", ".mp4", ".webm", ".mov")

# label -> (extension, muxer, video encoder, audio encoder)
# AAC rather than Opus for MKV. The hiss between words was the encoder, not
# the room: feeding all three digital silence, every Opus setting tried came
# back at about -67 dBFS of coding noise (64k, 96k, 128k, voice mode,
# complexity 10 and DTX were all within 2dB of each other), while AAC came
# back at -369, which is silence. WebM has to stay on Opus, the format does
# not allow AAC.
CONTAINERS = {
    "MKV":  ("mkv",  "matroskamux", "x264enc tune=zerolatency bitrate=4000",
             "voaacenc bitrate=128000"),
    "MP4":  ("mp4",  "mp4mux",      "x264enc tune=zerolatency bitrate=4000",
             "voaacenc bitrate=128000"),
    "WebM": ("webm", "webmmux",     "vp8enc deadline=1",                     "opusenc"),
}


def _stable_camera_ids():
    """{/dev/videoN: stable-id} from /dev/v4l/by-id.

    The link names carry the vendor, model and serial, plus a -video-indexN
    suffix identifying the *node* rather than the device. Stripping that
    suffix gives one id per physical camera, which is what we want to group
    on and what we persist in the config.
    """
    import re
    out = {}
    byid = pathlib.Path("/dev/v4l/by-id")
    if not byid.is_dir():
        return out
    for link in sorted(byid.iterdir()):
        try:
            target = str(link.resolve())
        except OSError:
            continue
        out[target] = re.sub(r"-video-index\d+$", "", link.name)
    return out


def list_v4l2_cameras():
    """Return [(path, human_name, stable_id)], one entry per physical camera.

    A single camera exposes several /dev/videoN nodes (capture, metadata,
    subdev), so we keep only nodes reporting "Video Capture" and then take
    the first node per physical device.

    Grouping is by the /dev/v4l/by-id identity, not by the card name. Card
    names are per *model*, so two cameras of the same model collapsed into
    one and the second silently disappeared.
    """
    import subprocess
    stable = _stable_camera_ids()
    devs = []
    seen = set()

    def _devnum(pth):
        tail = pth.name[5:]
        return int(tail) if tail.isdigit() else 9999

    for d in sorted(pathlib.Path("/dev").glob("video*"), key=_devnum):
        try:
            info = subprocess.run(["v4l2-ctl", "-d", str(d), "--info"],
                                  capture_output=True, text=True, timeout=1).stdout
        except Exception:
            continue
        # Must be an actual capture device (not metadata/subdev)
        # Look at Device Caps line — should include "Video Capture"
        capture = False
        card = "Camera"
        for line in info.splitlines():
            line = line.strip()
            if line.startswith("Card type"):
                card = line.split(":", 1)[1].strip()
            if "Video Capture" in line and "Metadata" not in line:
                capture = True
        if not capture:
            continue
        # Fall back to the card name only when by-id is unavailable, which
        # is the old (lossy) behaviour but better than dropping the device.
        sid = stable.get(str(d)) or f"card:{card}"
        if sid in seen:
            continue
        seen.add(sid)
        devs.append((str(d), card, sid))
    return devs


_MODE_CACHE = {}


def _enumerate_modes(dev):
    """[(fourcc, w, h, fps)] the device advertises. Cached: shelling out to
    v4l2-ctl on every shutter press would be silly."""
    if dev in _MODE_CACHE:
        return _MODE_CACHE[dev]
    import subprocess, re
    try:
        out = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats-ext"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    fmt = size = None
    modes = []
    for line in out.splitlines():
        m = re.search(r"\]: '(\w+)'", line)
        if m:
            fmt = m.group(1); continue
        m = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2))); continue
        m = re.search(r"\(([\d.]+) fps\)", line)
        if m and fmt and size:
            modes.append((fmt, size[0], size[1], float(m.group(1))))
    _MODE_CACHE[dev] = modes
    return modes


def best_mode(dev):
    """Pick a sane capture mode for a device: (fourcc, w, h, fps) or None.

    Without a caps filter GStreamer takes whatever v4l2src offers first,
    which on the Z13 rear camera is YUYV 2592x1944 at *2 fps*. That is the
    "laggy rear camera". The same sensor does MJPG 1600x1200 at 30.
    """
    modes = _enumerate_modes(dev)
    if not modes:
        return None
    # Smooth first, then detail. Cap the pixel count so decode stays cheap;
    # stills are cropped from this frame anyway.
    usable = [m for m in modes if m[3] >= 20 and m[1] * m[2] <= 2_100_000]
    if not usable:
        usable = [m for m in modes if m[3] >= 10] or modes
    # Prefer MJPG: the raw modes on these sensors are the slow ones.
    mjpg = [m for m in usable if m[0] == "MJPG"]
    pool = mjpg or usable
    # Stick to the sensor's native aspect. The Z13 rear sensor is 4:3, and
    # picking one of its 16:9 modes squashes the picture, which is the
    # stretching this app had before it stopped forcing caps at all.
    biggest = max(modes, key=lambda m: m[1] * m[2])
    native = biggest[1] / biggest[2]
    same = [m for m in pool if abs(m[1] / m[2] - native) < 0.02]
    if same:
        pool = same
    return max(pool, key=lambda m: (m[1] * m[2], m[3]))


def best_still_mode(dev):
    """Largest MJPG mode on a device, ignoring framerate.

    Stills come off the preview stream, so they were only ever as large as
    the preview: 1600x1200 on the rear camera and 640x480 on the front. The
    rear sensor actually does 3264x2448, so it is worth briefly retuning the
    camera for the shot.
    """
    modes = _enumerate_modes(dev)
    mjpg = [m for m in modes if m[0] == "MJPG"]
    if not mjpg:
        return None
    biggest = max(modes, key=lambda m: m[1] * m[2])
    native = biggest[1] / biggest[2]
    same = [m for m in mjpg if abs(m[1] / m[2] - native) < 0.02]
    return max(same or mjpg, key=lambda m: m[1] * m[2])


# Voice band-pass, applied inside our own pipeline.
#
# Everything outside roughly 110Hz-7.5kHz is noise for speech: rumble below,
# hiss above. Measured on the internal mic in a quiet room, this takes the
# noise floor from -46.5 dBFS to -62.3, about 16dB, which is the difference
# between clearly audible hiss and inaudible.
#
# Deliberately not a PulseAudio module. A module is system state: it outlives
# the app, keeps a capture stream open, and lights the system microphone
# indicator with nothing recording. This lives and dies with the pipeline, so
# Lens can be packaged and shipped without touching the machine it runs on.
# name -> (videobalance props, coloreffects preset)
FILTERS = {
    "None":   ({}, "none"),
    "Mono":   ({"saturation": 0.0}, "none"),
    "Vivid":  ({"saturation": 1.7, "contrast": 1.15}, "none"),
    "Warm":   ({"hue": 0.06, "saturation": 1.15}, "none"),
    "Cool":   ({"hue": -0.08, "saturation": 1.05}, "none"),
    "Sepia":  ({}, "sepia"),
    "X-Ray":  ({}, "xray"),
    "Cross":  ({}, "xpro"),
}

# Sprite sheet for the save indicator: 32 frames laid out in one row.
# The Club Penguin dance, recoloured black by tools/make_penguin_sheet.py.
# 141 frames at 50ms is the source GIF's own timing, read from its frame
# delays rather than guessed: 7.05 seconds for one loop, which is why the
# indicator is held for that long.
TUX_SHEET  = "penguin_saving.png"
TUX_FRAMES = 141
TUX_COLS   = 12          # the sheet is a grid; one row would be 17000px wide
TUX_MS     = 50          # 20fps, the rate the frames were authored at
SAVING_MIN_VISIBLE = 7.05
# Measured in the app with the real style applied: 'Saving' is 58px and
# each dot adds 10, so 'Saving...' needs 88. It was set to 84, four short,
# and the label quietly ellipsised itself into looking crushed.
SAVING_LABEL_W = 94
TUX_W, TUX_H = 124, 120
# Corner size. The bottom strip between the flip button and the window edge
# is about 50px, so this is close to the ceiling.
TUX_SMALL_H = 44
TUX_SMALL_W = round(TUX_W * TUX_SMALL_H / TUX_H)


def asset(name):
    """Find a bundled asset whether running from the tree or installed."""
    here = pathlib.Path(__file__).resolve().parent
    for base in (here / "assets", here.parent / "assets",
                 pathlib.Path("/usr/share/lens/assets")):
        f = base / name
        if f.exists():
            return f
    return None


def tux_frames():
    """Slice the sheet once and hand back textures, or None if it is missing.

    Cached on the function because the overlay is rebuilt per save and
    re-decoding 32 frames each time would stall the very moment it exists
    to cover.
    """
    if not hasattr(tux_frames, "_cache"):
        tux_frames._cache = None
        f = asset(TUX_SHEET)
        if f is not None:
            try:
                sheet = GdkPixbuf.Pixbuf.new_from_file(str(f))
                rows = (TUX_FRAMES + TUX_COLS - 1) // TUX_COLS
                w = sheet.get_width() // TUX_COLS
                h = sheet.get_height() // rows
                # Scaled here rather than by the widget. A Gtk.Picture takes
                # its natural width from the texture, so full-size frames made
                # the chip 117px wide per frame while drawing a 29px penguin
                # centred in it, leaving the bird 99px short of the corner it
                # was supposed to be sitting in.
                tux_frames._cache = [
                    Gdk.Texture.new_for_pixbuf(
                        sheet.new_subpixbuf((i % TUX_COLS) * w,
                                            (i // TUX_COLS) * h, w, h)
                        .scale_simple(TUX_SMALL_W, TUX_SMALL_H,
                                      GdkPixbuf.InterpType.BILINEAR))
                    for i in range(TUX_FRAMES)]
            except Exception as e:
                print(f"Lens: cannot load {f}: {e}", file=sys.stderr)
    return tux_frames._cache


def have(element):
    """Is this GStreamer element installed?"""
    return Gst.ElementFactory.find(element) is not None


# Measured on a real take from this machine, per band, speech against the
# silence in the same file. 72 percent of the noise energy sits between 300
# and 4800Hz, inside the voice, where SNR was 4.5dB at 1200-2400. Nothing
# that cuts by frequency can help with that: there is no band to remove that
# is not also the voice. RNNoise separates them by what they are rather than
# where they are, and on that same recording it moved 300-600Hz from 11.6dB
# SNR to 82.9, and the floor during silence down 61.8dB.
#
# It must run at 48kHz. It analyses fixed 480-sample frames on the
# assumption they are 10ms, and at 44100 every band it reasons about lands
# in the wrong place: audible, but the words stop being words.
RNNOISE = "ladspa-librnnoise-ladspa-so-noise-suppressor-mono"
# Suppression that hard leaves the voice wobbling, because at 4.5dB input SNR
# RNNoise's per-band gain estimate swings about: measured 17.6dB of standard
# deviation in the gain it applies across sustained speech. Blending a little
# untouched signal back steadies it. On a real take, 5 percent dry took that
# to 4.8dB while still suppressing 25.8dB, which is far more than needed.
#
# The dry path must be delayed to match, and the mixer will not do it: the
# LADSPA plugin does not report its latency, so a naive blend put every sound
# in twice, 145ms apart. Measured with tone bursts, 145ms lines them up
# exactly. This number belongs to the default grace settings; changing those
# changes the latency, which is why they are not exposed.
NR_LATENCY_MS = 145


CONTAINERS = {
    "MKV":  ("mkv",  "matroskamux", "x264enc tune=zerolatency bitrate=4000",
             "voaacenc bitrate=128000"),
    "MP4":  ("mp4",  "mp4mux",      "x264enc tune=zerolatency bitrate=4000",
             "voaacenc bitrate=128000"),
    "WebM": ("webm", "webmmux",     "vp8enc deadline=1",                     "opusenc"),
}


# Voice band-pass, applied inside our own pipeline.
#
# Everything outside roughly 110Hz-7.5kHz is noise for speech: rumble below,
# hiss above. Measured on the internal mic in a quiet room, this takes the
# noise floor from -46.5 dBFS to -62.3, about 16dB, which is the difference
# between clearly audible hiss and inaudible.
#
# Deliberately not a PulseAudio module. A module is system state: it outlives
# the app, keeps a capture stream open, and lights the system microphone
# indicator with nothing recording. This lives and dies with the pipeline, so
# Lens can be packaged and shipped without touching the machine it runs on.
# name -> (videobalance props, coloreffects preset)
FILTERS = {
    "None":   ({}, "none"),
    "Mono":   ({"saturation": 0.0}, "none"),
    "Vivid":  ({"saturation": 1.7, "contrast": 1.15}, "none"),
    "Warm":   ({"hue": 0.06, "saturation": 1.15}, "none"),
    "Cool":   ({"hue": -0.08, "saturation": 1.05}, "none"),
    "Sepia":  ({}, "sepia"),
    "X-Ray":  ({}, "xray"),
    "Cross":  ({}, "xpro"),
}

def audio_facts():
    """Everything about the capture path that can actually be read back.

    Written because this took a very long time to diagnose by ear. The
    microphone hiss on this machine turned out to be a +30dB analog preamp
    that PipeWire only starts lowering below about 30 percent volume: above
    that it attenuates in software, so turning the level down moves signal
    and hiss together and appears to do nothing. That is invisible unless
    something shows you the hardware gain separately from the software one.
    """
    import subprocess
    f = {}

    def run(*a):
        try:
            return subprocess.run(a, capture_output=True, text=True,
                                  timeout=2).stdout
        except Exception:
            return ""
    info = run("pactl", "info")
    for line in info.splitlines():
        if line.startswith("Server Name:"):
            f["backend"] = line.split(":", 1)[1].strip()
        elif line.startswith("Default Source:"):
            f["device"] = line.split(":", 1)[1].strip()
    src = f.get("device", "")
    block, seen = [], False
    for line in run("pactl", "list", "sources").splitlines():
        if line.strip() == f"Name: {src}":
            seen = True
        elif seen and line.startswith("Source #"):
            break
        elif seen:
            block.append(line.strip())
    for line in block:
        if line.startswith("Sample Specification:"):
            f["format"] = line.split(":", 1)[1].strip()
        elif line.startswith("Volume:") and "front-left" in line:
            part = [x for x in line.split(",") if "%" in x]
            if part:
                f["sw_volume"] = part[0].split("/")[-2].strip() if "/" in part[0] else ""
        elif line.startswith("Mute:"):
            f["muted"] = line.split(":", 1)[1].strip()
    # the analog stage, which is the one that matters and the one nothing shows
    for card in ("1", "0", "2"):
        out = run("amixer", "-c", card, "sget", "Capture")
        m = re.search(r"Front Left:.*?\[(-?[\d.]+)dB\]", out)
        if m:
            f["analog_gain"] = f"{float(m.group(1)):+.2f} dB"
            pct = re.search(r"Front Left:.*?\[(\d+)%\]", out)
            if pct:
                f["analog_pct"] = int(pct.group(1))
            break
    mods = [l for l in run("pactl", "list", "short", "modules").splitlines()
            if any(k in l.lower() for k in ("echo", "noise", "agc", "filter-chain"))]
    f["processing"] = ", ".join(l.split()[1] for l in mods) if mods else "none loaded"
    return f


def list_audio_sources():
    """[(name, description)] of real capture sources."""
    import subprocess
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return []
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or "monitor" in parts[1]:
            continue
        # Trim the long alsa_input.pci-... prefix down to something readable
        pretty = parts[1].split(".")[-1].replace("_", " ")
        res.append((parts[1], pretty))
    return res


def has_microphone():
    """True if PipeWire/Pulse reports a real capture source."""
    import subprocess
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return False
    return any(l.strip() and "monitor" not in l.split("\t")[1]
               for l in out.splitlines() if "\t" in l)


def next_name(folder, prefix, kind, ext):
    """Next free {prefix}_{IMG|VID}_{NNNN}.{ext} in folder.

    Sequential rather than timestamped: it sorts naturally, reads at a
    glance, and matches what cameras actually do. The scan is over existing
    names rather than a stored counter, so deleting the newest file and
    shooting again cannot silently overwrite something.
    """
    import re
    pat = re.compile(rf"^{re.escape(prefix)}_{kind}_(\d+)\.", re.I)
    top = 0
    try:
        for f in folder.iterdir():
            m = pat.match(f.name)
            if m:
                top = max(top, int(m.group(1)))
    except OSError:
        pass
    return folder / f"{prefix}_{kind}_{top + 1:04d}.{ext}"


_TRASH_OVERRIDE = None


def trash_dir():
    return _TRASH_OVERRIDE or (CONFIG_DIR / "trash")


def set_trash_dir(path):
    """Point the bin somewhere else. Module level because purge_trash and the
    gallery both need it and neither has the window to hand."""
    global _TRASH_OVERRIDE
    _TRASH_OVERRIDE = pathlib.Path(path).expanduser() if path else None
    if _TRASH_OVERRIDE:
        try:
            _TRASH_OVERRIDE.mkdir(parents=True, exist_ok=True)
        except OSError:
            _TRASH_OVERRIDE = None


def purge_trash(days):
    """Delete trashed files older than `days`. 0 disables purging."""
    if not days:
        return
    import time
    d = trash_dir()
    if not d.is_dir():
        return
    cutoff = time.time() - days * 86400
    for f in d.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                print(f"Lens: purged {f.name} from trash")
        except OSError:
            pass


def is_video(path):
    return str(path).lower().endswith(VIDEO_EXTS)


def video_thumbnail(path):
    """First frame of a clip as a jpg, cached. Returns None if it fails."""
    src = pathlib.Path(path)
    try:
        THUMBS.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    out = THUMBS / (src.stem + ".jpg")
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    import subprocess
    # Half a second in, to skip the black frame most captures start on.
    # Very short clips have nothing there, so fall back to the first frame.
    for seek in ("0.5", "0"):
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", seek, "-i", str(src),
                 "-frames:v", "1", "-q:v", "3", str(out)],
                capture_output=True, timeout=10)
        except Exception:
            return None
        if out.exists() and out.stat().st_size > 0:
            return out
    return None


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:
        return {}


def save_settings(data):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Lens: could not save settings: {e}", file=sys.stderr)


def mains_online():
    """True/False if a mains supply says so, None if there is none to ask."""
    base = pathlib.Path("/sys/class/power_supply")
    if not base.is_dir():
        return None
    seen = False
    for d in sorted(base.iterdir()):
        try:
            if (d / "type").read_text().strip() != "Mains":
                continue
            seen = True
            if (d / "online").read_text().strip() == "1":
                return True
        except Exception:
            continue
    return False if seen else None


def read_battery():
    """(percent, status, energy_uWh, power_uW) from sysfs.

    Power is returned rather than a runtime so the caller can average it;
    a single instantaneous reading swings wildly. Any field may be None.
    """
    base = pathlib.Path("/sys/class/power_supply")
    for name in ("BAT0", "BAT1", "BATT"):
        d = base / name
        if not d.is_dir():
            continue

        def rd(f):
            try:
                return int((d / f).read_text().strip())
            except Exception:
                return None

        try:
            status = (d / "status").read_text().strip()
        except Exception:
            status = ""
        pct = rd("capacity")
        # energy_*/power_* on most laptops, charge_*/current_* on some.
        energy = rd("energy_now") or rd("charge_now")
        power = rd("power_now") or rd("current_now")
        watts = None
        if energy and power and power > 0:
            # Units are not reliable here. This machine reports energy_now in
            # uWh but power_now in mW, so reading both as uW gives a runtime
            # of 618 hours. Try each interpretation and keep the one that is
            # physically believable.
            for scale in (1, 1000):
                cand = energy / (power * scale) * 3600
                if 300 <= cand <= 24 * 3600:
                    watts = power * scale
                    break
        return pct, status, energy, watts
    return None, "", None, None


class AudioMeter(Gtk.DrawingArea):
    """Segmented level meter, camcorder style.

    Green up to about -12dBFS, amber to -3, red at the top, so clipping is
    visible before it happens rather than after.
    """

    SEGMENTS = 9

    def __init__(self):
        super().__init__()
        self.level = 0.0       # 0..1
        self.muted = False
        # Sized to sit with the top row rather than under it. At 46x11 the
        # bars read as a hairline next to chips with 14px bold text.
        self.set_content_width(66)
        self.set_content_height(18)
        self.set_valign(Gtk.Align.CENTER)
        self.set_can_target(False)
        self.set_draw_func(self._draw)

    def set_db(self, db):
        """dBFS in, 0..1 out. -50dB is the floor: below that it is silence."""
        if db is None or db < -50:
            frac = 0.0
        else:
            frac = max(0.0, min(1.0, (db + 50.0) / 50.0))
        # Fall faster than nothing but slower than the signal, so the meter
        # reads as a meter rather than a strobe.
        self.level = frac if frac > self.level else self.level * 0.65 + frac * 0.35
        self.queue_draw()

    def set_muted(self, muted):
        if muted != self.muted:
            self.muted = muted
            self.queue_draw()

    def _draw(self, area, cr, w, h):
        gap = 2
        seg_w = (w - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        lit = 0 if self.muted else int(self.level * self.SEGMENTS + 0.5)
        for i in range(self.SEGMENTS):
            frac = (i + 1) / self.SEGMENTS
            if i < lit:
                if frac > 0.88:
                    cr.set_source_rgb(1.0, 0.28, 0.24)      # clipping
                elif frac > 0.72:
                    cr.set_source_rgb(1.0, 0.75, 0.20)
                else:
                    cr.set_source_rgb(0.35, 0.86, 0.45)
            else:
                cr.set_source_rgba(1, 1, 1, 0.16)
            cr.rectangle(i * (seg_w + gap), 0, seg_w, h)
            cr.fill()


class BatteryGauge(Gtk.DrawingArea):
    """Camcorder-style battery: outline, nub, and a bar that drains."""

    def __init__(self):
        super().__init__()
        self.level = 1.0
        self.charging = False
        self.set_content_width(30)
        self.set_content_height(15)
        self.set_valign(Gtk.Align.CENTER)
        self.set_can_target(False)
        self.set_draw_func(self._draw)

    def set_level(self, frac, charging=False):
        self.level = max(0.0, min(1.0, frac))
        self.charging = charging
        self.queue_draw()

    def _draw(self, area, cr, w, h):
        if self.charging:
            cr.set_source_rgb(0.45, 0.85, 0.45)
        elif self.level <= 0.15:
            cr.set_source_rgb(1.0, 0.30, 0.30)
        elif self.level <= 0.30:
            cr.set_source_rgb(1.0, 0.75, 0.20)
        else:
            cr.set_source_rgb(1.0, 1.0, 1.0)
        body = w - 4
        cr.set_line_width(1.5)
        cr.rectangle(0.75, 0.75, body - 1.5, h - 1.5)
        cr.stroke()
        cr.rectangle(body, h / 2 - 3, 3, 6)      # the nub
        cr.fill()
        inner = (body - 5) * self.level
        if inner > 0.5:
            cr.rectangle(2.5, 2.5, inner, h - 5)
            cr.fill()


class NoSignal(Gtk.DrawingArea):
    """Analogue static, for when there is nothing to show.

    A black rectangle is indistinguishable from a camera pointed at
    something dark, from a camera another process has taken, and from no
    camera at all. Static is unambiguous: everyone born before streaming
    knows exactly what it means.

    The noise is a handful of small tiles cycled at a low rate rather than
    fresh noise every frame. Generating 300k random pixels sixty times a
    second in Python would cost more than the viewfinder it is standing in
    for, and real analogue static repeats to the eye anyway.
    """

    TILES = 8
    TILE_W, TILE_H = 148, 112
    MS = 66                     # ~15fps, which is where static stops
                                # looking like it is stepping

    def __init__(self):
        super().__init__()
        self._tiles = self._make_tiles()
        self._i = 0
        self.set_draw_func(self._draw)
        self._tick = GLib.timeout_add(self.MS, self._advance)
        self.connect("destroy", self._stop)

    def _stop(self, *_):
        if self._tick:
            GLib.source_remove(self._tick)
            self._tick = None

    def _advance(self):
        self._i = (self._i + 1) % len(self._tiles)
        self.queue_draw()
        return True

    def _make_tiles(self):
        import cairo
        w, h = self.TILE_W, self.TILE_H
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, w)
        out = []
        for _ in range(self.TILES):
            raw = os.urandom(w * h)
            data = bytearray(stride * h)
            k = 0
            for y in range(h):
                row = y * stride
                for x in range(w):
                    # Weighted towards dark so the white speckle reads as
                    # sparks on a dim screen rather than a grey wash.
                    v = raw[k] // 2 + (raw[k] > 214) * 120
                    k += 1
                    o = row + x * 4
                    data[o] = data[o + 1] = data[o + 2] = min(255, v)
            out.append(cairo.ImageSurface.create_for_data(
                data, cairo.FORMAT_RGB24, w, h, stride))
        return out

    def _draw(self, area, cr, width, height):
        import cairo
        cr.save()
        cr.scale(width / self.TILE_W, height / self.TILE_H)
        cr.set_source_surface(self._tiles[self._i], 0, 0)
        cr.get_source().set_filter(cairo.FILTER_NEAREST)
        cr.paint()
        cr.restore()
        # Scanlines. Without them it reads as digital noise; the dark gaps
        # are what make it a tube.
        cr.set_source_rgba(0, 0, 0, 0.30)
        y = 0
        while y < height:
            cr.rectangle(0, y, width, 2)
            y += 4
        cr.fill()
        # Vignette, because a CRT is never evenly lit at the corners.
        rg = cairo.RadialGradient(width / 2, height / 2, min(width, height) * 0.25,
                                  width / 2, height / 2, max(width, height) * 0.72)
        rg.add_color_stop_rgba(0, 0, 0, 0, 0.0)
        rg.add_color_stop_rgba(1, 0, 0, 0, 0.65)
        cr.set_source(rg)
        cr.rectangle(0, 0, width, height)
        cr.fill()


class MeterBox(Gtk.DrawingArea):
    """The yellow square a phone camera draws where you tap.

    On this hardware it cannot mean focus: neither camera exposes any focus
    control at all, they are fixed-focus. It means exposure, which they do
    support, so the box marks what is being metered rather than pretending
    to rack a lens that is not there.
    """

    def __init__(self):
        super().__init__()
        self.point = None
        self.scale = 1.0
        self.alpha = 0.0
        self.set_can_target(False)
        self.set_draw_func(self._draw)

    def ping(self, x, y):
        self.point = (x, y)
        self.scale, self.alpha = 1.45, 1.0
        self.queue_draw()
        if getattr(self, "_tick", None):
            GLib.source_remove(self._tick)
        self._tick = GLib.timeout_add(16, self._step)

    def _step(self):
        # Shrink onto the point, hold, then fade. Reads as "locked here".
        if self.scale > 1.0:
            self.scale = max(1.0, self.scale - 0.035)
        else:
            self.alpha -= 0.022
        self.queue_draw()
        if self.alpha <= 0:
            self.point = None
            self._tick = None
            return False
        return True

    def _draw(self, area, cr, W, H):
        if not self.point or self.alpha <= 0:
            return
        x, y = self.point
        half = 42 * self.scale
        cr.set_source_rgba(1.0, 0.78, 0.13, min(1.0, self.alpha))
        cr.set_line_width(2)
        cr.rectangle(x - half, y - half, half * 2, half * 2)
        cr.stroke()
        # Corner ticks, so it reads as a reticle and not a selection.
        t = half * 0.32
        for cx, cy, sx, sy in ((x - half, y - half, 1, 1), (x + half, y - half, -1, 1),
                               (x - half, y + half, 1, -1), (x + half, y + half, -1, -1)):
            cr.move_to(cx, cy + sy * t); cr.line_to(cx, cy); cr.line_to(cx + sx * t, cy)
        cr.set_line_width(3)
        cr.stroke()


class CropOverlay(Gtk.DrawingArea):
    """Crop rectangle drawn over the picture, in widget coordinates.

    Kept separate from the image so the picture itself is never re-rendered
    while dragging: only this layer redraws, which keeps the drag smooth on
    an 8MP photo.
    """

    HANDLE = 11

    def __init__(self, on_change=None):
        super().__init__()
        self.rect = None          # (x, y, w, h) in widget space
        self.frame = None         # letterboxed picture area in widget space
        self.ratio = None         # locked aspect, or None for free
        self.on_change = on_change
        self.active = False
        self._drag_mode = None
        self._start = None
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        self.add_controller(drag)

    # ---- geometry ----
    def set_frame(self, frame):
        """Where the picture actually sits, after letterboxing."""
        changed = frame != self.frame
        self.frame = frame
        if changed and self.active:
            self.reset()

    def reset(self):
        if not self.frame:
            return
        fx, fy, fw, fh = self.frame
        if self.ratio:
            w, h = fw, fw / self.ratio
            if h > fh:
                h, w = fh, fh * self.ratio
        else:
            w, h = fw * 0.8, fh * 0.8
        self.rect = (fx + (fw - w) / 2, fy + (fh - h) / 2, w, h)
        self.queue_draw()
        if self.on_change:
            self.on_change()

    def set_ratio(self, ratio):
        self.ratio = ratio
        self.reset()

    def _hit(self, x, y):
        if not self.rect:
            return None
        rx, ry, rw, rh = self.rect
        h = self.HANDLE
        near_l, near_r = abs(x - rx) < h, abs(x - (rx + rw)) < h
        near_t, near_b = abs(y - ry) < h, abs(y - (ry + rh)) < h
        if near_l and near_t: return "nw"
        if near_r and near_t: return "ne"
        if near_l and near_b: return "sw"
        if near_r and near_b: return "se"
        if near_l: return "w"
        if near_r: return "e"
        if near_t: return "n"
        if near_b: return "s"
        if rx <= x <= rx + rw and ry <= y <= ry + rh: return "move"
        return None

    def _begin(self, g, x, y):
        self._drag_mode = self._hit(x, y)
        self._start = self.rect

    def _update(self, g, dx, dy):
        if not self._drag_mode or not self._start or not self.frame:
            return
        fx, fy, fw, fh = self.frame
        x, y, w, h = self._start
        m = self._drag_mode
        if m == "move":
            x, y = x + dx, y + dy
        else:
            if "w" in m: x, w = x + dx, w - dx
            if "e" in m: w = w + dx
            if "n" in m: y, h = y + dy, h - dy
            if "s" in m: h = h + dy
            if self.ratio:
                # Drive height from width so a locked ratio cannot drift.
                h = w / self.ratio
                if "n" in m:
                    y = self._start[1] + self._start[3] - h
        w, h = max(40, w), max(40, h)
        x = min(max(x, fx), fx + fw - w)
        y = min(max(y, fy), fy + fh - h)
        w = min(w, fx + fw - x)
        h = min(h, fy + fh - y)
        self.rect = (x, y, w, h)
        self.queue_draw()
        if self.on_change:
            self.on_change()

    def _draw(self, area, cr, W, H):
        if not (self.active and self.rect):
            return
        x, y, w, h = self.rect
        # Dim everything outside the crop so the kept area reads clearly.
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.rectangle(0, 0, W, H)
        cr.rectangle(x, y, w, h)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.fill()
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        # Thirds inside the crop, the way every camera crop tool does.
        cr.set_source_rgba(1, 1, 1, 0.28)
        cr.set_line_width(1)
        for i in (1, 2):
            cr.move_to(x + w * i / 3, y); cr.line_to(x + w * i / 3, y + h)
            cr.move_to(x, y + h * i / 3); cr.line_to(x + w, y + h * i / 3)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(2)
        cr.rectangle(x, y, w, h)
        cr.stroke()
        # Corner grips
        g = 18
        cr.set_line_width(4)
        for cx, cy, sx, sy in ((x, y, 1, 1), (x + w, y, -1, 1),
                               (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
            cr.move_to(cx, cy + sy * g); cr.line_to(cx, cy); cr.line_to(cx + sx * g, cy)
        cr.stroke()


class TransformBox(Gtk.Widget):
    """Holds one child and draws it turned, scaled or mirrored about its centre.

    CSS cannot do this job. A transform there is a fixed string, but the
    scale a rotation needs depends on the image's aspect and the space it has
    to turn in, and it changes on every frame of the turn: a landscape photo
    sweeps through its own diagonal at 45 degrees and needs to be smaller
    then than at either end. A fixed scale either overflows mid-turn or
    shrinks the photo more than it ever needed.
    """

    __gtype_name__ = "LensTransformBox"

    def __init__(self, child):
        super().__init__()
        self._child = child
        child.set_parent(self)
        self.angle = 0.0
        self.scale = 1.0
        self.flip_x = 1.0
        self.flip_y = 1.0

    def do_measure(self, orientation, for_size):
        return self._child.measure(orientation, for_size)

    def do_size_allocate(self, width, height, baseline):
        self._child.allocate(width, height, baseline, None)

    def do_snapshot(self, snapshot):
        if (self.angle, self.scale, self.flip_x, self.flip_y) == (0.0, 1.0, 1.0, 1.0):
            self.snapshot_child(self._child, snapshot)
            return
        w, h = self.get_width(), self.get_height()
        mid = Graphene.Point()
        mid.init(w / 2, h / 2)
        back = Graphene.Point()
        back.init(-w / 2, -h / 2)
        snapshot.save()
        snapshot.translate(mid)
        if self.angle:
            snapshot.rotate(self.angle)
        snapshot.scale(self.scale * self.flip_x, self.scale * self.flip_y)
        snapshot.translate(back)
        self.snapshot_child(self._child, snapshot)
        snapshot.restore()

    def do_dispose(self):
        if self._child is not None:
            self._child.unparent()
            self._child = None
        Gtk.Widget.do_dispose(self)


class GalleryView(Gtk.Box):
    """Browse and edit what has been shot: rotate, mirror, crop, save.

    Edits are non-destructive until saved, and saving writes a new file
    rather than overwriting: losing an original to a mis-drag would be a
    poor trade for the convenience.
    """

    THUMB = 74

    def __init__(self, on_close, on_open_external, on_settings=lambda: None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("gal")
        self.on_close = on_close
        self.on_open_external = on_open_external
        self.on_settings = on_settings
        self.in_bin = False
        self._live_paths = []
        self.paths = []
        self.index = 0
        self.src = None
        self.pic_dir = DEF_PICTURES
        self.vid_dir = DEF_VIDEOS            # untouched pixbuf
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False
        self._strip_buttons = []

        # ---- header
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.add_css_class("gal-bar")
        head.set_margin_top(8); head.set_margin_bottom(8)
        head.set_margin_start(12); head.set_margin_end(12)

        back = self._tool("go-previous-symbolic", "Back to camera",
                          lambda: self.on_close())
        head.append(back)

        # EditableLabel: reads as text, turns into an entry on click, and
        # commits on Enter. Renaming is a normal thing to want and hiding it
        # behind a dialog would be worse than showing it in place.
        self.title = Gtk.EditableLabel(text="")
        self.title.add_css_class("gal-title")
        self.title.set_hexpand(True)
        self.title.set_halign(Gtk.Align.FILL)
        self.title.set_margin_start(8)
        self.title.connect("notify::editing", self._on_title_edit)
        self._tame_title()
        head.append(self.title)

        self.counter = Gtk.Label(label="")
        self.counter.add_css_class("gal-count")
        self.counter.set_margin_end(8)
        head.append(self.counter)

        self._edit_tools = []
        for icon, tip, cb in (
            ("object-rotate-left-symbolic",  "Rotate left",  lambda: self._rotate(-90)),
            ("object-rotate-right-symbolic", "Rotate right", lambda: self._rotate(90)),
            ("object-flip-horizontal-symbolic", "Mirror horizontally", lambda: self._flip(True)),
            ("object-flip-vertical-symbolic",   "Mirror vertically",   lambda: self._flip(False)),
        ):
            b = self._tool(icon, tip, cb)
            self._edit_tools.append(b)
            head.append(b)

        self.btn_crop = Gtk.ToggleButton()
        self.btn_crop.set_child(self._icon("edit-cut-symbolic"))
        self.btn_crop.add_css_class("gal-tool")
        self.btn_crop.set_tooltip_text("Crop")
        self.btn_crop.connect("toggled", lambda b: self._set_cropping(b.get_active()))
        self._edit_tools.append(self.btn_crop)
        head.append(self.btn_crop)

        self.ratio_btn = Gtk.MenuButton()
        self.ratio_btn.set_child(Gtk.Label(label="Free"))
        self.ratio_btn.add_css_class("gal-tool")
        self.ratio_btn.set_tooltip_text("Crop ratio")
        self.ratio_pop = Gtk.Popover()
        self.ratio_btn.set_popover(self.ratio_pop)
        self.ratio_btn.set_visible(False)
        self._build_ratio_menu()
        head.append(self.ratio_btn)

        self.btn_bin = Gtk.ToggleButton()
        self.btn_bin.set_child(self._icon("user-trash-full-symbolic"))
        self.btn_bin.add_css_class("gal-tool")
        self.btn_bin.set_tooltip_text("Recycle bin")
        self.btn_bin.connect("toggled", lambda b: self._show_bin(b.get_active()))
        head.append(self.btn_bin)
        head.append(self._tool("emblem-system-symbolic", "Settings",
                               lambda: self.on_settings()))
        head.append(self._tool("folder-symbolic", "Show in Files",
                               self._show_in_files))
        head.append(self._tool("user-trash-symbolic", "Move to trash",
                               self._trash_current))

        self.btn_restore = Gtk.Button(label="Restore")
        self.btn_restore.add_css_class("gal-save")
        self.btn_restore.set_visible(False)
        self.btn_restore.connect("clicked", lambda *_: self._restore_current())
        head.append(self.btn_restore)

        self.btn_save = Gtk.Button(label="Save a copy")
        self.btn_save.add_css_class("gal-save")
        self.btn_save.set_sensitive(False)
        self.btn_save.connect("clicked", lambda *_: self._save())
        head.append(self.btn_save)
        self.append(head)

        # ---- image + crop layer
        stack = Gtk.Overlay()
        stack.set_hexpand(True); stack.set_vexpand(True)
        self.pic = Gtk.Picture()
        self.pic.set_can_shrink(True)
        self.pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.pic.set_hexpand(True); self.pic.set_vexpand(True)
        # An AspectFrame so the picture's box IS the image. Transforming the
        # picture while it spanned the whole stage turned the empty letterbox
        # region along with it, so a rotate visibly swung the black bars
        # around instead of just the photo.
        self.pic_frame = Gtk.AspectFrame(ratio=4 / 3, obey_child=False)
        self.pic_frame.set_xalign(0.5); self.pic_frame.set_yalign(0.5)
        self.pic_frame.set_hexpand(True); self.pic_frame.set_vexpand(True)
        self.tbox = TransformBox(self.pic)
        self.pic_frame.set_child(self.tbox)
        stack.set_child(self.pic_frame)
        # Belt and braces: the fit maths keeps a turning photo inside the
        # stage, but if it ever did reach the edge it must be cut off there
        # rather than drawn over the toolbar.
        stack.set_overflow(Gtk.Overflow.HIDDEN)
        self.stage = stack
        self.crop = CropOverlay(on_change=lambda: None)
        stack.add_overlay(self.crop)

        # Click the left or right third to page through, like a photo viewer.
        page = Gtk.GestureClick.new()
        page.set_button(1)
        page.connect("released", self._on_page_click)
        self.pic.add_controller(page)
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        # Also on the picture itself, which is where the pointer actually is.
        pic_scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES)
        pic_scroll.connect("scroll", self._on_scroll)
        stack.add_controller(pic_scroll)

        # Visible paging targets. Clicking the edge of the photo works, but
        # nothing tells you that, so the arrows say it out loud.
        self.arrow_l = self._arrow("go-previous-symbolic", Gtk.Align.START,
                                   lambda: self.go(self.index - 1))
        self.arrow_r = self._arrow("go-next-symbolic", Gtk.Align.END,
                                   lambda: self.go(self.index + 1))
        stack.add_overlay(self.arrow_l)
        stack.add_overlay(self.arrow_r)

        # Play sits on the clip rather than in the toolbar. Up there it only
        # existed for videos, so it shifted trash sideways every time one came
        # up and you had to re-aim between deletes.
        self.btn_play = Gtk.Button()
        play_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        play_icon.set_pixel_size(46)
        self.btn_play.set_child(play_icon)
        self.btn_play.add_css_class("gal-play")
        self.btn_play.set_halign(Gtk.Align.CENTER)
        self.btn_play.set_valign(Gtk.Align.CENTER)
        self.btn_play.set_tooltip_text("Play this clip")
        self.btn_play.set_visible(False)
        self.btn_play.connect(
            "clicked", lambda *_: self.on_open_external(self.paths[self.index]))
        stack.add_overlay(self.btn_play)

        self.empty = Gtk.Label(label="Nothing here yet")
        self.empty.add_css_class("gal-empty")
        stack.add_overlay(self.empty)
        self.empty.set_visible(False)
        self.append(stack)

        # ---- filmstrip
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self.strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.strip.set_margin_start(10); self.strip.set_margin_end(10)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        sc.set_child(self.strip)
        sc.set_size_request(-1, self.THUMB + 26)
        sc.add_css_class("gal-strip")
        # A vertical wheel over a horizontal strip does nothing by default,
        # so translate it: the wheel moves the strip under the pointer.
        strip_scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES)
        strip_scroll.connect("scroll", self._on_strip_scroll)
        sc.add_controller(strip_scroll)
        self.strip_scroller = sc
        self.append(sc)

    # ---- small builders ----
    @staticmethod
    def _icon(name):
        i = Gtk.Image.new_from_icon_name(name)
        i.set_pixel_size(16)
        return i

    def _tool(self, icon, tip, cb):
        b = Gtk.Button()
        b.set_child(self._icon(icon))
        b.add_css_class("gal-tool")
        b.set_tooltip_text(tip)
        b.connect("clicked", lambda *_: cb())
        return b

    def _build_ratio_menu(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6); box.set_margin_bottom(6)
        box.set_margin_start(6); box.set_margin_end(6)
        for label, r in (("Free", None), ("1:1", 1.0), ("4:3", 4/3),
                         ("3:2", 3/2), ("16:9", 16/9), ("9:16", 9/16)):
            b = Gtk.Button(label=label)
            b.add_css_class("cam-row")
            b.connect("clicked", lambda _b, lb=label, rr=r: self._set_ratio(lb, rr))
            box.append(b)
        self.ratio_pop.set_child(box)

    def _set_ratio(self, label, ratio):
        self.ratio_pop.popdown()
        self.ratio_btn.set_child(Gtk.Label(label=label))
        self.crop.set_ratio(ratio)

    def _show_bin(self, on):
        """Swap the list between what is live and what is in the bin.

        Same view rather than a separate screen: the actions that make sense
        differ, but browsing does not, and a second gallery to maintain would
        drift from this one.
        """
        self.in_bin = on
        if on:
            self._live_paths = list(self.paths)
            items = []
            for f in sorted(trash_dir().glob("*")):
                try:
                    if f.is_file() and f.stat().st_size:
                        items.append((f.stat().st_mtime, f))
                except OSError:
                    pass
            items.sort()
            self.paths = [str(f) for _, f in items]
        else:
            self.paths = self._live_paths
        self.index = 0
        self.btn_restore.set_visible(on)
        self._build_strip()
        self._show()

    def _restore_current(self):
        if not (self.in_bin and self.paths):
            return
        src = pathlib.Path(self.paths[self.index])
        home = self.vid_dir if is_video(src) else self.pic_dir
        dest = home / src.name
        n = 2
        while dest.exists():
            dest = home / f"{src.stem}-{n}{src.suffix}"
            n += 1
        try:
            src.rename(dest)
        except OSError as e:
            print(f"Lens: restore failed: {e}", file=sys.stderr)
            return
        print(f"Lens: restored {dest.name}")
        del self.paths[self.index]
        self.index = max(0, min(self.index, len(self.paths) - 1))
        self._build_strip()
        self._show()

    def _arrow(self, icon, align, cb):
        b = Gtk.Button()
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(30)
        b.set_child(img)
        b.add_css_class("gal-arrow")
        b.set_halign(align)
        b.set_valign(Gtk.Align.CENTER)
        b.set_margin_start(14); b.set_margin_end(14)
        b.connect("clicked", lambda *_: cb())
        return b

    def _on_key(self, ctrl, keyval, keycode, state):
        from gi.repository import Gdk as _Gdk
        if self.title.get_property("editing"):
            return False               # typing a filename, not navigating
        if keyval in (_Gdk.KEY_Left, _Gdk.KEY_Up, _Gdk.KEY_Page_Up):
            self.go(self.index - 1); return True
        if keyval in (_Gdk.KEY_Right, _Gdk.KEY_Down, _Gdk.KEY_Page_Down):
            self.go(self.index + 1); return True
        if keyval == _Gdk.KEY_Home:
            self.go(0); return True
        if keyval == _Gdk.KEY_End:
            self.go(len(self.paths) - 1); return True
        if keyval == _Gdk.KEY_Delete:
            self._trash_current(); return True
        return False

    def _tame_title(self):
        """Stop a long filename shoving the toolbar buttons off the end.

        An EditableLabel asks for the full width of its text, so a long name
        pushed trash and the rest sideways. Its inner label and entry are not
        exposed as properties, so reach them through the tree and cap what
        they ask for -- the name then ellipsises under the controls instead
        of moving them.
        """
        def walk(w):
            c = w.get_first_child()
            while c is not None:
                if isinstance(c, Gtk.Label):
                    c.set_ellipsize(Pango.EllipsizeMode.END)
                    c.set_max_width_chars(1)
                    c.set_xalign(0.0)
                elif isinstance(c, Gtk.Text):
                    c.set_max_width_chars(1)
                    c.set_propagate_text_width(False)
                walk(c)
                c = c.get_next_sibling()
        walk(self.title)

    def _on_title_edit(self, *_):
        """Rename on commit. Editing stops both when confirmed and when
        cancelled, so the text is compared rather than assumed changed."""
        if self.title.get_property("editing") or not self.paths:
            return
        new = (self.title.get_text() or "").strip()
        cur = pathlib.Path(self.paths[self.index])
        if not new or new == cur.name:
            return
        if "/" in new:
            self.title.set_text(cur.name)
            return
        if not pathlib.Path(new).suffix:
            new += cur.suffix          # keep it openable
        dest = cur.with_name(new)
        if dest.exists():
            print(f"Lens: {new} already exists", file=sys.stderr)
            self.title.set_text(cur.name)
            return
        try:
            cur.rename(dest)
        except OSError as e:
            print(f"Lens: rename failed: {e}", file=sys.stderr)
            self.title.set_text(cur.name)
            return
        self.paths[self.index] = str(dest)
        print(f"Lens: renamed to {dest.name}")

    def _show_in_files(self):
        if not self.paths:
            return
        f = pathlib.Path(self.paths[self.index])
        try:
            # Ask the file manager to reveal and select the file. Falls back
            # to opening the folder if nothing implements the interface.
            Gio.DBusConnection.call_sync(
                Gio.bus_get_sync(Gio.BusType.SESSION, None),
                "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1", "ShowItems",
                GLib.Variant("(ass)", ([f.as_uri()], "")),
                None, Gio.DBusCallFlags.NONE, 3000, None)
        except Exception:
            Gio.AppInfo.launch_default_for_uri(f.parent.as_uri(), None)

    def _trash_current(self):
        """Move to Lens's own trash rather than deleting.

        Its own, not the desktop's, because the retention rule is ours: the
        purge on startup has to know when things arrived.
        """
        if not self.paths:
            return
        src = pathlib.Path(self.paths[self.index])
        if self.in_bin:
            # Already in the bin: this is the permanent one.
            try:
                src.unlink()
                print(f"Lens: deleted {src.name}")
            except OSError as e:
                print(f"Lens: delete failed: {e}", file=sys.stderr)
                return
            del self.paths[self.index]
            self._drop_strip_item(self.index)
            self.index = max(0, min(self.index, len(self.paths) - 1))
            self._show()
            return
        dest = trash_dir() / src.name
        n = 2
        while dest.exists():
            dest = trash_dir() / f"{src.stem}-{n}{src.suffix}"
            n += 1
        try:
            trash_dir().mkdir(parents=True, exist_ok=True)
            src.rename(dest)
        except OSError as e:
            print(f"Lens: could not trash {src.name}: {e}", file=sys.stderr)
            return
        print(f"Lens: trashed {src.name}")
        # Keep the live list in step, or leaving the bin would resurrect
        # everything trashed since it was last opened.
        gone = self.paths[self.index]
        if gone in getattr(self, "_live_paths", []):
            self._live_paths.remove(gone)
        del self.paths[self.index]
        self._drop_strip_item(self.index)
        if self.index >= len(self.paths):
            self.index = max(0, len(self.paths) - 1)
        self._show()

    # ---- content ----
    def load(self, paths, start=None):
        """Everything shot, clips included.

        Clips cannot be edited here, but excluding them meant a tap on the
        preview went straight to an external player whenever the newest
        item was a video, and the gallery never opened at all.
        """
        self.paths = [str(p) for p in paths]
        self.index = 0
        if start and str(start) in self.paths:
            self.index = self.paths.index(str(start))
        self._build_strip()
        self._show()

    def _build_strip(self):
        while (c := self.strip.get_first_child()) is not None:
            self.strip.remove(c)
        self._strip_buttons = []
        for i, p in enumerate(self.paths):
            b = Gtk.Button()
            b.add_css_class("gal-thumb")
            img = Gtk.Image()
            thumb_src = video_thumbnail(p) if is_video(p) else p
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(thumb_src), self.THUMB, self.THUMB, True)
                img.set_from_paintable(Gdk.Texture.new_for_pixbuf(pb))
            except Exception:
                img.set_from_icon_name(
                    "video-x-generic-symbolic" if is_video(p)
                    else "image-missing-symbolic")
            img.set_pixel_size(self.THUMB)
            b.set_child(img)
            # Resolved at click time, so removing a row does not leave
            # every later thumbnail pointing at the wrong photo.
            b.connect("clicked", self._on_thumb_clicked)
            self.strip.append(b)
            self._strip_buttons.append(b)

    def _on_thumb_clicked(self, btn):
        try:
            self.go(self._strip_buttons.index(btn))
        except ValueError:
            pass

    def _drop_strip_item(self, i):
        """Remove one thumbnail in place.

        A full rebuild re-decodes every JPEG in the folder and momentarily
        empties the scroller, which left the strip looking blank until the
        gallery was closed and reopened.
        """
        if not (0 <= i < len(self._strip_buttons)):
            return self._build_strip()
        self.strip.remove(self._strip_buttons.pop(i))

    def _scroll_strip_to_current(self):
        """Keep the selected thumbnail on screen."""
        if not (0 <= self.index < len(self._strip_buttons)):
            return False
        btn = self._strip_buttons[self.index]
        ok, r = btn.compute_bounds(self.strip)
        if not ok:
            return False
        adj = self.strip_scroller.get_hadjustment()
        page = adj.get_page_size()
        if page <= 0:
            return False
        if r.origin.x < adj.get_value():
            adj.set_value(r.origin.x)
        elif r.origin.x + r.size.width > adj.get_value() + page:
            adj.set_value(r.origin.x + r.size.width - page)
        return False

    def go(self, i):
        if not self.paths:
            return
        self.index = max(0, min(len(self.paths) - 1, i))
        self._show()

    def _show(self):
        have = bool(self.paths)
        self.empty.set_visible(not have)
        self.btn_crop.set_sensitive(have)
        self.pic.set_visible(have)
        if not have:
            self.title.set_text("")
            self.counter.set_label("")
            self.arrow_l.set_visible(False)
            self.arrow_r.set_visible(False)
            self.pic.set_paintable(None)
            return
        path = self.paths[self.index]
        video = is_video(path)
        # A clip shows its poster frame. Editing is disabled rather than
        # hidden, so the tools stay where they are and simply grey out.
        load_from = video_thumbnail(path) if video else path
        try:
            self.src = (GdkPixbuf.Pixbuf.new_from_file(str(load_from))
                        if load_from else None)
        except Exception as e:
            print(f"Lens: cannot open {path}: {e}", file=sys.stderr)
            self.src = None
        self.btn_play.set_visible(video)
        for b in self._edit_tools:
            b.set_sensitive(not video)
        self.rotation = 0
        self.flip_h = self.flip_v = False
        self.btn_crop.set_active(False)
        self.btn_save.set_sensitive(False)
        self.title.set_text(pathlib.Path(path).name)
        self.counter.set_label(f"{self.index + 1} / {len(self.paths)}")
        self.arrow_l.set_visible(self.index > 0)
        self.arrow_r.set_visible(self.index < len(self.paths) - 1)
        for i, b in enumerate(self._strip_buttons):
            if i == self.index:
                b.add_css_class("gal-thumb-active")
            else:
                b.remove_css_class("gal-thumb-active")
        GLib.idle_add(self._scroll_strip_to_current)
        self._render()

    def _edited(self):
        """Apply rotation and mirroring to the source pixbuf."""
        pb = self.src
        if pb is None:
            return None
        if self.rotation:
            rot = {90: GdkPixbuf.PixbufRotation.CLOCKWISE,
                   180: GdkPixbuf.PixbufRotation.UPSIDEDOWN,
                   270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE}[self.rotation % 360]
            pb = pb.rotate_simple(rot)
        if self.flip_h:
            pb = pb.flip(True)
        if self.flip_v:
            pb = pb.flip(False)
        return pb

    def _render(self):
        pb = self._edited()
        if pb is None:
            return
        self.pic.set_paintable(Gdk.Texture.new_for_pixbuf(pb))
        h = pb.get_height()
        if h:
            self.pic_frame.set_ratio(pb.get_width() / h)
        GLib.idle_add(self._sync_crop_frame)

    def _sync_crop_frame(self):
        """Tell the crop layer where the letterboxed picture actually is."""
        pb = self._edited()
        if pb is None:
            return False
        W, H = self.crop.get_width(), self.crop.get_height()
        iw, ih = pb.get_width(), pb.get_height()
        if W <= 1 or H <= 1 or not iw or not ih:
            return False
        s = min(W / iw, H / ih)
        dw, dh = iw * s, ih * s
        self.crop.set_frame(((W - dw) / 2, (H - dh) / 2, dw, dh))
        return False

    # ---- actions ----
    def _animate(self, cls, then, ms=260):
        """Play a CSS class on the picture, then swap the pixbuf underneath.

        The return leg has to be instant. Dropping the class on its own
        animates the transform back to identity, so a mirror played the flip
        twice and a rotate visibly counter-rotated after turning the right
        way. Suppressing the transition for that one frame -- while the
        already-transformed pixbuf goes in -- makes it read as a single
        continuous move.
        """
        if getattr(self, "_anim_busy", False):
            return
        self._anim_busy = True
        self.pic.add_css_class(cls)

        def done():
            # Swap under a frozen transition, holding the shrink. The picture
            # is already at 0.82 and rotated; putting the turned pixbuf in at
            # the same instant means nothing appears to move.
            self.pic.add_css_class("no-anim")
            self.pic.remove_css_class(cls)
            self.pic.add_css_class("settle")
            then()

            def unfreeze():
                self.pic.remove_css_class("no-anim")
                # Releasing the shrink with the transition live lets it grow
                # into its new shape, which is what sells a 4:3 becoming 3:4.
                self.pic.remove_css_class("settle")
                self._anim_busy = False
                return False
            GLib.timeout_add(30, unfreeze)
            return False
        GLib.timeout_add(ms, done)

    def _fit_scale(self, w, h, deg):
        """Largest scale at which a w x h box, turned by deg, still fits.

        At 0 and 90 this is the scale that makes the turning photo land
        exactly on the box the AspectFrame is about to give it, so the swap
        at the end of the turn moves nothing. In between it follows the
        photo's own diagonal.
        """
        a = math.radians(deg)
        c, sn = abs(math.cos(a)), abs(math.sin(a))
        bw, bh = w * c + h * sn, w * sn + h * c
        sw, sh = self.stage.get_width(), self.stage.get_height()
        if min(bw, bh, sw, sh) <= 0:
            return 1.0
        # Deliberately uncapped. Turning a portrait back to landscape has to
        # grow, because the box it lands in is wider than the one it left,
        # and clamping to 1.0 made the turn stop short and then jump the rest
        # of the way. At rest this is 1.0 anyway: the photo is already fitted
        # to the stage, so one of the two terms is exactly 1.
        return min(sw / bw, sh / bh)

    def _drive(self, ms, step, finish):
        """Run step(0..1) once per frame, then finish(), on the frame clock."""
        if getattr(self, "_anim_busy", False):
            return
        self._anim_busy = True
        state = {"t0": None}

        def tick(widget, clock):
            if state["t0"] is None:
                state["t0"] = clock.get_frame_time()
            t = min(1.0, (clock.get_frame_time() - state["t0"]) / (ms * 1000.0))
            step(t * t * (3.0 - 2.0 * t))       # smoothstep
            if t >= 1.0:
                finish()
                self._anim_busy = False
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE
        self.tbox.add_tick_callback(tick)

    def _rotate(self, deg):
        if self.src is None:
            return
        self.btn_save.set_sensitive(True)
        a = self.pic.get_allocation()
        w, h = a.width or 1, a.height or 1
        tb = self.tbox

        def step(e):
            tb.angle = deg * e
            tb.scale = self._fit_scale(w, h, tb.angle)
            tb.queue_draw()

        def finish():
            # Texture, frame ratio and transform all change in the same tick,
            # before layout and snapshot run, so there is no frame where a
            # turned photo is drawn in an unturned box.
            self.rotation = (self.rotation + deg) % 360
            self._render()
            tb.angle, tb.scale = 0.0, 1.0
            tb.queue_draw()
        self._drive(420, step, finish)

    def _flip(self, horizontal):
        if self.src is None:
            return
        self.btn_save.set_sensitive(True)
        tb = self.tbox
        done = {"swapped": False}

        def step(e):
            # Squash to nothing, turn the picture over at the edge, come back.
            v = abs(1.0 - 2.0 * e)
            if horizontal:
                tb.flip_x = v
            else:
                tb.flip_y = v
            if not done["swapped"] and e >= 0.5:
                done["swapped"] = True
                if horizontal:
                    self.flip_h = not self.flip_h
                else:
                    self.flip_v = not self.flip_v
                self._render()
            tb.queue_draw()

        def finish():
            tb.flip_x = tb.flip_y = 1.0
            tb.queue_draw()
        self._drive(340, step, finish)

    def _set_cropping(self, on):
        self.crop.active = on
        # Shown only while cropping. A ratio control with nothing to apply it
        # to is just a stray "Free" sitting in the toolbar.
        self.ratio_btn.set_visible(on)
        if on:
            self._sync_crop_frame()
            self.crop.reset()
            self.btn_save.set_sensitive(True)
        self.crop.queue_draw()

    def _save(self):
        pb = self._edited()
        if pb is None:
            return
        if self.crop.active and self.crop.rect and self.crop.frame:
            fx, fy, fw, fh = self.crop.frame
            rx, ry, rw, rh = self.crop.rect
            sx, sy = pb.get_width() / fw, pb.get_height() / fh
            x = max(0, int((rx - fx) * sx)); y = max(0, int((ry - fy) * sy))
            w = min(pb.get_width() - x, int(rw * sx))
            h = min(pb.get_height() - y, int(rh * sy))
            if w > 0 and h > 0:
                pb = pb.new_subpixbuf(x, y, w, h)
        src = pathlib.Path(self.paths[self.index])
        out = src.with_name(f"{src.stem}-edit{src.suffix}")
        n = 2
        while out.exists():
            out = src.with_name(f"{src.stem}-edit{n}{src.suffix}")
            n += 1
        try:
            pb.savev(str(out), "jpeg", ["quality"], ["95"])
            print(f"Lens: saved {out.name}")
        except Exception as e:
            print(f"Lens: could not save: {e}", file=sys.stderr)
            return
        # Show the result. Saving silently and leaving the original on screen
        # is why this looked like it had done nothing. The dim-and-drop plays
        # first so the crop reads as the offcut falling away.
        self.paths.insert(self.index + 1, str(out))
        self._build_strip()
        if self.crop.active:
            self.crop.active = False
            self.crop.queue_draw()
            self.btn_crop.set_active(False)
            self._animate("crop-drop", lambda: self.go(self.index + 1), ms=300)
        else:
            self.go(self.index + 1)

    # ---- navigation ----
    def _on_page_click(self, gesture, n_press, x, y):
        w = self.pic.get_width() or 1
        if x < w * 0.28:
            self.go(self.index - 1)
        elif x > w * 0.72:
            self.go(self.index + 1)

    def _on_strip_scroll(self, ctrl, dx, dy):
        adj = self.strip_scroller.get_hadjustment()
        d = dx if abs(dx) > abs(dy) else dy
        adj.set_value(adj.get_value() + d * 90)
        return True

    def _on_scroll(self, ctrl, dx, dy):
        # Either axis pages. A wheel gives dy, a touchpad swipe gives dx, and
        # both mean the same thing here.
        d = dx if abs(dx) > abs(dy) else dy
        if d:
            self.go(self.index + (1 if d > 0 else -1))
        return True


class LensWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Lens")
        # Landscape default — works well on laptops. Resizable to portrait for tablets.
        self.set_default_size(960, 640)
        self.set_size_request(320, 400)   # allow shrinking down to phone-ish sizes
        self.set_resizable(True)
        self.pipeline = None
        self.paintable = None
        self.cameras = list_v4l2_cameras() or [("/dev/video0", "Camera", "fallback")]
        print(f"Lens: detected {len(self.cameras)} camera(s):")
        for c in self.cameras: print(f"   {c}")
        self.recording = False
        self._stopping = False
        self._eos_handler = None
        self._eos_timeout = None
        self._saving_timer = None
        self.aspects = ["4:3", "16:9", "1:1"]

        # Restore what was set last time. Loaded before the pipeline starts,
        # since cam_idx decides which device it opens.
        cfg = load_settings()
        self.aspect_idx = cfg.get("aspect_idx", 0)
        if not 0 <= self.aspect_idx < len(self.aspects):
            self.aspect_idx = 0
        self.pic_dir = pathlib.Path(cfg.get("pic_dir", str(DEF_PICTURES))).expanduser()
        self.vid_dir = pathlib.Path(cfg.get("vid_dir", str(DEF_VIDEOS))).expanduser()
        self.name_prefix = cfg.get("name_prefix", "Lens") or "Lens"
        self.trash_days = int(cfg.get("trash_days", 30))
        # Both of these decide where the bin is and whether to empty it, so
        # they have to land before the purge below runs.
        self.trash_auto = bool(cfg.get("trash_auto", True))
        self.trash_path = cfg.get("trash_path") or ""
        set_trash_dir(self.trash_path)
        for d in (self.pic_dir, self.vid_dir, trash_dir()):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"Lens: cannot create {d}: {e}", file=sys.stderr)
        purge_trash(self.trash_days if self.trash_auto else 0)
        self.container = cfg.get("container", "MKV")
        if self.container not in CONTAINERS:
            self.container = "MKV"
        # (w, h, fps) the user pinned, or None to auto-pick
        self.mode_override = cfg.get("mode_override")
        self.mic_source = cfg.get("mic_source")     # None = system default
        self.flash_enabled = bool(cfg.get("flash", False))
        self.zoom = float(cfg.get("zoom", 1.0))
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._zoom_syncing = False
        self.filter_name = cfg.get("filter", "None")
        if self.filter_name not in FILTERS:
            self.filter_name = "None"
        self.mic_available = has_microphone()
        self.mic_enabled = bool(cfg.get("mic_enabled", True))
        self.mic_gain = float(cfg.get("mic_gain", 1.0))
        self.mic_rate = int(cfg.get("mic_rate", 0)) or 0   # 0 = let it choose
        # Deliberately off, as opposed to missing. A machine with one
        # camera, or none, is normal, and so is not wanting it on.
        self.camera_off = bool(cfg.get("camera_off", False))
        # UVC cameras will quietly drop to a fraction of their rated frame
        # rate to hold the shutter open longer in dim light. Measured on this
        # machine in a normally lit room: 7.4fps with it on, 29.9 with it off.
        # A viewfinder at 7fps looks broken, so smooth is the default and the
        # trade is offered rather than hidden.
        self.smooth_motion = bool(cfg.get("smooth_motion", True))
        self.denoise = bool(cfg.get("denoise", True))
        self.nr_mix = float(cfg.get("nr_mix", 0.05))
        self.mirror_view = bool(cfg.get("mirror_view", True))
        self.mirror_saved = bool(cfg.get("mirror_saved", False))
        self.exposure_auto = True
        self.exposure_val = None
        self._exp_range = None
        self._exp_range_for = None
        self._exp_dragging = False
        self._exp_start = None
        self._exp_ref = None
        self._pan_last = (0.0, 0.0)
        self._exp_snapping = False
        self.mic_active = False
        self._audio_mon = None
        self._audio_mon_handler = None
        self._hud_items = []
        self._hud_hidden = set()
        self.cur_res = None
        self._fps_count = 0
        self._fps_shown = 0
        self._frames_total = 0
        self._no_signal_on = False
        self._wd_idle = 0
        self._wd_last = -1
        self.overlay_mode = int(cfg.get("overlay_mode", 0)) % 4
        self.grid_visible = self.overlay_mode == 1
        # How long each camera takes to deliver its first frame, keyed by
        # stable id. Measured on use and persisted, so the flip is tuned from
        # the second run onwards. The Z13 rear camera needs ~690ms and the
        # front ~200ms, and it is a fixed firmware cost: every resolution the
        # rear offers takes the same time, so there is nothing to optimise
        # away. The animation is stretched to cover it instead.
        self._cam_latency = dict(cfg.get("cam_latency", {}))
        self._flip_t0 = None
        self.timer_sec = cfg.get("timer_sec", 0)
        if self.timer_sec not in (0, 3, 10):
            self.timer_sec = 0
        self.video_mode = bool(cfg.get("video_mode", False))
        # Resolve the saved camera by its stable id, not by position. USB
        # enumeration order is not stable across boots or replugs, so a saved
        # index could silently select a different camera. Falls back to the
        # first camera when the saved one is not present.
        self.cam_idx = 0
        saved = cfg.get("cam_id")
        if saved:
            for i, cam in enumerate(self.cameras):
                if cam[2] == saved:
                    self.cam_idx = i
                    break
            else:
                print(f"Lens: saved camera {saved} not present, using "
                      f"{self.cameras[0][1]}")
        self.last_photo = None
        self.countdown_val = 0

        # Nothing to release beyond our own pipelines now that denoising is
        # in-pipeline rather than a system module, but shutting the capture
        # down cleanly still matters: an open pulsesrc keeps the system
        # microphone indicator lit for as long as the process lives.
        self.connect("close-request", self._on_close)
        import signal as _sig
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, _sig.SIGTERM, self._on_signal)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, _sig.SIGINT, self._on_signal)
        self._build_ui()
        self._install_breakpoints()
        self._apply_settings()
        self._start_pipeline(defer_attach=True)
        # Watches for frames stopping, whatever the cause: no camera,
        # a device another process is holding, or one unplugged while
        # running. All of those used to look like a black rectangle.
        self._wd_timer = GLib.timeout_add_seconds(1, self._signal_watchdog)

    # ---------- pipeline ----------
    def _start_pipeline(self, defer_attach=False, on_ready=None):
        if getattr(self, "camera_off", False):
            # Switched off by choice, which is not the same as missing. Tear
            # down whatever is running and leave the static up rather than
            # reopening a device the user has said they do not want opened.
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline.get_state(2 * Gst.SECOND)
                self.pipeline = None
                self.paintable = None
            self._set_no_signal(True, "camera switched off")
            self._refresh_flip_button()
            return
        # Always tear the old one down first. Running two pipelines and
        # swapping paintables out from under a live Picture segfaulted on
        # rapid flips (gdk_device_get_n_axes assertion, then SIGSEGV). The
        # freeze frame is what covers the gap now, so nothing is referencing
        # a paintable while it is being destroyed.
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            # Bounded. CLOCK_TIME_NONE waits forever, and a live source that
            # will not shut down takes the whole UI with it: this is what
            # made switching cameras hang.
            self.pipeline.get_state(2 * Gst.SECOND)
            self.pipeline = None
            self.paintable = None

        # Set before the caps are negotiated: the sensor decides its rate
        # when the stream starts, so changing this afterwards has no effect
        # until the next start.
        self._apply_frame_rate_policy()
        dev = self.cameras[self.cam_idx][0]
        src = self._source_for(dev)
        # Preview branch + photo-capture branch, both fed by the same v4l2src via tee.
        # The photo branch produces encoded JPEG buffers on the appsink,
        # and we pull the latest one when the shutter is pressed.
        # Let the camera pick its native resolution — forcing 1280x720 stretched
        # 4:3 sensors (like the Z13 rear camera at 2592x1944) into 16:9.
        # videocrop trims the edges and the sink scales what is left back up:
        # digital zoom, adjustable live without rebuilding the pipeline.
        pipe_str = (
            f"{src} ! videocrop name=zoom ! videobalance name=vb ! "
            f"coloreffects name=fx ! tee name=t "
            # Mirroring goes after the tee, on the display branch alone. A
            # front camera shown unmirrored feels wrong because you move one
            # way and your image moves the other, but the file should still
            # come out the way the room actually was: mirrored, any writing
            # in shot reads backwards.
            f"t. ! queue max-size-buffers=2 leaky=downstream ! videoflip name=mirror ! "
            f"gtk4paintablesink name=sink "
            f"t. ! queue max-size-buffers=2 leaky=downstream ! videoflip name=savemirror ! "
            f"jpegenc quality=92 ! "
            f"appsink name=photosink emit-signals=false max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(pipe_str)
        sink = self.pipeline.get_by_name("sink")
        self.zoom_elem = self.pipeline.get_by_name("zoom")
        self.vb_elem = self.pipeline.get_by_name("vb")
        self.fx_elem = self.pipeline.get_by_name("fx")
        self._apply_zoom()
        self._apply_filter()
        self._apply_mirror()
        new_paintable = sink.props.paintable
        self.paintable = new_paintable

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_err)

        # Count delivered frames so the HUD can show the rate actually being
        # displayed, not the rate the caps asked for.
        self._fps_count = 0
        new_paintable.connect("invalidate-contents", self._on_frame)

        if defer_attach:
            # A gtk4paintablesink paintable has no content and zero intrinsic
            # size until its first frame lands, so attaching it early blanks
            # the view and collapses the aspect frame. Whatever is on screen
            # (the freeze frame) stays until the camera is actually producing.
            state = {"swapped": False}

            def _commit():
                self.picture.set_paintable(new_paintable)

            def _swap(*_):
                if state["swapped"]:
                    return False
                state["swapped"] = True
                if state.get("handler"):
                    try:
                        new_paintable.disconnect(state["handler"])
                    except Exception:
                        pass
                if on_ready:
                    on_ready(_commit)
                else:
                    _commit()
                return False

            state["handler"] = new_paintable.connect("invalidate-contents", _swap)
            GLib.timeout_add(1200, _swap)   # fallback if no frame ever comes
        else:
            self.picture.set_paintable(new_paintable)
            if on_ready:
                on_ready(None)

        self.pipeline.set_state(Gst.State.PLAYING)

    def effective_res(self):
        """Resolution after the aspect crop, which is what gets saved.

        The sensor mode is 1600x1200, but in 1:1 the frame you see and the
        file you get are 1200x1200. Reporting the sensor size there was
        simply wrong.
        """
        if getattr(self, 'camera_off', False):
            # Switched off by choice. Tear down whatever is running and
            # leave the static up rather than reopening the device.
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
            self._set_no_signal(True, 'camera switched off')
            return
        if not self.cur_res:
            return None
        w, h = self.cur_res
        target = {"4:3": 4 / 3, "16:9": 16 / 9, "1:1": 1.0}[self.aspects[self.aspect_idx]]
        if w / h > target:
            return int(h * target), h
        return w, int(w / target)

    def _on_meter_tap(self, gesture, n_press, x, y):
        """Meter for what was tapped, unless the finger moved."""
        if getattr(self, "_exp_dragging", False):
            return          # that was an exposure drag, not a tap
        self.meter_box.ping(x, y)
        self._meter_at(x, y)

    # ---- manual exposure ----
    def _v4l2(self, *args):
        import subprocess
        dev = self.cameras[self.cam_idx][0]
        try:
            r = subprocess.run(["v4l2-ctl", "-d", dev] + list(args),
                               capture_output=True, text=True, timeout=1.5)
            return r.stdout
        except Exception:
            return ""

    def _exposure_range(self):
        """min/max for this camera, since the two differ (1-5000 vs 50-10000)."""
        dev = self.cameras[self.cam_idx][0]
        if self._exp_range_for == dev and self._exp_range:
            return self._exp_range
        out = self._v4l2("--list-ctrls")
        lo, hi = 1, 5000
        for line in out.splitlines():
            if "exposure_time_absolute" in line:
                for tok in line.split():
                    if tok.startswith("min="):
                        lo = int(tok[4:])
                    elif tok.startswith("max="):
                        hi = int(tok[4:])
        self._exp_range, self._exp_range_for = (lo, hi), dev
        return self._exp_range

    def _current_exposure(self):
        out = self._v4l2("-C", "exposure_time_absolute")
        try:
            return int(out.split(":")[1])
        except Exception:
            lo, hi = self._exposure_range()
            return (lo + hi) // 8

    def _on_exp_slider(self, sc):
        """Centre hands control back, anything else takes it."""
        ev = sc.get_value()
        if abs(ev) < 0.05:
            if not self.exposure_auto:
                self._exposure_to_auto()
            return
        if self._exp_ref is None:
            # Whatever the camera had settled on is zero stops. Anchored once,
            # so later moves read against the same place rather than the
            # scale shifting under you.
            self._exp_ref = self._current_exposure()
        lo, hi = self._exposure_range()
        want = self._exp_ref * (2.0 ** ev)
        val = int(max(lo, min(hi, want)))
        self._set_exposure(val)
        # The sensor runs out before the slider does. When it does, put the
        # handle where the camera actually ended up rather than leaving it
        # somewhere that claims two stops it never gave you.
        got = self._exposure_ev()
        if abs(got - ev) > 0.05 and not self._exp_snapping:
            self._exp_snapping = True
            sc.set_value(got)
            self._exp_snapping = False

    def _on_exp_begin(self, gesture, x, y):
        self._exp_dragging = False
        self._pan_last = (0.0, 0.0)

    def _on_exp_update(self, gesture, dx, dy):
        """Drag the zoomed view around. Does nothing at 1x, where there is
        nothing off screen to bring into it."""
        if self.zoom <= 1.001:
            return
        if not self._exp_dragging and abs(dx) < 8 and abs(dy) < 8:
            return                      # still could be a tap
        if not self._exp_dragging:
            self._refresh_pan_cursor(grabbing=True)
        self._exp_dragging = True
        lx, ly = self._pan_last
        self._pan_by(dx - lx, dy - ly)
        self._pan_last = (dx, dy)

    def _on_exp_end(self, gesture, dx, dy):
        self._refresh_pan_cursor()
        GLib.timeout_add(60, lambda: (setattr(self, "_exp_dragging", False), False)[1])

    def _set_exposure(self, val):
        self._v4l2("-c", "auto_exposure=1", "-c", f"exposure_time_absolute={val}")
        self.exposure_auto = False
        self.exposure_val = val
        # Stops away from what the camera chose, not a shutter fraction. 1/18
        # is a true reading of the sensor but it answers a question nobody
        # asked; what you want to know while dragging is how much brighter
        # than normal you have made it.
        self.exp_label.set_label(f"{self._exposure_ev():+.1f} EV")
        self._refresh_exposure_button()

    def _exposure_ev(self):
        ref = self._exp_ref or self.exposure_val or 1
        return math.log2(max(self.exposure_val or 1, 1) / max(ref, 1))

    def _exposure_to_auto(self, *_):
        self._v4l2("-c", "auto_exposure=3")
        self.exposure_auto = True
        self.exposure_val = None
        self._exp_ref = None
        self.exp_label.set_visible(False)
        self._refresh_exposure_button()

    def _refresh_exposure_button(self):
        b = getattr(self, "btn_exp", None)
        if b is None:
            return
        # The value itself is the state. "AE" against "MAN" was two words that
        # look alike at a glance, and the colour alone was too quiet to read.
        if self.exposure_auto:
            b.set_label("AE")
            b.remove_css_class("pill-active")
            if getattr(self, "exp_slider", None) is not None and \
                    abs(self.exp_slider.get_value()) > 0.05:
                self.exp_slider.set_value(0.0)
            b.set_tooltip_text("Exposure is automatic. Drag up or down on the "
                               "picture to set it by hand")
        else:
            b.set_label(f"{self._exposure_ev():+.1f}")
            b.add_css_class("pill-active")
            b.set_tooltip_text(
                f"Exposure held {self._exposure_ev():+.1f} stops from auto "
                f"(1/{max(1, int(10000 / max(self.exposure_val or 1, 1)))}s). "
                f"Click for automatic")

    def _meter_at(self, x, y):
        """Re-meter for what was tapped.

        The camera meters the whole frame, so a backlit subject comes out a
        silhouette. Toggling auto-exposure off and back on makes it re-run
        its own metering for the scene as it is now, which is what actually
        fixes that. Honest about the limit: this is a re-meter, not a spot
        meter, because these cameras expose no region-of-interest control
        that works.
        """
        dev = self.cameras[self.cam_idx][0]
        import subprocess
        for val in ("1", "3"):
            try:
                subprocess.run(["v4l2-ctl", "-d", dev, "-c", f"auto_exposure={val}"],
                               capture_output=True, timeout=1)
            except Exception:
                return

    def _apply_frame_rate_policy(self):
        """Let the sensor slow down for light, or hold it to its rated rate.

        exposure_dynamic_framerate is the UVC control for this. It is on by
        default on both cameras here, which is why the preview sat at 7fps
        in a dim room while every resolution and both devices reported the
        same number: the cap is exposure, not bandwidth.
        """
        if not self.cameras or self.camera_off:
            return
        dev = self.cameras[self.cam_idx][0]
        want = "0" if self.smooth_motion else "1"
        try:
            subprocess.run(["v4l2-ctl", "-d", dev, "-c",
                            f"exposure_dynamic_framerate={want}"],
                           capture_output=True, timeout=1.5)
        except Exception:
            pass

    def _apply_mirror(self):
        """Point the flip elements at the current setting.

        method is live, so this costs nothing and needs no rebuild.
        """
        for name, on in (("mirror", self.mirror_view),
                         ("savemirror", self.mirror_view and self.mirror_saved),
                         ("recmirror", self.mirror_view and self.mirror_saved)):
            el = self.pipeline.get_by_name(name) if self.pipeline else None
            if el is not None:
                el.set_property("method", 4 if on else 0)   # 4 = horizontal-flip

    def _apply_filter(self):
        """Set the look. Live properties, so switching costs nothing."""
        if not getattr(self, "vb_elem", None):
            return
        props, preset = FILTERS.get(self.filter_name, FILTERS["None"])
        # Reset to neutral first: presets set different subsets, and leftovers
        # from the previous one would compound.
        for k, v in (("brightness", 0.0), ("contrast", 1.0),
                     ("saturation", 1.0), ("hue", 0.0)):
            self.vb_elem.set_property(k, props.get(k, v))
        if self.fx_elem:
            self.fx_elem.set_property("preset", preset)

    def set_filter(self, name):
        self.filter_name = name if name in FILTERS else "None"
        self._apply_filter()
        self.btn_fx.set_label(self.filter_name if self.filter_name != "None" else "FX")
        if self.filter_name == "None":
            self.btn_fx.remove_css_class("pill-active")
        else:
            self.btn_fx.add_css_class("pill-active")
        self._save_settings()

    def _apply_zoom(self):
        """Crop to the current zoom factor, offset by where you have panned.

        Zooming used to crop dead centre, which is the one place you often do
        not want when the interesting part is off to one side.
        """
        if not getattr(self, "zoom_elem", None) or not self.cur_res:
            return
        w, h = self.cur_res
        z = max(1.0, min(4.0, self.zoom))
        mx = int(w - w / z)          # total pixels there are to crop
        my = int(h - h / z)
        # pan runs -1..1, so 0 keeps the old centred behaviour and +-1 pushes
        # the window right up against an edge.
        left = int(mx * (1.0 + self.pan_x) / 2)
        top = int(my * (1.0 + self.pan_y) / 2)
        left = max(0, min(mx, left)); top = max(0, min(my, top))
        # Keep them even: odd crops upset some colour formats.
        left -= left % 2
        top -= top % 2
        for prop, val in (("left", left), ("right", max(0, mx - left)),
                          ("top", top), ("bottom", max(0, my - top))):
            self.zoom_elem.set_property(prop, val)

    def _on_zoom_slider(self, sc):
        """Ignore the value-changed we caused ourselves."""
        if self._zoom_syncing:
            return
        self.set_zoom(sc.get_value())

    def _refresh_pan_cursor(self, grabbing=False):
        """Open hand when there is something to drag, closed while dragging.

        The pointer is the only thing that says a zoomed picture can be moved
        at all.
        """
        if self.zoom <= 1.001:
            name = "default"
        else:
            name = "grabbing" if grabbing else "grab"
        try:
            self.frame_stack.set_cursor(Gdk.Cursor.new_from_name(name, None))
        except Exception:
            pass

    def _pan_by(self, dx, dy):
        """Shift the zoom window by a drag measured in widget pixels."""
        if self.zoom <= 1.001 or not self.cur_res:
            return
        w, h = self.cur_res
        z = self.zoom
        mx, my = w - w / z, h - h / z
        vw = max(1, self.frame_stack.get_width())
        vh = max(1, self.frame_stack.get_height())
        if mx > 0:
            # Drag right, the picture follows your finger, so the crop window
            # moves left. Mirrored, that reverses: the flip happens after the
            # crop, so moving the window right makes the image travel right on
            # screen rather than left.
            sign = -1.0 if self.mirror_view else 1.0
            self.pan_x -= sign * 2.0 * (dx * (w / z) / vw) / mx
        if my > 0:
            self.pan_y -= 2.0 * (dy * (h / z) / vh) / my
        self.pan_x = max(-1.0, min(1.0, self.pan_x))
        self.pan_y = max(-1.0, min(1.0, self.pan_y))
        self._apply_zoom()

    def set_zoom(self, z):
        was = self.zoom
        self.zoom = max(1.0, min(4.0, z))
        if self.zoom <= 1.001 and was > 1.001:
            self.pan_x = self.pan_y = 0.0   # nothing off screen left to find
        self._apply_zoom()
        self._refresh_pan_cursor()
        self.zoom_label.set_label(f"{self.zoom:.1f}x")
        # The floating chip is for the moment you are turning the wheel; the
        # bar is always there, so it does not need to double up.
        self.zoom_label.set_visible(False)
        # The slider is one of several ways in (pinch, wheel, the buttons),
        # so it follows the value rather than owning it. This sync is
        # unconditional on purpose: the flag belongs to the slider's own
        # callback, and testing it here meant any zoom that arrived while a
        # sync was in flight left the handle behind. That is how the wheel
        # ended up showing 1.0x on a picture that was really at 2.4x.
        sl = getattr(self, "zoom_slider", None)
        if sl is not None and abs(sl.get_value() - self.zoom) > 1e-4:
            self._zoom_syncing = True
            sl.set_value(self.zoom)
            self._zoom_syncing = False
        if getattr(self, "zoom_reset", None) is not None:
            self.zoom_reset.set_label(f"{self.zoom:.1f}x")
            if self.zoom > 1.001:
                self.zoom_reset.add_css_class("pill-active")
            else:
                self.zoom_reset.remove_css_class("pill-active")
        self._save_settings()

    def _open_camera_menu(self, *_):
        """Which cameras exist, right now rather than at startup.

        A camera that was unplugged, or one another process had taken and
        has since released, is the usual reason for being on this screen,
        so the list is re-read on every open.
        """
        found = list_v4l2_cameras()
        box = self._menu_box()
        box.append(self._menu_head("CAMERA"))
        box.append(self._menu_row("No camera", self.camera_off,
                                  self._camera_none))
        box.append(Gtk.Separator())
        if not found:
            row = Gtk.Label(label="No cameras detected")
            row.add_css_class("menu-dim")
            row.set_margin_start(12); row.set_margin_end(12)
            row.set_margin_top(6); row.set_margin_bottom(6)
            box.append(row)
        else:
            for i, (path, name, _sid) in enumerate(found):
                box.append(self._menu_row(
                    f"{name}  ({path})",
                    (not self.camera_off) and i == self.cam_idx,
                    lambda n=i: self._pick_camera(n)))
        box.append(Gtk.Separator())
        box.append(self._menu_row("Look again", False, self._rescan_cameras,
                                  icon="view-refresh-symbolic"))
        self.camera_popover.set_child(box)
        ok, r = self.btn_no_signal.compute_bounds(self.frame_stack)
        if ok:
            self.camera_popover.set_pointing_to(
                Gdk.Rectangle(x=int(r.origin.x), y=int(r.origin.y),
                              width=int(r.size.width), height=int(r.size.height)))
        self.camera_popover.popup()

    def _camera_none(self):
        """Turn the camera off on purpose.

        Distinct from having none: the static says the same thing either way,
        but this survives a restart and stops anything trying to reopen a
        device the user has said they do not want opened.
        """
        self.camera_popover.popdown()
        self.camera_off = True
        self._save_settings()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        self._set_no_signal(True, "camera switched off")
        self._refresh_flip_button()

    def _pick_camera(self, idx):
        self.camera_popover.popdown()
        found = list_v4l2_cameras()
        if not found:
            return
        self.camera_off = False
        self._save_settings()
        self.cameras = found
        self.cam_idx = max(0, min(idx, len(found) - 1))
        self._save_settings()
        self._refresh_flip_button()
        self._start_pipeline()

    def _rescan_cameras(self):
        self.camera_popover.popdown()
        found = list_v4l2_cameras()
        if found:
            self.cameras = found
            self.cam_idx = min(self.cam_idx, len(found) - 1)
        self._start_pipeline()

    def _refresh_flip_button(self):
        """Only offer the flip when there is something to flip to.

        A machine with one camera, or none, is ordinary; a button that
        cycles a list of one is just a button that appears to do nothing.
        """
        b = getattr(self, "btn_flip", None)
        if b is None:
            return
        # Always present and always sensitive. Hiding it when there was
        # nothing to flip to also hid the only way of reaching the camera
        # menu, which is where 'no camera' lives. With fewer than two
        # cameras a tap opens the menu rather than cycling a list of one.
        b.set_visible(True)
        b.set_sensitive(True)
        many = len(self.cameras) > 1 and not self.camera_off
        b.set_tooltip_text("Switch camera (hold to choose)" if many
                           else "Choose a camera")

    def _set_no_signal(self, on, why=""):
        if bool(on) == bool(getattr(self, "_no_signal_on", False)):
            return
        self._no_signal_on = bool(on)
        self.no_signal.set_visible(on)
        self.btn_no_signal.set_visible(on)
        if on and why:
            print(f"Lens: no signal ({why})", file=sys.stderr)

    def _signal_watchdog(self):
        """Static whenever frames have stopped arriving.

        Counting frames rather than trusting the pipeline state: a v4l2
        source another process already holds will happily report PLAYING
        and then never produce anything, which is exactly the case that
        used to leave a black rectangle and no explanation.
        """
        if self.viewer.get_visible():
            return True
        if getattr(self, "camera_off", False):
            # Switched off deliberately. The idle counter is still winding up
            # from when frames were arriving, so without this the watchdog
            # spends its first three seconds concluding all is well and
            # switching the static back off underneath the user's choice.
            self._set_no_signal(True)
            return True
        seen = self._frames_total
        moved = seen != getattr(self, "_wd_last", -1)
        self._wd_last = seen
        if moved:
            self._wd_idle = 0
        else:
            self._wd_idle = getattr(self, "_wd_idle", 0) + 1
        # Three quiet seconds. Long enough to survive a camera swap or the
        # pipeline rebuild that recording does, short enough to explain
        # itself before anyone reaches for the mouse.
        self._set_no_signal(self._wd_idle >= 3,
                            "no frames" if self._wd_idle >= 3 else "")
        return True

    def _on_frame(self, *_):
        self._frames_total = getattr(self, "_frames_total", 0) + 1
        self._fps_count += 1

    def _freeze_frame(self):
        """Render the live viewfinder into a still texture.

        A GdkTexture is itself a paintable with a real intrinsic size, so
        holding one in the Picture keeps both the image and the layout stable
        while the camera is swapped underneath.
        """
        p = self.paintable
        if p is None:
            return None
        try:
            w = int(p.get_intrinsic_width()) or self.picture.get_width()
            h = int(p.get_intrinsic_height()) or self.picture.get_height()
            if w <= 0 or h <= 0:
                return None
            snap = Gtk.Snapshot()
            p.snapshot(snap, w, h)
            node = snap.to_node()
            native = self.get_native()
            renderer = native.get_renderer() if native else None
            if node is None or renderer is None:
                return None
            return renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
        except Exception as e:
            print(f"Lens: freeze frame failed: {e}", file=sys.stderr)
            return None

    def _source_for(self, dev):
        """v4l2src plus the caps needed to actually get a usable framerate."""
        mode = None
        if self.mode_override:
            ow, oh, ofps = self.mode_override
            for m in _enumerate_modes(dev):
                if (m[1], m[2]) == (ow, oh) and abs(m[3] - ofps) < 0.5:
                    mode = m
                    break
        mode = mode or best_mode(dev)
        if not mode:
            self.cur_res = None
            return f"v4l2src device={dev} ! videoconvert"
        fourcc, w, h, fps = mode
        self.cur_res = (w, h)
        print(f"Lens: {dev} -> {fourcc} {w}x{h} @ {fps:g}fps")
        rate = int(round(fps))
        if fourcc == "MJPG":
            return (f"v4l2src device={dev} ! "
                    f"image/jpeg,width={w},height={h},framerate={rate}/1 ! "
                    f"jpegdec ! videoconvert")
        return (f"v4l2src device={dev} ! "
                f"video/x-raw,width={w},height={h},framerate={rate}/1 ! videoconvert")

    def _on_bus_err(self, bus, msg):
        err, dbg = msg.parse_error()
        print("gst error:", err, dbg, file=sys.stderr)

    # ---------- UI ----------
    def _build_ui(self):
        # Root overlay only carries things that must cover the whole window
        # (capture flash, full-screen viewer). Everything else lives in a
        # vertical split so the controls sit below the picture rather than on
        # top of it.
        root = Gtk.Overlay()
        self.set_content(root)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_child(column)
        self._column = column

        # The viewfinder and the things that genuinely belong over the image.
        # Deliberately NOT a WindowHandle: the top bar is the only thing that
        # moves the window, so dragging on the picture cannot shove the window
        # around by accident.
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True); overlay.set_hexpand(True)
        column.append(overlay)

        # Black background + viewfinder
        bg = Gtk.Box()
        bg.set_hexpand(True); bg.set_vexpand(True)
        bg.add_css_class("bg-black")
        overlay.set_child(bg)

        # Wrap the Picture in an AspectFrame so the visible viewport can be
        # constrained to the chosen aspect ratio (4:3 / 16:9 / 1:1).
        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.COVER)  # fill + crop
        # A transparent stand-in, so the Picture has an intrinsic size from
        # the moment the window opens. With no paintable at all it reports
        # nothing, the aspect frame collapses, and the overlay is squashed
        # into a band until the camera delivers its first frame about a
        # second later. Same fault as the record swap, at startup.
        _ph = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 64, 48)
        _ph.fill(0x00000000)
        self._placeholder = Gdk.Texture.new_for_pixbuf(_ph)
        self.picture.set_paintable(self._placeholder)
        self.picture.set_hexpand(True); self.picture.set_vexpand(True)

        # Grid lives inside the aspect frame, over the picture only. On the
        # window overlay it drew across the letterbox bars too, where a
        # rule-of-thirds guide means nothing.
        self.grid_widget = _GridOverlay()
        self.grid_widget.set_visible(False)

        self.no_signal = NoSignal()
        self.no_signal.set_visible(False)
        self.no_signal.set_can_target(False)     # the banner takes the click
        self.btn_no_signal = Gtk.Button(label="NO SIGNAL")
        self.btn_no_signal.add_css_class("nosignal")
        self.btn_no_signal.set_halign(Gtk.Align.CENTER)
        self.btn_no_signal.set_valign(Gtk.Align.CENTER)
        self.btn_no_signal.set_visible(False)
        self.btn_no_signal.set_tooltip_text("Click to choose a camera")
        self.btn_no_signal.connect("clicked", self._open_camera_menu)
        self.camera_popover = Gtk.Popover()
        self.frame_stack = Gtk.Overlay()
        # Nothing about the viewfinder's size may depend on the video. The
        # measured child is an empty box that expands and asks for nothing,
        # so the aspect frame sizes purely from the space it is given and
        # the ratio, and the picture rides along as an overlay.
        #
        # Every viewfinder collapse in this project came from the opposite
        # arrangement: a Picture whose natural size is the paintable's, and
        # a paintable that reports nothing until its first frame. That is
        # one bug, and it appeared at startup, on starting a recording, on
        # stopping one, and again the moment a placeholder smaller than the
        # window was used to paper over it. Patching each site individually
        # kept missing the next one.
        #
        # An earlier attempt at this left the child with no expand set, so
        # it measured zero and stayed zero. The expand flags are the whole
        # trick.
        self._stage_spacer = Gtk.Box()
        self._stage_spacer.set_hexpand(True)
        self._stage_spacer.set_vexpand(True)
        self.frame_stack.set_child(self._stage_spacer)
        # Gtk.Overlay does not clip its overlay children, so a HUD row wider
        # than the picture will happily draw past the window edge. The fit
        # below keeps that from happening, but this makes it impossible
        # rather than merely unlikely.
        self.frame_stack.set_overflow(Gtk.Overflow.HIDDEN)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        self.frame_stack.add_overlay(self.picture)
        # Parent the camera menu to the viewfinder, not to the banner. GTK CSS
        # inherits, and the banner carries letter-spacing 5px in bold
        # monospace to look like a tube caption, which the menu inside it
        # then inherited: every entry came out stretched and wrong. It still
        # points at the banner.
        self.camera_popover.set_parent(self.frame_stack)
        self.frame_stack.add_overlay(self.no_signal)
        self.frame_stack.add_overlay(self.btn_no_signal)
        self.frame_stack.add_overlay(self.grid_widget)
        self.meter_box = MeterBox()
        self.frame_stack.add_overlay(self.meter_box)
        # Zoom readout, shown only while zoomed so it is not permanent chrome.
        self.zoom_reset = Gtk.Button(label="1.0x")
        self.zoom_reset.add_css_class("zoomstep")
        self.zoom_reset.add_css_class("zoomval")
        self.zoom_reset.set_tooltip_text("Tap to go back to 1x")
        self.zoom_reset.connect("clicked", lambda *_: self.set_zoom(1.0))

        self.zoom_label = Gtk.Label(label="1.0x")
        self.zoom_label.add_css_class("hud-mono")
        self.zoom_label.add_css_class("hud-chip")
        self.zoom_label.set_halign(Gtk.Align.CENTER)
        self.zoom_label.set_valign(Gtk.Align.END)
        self.zoom_label.set_margin_bottom(16)
        self.zoom_label.set_visible(False)
        self.zoom_label.set_can_target(False)
        self.frame_stack.add_overlay(self.zoom_label)
        self.frame_stack.add_css_class("viewflip")
        # Nothing in the overlay may paint outside the picture, whatever
        # its natural width says.
        self.frame_stack.set_overflow(Gtk.Overflow.HIDDEN)
        # Right-click the picture for the overlay checklist. It is a context
        # menu, so it belongs on the right button: left-click stays free for
        # anything that should act on the shot itself.
        self.view_popover = Gtk.Popover()
        self.view_popover.set_parent(self.frame_stack)
        meter_click = Gtk.GestureClick.new()
        meter_click.set_button(1)
        meter_click.connect("released", self._on_meter_tap)
        self.frame_stack.add_controller(meter_click)

        # Drag up and down on the viewfinder to set exposure by hand. This is
        # where a phone puts focus, but neither camera on this machine has a
        # focus motor (no focus_absolute, no focus_automatic_continuous on
        # either node), so there is nothing to drag. Exposure is the control
        # that exists and it is the one that helps in a dim or backlit room.
        exp_drag = Gtk.GestureDrag.new()
        exp_drag.set_button(1)
        exp_drag.connect("drag-begin", self._on_exp_begin)
        exp_drag.connect("drag-update", self._on_exp_update)
        exp_drag.connect("drag-end", self._on_exp_end)
        self.frame_stack.add_controller(exp_drag)

        self.exp_label = Gtk.Label(label="")
        self.exp_label.add_css_class("hud-mono")
        self.exp_label.add_css_class("hud-chip")
        self.exp_label.set_halign(Gtk.Align.CENTER)
        self.exp_label.set_valign(Gtk.Align.START)
        self.exp_label.set_margin_top(16)
        self.exp_label.set_visible(False)
        self.exp_label.set_can_target(False)
        self.frame_stack.add_overlay(self.exp_label)

        zoom_scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        zoom_scroll.connect(
            "scroll", lambda c, dx, dy: (self.set_zoom(self.zoom - dy * 0.25), True)[1])
        self.frame_stack.add_controller(zoom_scroll)

        # Pinch, for the touchscreen this whole app is meant for. Scroll only
        # ever worked with a mouse, and a feature you cannot find is not a
        # feature.
        pinch = Gtk.GestureZoom.new()
        pinch.connect("begin", lambda *_: setattr(self, "_pinch_base", self.zoom))
        pinch.connect("scale-changed",
                      lambda g, sc: self.set_zoom(getattr(self, "_pinch_base", 1.0) * sc))
        self.frame_stack.add_controller(pinch)

        # And a visible one, because a gesture with nothing on screen to hint
        # at it is invisible.
        # Up the right edge, centred. The HUD lives in the four corners, so
        # the bottom middle it used to occupy was between the mic meter and
        # the resolution readout and collided with both as soon as the window
        # narrowed. The middle of an edge is the one place nothing else wants.
        self.zoom_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.zoom_bar.add_css_class("zoombar")
        self.zoom_bar.set_halign(Gtk.Align.END)
        self.zoom_bar.set_valign(Gtk.Align.CENTER)
        self.zoom_bar.set_margin_end(10)
        plus = Gtk.Button(label="+")
        plus.add_css_class("zoomstep")
        plus.connect("clicked", lambda *_: self.set_zoom(self.zoom + 0.25))
        minus = Gtk.Button(label="\u2212")
        minus.add_css_class("zoomstep")
        minus.connect("clicked", lambda *_: self.set_zoom(self.zoom - 0.25))
        self.zoom_slider = Gtk.Scale.new_with_range(
            Gtk.Orientation.VERTICAL, 1.0, 4.0, 0.05)
        self.zoom_slider.set_inverted(True)      # up is more
        self.zoom_slider.set_draw_value(False)
        self.zoom_slider.set_size_request(-1, 130)
        self.zoom_slider.set_value(self.zoom)
        self.zoom_slider.connect("value-changed", self._on_zoom_slider)
        self.zoom_bar.append(plus)
        self.zoom_bar.append(self.zoom_slider)
        self.zoom_bar.append(minus)
        self.zoom_bar.append(self.zoom_reset)
        self.frame_stack.add_overlay(self.zoom_bar)

        view_click = Gtk.GestureClick.new()
        view_click.set_button(3)
        view_click.connect("released", self._on_view_clicked)
        self.frame_stack.add_controller(view_click)
        frame_stack = self.frame_stack

        # Refit the HUD whenever the viewfinder changes size. It was only
        # recomputed on a handful of explicit events, so any resize they did
        # not cover left the readouts laid out for the previous width and the
        # right-hand corner sitting out over the black border until something
        # else happened to trigger a refit. A tick callback catches every
        # cause at once: startup, aspect change, window resize, fullscreen.
        self._last_stack_w = -1

        def _watch_size(widget, clock):
            w = widget.get_width()
            if w != self._last_stack_w and w > 1:
                self._last_stack_w = w
                self._fit_hud()
            return GLib.SOURCE_CONTINUE
        self.frame_stack.add_tick_callback(_watch_size)

        self.aspect_frame = Gtk.AspectFrame.new(0.5, 0.5, 4/3, False)
        self.aspect_frame.add_css_class("viewframe")
        self.aspect_frame.set_child(frame_stack)
        self.aspect_frame.set_hexpand(True); self.aspect_frame.set_vexpand(True)
        bg.append(self.aspect_frame)

        # ---- Top bar ----
        # Its own strip above the viewfinder rather than floating over the
        # picture, so nothing covers the shot. Exit left, mode pills centred,
        # fullscreen right.
        # The left of the top bar was empty, and settings had only been
        # reachable from inside the gallery.
        self.btn_settings = Gtk.Button()
        self.btn_settings.set_child(Gtk.Image.new_from_icon_name("emblem-system-symbolic"))
        self.btn_settings.get_child().set_pixel_size(18)
        self.btn_settings.add_css_class("winctl")
        self.btn_settings.set_valign(Gtk.Align.CENTER)
        self.btn_settings.set_tooltip_text("Settings")
        self.btn_settings.connect("clicked", lambda *_: self._open_settings())

        self.btn_flash = Gtk.ToggleButton()
        self.flash_icon = Gtk.Image.new_from_icon_name("thunderbolt-symbolic")
        self.btn_flash.set_child(self.flash_icon)
        self.flash_icon.set_pixel_size(18)
        self.btn_flash.add_css_class("winctl")
        self.btn_flash.set_valign(Gtk.Align.CENTER)
        self.btn_flash.set_tooltip_text(
            "Screen flash: light the subject with the display before the shot")
        self.btn_flash.connect("toggled", self._on_flash_toggled)

        # Exposure as a slider, not a gesture. A drag on the picture was
        # invisible, and it now belongs to panning a zoomed view anyway.
        # Centre is auto: leave it alone and the camera decides, move it and
        # you have taken over, which needs no separate mode switch.
        self.exp_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.exp_box.add_css_class("expbar")
        self.exp_box.set_valign(Gtk.Align.CENTER)
        self.btn_exp = Gtk.Button(label="AE")
        self.btn_exp.add_css_class("zoomstep")
        self.btn_exp.add_css_class("zoomval")
        self.btn_exp.set_tooltip_text("Exposure. Click to hand it back to the camera")
        self.btn_exp.connect("clicked", lambda *_: self._exposure_to_auto())
        self.exp_slider = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, -3.0, 3.0, 0.1)
        self.exp_slider.set_size_request(132, -1)
        self.exp_slider.set_draw_value(False)
        self.exp_slider.add_mark(0.0, Gtk.PositionType.BOTTOM, None)
        self.exp_slider.set_value(0.0)
        self.exp_slider.connect("value-changed", self._on_exp_slider)
        self.exp_box.append(self.btn_exp)
        self.exp_box.append(self.exp_slider)
        self.btn_grid   = self._pill_button("#",   self._toggle_grid,   width=42, tint=False)
        self.btn_aspect = self._pill_button(self.aspects[0], self._toggle_aspect, width=58, tint=False)
        # A real icon, not the "⏱" glyph: that rendered tiny and was barely
        # visible at any size.
        self.btn_timer = Gtk.Button()
        self.timer_icon = Gtk.Image.new_from_icon_name("stopwatch-symbolic")
        self.timer_icon.set_pixel_size(20)
        self.timer_label = Gtk.Label(label="")
        self.timer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.timer_box.set_halign(Gtk.Align.CENTER)
        self.timer_box.append(self.timer_icon)
        self.timer_box.append(self.timer_label)
        self.btn_timer.set_child(self.timer_box)
        self.btn_timer.set_size_request(58, 42)
        self.btn_timer.add_css_class("pill")
        self.btn_timer.connect("clicked", lambda *_: self._toggle_timer())
        self.btn_fx = Gtk.MenuButton(label="FX")
        self.btn_fx.set_size_request(58, 42)
        self.btn_fx.add_css_class("pill")
        self.btn_fx.set_tooltip_text("Look")
        self.fx_popover = Gtk.Popover()
        self.btn_fx.set_popover(self.fx_popover)
        fxbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        fxbox.set_margin_top(6); fxbox.set_margin_bottom(6)
        fxbox.set_margin_start(6); fxbox.set_margin_end(6)
        for name in FILTERS:
            b = Gtk.Button(label=name)
            b.add_css_class("cam-row")
            b.connect("clicked", lambda _b, n=name: (
                self.fx_popover.popdown(), self.set_filter(n)))
            fxbox.append(b)
        self.fx_popover.set_child(fxbox)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.append(self.exp_box)
        top.append(self.btn_grid); top.append(self.btn_aspect)
        top.append(self.btn_fx); top.append(self.btn_timer)

        # Standard window controls, right side, in the usual order:
        # minimize, maximize, close.
        def _win_button(icon, fn):
            b = Gtk.Button()
            img = Gtk.Image.new_from_icon_name(icon)
            img.set_pixel_size(16)
            b.set_child(img)
            b.add_css_class("winctl")
            b.set_valign(Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: fn())
            return b

        self.btn_min  = _win_button("window-minimize-symbolic", self.minimize)
        self.btn_max  = _win_button("window-maximize-symbolic", self._toggle_maximize)
        self.btn_exit = _win_button("window-close-symbolic", self.close)
        self.btn_exit.add_css_class("winctl-close")
        # Keep the old name working: F11 and the shortcut still fullscreen.
        self.btn_fullscreen = self.btn_max

        winctl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        winctl.append(self.btn_min); winctl.append(self.btn_max); winctl.append(self.btn_exit)
        # Track the real window state rather than toggling a flag, so the icon
        # stays right no matter how it got maximized: the button, a
        # double-click on the bar, or the window manager's own shortcut.
        self.connect("notify::maximized", self._on_maximized_changed)
        self.connect("notify::fullscreened", self._on_maximized_changed)

        # A plain Box with expanding spacers rather than a CenterBox. A
        # CenterBox inside the WindowHandle swallowed the drag and the window
        # would not move at all; the same handle around a Box works.
        gap_l = Gtk.Box(); gap_l.set_hexpand(True)
        gap_r = Gtk.Box(); gap_r.set_hexpand(True)
        # Dummy on the left the same width as the control group, so the mode
        # pills sit at the true centre of the bar rather than the centre of
        # whatever space is left over.
        ballast = Gtk.Box()
        ballast.set_size_request(3 * 34 + 2 * 2, -1)
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        top_bar.add_css_class("top-bar")
        self._ballast = ballast
        self._top_pills = top
        self._winctl = winctl
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        left.append(self.btn_settings)
        left.append(self.btn_flash)
        top_bar.append(left)
        top_bar.append(ballast)
        top_bar.append(gap_l)
        top_bar.append(top)
        top_bar.append(gap_r)
        top_bar.append(winctl)
        top_bar.set_margin_top(8); top_bar.set_margin_bottom(8)
        top_bar.set_margin_start(12); top_bar.set_margin_end(12)

        # The bar doubles as the titlebar this window does not have.
        top_handle = Gtk.WindowHandle()
        top_handle.set_child(top_bar)
        # prepend, not append: the viewfinder was added to the column further
        # up, so appending would put the bar underneath it.
        column.prepend(top_handle)

        # ---- Bottom control area ----
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bottom.set_valign(Gtk.Align.END)
        # Explicitly not expanding. GTK computes expand from descendants, and
        # act_base inside sets vexpand so it fills the action overlay; that
        # propagated all the way up here and made the controls compete with
        # the viewfinder for the spare vertical space, taking half of it and
        # then drawing only its natural height at the bottom of the slot. It
        # went unnoticed while the Picture's own natural height was large
        # enough to win the argument.
        bottom.set_vexpand(False)
        bottom.add_css_class("bottom-bar")
        # Natural min via child widgets, don't force size — lets window shrink freely
        column.append(bottom)

        # Mode pill
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        mode_row.set_halign(Gtk.Align.CENTER)
        mode_row.set_margin_top(12); mode_row.set_margin_bottom(6)
        mode_row.add_css_class("mode-pill")
        self.btn_photo = Gtk.Button(label="PHOTO")
        self.btn_photo.add_css_class("mode-btn"); self.btn_photo.add_css_class("mode-active")
        self.btn_photo.connect("clicked", lambda *_: self._set_video_mode(False))
        self.btn_video = Gtk.Button(label="VIDEO")
        self.btn_video.add_css_class("mode-btn")
        self.btn_video.connect("clicked", lambda *_: self._set_video_mode(True))
        mode_row.append(self.btn_photo); mode_row.append(self.btn_video)
        bottom.append(mode_row)

        # Action area — Gtk.Overlay so each button is positioned independently.
        # Shutter stays dead-center regardless of deck/flip sizes.
        act_area = Gtk.Overlay()
        act_area.set_size_request(-1, 160)   # tall enough for the enlarged deck
        act_area.set_margin_top(12); act_area.set_hexpand(True)
        act_area.set_vexpand(False)
        act_base = Gtk.Box(); act_base.set_hexpand(True); act_base.set_vexpand(True)
        act_area.set_child(act_base)
        bottom.append(act_area)

        # SHUTTER — always centered in the action area
        # Ring plus a core that morphs: white circle for photo, red circle for
        # video standby, red square while recording.
        self.shutter = Gtk.Button()
        self.shutter_core = Gtk.Box()
        self.shutter_core.add_css_class("shutter-core")
        self.shutter_core.set_hexpand(True); self.shutter_core.set_vexpand(True)
        self.shutter.set_child(self.shutter_core)
        self.shutter.set_size_request(84, 84); self.shutter.add_css_class("shutter")
        self.shutter.set_halign(Gtk.Align.CENTER); self.shutter.set_valign(Gtk.Align.CENTER)
        self.shutter.set_hexpand(False); self.shutter.set_vexpand(False)
        self.shutter.connect("clicked", lambda *_: self._on_shutter())
        act_area.add_overlay(self.shutter)

        # Deck thumbnail — anchored LEFT, does not affect shutter position
        self.thumb = ThumbnailDeck(
            on_click=self._open_gallery,
            on_card_click=self._open_photo_viewer,
        )
        act_area.add_overlay(self.thumb)
        self._deck_refresh_id = None
        self._pic_monitor = None
        self._refresh_deck()
        self._watch_pictures()

        # Flip camera — anchored RIGHT, mirroring the deck on the left.
        # The deck's visible card sits at margin(28) + card centre(56) +
        # translate(80) = 164px in from the left edge, so the flip button is
        # placed 164px in from the right: margin_end(128) + half of 72.
        # translate(-20px) lifts the card 20px, and margin_bottom(40) lifts
        # this by the same 20px under CENTER alignment.
        self.flip_icon = Gtk.Image.new_from_icon_name("camera-switch-symbolic")
        self.flip_icon.set_pixel_size(34)
        self.flip_icon.add_css_class("flip-icon")
        self.btn_flip = Gtk.Button()
        self.btn_flip.set_child(self.flip_icon)
        self.btn_flip.set_size_request(72, 72); self.btn_flip.add_css_class("flip")
        self.btn_flip.set_halign(Gtk.Align.END); self.btn_flip.set_valign(Gtk.Align.CENTER)
        self.btn_flip.set_hexpand(False); self.btn_flip.set_vexpand(False)
        self.btn_flip.set_margin_end(128)
        self.btn_flip.set_margin_bottom(40)
        self._flip_spun = False
        self._flipping = False
        self._flip_shut = False
        self._flip_ready = False
        self._flip_commit = None
        self.btn_flip.connect("clicked", lambda *_: self._flip_or_choose())
        self.btn_flip.set_tooltip_text("Switch camera (hold to choose)")

        # Hold to pick a specific camera. Tapping cycles, which is fine for
        # two, but a machine with three or four wants to jump straight to one.
        self.cam_popover = Gtk.Popover()
        self.cam_popover.set_parent(self.btn_flip)
        self.cam_popover.set_position(Gtk.PositionType.TOP)
        self.cam_popover.set_has_arrow(True)
        hold = Gtk.GestureLongPress.new()
        hold.set_delay_factor(1.0)
        hold.connect("pressed", lambda *_: self._show_camera_menu())
        self.btn_flip.add_controller(hold)
        # Right-click gets there too, the usual way to reach extra options.
        rmb = Gtk.GestureClick.new()
        rmb.set_button(3)
        rmb.connect("pressed", lambda *_: self._show_camera_menu())
        self.btn_flip.add_controller(rmb)
        act_area.add_overlay(self.btn_flip)

        # Countdown overlay
        self.countdown_label = Gtk.Label(label="")
        self.countdown_label.set_visible(False)
        self.countdown_label.add_css_class("countdown")
        self.countdown_label.set_halign(Gtk.Align.CENTER)
        self.countdown_label.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(self.countdown_label)

        # Full-window black blink (photo capture feedback)
        self.flash_overlay = Gtk.Box()
        self.flash_overlay.add_css_class("flash")
        self.flash_overlay.set_visible(False)
        self.flash_overlay.set_can_target(False)
        root.add_overlay(self.flash_overlay)

        # ---- Camcorder HUD (video mode) ----
        # Four corners plus a centred clock, rather than everything crowded
        # into the top left. Each corner owns one idea:
        #   top left     am I rolling, and for how long
        #   top centre   wall clock
        #   top right    power
        #   bottom left  sound
        #   bottom right what is being written
        hud = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hud.set_margin_top(18); hud.set_margin_bottom(18)
        hud.set_margin_start(16); hud.set_margin_end(16)
        # Was can_target(False). The readouts are controls now, so the
        # overlay has to accept clicks; plain labels do not consume them,
        # so a click on empty overlay still reaches the viewfinder menu.
        hud.set_can_target(True)

        # --- top left: recording state
        rec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rec_row.set_valign(Gtk.Align.CENTER)
        rec_row.add_css_class("hud-chip")
        self.rec_dot = Gtk.Box()
        self.rec_dot.add_css_class("rec-dot")
        self.rec_dot.set_size_request(13, 13)
        self.rec_dot.set_valign(Gtk.Align.CENTER)
        self.rec_state_label = Gtk.Label(label="STBY")
        self.rec_state_label.add_css_class("hud-rec")
        self.rec_time_label = Gtk.Label(label="00:00:00")
        self.rec_time_label.add_css_class("hud-mono")
        rec_row.append(self.rec_dot)
        rec_row.append(self.rec_state_label)
        rec_row.append(self.rec_time_label)

        # --- top centre: clock
        self.hud_clock = Gtk.Label(label="")
        self.hud_clock.add_css_class("hud-mono")
        self.hud_clock.add_css_class("hud-chip")

        # --- top right: power
        bat_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bat_row.set_valign(Gtk.Align.CENTER)
        bat_row.add_css_class("hud-chip")
        self.bat_gauge = BatteryGauge()
        self.bat_label = Gtk.Label(label="--%")
        self.bat_label.add_css_class("hud-mono")
        self.bat_left_label = Gtk.Label(label="")
        self.bat_left_label.add_css_class("hud-dim")
        bat_row.append(self.bat_gauge)
        bat_row.append(self.bat_label)
        bat_row.append(self.bat_left_label)

        hud_top = Gtk.CenterBox()
        hud_top.set_valign(Gtk.Align.START)
        hud_top.set_start_widget(rec_row)
        hud_top.set_center_widget(self.hud_clock)
        hud_top.set_end_widget(bat_row)
        hud.append(hud_top)

        spacer = Gtk.Box(); spacer.set_vexpand(True)
        spacer.set_can_target(False)   # nothing here, so do not claim clicks
        hud.append(spacer)

        # --- bottom left: sound. Its own corner so the meter survives at
        # sizes where the top row has already had to shed things.
        audio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        audio_row.set_valign(Gtk.Align.CENTER)
        self.btn_mic = Gtk.Button()
        self.mic_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        self.mic_icon.set_pixel_size(22)
        self.btn_mic.set_child(self.mic_icon)
        self.btn_mic.add_css_class("hud-btn")
        self.btn_mic.add_css_class("hud-chip")
        self.btn_mic.add_css_class("hud-mic")
        self.btn_mic.set_valign(Gtk.Align.CENTER)
        self.btn_mic.connect("clicked", lambda *_: self._toggle_mic())
        self.mic_popover = Gtk.Popover()
        self.mic_popover.set_parent(self.btn_mic)
        mic_rmb = Gtk.GestureClick.new()
        mic_rmb.set_button(3)
        mic_rmb.connect("pressed", lambda *_: self._build_mic_menu())
        self.btn_mic.add_controller(mic_rmb)
        self.audio_meter = AudioMeter()
        self.audio_meter.add_css_class("hud-chip")
        audio_row.append(self.btn_mic)
        audio_row.append(self.audio_meter)

        # --- bottom right: what is being written
        # No chip on the row itself: it holds two independent readouts, so a
        # row-level background drew a box around two boxes. Each readout
        # carries its own instead.
        info_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        info_row.set_valign(Gtk.Align.CENTER)
        self.hud_fps = Gtk.Label(label="")
        self.hud_fps.add_css_class("hud-mono")
        self.btn_res = Gtk.MenuButton()
        self.btn_res.set_child(self.hud_fps)
        self.btn_res.add_css_class("hud-btn")
        self.btn_res.add_css_class("hud-chip")
        self.btn_res.set_tooltip_text("Capture mode")
        self.res_popover = Gtk.Popover()
        self.btn_res.set_popover(self.res_popover)
        self.btn_res.connect("notify::active", self._build_res_menu)

        self.hud_format = Gtk.Label(label="MKV  H.264")
        self.hud_format.add_css_class("hud-dim")
        self.btn_fmt = Gtk.MenuButton()
        self.btn_fmt.set_child(self.hud_format)
        self.btn_fmt.add_css_class("hud-btn")
        self.btn_fmt.add_css_class("hud-chip")
        self.btn_fmt.set_tooltip_text("Recording format")
        self.fmt_popover = Gtk.Popover()
        self.btn_fmt.set_popover(self.fmt_popover)
        self.btn_fmt.connect("notify::active", self._build_fmt_menu)

        info_row.append(self.btn_res)
        info_row.append(self.btn_fmt)

        hud_bot = Gtk.CenterBox()
        hud_bot.set_valign(Gtk.Align.END)
        hud_bot.set_start_widget(audio_row)
        hud_bot.set_end_widget(info_row)
        hud.append(hud_bot)

        self._rec_row, self._bat_row = rec_row, bat_row
        self._audio_row, self._info_row = audio_row, info_row
        self._hud_top, self._hud_bot = hud_top, hud_bot
        # What each row sheds when it runs out of room. The mic and its meter
        # are never shed: whether your voice is being captured matters as
        # much as whether the camera is rolling.
        # Every corner the viewfinder checklist can switch on and off. This
        # used to list only the clock, battery and format row, so unchecking
        # the recording state or the microphone hid them permanently: nothing
        # ever set them visible again.
        self._hud_items = [rec_row, self.hud_clock, bat_row, audio_row, info_row]
        self.rec_indicator = hud
        self.rec_indicator.set_visible(False)
        # On the picture, not the window: the readouts belong over the frame
        # you are shooting, not floating in the letterbox bars.
        self.frame_stack.add_overlay(self.rec_indicator)
        # The HUD is built last, so it lands on top of the zoom control, which
        # sits inside its bounds. Gestures still worked because the HUD is a
        # descendant of the viewfinder and events bubble up to it, but the
        # zoom bar is a sibling overlay and never saw them: visible, and
        # completely dead to the touch. Re-adding raises it back to the front.
        self.frame_stack.remove_overlay(self.zoom_bar)
        self.frame_stack.add_overlay(self.zoom_bar)
        # And the NO SIGNAL banner, for the same reason. It is the one control
        # that has to work when nothing else does, and it was sitting under
        # the HUD: visible, and unclickable.
        self.frame_stack.remove_overlay(self.btn_no_signal)
        self.frame_stack.add_overlay(self.btn_no_signal)
        self.hud_timer = None
        self._power_samples = []

        # Grid overlay (rule of thirds)

        # Saving indicator. Writing a clip takes a moment while the queues
        # drain, and without this the app just looks frozen.
        # A corner indicator rather than a full screen. Saving takes about a
        # tenth of a second now that EOS is actually being listened for, so
        # covering the whole window for it only produced a flash.
        self.saving = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.saving.add_css_class("saving-chip")
        self.saving.set_halign(Gtk.Align.END)
        self.saving.set_valign(Gtk.Align.END)
        self.saving.set_margin_end(6)
        self.saving.set_margin_bottom(3)
        self.tux = Gtk.Picture()
        self.tux.set_size_request(TUX_SMALL_W, TUX_SMALL_H)
        self.tux.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.tux.set_can_shrink(True)
        self.saving_label = Gtk.Label(label="Saving")
        self.saving_label.add_css_class("saving-text")
        # Fixed pixel width, left aligned. The dots are appended, and letting
        # the label grow shoved the penguin sideways three times a second.
        # width_chars is not enough: it reserves an average character width,
        # which still left the penguin moving over a 12px range.
        self.saving_label.set_size_request(SAVING_LABEL_W, -1)
        self.saving_label.set_xalign(0.0)
        # A size request is a minimum, so the label still grew past it as the
        # dots were added and carried the penguin along. Ellipsizing caps the
        # natural width as well, which is what actually pins it. The width is
        # set above the real text width, so nothing is ever ellipsized.
        self.saving_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.saving_label.set_max_width_chars(1)
        # Text first, penguin last, so the penguin is the thing in the corner
        # rather than the word. Right aligned, the box grows leftwards, so
        # whichever child is appended last is the one against the edge.
        self.saving.append(self.saving_label)
        self.saving.append(self.tux)
        self.saving.set_visible(False)
        # Never takes a click. It appears over the action area, and swallowing
        # a press meant for the shutter or the flip button would be worse than
        # not showing it at all.
        self.saving.set_can_target(False)
        # The window's bottom right corner, in the strip below the flip
        # button rather than over it. Measured: in a 960x640 window the flip
        # ends at y=582 and the shutter at x=522, so a short chip along the
        # bottom edge clears both.
        root.add_overlay(self.saving)

        # Gallery (last overlay so it sits on top of everything)
        self.viewer = GalleryView(
            on_close=self._close_photo_viewer,
            on_open_external=lambda p: Gio.AppInfo.launch_default_for_uri(
                "file://" + str(p), None),
            on_settings=self._open_settings)
        # Overlay children get their natural size unless told to expand, so
        # without these the gallery opened at zero size and a tap on the
        # preview looked like it did nothing at all.
        self.viewer.set_hexpand(True); self.viewer.set_vexpand(True)
        self.viewer.set_halign(Gtk.Align.FILL); self.viewer.set_valign(Gtk.Align.FILL)
        self.viewer.set_visible(False)
        root.add_overlay(self.viewer)

        # CSS
        css = Gtk.CssProvider()
        css.load_from_string("""
        .bg-black { background: black; }
        /* Everything behind the viewfinder is black, so the moment the
           picture re-measures during a flip there is nothing grey to show
           through: the default widget background was what flashed. */
        window, .viewframe, .viewflip, picture { background: black; }
        .bottom-bar { background: #000; }
        .top-bar    { background: #000; }
        .mode-pill { background: rgba(255,255,255,0.15); border-radius: 16px; padding: 2px; }
        .mode-btn { background: transparent; color: white; font-weight: bold;
                    font-size: 12px; padding: 4px 24px; border-radius: 14px;
                    border: none; box-shadow: none; }
        /* Adwaita blue, the standard GNOME accent. Reads as part of the
           desktop rather than a random highlight colour. */
        .mode-active { background: rgba(0,0,0,0.5); color: #62a0ea; }
        .shutter { background: transparent; border-radius: 42px; padding: 0;
                   border: 3px solid rgba(255,255,255,0.95);
                   min-width: 78px; min-height: 78px;
                   box-shadow: 0 0 0 1px rgba(0,0,0,0.45),
                               0 3px 16px rgba(0,0,0,0.55);
                   transition: transform 110ms ease-out, background 140ms; }
        .shutter:hover  { background: rgba(255,255,255,0.12); }
        .shutter:active { transform: scale(0.93); }
        /* The core carries the shape change so the ring stays put. */
        .shutter-core { background: white; border-radius: 34px; margin: 7px;
                        transition: background   200ms ease-out,
                                    border-radius 260ms cubic-bezier(.2,.7,.3,1),
                                    margin        260ms cubic-bezier(.2,.7,.3,1); }
        .shutter-core.video     { background: #ff3b30; }
        .shutter-core.recording { background: #ff3b30; border-radius: 7px;
                                  margin: 21px; }
        .shutter:active { background: #ddd; transform: scale(0.88); }
        .shutter:hover  { background: #f5f5f5; }
        .thumb          { min-width: 56px; min-height: 56px; padding: 0; }
        .thumb picture  { min-width: 56px; min-height: 56px; }
        .thumb:active   { transform: scale(0.9); }
        /* --- deck-of-cards thumbnail --- */
        .thumb-placeholder { color: rgba(255,255,255,0.6); }
        .deck-card {
            border-radius: 22px;                 /* much rounder corners */
            /* No white border. Separation between the fanned cards comes
               from the drop shadow instead, which reads cleaner over a
               photo than a hard white outline. */
            box-shadow: 0 4px 14px rgba(0,0,0,0.7);
            transition: transform 520ms cubic-bezier(.25,.46,.45,.94),
                        opacity   400ms ease-out;
        }
        /* Hand-of-cards style: bottom-center pivot.
           Idle = tight fan (just a hint), hold-to-expand fans wide. */
        .deck-card { transform-origin: 50% 100%; }
        /* Idle: stacked exactly, so only the most recent photo is visible. */
        .state-idle.deck-idx-0,
        .state-idle.deck-idx-1,
        .state-idle.deck-idx-2,
        .state-idle.deck-idx-3,
        .state-idle.deck-idx-4 { transform: translate(44px, -20px) rotate(0deg); }
        /* Peek: hovering for a moment tips the deck open a little, as a hint
           that there is a stack under there. Press and hold for the full fan. */
        .state-peek.deck-idx-0 { transform: translate(44px, -20px) rotate(-14deg); }
        .state-peek.deck-idx-1 { transform: translate(44px, -20px) rotate( -7deg); }
        .state-peek.deck-idx-2 { transform: translate(44px, -20px) rotate(  0deg); }
        .state-peek.deck-idx-3 { transform: translate(44px, -20px) rotate(  7deg); }
        .state-peek.deck-idx-4 { transform: translate(44px, -20px) rotate( 14deg); }
        /* Expanded: hand-of-cards fan. Rotation alone only spread the card
           centres over ~56px, about 14px per card, which is far too small to
           aim at with a finger, so each card also slides sideways. The X
           values here MUST stay in step with FAN_DX + FAN_OFFSETS in
           ThumbnailDeck or the hit zones drift away from what is drawn. */
        .state-expanded.deck-idx-0 { transform: translate(-16px, -20px) rotate(-30deg); }
        .state-expanded.deck-idx-1 { transform: translate( 14px, -20px) rotate(-15deg); }
        .state-expanded.deck-idx-2 { transform: translate( 44px, -20px) rotate(  0deg); }
        .state-expanded.deck-idx-3 { transform: translate( 74px, -20px) rotate( 15deg); }
        .state-expanded.deck-idx-4 { transform: translate(104px, -20px) rotate( 30deg); }
        /* "Pulled" — one card lifted farther and up */
        .pulled { transform: translate(0px, -80px) rotate(0deg) scale(1.4);
                  box-shadow: 0 8px 20px rgba(0,0,0,0.6); }
        /* Focused card — combined-class selectors so specificity matches
           .state-expanded.deck-idx-N (both are 2 classes = 20 specificity).
           Otherwise the state-expanded transform wins and the pop-up never
           applies. Also declared AFTER the state-expanded rules so
           equal-specificity ties break in our favor. */
        /* The focused card lifts straight up from where it already is, so the
           card under your finger is the one that rises. Sending them all to a
           shared X made the card jump sideways out from under you. Three
           classes here (30) beats .state-expanded.deck-idx-N (20). */
        .state-expanded.deck-idx-0.card-focused { transform: translate(-16px, -190px) rotate(0deg) scale(1.7); }
        .state-expanded.deck-idx-1.card-focused { transform: translate( 14px, -190px) rotate(0deg) scale(1.7); }
        .state-expanded.deck-idx-2.card-focused { transform: translate( 44px, -190px) rotate(0deg) scale(1.7); }
        .state-expanded.deck-idx-3.card-focused { transform: translate( 74px, -190px) rotate(0deg) scale(1.7); }
        .state-expanded.deck-idx-4.card-focused { transform: translate(104px, -190px) rotate(0deg) scale(1.7); }
        .state-idle.card-focused {
            transform: translate(44px, -190px) rotate(0deg) scale(1.7);
        }
        .card-focused {
            box-shadow: 0 18px 40px rgba(0,0,0,0.9);
        }
        /* ---- gallery ---- */
        /* Saving: a penguin having a nice time while the muxer finishes. */
        .saving-chip { background: rgba(12,12,16,0.82);
                       border-radius: 14px; padding: 4px 6px 4px 12px; }
        /* The sprite carries its own motion, so nothing to animate in CSS. */
        .saving-text { color: rgba(255,255,255,0.92); font-size: 14px;
                       font-family: monospace; letter-spacing: 2px; }
        .gal { background: #16161a; }
        .gal-bar { background: #16161a; }
        .gal-title { color: rgba(255,255,255,0.92); font-size: 14px; }
        .gal-tool { background: rgba(255,255,255,0.08); border: none;
                    color: white; border-radius: 8px;
                    min-width: 34px; min-height: 34px; padding: 0 8px; }
        .gal-tool:hover { background: rgba(255,255,255,0.18); }
        .gal-tool:checked { background: #62a0ea; color: #fff; }
        .gal-save { background: #62a0ea; color: white; border: none;
                    border-radius: 8px; padding: 6px 14px; font-weight: bold; }
        .gal-save:disabled { background: rgba(255,255,255,0.08);
                             color: rgba(255,255,255,0.35); }
        .gal-strip { background: #101013; border-top: 1px solid rgba(255,255,255,0.08); }
        .gal-thumb { padding: 3px; background: transparent; border: none;
                     border-radius: 8px; }
        .gal-thumb:hover { background: rgba(255,255,255,0.12); }
        .gal-thumb-active { background: #62a0ea; }
        .gal-empty { color: rgba(255,255,255,0.45); font-size: 16px; }
        /* Edit motion. The picture animates, then the pixbuf is swapped at
           the end, so a rotate looks like the photo turning rather than a
           different photo appearing. */
        .gal picture { transition: transform 240ms cubic-bezier(.3,.7,.3,1),
                                   opacity 200ms ease-out; }
        .gal picture.no-anim { transition: none; }
        .crop-drop { transform: scale(0.9) translateY(26px); opacity: 0.25; }
        .gal-count { color: rgba(255,255,255,0.55); font-size: 13px;
                     font-family: monospace; }
        .gal-play { background: rgba(0,0,0,0.46); color: white; border: none;
                    border-radius: 50px; min-width: 92px; min-height: 92px;
                    padding: 0; transition: background 140ms ease-out,
                                            transform 140ms ease-out; }
        .gal-play:hover  { background: rgba(0,0,0,0.68); transform: scale(1.06); }
        .gal-play:active { background: rgba(0,0,0,0.80); transform: scale(0.96); }
        .gal-arrow { background: rgba(0,0,0,0.55); color: white; border: none;
                     border-radius: 34px; min-width: 68px; min-height: 68px;
                     opacity: 0.65; transition: opacity 120ms ease-out,
                                                background 120ms ease-out; }
        .gal-arrow:hover { opacity: 1; background: rgba(0,0,0,0.75); }
        /* Full-screen photo viewer */
        /* Camera flip: the viewfinder squeezes left-to-right to a vertical
           sliver, the pipeline is swapped while it is invisible, then it
           opens back out. This used to be perspective()+rotateY(), but GTK's
           3D projection over a GStreamer paintable rendered as a squashed
           band with a triangular artifact. A plain 2D scaleX reads as the
           same turn and composites cleanly. */
        /* Accelerate into the close, decelerate out of the open. One
           symmetric ease for both directions made the hold in the middle
           read as a stall. */
        /* Each tier gets its own curve, front-loaded harder the longer it
           runs, so the visible part of the close takes about the same
           WALL-CLOCK time in every tier (88-110ms to reach 80% shut). A
           single shared curve stretched over a longer duration made the
           slow camera's opening movement slower too, which is what read as
           asymmetric even when both directions were smooth. The remaining
           travel creeps from a narrow sliver down to closed, covering the
           rest of the wake without ever stopping. */
        .viewflip { transition: transform 190ms cubic-bezier(0, .15, .55, 1); }
        .viewflip.flip-fast  { transition-duration: 190ms;
                               transition-timing-function: cubic-bezier(0, .15, .55, 1); }
        .viewflip.flip-med   { transition-duration: 300ms;
                               transition-timing-function: cubic-bezier(0, .50, .32, 1); }
        .viewflip.flip-slow  { transition-duration: 420ms;
                               transition-timing-function: cubic-bezier(0, .72, .28, 1); }
        .viewflip.flip-vslow { transition-duration: 560ms;
                               transition-timing-function: cubic-bezier(0, .82, .20, 1); }
        .viewflip.flip-xslow { transition-duration: 700ms;
                               transition-timing-function: cubic-bezier(0, .88, .15, 1); }
        .viewflip.opening { transition: transform 300ms cubic-bezier(0, 0, .2, 1); }
        .viewflip.flipped { transform: scaleX(0.02); }
        .viewer-bg { background: rgba(0,0,0,0.95); }
        .flip:active    { transform: scale(0.9); }
        .pill:active    { transform: scale(0.9); }
        /* Camcorder HUD. Monospace and a hard shadow so it stays legible
           over any scene, the way a viewfinder overlay has to be. */
        /* Tabular figures: without this the timer and clock shuffle
           sideways every time a digit changes width. */
        .hud-mono, .hud-dim, .hud-rec { font-feature-settings: "tnum" 1; }
        /* Each corner sits on its own scrim. A text shadow alone disappeared
           against a bright scene, and it could never help the mic icon,
           which is line art rather than text. */
        .hud-chip { background: rgba(0,0,0,0.45);
                    border-radius: 9px;
                    padding: 3px 9px; }
        window.size-compact .hud-mono,
        window.size-compact .hud-rec { font-size: 12px; }
        window.size-compact .hud-dim { font-size: 10px; }
        window.size-tiny .hud-mono,
        window.size-tiny .hud-rec { font-size: 11px; }
        window.size-tiny .hud-dim { font-size: 10px; }
        .hud-mono { color: white; font-family: monospace; font-size: 13px;
                    font-weight: bold; letter-spacing: 1px;
                    text-shadow: 0 1px 2px rgba(0,0,0,1); }
        .hud-dim  { color: rgba(255,255,255,0.72); font-family: monospace;
                    font-size: 11px; letter-spacing: 0px;
                    text-shadow: 0 1px 2px rgba(0,0,0,1); }
        .hud-rec  { color: #ff3b30; font-family: monospace; font-size: 13px;
                    font-weight: bold; letter-spacing: 2px;
                    text-shadow: 0 1px 2px rgba(0,0,0,1); }
        .hud-rec.standby { color: rgba(255,255,255,0.75); }
        /* GtkMenuButton wraps an inner button, so styling only the outer
           widget left that inner one drawing its default frame: a visible
           box around the resolution and format readouts. Both levels have
           to be flattened. */
        /* GtkMenuButton is a menubutton wrapping a real button. The inner
           one has to be reset completely, not just given a transparent
           background: Adwaita paints it with background-image, so clearing
           only background-color left a second box drawn inside the chip. */
        /* No background declaration here: .hud-chip supplies it, and this
           rule comes later in the sheet, so setting one would win and the
           chip would disappear. */
        .hud-btn { padding: 0; border: none; box-shadow: none;
                   min-width: 0; min-height: 0; }
        .hud-btn > button,
        .hud-btn > button:hover,
        .hud-btn > button:active,
        .hud-btn > button:checked,
        .hud-btn > button:focus {
            background-image: none;
            background-color: transparent;
            border: none; border-radius: 0; box-shadow: none; outline: none;
            padding: 3px 9px; margin: 0;
            min-width: 0; min-height: 0; color: inherit; }
        /* Hover belongs to the chip, so the whole readout lights up. */
        .hud-btn.hud-chip:hover { background-color: rgba(255,255,255,0.20); }
        /* Give the mic a fixed footprint so muting cannot resize it, and so
           the icon is a decent target rather than a tiny glyph when live. */
        /* A circle, not a small rounded chip. The mic is the one HUD control
           that is pressed rather than read, and at chip size it was a much
           lighter target than anything in the top bar. */
        .hud-mic { background: rgba(0,0,0,0.55); border-radius: 50%;
                   padding: 0; min-width: 38px; min-height: 38px; }
        .hud-mic:hover { background: rgba(0,0,0,0.72); }
        .hud-mic > button { min-width: 38px; min-height: 38px;
                            border-radius: 50%; }
        .hud-btn-off { color: rgba(255,255,255,0.35); }
        /* Filled, not outlined. A red line-art glyph over a bright picture
           was almost invisible, which defeats the point of warning that the
           take will be silent. */
        /* Colour only. Adding padding or a radius here changed the button's
           size when muted, so the icon jumped out of line with the meter
           beside it. The chip already supplies the box, in both states. */
        .hud-btn-muted { background-color: #e01b24; color: #fff; }
        .hud-btn-muted:hover { background-color: #f0323b; }
        .thumb { background: rgba(255,255,255,0.2); border-radius: 8px;
                 border: 1px solid white; }
        /* Was rgba(0,0,0,0.55), which is invisible on a black control bar:
           the button was already 84px but only its 36px icon read, so it
           looked tiny next to the preview it is supposed to mirror. */
        .flip { background: rgba(255,255,255,0.10); border-radius: 48px;
                color: white; border: none;
                transition: background 140ms ease-out, transform 120ms ease-out; }
        .flip:hover { background: rgba(255,255,255,0.20); }
        .flip:disabled { opacity: 0.35; }
        /* Mirrors horizontally and stays that way, so the icon shows which
           camera you are on rather than just animating and resetting. */
        .flip-icon { transition: transform 260ms cubic-bezier(.45,0,.55,1); }
        .flip-icon.mirrored { transform: scaleX(-1); }
        .pill { background: rgba(255,255,255,0.12); border-radius: 21px;
                color: white; font-weight: bold; font-size: 14px;
                padding: 4px 12px; border: none;
                transition: background 120ms ease-out; }
        .pill:hover { background: rgba(255,255,255,0.22); }
        .winctl { background: rgba(255,255,255,0.10); border: none; color: white;
                  border-radius: 17px; min-width: 34px; min-height: 34px;
                  padding: 0; transition: background 120ms ease-out; }
        window.size-compact .winctl,
        window.size-tiny .winctl { min-width: 28px; min-height: 28px;
                                   border-radius: 14px; }
        window.size-compact .pill { padding: 3px 8px; font-size: 13px; }
        .winctl:hover { background: rgba(255,255,255,0.22); }
        .winctl-close:hover { background: #e01b24; }
        .flash-on { color: #ffc107; background: rgba(255,193,7,0.18); }
        .flash-on:hover { background: rgba(255,193,7,0.30); }
        .nosignal { background: rgba(0,0,0,0.62); color: #e8e8ec;
                    border: 2px solid rgba(255,255,255,0.55);
                    border-radius: 3px; padding: 10px 22px;
                    font-family: monospace; font-size: 19px;
                    letter-spacing: 5px; font-weight: bold; }
        .nosignal:hover { background: rgba(0,0,0,0.78);
                          border-color: rgba(255,255,255,0.85); }
        .zoombar { background: rgba(0,0,0,0.45); border-radius: 20px;
                   padding: 4px 3px; }
        .expbar { background: rgba(255,255,255,0.06);
                  border-radius: 21px; padding: 0 6px 0 0; }
        .zoomstep { background: none; background-image: none; border: none;
                    color: white; min-width: 34px; min-height: 30px;
                    border-radius: 16px; padding: 0 6px; font-size: 15px; }
        .zoomstep:hover { background: rgba(255,255,255,0.16); }
        .zoomval { font-family: monospace; min-width: 52px; }
        .exp-manual { color: #ffc107; background: rgba(255,193,7,0.18); }
        .winctl:disabled { color: rgba(255,255,255,0.25);
                           background: rgba(255,255,255,0.04); }
        .cam-row { background: transparent; border: none; color: white;
                   padding: 6px 10px; border-radius: 8px; font-size: 13px; }
        .cam-row:hover { background: rgba(255,255,255,0.12); }
        .cam-row-active { color: #62a0ea; }
        .pill-active { background: #62a0ea; color: white; }
        /* A GtkMenuButton wraps a real button, and its default frame
           drew a second box inside the FX pill. */
        .pill > button { background: none; background-image: none;
                         background-color: transparent; border: none;
                         box-shadow: none; outline: none; padding: 0;
                         min-width: 0; min-height: 0; color: inherit; }
        .pill-off { color: rgba(255,255,255,0.35); }
        /* Off, not broken: dim enough to read as inactive but still
           legible, unlike the old 35% which vanished. */
        .pill-dim { color: rgba(255,255,255,0.5);
                    background: rgba(255,255,255,0.06); }
        .countdown { color: white; font-size: 96px; font-weight: bold;
                     background: rgba(0,0,0,0.5); padding: 30px 50px; border-radius: 60px; }
        .flash { background: black; }
        .flash-white { background: #ffffff; }
        .rec-dot { background: #ff3b30; border-radius: 7px;
                   box-shadow: 0 0 8px rgba(255,59,48,0.9);
                   transition: opacity 180ms ease-out; }
        .rec-dot.blink   { opacity: 0.15; }
        .rec-dot.standby { background: rgba(255,255,255,0.55);
                           box-shadow: none; opacity: 1; }
        .rec-time { color: white; font-family: monospace; font-size: 14px;
                    font-weight: bold; background: rgba(0,0,0,0.5);
                    padding: 2px 8px; border-radius: 8px; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Keyboard shortcuts
        ctrl = Gtk.ShortcutController()
        def _esc(*_):
            if self.viewer.get_visible():
                self._close_photo_viewer(); return
            self.unfullscreen()
        for accel, fn in [("space", self._on_shutter), ("Return", self._on_shutter),
                          ("f", self._flip_camera), ("g", self._toggle_grid),
                          ("F11", self._toggle_fullscreen),
                          ("Escape", _esc)]:
            sc = Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(accel),
                Gtk.CallbackAction.new(lambda w, args, f=fn: (f(), True)[1]))
            ctrl.add_shortcut(sc)
        self.add_controller(ctrl)

    def _pill_button(self, label, cb, width=42, tint=False):
        b = Gtk.Button(label=label)
        b.set_size_request(width, 42)
        b.add_css_class("pill")
        if tint: b.add_css_class("pill-active")
        b.connect("clicked", lambda *_: cb())
        return b

    # ---------- Actions ----------
    def _install_breakpoints(self):
        """Re-lay the controls when the window gets narrow.

        At the 320px minimum the deck alone wants 328px, the shutter 84 and
        the flip button 200, so everything piled on top of everything else.
        Both breakpoints call the same recompute, which reads the real width,
        so overlapping conditions cannot fight each other.
        """
        for cond in ("max-width: 640px", "max-width: 430px"):
            try:
                bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(cond))
            except Exception as e:
                print(f"Lens: breakpoint {cond} failed: {e}", file=sys.stderr)
                continue
            bp.connect("apply", lambda *_: self._resize_layout())
            bp.connect("unapply", lambda *_: self._resize_layout())
            self.add_breakpoint(bp)
        self._size_class = None
        self._resize_pending = None
        # Breakpoints only fire on threshold crossings, so also watch the
        # window's own size for the continuous part, debounced so a drag
        # does not rebuild thumbnails on every frame.
        self.connect("notify::default-width", self._queue_resize_layout)
        prev = {"w": 0}

        def _poll():
            cur = self.get_width()
            if cur and abs(cur - prev["w"]) >= 8:
                prev["w"] = cur
                self._queue_resize_layout()
            return True

        GLib.timeout_add(200, _poll)
        GLib.idle_add(self._resize_layout)

    def _queue_resize_layout(self, *_):
        if self._resize_pending:
            GLib.source_remove(self._resize_pending)
        self._resize_pending = GLib.timeout_add(120, self._do_queued_resize)

    def _do_queued_resize(self):
        self._resize_pending = None
        self._resize_layout()
        return False

    def _resize_layout(self, *_):
        w = self.get_width() or self.get_default_size()[0]
        if w <= 430:
            cls = "tiny"
        elif w <= 640:
            cls = "compact"
        else:
            cls = "roomy"
        # Deliberately not short-circuiting on an unchanged class: the deck
        # size is continuous now, so it has to be recalculated on any resize.
        self._size_class = cls
        for c in ("size-tiny", "size-compact", "size-roomy"):
            self.remove_css_class(c)
        self.add_css_class(f"size-{cls}")

        # Everything in the action row scales continuously with the window,
        # rounded to 4-8px steps so a slow drag does not thrash. Snapping
        # between two fixed sizes was what made resizing look jumpy.
        shutter = max(52, min(84, int(w * 0.10) // 4 * 4))
        thumb = max(64, min(112, int(w * 0.13) // 8 * 8))
        # Tie the flip button to the preview rather than to the window, since
        # the two are mirrored: a 72px button opposite a 112px preview looked
        # lopsided even with their centres lined up exactly.
        flip = max(44, min(96, int(thumb * 0.78) // 4 * 4))
        self.shutter.set_size_request(shutter, shutter)
        self.btn_flip.set_size_request(flip, flip)
        self.flip_icon.set_pixel_size(max(20, int(flip * 0.44)))

        if cls != "tiny":
            self.thumb.set_visible(True)
            deck_margin = 28 if cls == "roomy" else 14
            self.thumb.set_scale(thumb, thumb + 56, deck_margin)
            # Mirror the flip button about the preview: put its centre the
            # same distance in from the right edge as the preview's centre is
            # from the left. Both sides scale, so this has to be computed
            # rather than fixed.
            preview_centre = deck_margin + self.thumb.FAN_DX + thumb / 2
            self.btn_flip.set_margin_end(max(10, int(preview_centre - flip / 2)))
        else:
            # No room for a preview deck next to a shutter and a flip button.
            # The deck is the one to drop: the gallery is still reachable, and
            # losing the shutter would make the app useless at this size.
            self.thumb.set_visible(False)
            self.btn_flip.set_margin_end(10)

        # Top bar: the window controls must survive at every width, so the
        # decorative ballast goes first and the mode pills go next. At 380px
        # ballast(106) + pills(174) + controls(106) overflowed and the close
        # button was pushed off the edge.
        # The ballast exists purely to balance the window-control group so
        # the mode pills sit at true centre. The controls shrink at narrow
        # widths, so a fixed 106px ballast pushed the pills off centre.
        btn = 34 if cls == "roomy" else 28
        # The left group already occupies two buttons' worth, so the ballast
        # only needs to make up the difference against three on the right.
        self._ballast.set_size_request(max(0, btn - 4), -1)
        self._ballast.set_visible(cls == "roomy")
        self.btn_flash.set_visible(cls != "tiny")
        self._top_pills.set_visible(cls != "tiny")
        self.btn_flip.set_visible(True)

        # HUD: hide by priority rather than shrink into illegibility.
        GLib.idle_add(self._fit_hud)
        self._refresh_deck()
        return False

    def _apply_settings(self):
        """Push the restored settings into the widgets, without animating."""
        ratio = {"4:3": 4 / 3, "16:9": 16 / 9, "1:1": 1.0}[self.aspects[self.aspect_idx]]
        self.aspect_frame.set_ratio(ratio)
        self.btn_aspect.set_label(self.aspects[self.aspect_idx])

        self._apply_overlay_mode()

        self._refresh_timer_button()
        self.btn_flash.set_active(self.flash_enabled)
        self._refresh_flash_button()

        # _set_video_mode does the HUD and shutter work, so route through it
        # rather than duplicating. It early-returns while recording, which
        # cannot be the case at startup.
        want_video = self.video_mode
        self.video_mode = False
        self._set_video_mode(want_video)

    def _save_settings(self):
        save_settings({
            "aspect_idx":   self.aspect_idx,
            "grid_visible": self.grid_visible,
            "overlay_mode": getattr(self, "overlay_mode", 0),
            "mic_enabled":  self.mic_enabled,
            "mic_gain":     self.mic_gain,
            "mic_rate":     self.mic_rate,
            "camera_off":   self.camera_off,
            "smooth_motion": self.smooth_motion,
            "denoise":      self.denoise,
            "nr_mix":       self.nr_mix,
            "trash_auto":   self.trash_auto,
            "trash_path":   self.trash_path,
            "mirror_view":  self.mirror_view,
            "mirror_saved": self.mirror_saved,
            "container":    self.container,
            "pic_dir":      str(self.pic_dir),
            "vid_dir":      str(self.vid_dir),
            "name_prefix":  self.name_prefix,
            "trash_days":   self.trash_days,
            "mode_override": self.mode_override,
            "mic_source":   self.mic_source,
            "flash":        self.flash_enabled,
            "zoom":         self.zoom,
            "filter":       self.filter_name,
            "timer_sec":    self.timer_sec,
            "video_mode":   self.video_mode,
            "cam_id":       self.cameras[self.cam_idx][2],
            "cam_latency":  self._cam_latency,
        })

    def _show_saving(self, on):
        """Dancing penguin while the file is written.

        Held for a moment even once the file is done. A save is about a
        tenth of a second, and an indicator that appears and vanishes inside
        one frame is just a flicker: you cannot tell whether it saved or
        whether something twitched.
        """
        frames = tux_frames()
        self.tux.set_visible(frames is not None)
        if not on:
            shown = time.monotonic() - getattr(self, "_saving_since", 0.0)
            left = SAVING_MIN_VISIBLE - shown
            if left > 0:
                if getattr(self, "_saving_hide", None):
                    GLib.source_remove(self._saving_hide)
                self._saving_hide = GLib.timeout_add(
                    int(left * 1000), lambda: (self._show_saving(False), False)[1])
                return
            if getattr(self, "_saving_hide", None):
                GLib.source_remove(self._saving_hide)
                self._saving_hide = None
        else:
            self._saving_since = time.monotonic()
            if getattr(self, "_saving_hide", None):
                GLib.source_remove(self._saving_hide)
                self._saving_hide = None
            if getattr(self, "_saving_timer", None):
                # Already dancing. Stop here and let it carry on: the clock
                # above has been reset, so the hold extends. Falling through
                # would add a second tick timer to the same widget without
                # removing the first, and two more on the save after that.
                # Three overlapping saves ran the sprite at 36 frames a
                # second instead of 11, and leaked a timer each time.
                self.saving.set_visible(True)
                return
        self.saving.set_visible(on)
        if on:
            self._saving_frame = 0
            self._saving_dots = 0
            self.saving_label.set_label("Saving")
            if frames:
                self.tux.set_paintable(frames[0])

            def tick():
                if not self.saving.get_visible():
                    self._saving_timer = None
                    return False
                self._saving_frame += 1
                if frames:
                    self.tux.set_paintable(
                        frames[self._saving_frame % len(frames)])
                # The dots run slower than the sprite, so they read as a
                # count rather than a flicker.
                dots = (self._saving_frame // 5) % 4
                if dots != self._saving_dots:
                    self._saving_dots = dots
                    self.saving_label.set_label("Saving" + "." * dots)
                return True
            self._saving_timer = GLib.timeout_add(TUX_MS, tick)
        elif getattr(self, "_saving_timer", None):
            GLib.source_remove(self._saving_timer)
            self._saving_timer = None

    def _on_flash_toggled(self, btn):
        self.flash_enabled = btn.get_active()
        self._refresh_flash_button()
        self._save_settings()

    def _refresh_flash_button(self):
        """Outline when off, filled yellow when armed. The state has to be
        readable at a glance: firing a white screen unexpectedly is worse
        than missing the shot."""
        # One icon, two colours. There is no outline variant of the bolt in
        # the theme, and colour is the clearer signal anyway.
        if self.flash_enabled:
            self.btn_flash.add_css_class("flash-on")
            self.btn_flash.set_tooltip_text("Screen flash ON")
        else:
            self.btn_flash.remove_css_class("flash-on")
            self.btn_flash.set_tooltip_text("Screen flash off")

    def _screen_flash_then(self, fn):
        """Light the subject with the display, then shoot.

        A laptop has no LED, but a white screen at full brightness is a
        usable fill light at arm's length. The delay lets exposure settle,
        otherwise the frame is metered for the dark room it just left.
        """
        self.flash_overlay.remove_css_class("flash")
        self.flash_overlay.add_css_class("flash-white")
        self.flash_overlay.set_opacity(1.0)
        self.flash_overlay.set_visible(True)

        def _shoot():
            fn()
            self.flash_overlay.set_visible(False)
            self.flash_overlay.remove_css_class("flash-white")
            self.flash_overlay.add_css_class("flash")
            return False
        GLib.timeout_add(650, _shoot)

    def _toggle_grid(self):
        """Cycle what is drawn over the picture: HUD, HUD + grid, nothing.

        Folding both into one control keeps the top bar short, and gives a
        way to clear the frame completely for a clean shot.
        """
        self.overlay_mode = (getattr(self, "overlay_mode", 0) + 1) % 4
        self._apply_overlay_mode()
        self._save_settings()

    def _apply_overlay_mode(self):
        """0 readouts, 1 readouts + thirds, 2 readouts + 2x2, 3 nothing."""
        m = self.overlay_mode
        self.grid_visible = m in (1, 2)
        self.grid_widget.set_divisions(3 if m == 1 else 2)
        self.grid_widget.set_visible(self.grid_visible)
        self.rec_indicator.set_visible(m != 3 and self.video_mode)
        self.btn_grid.remove_css_class("pill-active")
        self.btn_grid.remove_css_class("pill-off")
        if m == 1:
            self.btn_grid.add_css_class("pill-active")
            self.btn_grid.set_tooltip_text("Grid: rule of thirds")
        elif m == 2:
            self.btn_grid.add_css_class("pill-active")
            self.btn_grid.set_tooltip_text("Grid: halves")
        elif m == 3:
            self.btn_grid.add_css_class("pill-off")
            self.btn_grid.set_tooltip_text("Overlay off")
        else:
            self.btn_grid.set_tooltip_text("Readouts only, no grid")

    def _toggle_aspect(self):
        old = self.aspect_frame.get_ratio()
        self.aspect_idx = (self.aspect_idx + 1) % len(self.aspects)
        self.btn_aspect.set_label(self.aspects[self.aspect_idx])
        new = {"4:3": 4/3, "16:9": 16/9, "1:1": 1.0}[self.aspects[self.aspect_idx]]

        # Smooth ratio interpolation. No blur: defocusing the whole viewfinder
        # mid-transition is uncomfortable to look at.
        target = Adw.CallbackAnimationTarget.new(
            lambda v: self.aspect_frame.set_ratio(v))
        anim = Adw.TimedAnimation.new(self.aspect_frame, old, new, 380, target)
        anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        anim.play()
        # Aspect changes resize the picture without resizing the window.
        GLib.timeout_add(420, self._fit_hud)
        self._save_settings()

    def _toggle_timer(self):
        seq = [0, 3, 10]
        cur = seq.index(self.timer_sec) if self.timer_sec in seq else 0
        self.timer_sec = seq[(cur + 1) % len(seq)]
        self._refresh_timer_button()
        self._save_settings()

    def _refresh_timer_button(self):
        on = self.timer_sec > 0
        self.timer_label.set_label(f"{self.timer_sec}s" if on else "")
        self.timer_label.set_visible(on)
        self.btn_timer.set_tooltip_text(
            f"Self-timer {self.timer_sec}s" if on else "Self-timer off")
        if on:
            self.btn_timer.add_css_class("pill-active")
            self.btn_timer.remove_css_class("pill-dim")
        else:
            self.btn_timer.remove_css_class("pill-active")
            self.btn_timer.add_css_class("pill-dim")

    def _set_video_mode(self, video):
        if self.recording: return
        self.video_mode = video
        if video:
            self.btn_video.add_css_class("mode-active")
            self.btn_photo.remove_css_class("mode-active")
            self.shutter_core.add_css_class("video")
            # No flash in video: a single white pulse before a clip would
            # light one frame and blow out the start of the take.
            self.btn_flash.set_sensitive(False)
            self.btn_flash.set_tooltip_text("Screen flash is for photos only")
            self.rec_indicator.set_visible(getattr(self, "overlay_mode", 0) in (0, 1))
            self._start_audio_monitor()
            self._hud_standby()
            self._hud_update()
            if not self.hud_timer:
                self.hud_timer = GLib.timeout_add_seconds(1, self._hud_update)
        else:
            self.btn_photo.add_css_class("mode-active")
            self.btn_video.remove_css_class("mode-active")
            self.shutter_core.remove_css_class("video")
            self.btn_flash.set_sensitive(True)
            self._refresh_flash_button()
            self.rec_indicator.set_visible(False)
            if self.hud_timer:
                GLib.source_remove(self.hud_timer); self.hud_timer = None
            self._stop_audio_monitor()
        self._save_settings()

    def _on_signal(self, *_):
        self._on_close()
        self.close()
        return GLib.SOURCE_REMOVE

    def _on_close(self, *_):
        """Release the microphone on the way out.

        Without this the suppression module outlived the window and GNOME
        kept showing the mic as in use after Lens was closed.
        """
        self._stop_audio_monitor()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        return False

    def _audio_src(self):
        """The microphone, as it is.

        Everything that used to sit here has gone: suppression, gate,
        compressor, EQ, limiter and a calibration routine to drive them. Each
        one measured well on its own and the result still sounded worse than
        the bare mic, so the honest thing is to record what the microphone
        hears and leave the shaping to whoever edits it.

        What is left is the volume element, and that is not processing: it is
        how mute works without cutting the file in two, and how the level
        control works at all.
        """
        src = f"pulsesrc device={self.mic_source}" if self.mic_source else "pulsesrc"
        vol = (f"volume name=micvol volume={self.mic_gain:.2f} "
               f"mute={'true' if not self.mic_enabled else 'false'}")
        if self.denoise and have(RNNOISE):
            head = (f"{src} provide-clock=false ! audioconvert ! "
                    f"audioresample quality=8 ! audio/x-raw,rate=48000,channels=1")
            if self.nr_mix > 0.001:
                dry = self.nr_mix
                return (f"{head} ! tee name=nrt "
                        f"nrt. ! queue ! {RNNOISE} ! "
                        f"volume name=nrwet volume={1.0 - dry:.3f} ! nrmix. "
                        f"nrt. ! queue name=nrdry ! "
                        f"volume name=nrdryvol volume={dry:.3f} ! nrmix. "
                        f"audiomixer name=nrmix ! {vol} ! audioconvert")
            return f"{head} ! {RNNOISE} ! {vol} ! audioconvert"
        parts = [f"{src} provide-clock=false", "audioconvert"]
        # Some cheap capture hardware only behaves at one rate.
        if self.mic_rate:
            parts.append("audioresample quality=8")
            parts.append(f"audio/x-raw,rate={self.mic_rate}")
        parts.append(vol)
        parts.append("audioconvert")
        return " ! ".join(parts)

    def _align_nr(self, pipeline):
        """Delay the dry path so the blend does not arrive twice.

        Has to be done on the pad after the pipeline is built, because it is
        a timestamp offset rather than anything expressible in the launch
        string.
        """
        if pipeline is None:
            return
        q = pipeline.get_by_name("nrdry")
        if q is not None:
            pad = q.get_static_pad("src")
            if pad is not None:
                pad.set_offset(NR_LATENCY_MS * Gst.MSECOND)

    # ---- live audio level ----
    def _feed_settings_meter(self, st):
        """Mirror the level into the settings dialog while it is open."""
        m = getattr(self, "set_meter", None)
        if m is None or not m.get_mapped():
            return
        try:
            peak = st.get_value("peak")
            rms = st.get_value("rms")
            pv = max(peak) if peak else -60.0
            rv = max(rms) if rms else -60.0
        except Exception:
            return
        m.set_db(rv)
        self.set_meter_label.set_label(
            "clipping" if pv > -0.5 else f"{pv:5.1f} dB")
        rows = getattr(self, "_diag_rows", None)
        if rows and "levels" in rows:
            # A rolling minimum is the noise floor: the quietest the room has
            # been since the panel opened.
            self._diag_floor = min(getattr(self, "_diag_floor", 0.0) or 0.0, rv)
            rows["levels"].set_subtitle(
                f"peak {pv:.1f} dB   rms {rv:.1f} dB   "
                f"quietest {self._diag_floor:.1f} dB"
                + ("   CLIPPING" if pv > -0.5 else ""))

    def _start_audio_monitor(self):
        """Small audio-only pipeline that reports levels.

        Separate from the recording pipeline so the meter works in standby:
        the point is to see the mic is picking you up *before* you hit
        record. Only runs in video mode with the mic on, so the microphone
        is not live while you are taking photos.
        """
        if self._audio_mon or not (self.mic_available and self.mic_enabled):
            return
        try:
            self._audio_mon = Gst.parse_launch(
                f"{self._audio_src()} ! level interval=100000000 ! "
                f"fakesink sync=false")
            self._align_nr(self._audio_mon)
            bus = self._audio_mon.get_bus()
            bus.add_signal_watch()
            self._audio_mon_handler = bus.connect("message::element", self._on_level)
            self._audio_mon.set_state(Gst.State.PLAYING)
        except Exception as e:
            print(f"Lens: audio monitor failed: {e}", file=sys.stderr)
            self._audio_mon = None

    def _stop_audio_monitor(self):
        if not self._audio_mon:
            return
        try:
            bus = self._audio_mon.get_bus()
            if self._audio_mon_handler:
                bus.disconnect(self._audio_mon_handler)
            bus.remove_signal_watch()
            self._audio_mon.set_state(Gst.State.NULL)
        except Exception:
            pass
        self._audio_mon = None
        self._audio_mon_handler = None
        self.audio_meter.set_db(None)

    def _on_level(self, bus, msg):
        st = msg.get_structure()
        if not st or st.get_name() != "level":
            return
        self._feed_settings_meter(st)
        try:
            rms = st.get_value("rms")
            self.audio_meter.set_db(max(rms) if rms else None)
        except Exception:
            pass

    # ---- overlay menus ----
    @staticmethod
    def _menu_box():
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        b.set_margin_top(6); b.set_margin_bottom(6)
        b.set_margin_start(6); b.set_margin_end(6)
        return b

    @staticmethod
    def _menu_head(text):
        l = Gtk.Label(label=text)
        l.add_css_class("hud-dim")
        l.set_halign(Gtk.Align.START)
        l.set_margin_bottom(4)
        return l

    def _menu_row(self, text, checked, on_click, icon="object-select-symbolic"):
        row = Gtk.Button()
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tick = Gtk.Image.new_from_icon_name(icon if checked else None)
        tick.set_pixel_size(14)
        tick.set_size_request(16, -1)
        lbl = Gtk.Label(label=text)
        lbl.set_halign(Gtk.Align.START); lbl.set_hexpand(True)
        lbl.set_ellipsize(3); lbl.set_max_width_chars(30)
        inner.append(tick); inner.append(lbl)
        row.set_child(inner)
        row.add_css_class("cam-row")
        if checked:
            row.add_css_class("cam-row-active")
        row.connect("clicked", lambda *_: on_click())
        return row

    def _build_res_menu(self, *_):
        if not self.btn_res.get_active():
            return
        box = self._menu_box()
        box.append(self._menu_head("CAPTURE MODE"))
        dev = self.cameras[self.cam_idx][0]
        modes = [m for m in _enumerate_modes(dev) if m[0] == "MJPG"]
        modes.sort(key=lambda m: (-m[1] * m[2], -m[3]))
        # One entry per resolution, keeping its fastest rate. The nominal
        # rate is deliberately not shown: the HUD reports the rate actually
        # being delivered, and printing a different number next to the same
        # camera invites the two to be compared as if both were real.
        seen = set()
        auto = self.mode_override is None
        box.append(self._menu_row("Automatic", auto, self._set_mode_auto))
        for fourcc, w, h, fps in modes:
            if (w, h) in seen:
                continue
            seen.add((w, h))
            cur = (not auto and tuple(self.mode_override)[:2] == (w, h))
            box.append(self._menu_row(
                f"{w}\u00d7{h}", cur,
                lambda w=w, h=h, f=fps: self._set_mode(w, h, f)))
        self.res_popover.set_child(box)

    def _set_mode_auto(self):
        self.res_popover.popdown()
        self.mode_override = None
        self._save_settings()
        self._start_pipeline()

    def _set_mode(self, w, h, fps):
        self.res_popover.popdown()
        self.mode_override = [w, h, fps]
        self._save_settings()
        self._start_pipeline()

    def _build_fmt_menu(self, *_):
        if not self.btn_fmt.get_active():
            return
        box = self._menu_box()
        box.append(self._menu_head("RECORD AS"))
        for name, (ext, _mux, venc, aenc) in CONTAINERS.items():
            v = "VP8" if venc.startswith("vp8") else "H.264"
            a = "AAC" if aenc.startswith("voaac") else "Opus"
            box.append(self._menu_row(f"{name}   {v} / {a}",
                                      name == self.container,
                                      lambda n=name: self._set_container(n)))
        self.fmt_popover.set_child(box)

    def _set_container(self, name):
        self.fmt_popover.popdown()
        if self.recording:
            return          # cannot change the container mid-file
        self.container = name
        self._save_settings()
        self._hud_update()

    def _build_mic_menu(self):
        box = self._menu_box()
        box.append(self._menu_head("AUDIO INPUT"))
        box.append(self._menu_row("System default", self.mic_source is None,
                                  lambda: self._set_mic_source(None)))
        for name, desc in list_audio_sources():
            box.append(self._menu_row(desc, self.mic_source == name,
                                      lambda n=name: self._set_mic_source(n)))
        self.mic_popover.set_child(box)
        self.mic_popover.popup()

    def _set_mic_source(self, name):
        self.mic_popover.popdown()
        self.mic_source = name
        self._save_settings()
        # Restart the meter so it listens to the new input straight away.
        self._stop_audio_monitor()
        if self.video_mode:
            self._start_audio_monitor()

    def _on_view_clicked(self, gesture, n_press, x, y):
        """Right-click the picture: choose which readouts are shown."""
        box = self._menu_box()
        box.append(self._menu_head("OVERLAY"))
        items = [
            ("Recording state", self._rec_row),
            ("Clock",           self.hud_clock),
            ("Battery",         self._bat_row),
            ("Microphone",      self._audio_row),
            ("Format and rate", self._info_row),
        ]
        for label, wdg in items:
            on = wdg not in self._hud_hidden
            box.append(self._menu_row(
                label, on, lambda w=wdg: self._toggle_hud_item(w)))
        self.view_popover.set_child(box)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.view_popover.set_pointing_to(rect)
        self.view_popover.popup()

    def _toggle_hud_item(self, wdg):
        self.view_popover.popdown()
        if wdg in self._hud_hidden:
            self._hud_hidden.remove(wdg)
            # Switching something on while the whole overlay is off via the
            # '#' button would otherwise do nothing visible.
            if self.overlay_mode == 3:
                self.overlay_mode = 0
                self._apply_overlay_mode()
                self._save_settings()
        else:
            self._hud_hidden.add(wdg)
        self._fit_hud()

    def _refresh_mic_icon(self):
        for c in ("hud-btn-off", "hud-btn-muted"):
            self.btn_mic.remove_css_class(c)
        if not self.mic_available:
            # Nothing to record from: state the fact, quietly.
            self.mic_icon.set_from_icon_name("microphone-disabled-symbolic")
            self.mic_icon.set_pixel_size(22)
            self.btn_mic.set_tooltip_text("No microphone found")
            self.btn_mic.add_css_class("hud-btn-off")
        elif self.mic_enabled:
            self.mic_icon.set_from_icon_name("audio-input-microphone-symbolic")
            self.mic_icon.set_pixel_size(22)
            self.btn_mic.set_tooltip_text("Microphone on, click to mute")
        else:
            # Muted is a warning, not a disabled state: you are about to
            # record something silent. Grey made it nearly invisible.
            self.mic_icon.set_from_icon_name("microphone-disabled-symbolic")
            self.mic_icon.set_pixel_size(22)
            self.btn_mic.set_tooltip_text("Microphone MUTED, click to unmute")
            self.btn_mic.add_css_class("hud-btn-muted")
        self.btn_mic.set_sensitive(self.mic_available)
        self.audio_meter.set_muted(not (self.mic_available and self.mic_enabled))

    def _toggle_mic(self):
        if not self.mic_available:
            return
        self.mic_enabled = not self.mic_enabled
        if self.recording:
            # Live. The branch is already there and only its mute flips, so
            # the take carries on and the file stays in one piece.
            v = self.pipeline.get_by_name("micvol")
            if v is not None:
                v.props.mute = not self.mic_enabled
        elif self.mic_enabled and self.video_mode:
            self._start_audio_monitor()
        else:
            self._stop_audio_monitor()
        self._refresh_mic_icon()
        self._fit_hud()
        self._save_settings()

    def _fit_hud(self, *_):
        """Shed whole corners, never half of one.

        Each row keeps its left corner and drops its right (and the clock)
        when the picture gets narrow, so what survives longest is: am I
        rolling and for how long, and is my voice going in. A row whose left
        corner alone will not fit hides completely.
        """
        stack_w = self.frame_stack.get_width()
        if stack_w <= 1:
            return False
        # side margins (16 each) plus a gap so opposite corners never touch
        avail = stack_w - 32 - 28

        def w_of(*widgets):
            return sum(x.get_preferred_size()[1].width for x in widgets
                       if x.get_visible())

        # Restore exactly what the checklist says, then let the fitting below
        # hide more if there is no room. Anything the fitting hides comes back
        # on the next pass, because this rebuilds from the checklist rather
        # than from current visibility.
        self._hud_top.set_visible(True)
        self._hud_bot.set_visible(True)
        for wdg in self._hud_items:
            wdg.set_visible(wdg not in self._hud_hidden)

        # Top row is a CenterBox, so the clock is centred in the whole row
        # rather than in the space left over. Comparing the sum of the three
        # groups is not enough: a wide left corner reaches past where the
        # centred clock starts and they overlap even when the total fits.
        # What matters is whether each side clears its half.
        inner = stack_w - 32

        def too_wide(row):
            """Ask the row how wide it needs to be, rather than adding up
            its parts.

            The parts summed to less than the space available while the row
            still overflowed by 31px, because a CenterBox centring its middle
            child around a wide start child needs more width than start plus
            centre plus end. Modelling that arithmetic is how the right-hand
            readout ended up sitting over the black border, and the row was
            never asked what it actually wanted.
            """
            return row.get_preferred_size()[1].width > inner

        # Top row: shed the centre first, then the right corner, then all of
        # it, re-measuring after each so the next decision sees the truth.
        if too_wide(self._hud_top):
            self.hud_clock.set_visible(False)
            if too_wide(self._hud_top):
                self._bat_row.set_visible(False)
                if too_wide(self._hud_top):
                    self._hud_top.set_visible(False)

        # Bottom row: sound on the left, format on the right.
        if too_wide(self._hud_bot):
            self._info_row.set_visible(False)
            if too_wide(self._hud_bot):
                self._hud_bot.set_visible(False)
        return False

    def _hud_standby(self):
        self.rec_state_label.set_label("STBY")
        self.rec_state_label.add_css_class("standby")
        self.rec_dot.add_css_class("standby")
        self.rec_dot.remove_css_class("blink")
        self.rec_time_label.set_label("00:00:00")

    def _hud_update(self):
        """Once a second: clock, battery, and the remaining-record estimate."""
        if not self.video_mode:
            self.hud_timer = None
            return False

        # Time only. The date costs about 170px of a picture that may only
        # be 380 wide at 1:1, and it is already in the filename and the file
        # metadata, so it was the least useful thing taking the most room.
        self.hud_clock.set_label(datetime.datetime.now().strftime("%H:%M:%S"))

        # Frames actually delivered in the last second, which is the real
        # rate: negotiated caps say 30 but a busy machine may not hit it.
        self._fps_shown = self._fps_count
        self._fps_count = 0
        eff = self.effective_res()
        res = f"{eff[0]}\u00d7{eff[1]}" if eff else ""
        # Two digits always. Dropping from 30 to 9 fps loses a character and
        # the whole readout, resolution included, slides sideways: the number
        # jitters exactly when the thing it is reporting has gone wrong and
        # you most want to read it. The font already has tabular figures, so
        # padding to a fixed width holds it still.
        rate = f"{self._fps_shown:2.0f} FPS" if self._fps_shown else ""
        self.hud_fps.set_label("  ".join(x for x in (res, rate) if x))
        self._refresh_mic_icon()

        ext, _mux, venc, aenc = CONTAINERS[self.container]
        vcodec = "VP8" if venc.startswith("vp8") else "H.264"
        acodec = ("AAC" if aenc.startswith("voaac") else "OPUS") \
            if (self.mic_available and self.mic_enabled) else "MUTE"
        self.hud_format.set_label(f"{self.container.upper()}  {vcodec}  {acodec}")

        pct, status, energy, watts = read_battery()
        # The battery's own status goes stale: BAT0 can still read Charging
        # after the charger is out. Mains is the ground truth, so if nothing
        # is plugged in then nothing is charging, whatever BAT0 claims.
        charging = status.lower().startswith("charg") and mains_online() is not False

        # power_now is instantaneous and jumps around, which made the estimate
        # swing between 2:35 and 0:37 between ticks. Average the last 30
        # samples so the number is steady enough to act on.
        if watts:
            self._power_samples.append(watts)
            del self._power_samples[:-30]
        secs = None
        if energy and self._power_samples:
            avg = sum(self._power_samples) / len(self._power_samples)
            if avg > 0:
                secs = int(energy / avg * 3600)
        if pct is None:
            self.bat_label.set_label("--%")
            self.bat_left_label.set_label("")
        else:
            self.bat_gauge.set_level(pct / 100.0, charging)
            self.bat_label.set_label(f"{pct}%")
            if charging:
                self.bat_left_label.set_label("CHG")
            elif secs:
                # Runtime on battery is the ceiling on how long you can keep
                # recording, which is the number that actually matters here.
                self.bat_left_label.set_label(
                    f"{secs // 3600}:{(secs % 3600) // 60:02d}")
            else:
                self.bat_left_label.set_label("")
        # The readouts change width as their content does: the battery goes
        # from '--%' at startup to '83% CHG' a second later, and the clock
        # appears from nothing. Refitting only on resize meant that growth
        # was never checked, and the top right corner sat outside the
        # picture until something else happened to trigger a fit.
        self._fit_hud()
        return True

    def _toggle_maximize(self):
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def _on_maximized_changed(self, *_):
        big = self.is_maximized() or self.is_fullscreen()
        self.btn_max.get_child().set_from_icon_name(
            "window-restore-symbolic" if big else "window-maximize-symbolic")
        self.btn_max.set_tooltip_text("Restore" if big else "Maximize")

    def _toggle_fullscreen(self):
        if self.is_fullscreen(): self.unfullscreen()
        else: self.fullscreen()
        self._on_maximized_changed()

    FLIP_MS = 140
    # Ceiling on the close, so a slow camera does not make its direction
    # feel sluggish next to the other one.
    FLIP_MAX_MS = 440

    def _show_camera_menu(self):
        """List every camera so one can be picked directly."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6); box.set_margin_bottom(6)
        box.set_margin_start(6); box.set_margin_end(6)
        head = Gtk.Label(label="CAMERA")
        head.add_css_class("hud-dim")
        head.set_halign(Gtk.Align.START)
        head.set_margin_bottom(4)
        box.append(head)

        # Off is a choice, not just a failure. It has to live here because
        # this is the menu people actually find: the other route to it was
        # the NO SIGNAL banner, which you can only see once you already have
        # no signal, so there was no way in.
        entries = [(None, "No camera")] + [
            (i, cam[1].split(":")[0].strip() or cam[0])
            for i, cam in enumerate(self.cameras)]
        for i, name in entries:
            # Card names carry a redundant "USB2.0 HD UVC WebCam: USB2.0 HD"
            # style repeat, so keep the part before the colon.
            row = Gtk.Button()
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            chosen = (i is None and self.camera_off) or \
                     (i is not None and not self.camera_off and i == self.cam_idx)
            tick = Gtk.Image.new_from_icon_name(
                "object-select-symbolic" if chosen else
                ("camera-disabled-symbolic" if i is None else "camera-photo-symbolic"))
            tick.set_pixel_size(14)
            lbl = Gtk.Label(label=name)
            lbl.set_halign(Gtk.Align.START); lbl.set_hexpand(True)
            lbl.set_ellipsize(3)          # PANGO_ELLIPSIZE_END
            lbl.set_max_width_chars(28)
            inner.append(tick); inner.append(lbl)
            row.set_child(inner)
            row.add_css_class("cam-row")
            if chosen:
                row.add_css_class("cam-row-active")
            row.connect("clicked", lambda _b, idx=i: self._select_camera(idx))
            box.append(row)

        self.cam_popover.set_child(box)
        self.cam_popover.popup()

    def _flip_or_choose(self):
        """Cycle when there is somewhere to cycle to, otherwise offer the list."""
        if len(self.cameras) > 1 and not self.camera_off:
            self._flip_camera()
        else:
            self._show_camera_menu()

    def _select_camera(self, idx):
        self.cam_popover.popdown()
        if idx is None:
            self._camera_none()
            return
        if self.camera_off:
            # Coming back from off: there is no running pipeline to animate
            # a flip out of, so just start the one that was asked for.
            self.camera_off = False
            self.cam_idx = max(0, min(idx, len(self.cameras) - 1))
            self._save_settings()
            self._refresh_flip_button()
            self._set_no_signal(False)
            self._start_pipeline()
            return
        if idx == self.cam_idx or self._flipping:
            return
        # Reuse the flip animation, just aimed at a specific camera.
        self._flip_camera(target=idx)

    def _flip_camera(self, target=None):
        if len(self.cameras) < 2: return
        if self._flipping: return          # ignore taps during the turn
        self._flipping = True
        # Toggle the mirror and leave it there until the next press.
        if self.flip_icon.has_css_class("mirrored"):
            self.flip_icon.remove_css_class("mirrored")
        else:
            self.flip_icon.add_css_class("mirrored")

        # Bring the new camera up *now*, in parallel with the animation,
        # rather than waiting for the squeeze to finish first. Starting it
        # afterwards meant the view sat shut for the few hundred ms the
        # camera needed, which is the pause that made this look broken.
        self._flip_shut = False
        self._flip_commit = None
        self._flip_ready = False
        # Hold the last live frame as a still while the cameras swap. The
        # view never blanks, the layout never collapses, and the old pipeline
        # can be torn down immediately instead of racing the new one.
        frozen = self._freeze_frame()
        if frozen is not None:
            self.picture.set_paintable(frozen)

        # Stretch the close to roughly how long this camera takes to wake up,
        # so the motion is still going when the frame lands instead of
        # finishing early and sitting frozen. Continuous and slow reads as
        # smooth; fast then stalled does not.
        nxt = (self.cam_idx + 1) % len(self.cameras) if target is None else target
        wake = self._cam_latency.get(self.cameras[nxt][2], 380)
        for cls in ("flip-fast", "flip-med", "flip-slow", "flip-vslow", "flip-xslow"):
            self.frame_stack.remove_css_class(cls)
        # Cover the whole wake again, but with a front-loaded curve rather
        # than a slower even one. The eye judges speed by the first movement,
        # not the total, so most of the travel happens early and the rest
        # creeps in. Capping the duration instead just moved the stall to the
        # end.
        want = wake * 0.9
        tier, close_ms = min(
            (("flip-fast", 190), ("flip-med", 300), ("flip-slow", 420),
             ("flip-vslow", 560), ("flip-xslow", 700)),
            key=lambda t: abs(t[1] - want))
        self.frame_stack.add_css_class(tier)
        self._flip_t0 = time.monotonic()

        # Let the duration change land before the transform changes. Setting
        # transition-duration and transform in the same style update makes GTK
        # apply the transform without animating it, which is the snap seen
        # when flipping toward the faster camera (the tier, and so the
        # duration, changes in that direction).
        def _begin_close():
            self.frame_stack.add_css_class("flipped")
            GLib.timeout_add(close_ms, self._flip_shut_cb)
            return False
        GLib.idle_add(_begin_close)

        self.cam_idx = (self.cam_idx + 1) % len(self.cameras) if target is None else target
        self._save_settings()
        cam = self.cameras[self.cam_idx]
        print(f"Lens: switching to camera {self.cam_idx}: {cam[1]} ({cam[0]})")
        # Separate devices, so the replacement runs while the old one is still
        # feeding the viewfinder.
        self._start_pipeline(defer_attach=True, on_ready=self._flip_ready_cb)

    def _flip_shut_cb(self):
        self._flip_shut = True
        self._flip_open_when_ready()
        return False

    def _flip_ready_cb(self, commit):
        if getattr(self, "_flip_t0", None):
            ms = int((time.monotonic() - self._flip_t0) * 1000)
            cam_id = self.cameras[self.cam_idx][2]
            prev = self._cam_latency.get(cam_id)
            # Smooth it a little so one slow start does not skew the tier.
            self._cam_latency[cam_id] = ms if prev is None else int(prev * 0.6 + ms * 0.4)
            self._flip_t0 = None
            self._save_settings()
        self._flip_commit = commit
        self._flip_ready = True
        self._flip_open_when_ready()

    def _flip_open_when_ready(self):
        """Open back out once the view is fully shut AND the new camera has a
        frame. Whichever finishes last drives it."""
        if not (self._flip_shut and self._flip_ready):
            return
        if self._flip_commit:
            self._flip_commit()
            self._flip_commit = None
        self.frame_stack.add_css_class("opening")

        def _begin_open():
            self.frame_stack.remove_css_class("flipped")
            GLib.timeout_add(320, self._flip_done)
            return False
        GLib.idle_add(_begin_open)

    def _flip_done(self):
        self.frame_stack.remove_css_class("opening")
        self._flipping = False
        return False

    def _on_shutter(self):
        if self.video_mode:
            if self.recording: self._stop_recording()
            else: self._start_recording()
            return
        if self.timer_sec > 0:
            self.countdown_val = self.timer_sec
            self.countdown_label.set_visible(True)
            self.countdown_label.set_label(str(self.countdown_val))
            GLib.timeout_add_seconds(1, self._tick_countdown)
        elif self.flash_enabled:
            self._screen_flash_then(self._take_photo)
        else:
            self._take_photo()

    def _tick_countdown(self):
        self.countdown_val -= 1
        if self.countdown_val <= 0:
            self.countdown_label.set_visible(False)
            if self.flash_enabled:
                self._screen_flash_then(self._take_photo)
            else:
                self._take_photo()
            return False
        self.countdown_label.set_label(str(self.countdown_val))
        return True

    def _grab_full_res(self):
        """One frame at the sensor's largest mode, by briefly retuning it.

        The preview stream is capped so the viewfinder stays at 30fps, which
        also capped stills. This frees the device, pulls a single frame at
        full resolution, and puts the preview back. The source is already
        MJPG so the bytes are saved as-is, with no decode/re-encode.
        """
        dev = self.cameras[self.cam_idx][0]
        still, prev = best_still_mode(dev), best_mode(dev)
        if not still or not prev or still[1] * still[2] <= prev[1] * prev[2]:
            return None                     # nothing to gain on this camera
        _, w, h, fps = still

        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            # Bounded. CLOCK_TIME_NONE waits forever, and a live source that
            # will not shut down takes the whole UI with it: this is what
            # made switching cameras hang.
            self.pipeline.get_state(2 * Gst.SECOND)
        data = None
        try:
            grab = Gst.parse_launch(
                f"v4l2src device={dev} num-buffers=6 ! "
                f"image/jpeg,width={w},height={h},framerate={int(round(fps))}/1 ! "
                f"appsink name=still emit-signals=false max-buffers=6 "
                f"drop=false sync=false")
            sink = grab.get_by_name("still")
            grab.set_state(Gst.State.PLAYING)
            # Keep pulling: the first frames come out before auto-exposure has
            # settled, so the last good one is the one worth keeping.
            for _ in range(6):
                sample = sink.emit("try-pull-sample", int(1.5 * Gst.SECOND))
                if not sample:
                    break
                buf = sample.get_buffer()
                ok, mi = buf.map(Gst.MapFlags.READ)
                if not ok:
                    continue
                try:
                    blob = bytes(mi.data)
                finally:
                    buf.unmap(mi)
                if len(blob) > 1000 and blob[:2] == b"\xff\xd8":
                    data = blob
            grab.set_state(Gst.State.NULL)
            grab.get_state(2 * Gst.SECOND)
        except Exception as e:
            print(f"Lens: full-res grab failed ({e}), using preview frame",
                  file=sys.stderr)
        self._start_pipeline()
        if data:
            print(f"Lens: captured at {w}x{h}")
        return data

    def _take_photo(self):
        # Prefer a full-resolution grab; fall back to the preview stream.
        data = self._grab_full_res()
        if data:
            self._save_photo(data)
            return
        appsink = self.pipeline.get_by_name("photosink")
        if not appsink:
            print("Lens: no photosink in pipeline"); return

        # Try up to 6 times over ~1.5 sec — first buffer can be tiny warmup data.
        # NOTE: try_pull_sample is a GObject signal in Python, not a direct method.
        data = None
        for attempt in range(6):
            sample = appsink.emit("try-pull-sample", int(0.25 * Gst.SECOND))
            if not sample:
                print(f"Lens: attempt {attempt+1}: no sample"); continue
            buf = sample.get_buffer()
            size = buf.get_size()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok: continue
            try:
                blob = bytes(mapinfo.data)  # copy out before unmap
            finally:
                buf.unmap(mapinfo)
            # A valid JPEG is at least a few KB and starts with 0xFF 0xD8
            if len(blob) > 1000 and blob[:2] == b"\xff\xd8":
                data = blob; break
            print(f"Lens: attempt {attempt+1}: got {size} bytes (not valid JPEG yet)")

        if not data:
            print("Lens: capture failed — no valid JPEG from pipeline")
            return
        self._save_photo(data)

    def _save_photo(self, data):
        path = next_name(self.pic_dir, self.name_prefix, "IMG", "jpg")
        path.write_bytes(data)
        # Center-crop to the current aspect ratio so the saved image matches
        # what the viewfinder showed.
        try:
            target_ratio = {"4:3": 4/3, "16:9": 16/9, "1:1": 1.0}[self.aspects[self.aspect_idx]]
            pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
            src_w, src_h = pb.get_width(), pb.get_height()
            src_ratio = src_w / src_h
            if abs(src_ratio - target_ratio) > 0.01:
                if src_ratio > target_ratio:   # too wide → crop sides
                    new_w = int(src_h * target_ratio); new_h = src_h
                    x = (src_w - new_w) // 2;   y = 0
                else:                          # too tall → crop top/bottom
                    new_w = src_w; new_h = int(src_w / target_ratio)
                    x = 0;   y = (src_h - new_h) // 2
                pb.new_subpixbuf(x, y, new_w, new_h).savev(str(path), "jpeg", ["quality"], ["92"])
        except Exception as e:
            print("crop failed, keeping original:", e)
        print(f"Lens: saved {path} ({path.stat().st_size // 1024} KB)")
        self.last_photo = str(path)
        # Flash animation + thumbnail update
        self._flash()
        self._refresh_deck()

    def _flash(self):
        """Full-screen black blink ~150ms, like a shutter closing. A white
        flash is what a real camera does, but on a screen at night it is
        genuinely painful, so this goes dark instead."""
        self.flash_overlay.set_visible(True)
        self.flash_overlay.set_opacity(0.85)
        # Fade out over 200ms
        def fade():
            o = self.flash_overlay.get_opacity() - 0.15
            if o <= 0:
                self.flash_overlay.set_visible(False)
                return False
            self.flash_overlay.set_opacity(o)
            return True
        GLib.timeout_add(20, fade)

    def _rec_tick(self):
        # _stopping as well as recording: the flag stays set until the file
        # is written, so checking recording alone left the clock running all
        # the way through saving and reporting a longer take than was shot.
        if not self.recording or self._stopping:
            return False
        self.rec_seconds += 1
        h, rem = divmod(self.rec_seconds, 3600)
        m, s = divmod(rem, 60)
        self.rec_time_label.set_label(f"{h:02d}:{m:02d}:{s:02d}")
        # Blink dot every tick
        if self.rec_dot.has_css_class("blink"): self.rec_dot.remove_css_class("blink")
        else: self.rec_dot.add_css_class("blink")
        return True

    def _start_recording(self):
        ext, muxer, venc, aenc = CONTAINERS[self.container]
        path = next_name(self.vid_dir, self.name_prefix, "VID", ext)
        # Freeze what is on screen before the preview pipeline goes away, so
        # the Picture keeps a paintable with a real size across the swap.
        frozen = self._freeze_frame()
        if frozen is not None:
            self.picture.set_paintable(frozen)
        # Rebuild pipeline for recording
        self.pipeline.set_state(Gst.State.NULL)
        dev = self.cameras[self.cam_idx][0]
        # Clips were silent: there was no audio branch at all. Opus into the
        # same matroska container, dropped if there is no capture source so a
        # machine without a mic still records video.
        # Built whenever a mic exists, muted rather than absent. You cannot
        # unmute into a branch that was never plumbed, and splicing one into
        # a running pipeline would break the file in two.
        self.mic_active = self.mic_available
        src = self._audio_src()
        # Every branch feeding the muxer gets a queue, and audio gets a
        # generous one. Without it pulsesrc fed the muxer directly while
        # x264 was encoding 1.9MP frames on the same pipeline, so the audio
        # branch was starved and the result crackled. audiorate patches any
        # timestamp gaps that slip through rather than letting them become
        # clicks.
        # 1s is plenty to survive an encoder stall, and every buffered
        # second has to drain on EOS before the file is complete.
        aq = ("queue max-size-time=1000000000 max-size-buffers=0 "
              "max-size-bytes=0")
        audio = (f" {src} ! {aq} ! audioresample ! audiorate ! "
                 f"{aenc} ! queue ! mux. ") if self.mic_active else ""
        # Zooming crops, so without a scale back up the encoded size would
        # change the moment you touched the zoom mid-take, renegotiating caps
        # under a running encoder. Lock it to what the frame is now.
        self._apply_frame_rate_policy()
        rw, rh = self.cur_res or (1280, 720)
        rw -= rw % 2
        rh -= rh % 2
        # Crop, balance and effects ahead of the tee, exactly as the standby
        # pipeline has them. They were missing here entirely, so hitting
        # record silently threw away the zoom and the filter: the viewfinder
        # kept showing them because it was still the old pipeline until this
        # one took over, and then everything quietly snapped back to 1x and
        # no look at all. Ahead of the tee so the file matches the preview.
        self.pipeline = Gst.parse_launch(
            f"{self._source_for(dev)} ! videocrop name=zoom ! "
            f"videobalance name=vb ! coloreffects name=fx ! tee name=t "
            # The display branch leaks, exactly as it does in standby. With a
            # plain queue it shared the encoder's backpressure and the
            # viewfinder fell to about 7fps while the file itself was fine:
            # the preview is the one place where dropping a late frame is
            # always the right answer, since nobody is going to watch it back.
            f"t. ! queue max-size-buffers=2 leaky=downstream ! "
            f"videoflip name=mirror ! videoconvert ! "
            f"gtk4paintablesink name=sink "
            # The encode branch deliberately does NOT leak. Dropping a frame
            # from the preview is free; dropping one from the file is not,
            # and a viewfinder stutter is the right price for a complete
            # recording.
            f"t. ! queue max-size-buffers=12 ! "
            f"videoflip name=recmirror ! "
            f"videoscale ! video/x-raw,width={rw},height={rh} ! "
            f"videoconvert ! {venc} ! "
            f"queue ! mux. "
            f"{audio}"
            f"{muxer} name=mux ! filesink location={path}"
        )
        sink = self.pipeline.get_by_name("sink")
        # Re-point at the new elements, or zoom and filters go on addressing
        # the pipeline that has just been torn down.
        self.zoom_elem = self.pipeline.get_by_name("zoom")
        self.vb_elem = self.pipeline.get_by_name("vb")
        self.fx_elem = self.pipeline.get_by_name("fx")
        self._apply_zoom()
        self._apply_filter()
        new_paintable = sink.props.paintable
        self.paintable = new_paintable
        # Hold the frozen frame until the new pipeline is actually producing.
        # Attaching a gtk4paintablesink paintable before its first frame gives
        # the Picture a zero intrinsic size, which collapsed the viewfinder to
        # half height for about 200ms every time recording started. The
        # preview path already deferred for this reason; this one did not.
        #
        # Count frames off this paintable as well. Only the preview pipeline
        # did, so the frame counter froze the moment recording started, and
        # three seconds later the watchdog announced NO SIGNAL over a
        # recording that was working perfectly and saved perfectly. The
        # counter also feeds the HUD's fps readout, which was reading zero
        # for the same reason.
        new_paintable.connect("invalidate-contents", self._on_frame)
        if frozen is not None:
            state = {"done": False}

            def swap(*_):
                if state["done"]:
                    return False
                state["done"] = True
                if state.get("h"):
                    try:
                        new_paintable.disconnect(state["h"])
                    except Exception:
                        pass
                self.picture.set_paintable(new_paintable)
                return False
            state["h"] = new_paintable.connect("invalidate-contents", swap)
            GLib.timeout_add(1200, swap)    # fallback if no frame ever comes
        else:
            self.picture.set_paintable(new_paintable)
        # Fresh pipeline, so the flips are back at their defaults.
        self._apply_mirror()
        self._align_nr(self.pipeline)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.recording = True
        self.shutter_core.add_css_class("recording")
        self.rec_seconds = 0
        self.rec_time_label.set_label("00:00:00")
        self.rec_state_label.set_label("REC")
        self.rec_state_label.remove_css_class("standby")
        self.rec_dot.remove_css_class("standby")
        self.rec_indicator.set_visible(True)
        GLib.timeout_add_seconds(1, self._rec_tick)

    def _stop_recording(self):
        """Ask the pipeline to finish, and wait for it without blocking.

        This used to send EOS and then block the main thread on
        timed_pop_filtered for up to 2s. That froze the UI, and 2s was not
        enough: the audio queue holds seconds of buffered samples that have
        to drain through the encoder and muxer first, so the pipeline was
        torn down mid-drain and the file came out truncated.
        """
        if not self.recording or self._stopping:
            return
        self._stopping = True
        self.rec_state_label.set_label("SAVE")
        self._show_saving(True)
        bus = self.pipeline.get_bus()
        # add_signal_watch, without which message::eos is never dispatched as
        # a signal and this handler cannot fire. It was missing, so every save
        # ran to the backstop below: 8 seconds, every time, no matter how
        # short the clip or how quickly the pipeline had actually finished.
        bus.add_signal_watch()
        self._eos_handler = bus.connect("message::eos", lambda *_: self._finish_stop())
        self.pipeline.send_event(Gst.Event.new_eos())
        # Generous backstop. Reaching it means something went wrong, and a
        # slightly damaged file beats a hung app.
        self._eos_timeout = GLib.timeout_add(8000, self._finish_stop)

    def _finish_stop(self, *_):
        if not self._stopping:
            return False
        self._stopping = False
        if self._eos_timeout:
            GLib.source_remove(self._eos_timeout)
            self._eos_timeout = None
        old = self.pipeline
        # Same freeze as on the way in. Tearing the recording pipeline down
        # leaves the Picture holding a paintable with no frames, so without
        # this the viewfinder collapsed and went black for a moment on stop
        # exactly as it used to on start.
        frozen = self._freeze_frame()
        if frozen is not None:
            self.picture.set_paintable(frozen)
        if old:
            if self._eos_handler:
                try:
                    old.get_bus().disconnect(self._eos_handler)
                    old.get_bus().remove_signal_watch()
                except Exception:
                    pass
                self._eos_handler = None
            old.set_state(Gst.State.NULL)
            # Bounded: CLOCK_TIME_NONE waits forever, and a source that will
            # not go down would hang the whole app rather than just this.
            old.get_state(2 * Gst.SECOND)
        self.pipeline = None
        self.recording = False
        self.shutter_core.remove_css_class("recording")
        # Back to standby rather than hiding the HUD: still in video mode.
        self._hud_standby()
        self._start_pipeline(defer_attach=frozen is not None)
        self._show_saving(False)
        print(f"Lens: saved video ({self.rec_seconds}s)")
        return False

    def _open_gallery(self, *_):
        folder = self.vid_dir if self.video_mode else self.pic_dir
        Gio.AppInfo.launch_default_for_uri("file://" + str(folder), None)

    def _open_settings(self):
        """Where files go, what they are called, how long the bin keeps them."""
        win = Adw.PreferencesWindow()
        win.set_transient_for(self)
        win.set_modal(True)
        win.set_default_size(520, 420)
        win.set_title("Lens Settings")

        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(title="Where things are saved")

        def folder_row(title, getter, setter):
            row = Adw.ActionRow(title=title, subtitle=str(getter()))
            btn = Gtk.Button(label="Choose")
            btn.set_valign(Gtk.Align.CENTER)

            def pick(*_):
                dlg = Gtk.FileDialog()
                dlg.set_title(title)
                dlg.set_initial_folder(Gio.File.new_for_path(str(getter())))

                def done(d, res):
                    try:
                        f = d.select_folder_finish(res)
                    except Exception:
                        return
                    if f:
                        setter(pathlib.Path(f.get_path()))
                        row.set_subtitle(str(getter()))
                        self._save_settings()
                        self._refresh_deck()
                dlg.select_folder(win, None, done)
            btn.connect("clicked", pick)
            row.add_suffix(btn)
            return row

        def set_pic(v):
            self.pic_dir = v; v.mkdir(parents=True, exist_ok=True)

        def set_vid(v):
            self.vid_dir = v; v.mkdir(parents=True, exist_ok=True)

        grp.add(folder_row("Photos", lambda: self.pic_dir, set_pic))
        grp.add(folder_row("Videos", lambda: self.vid_dir, set_vid))
        page.add(grp)

        grp2 = Adw.PreferencesGroup(
            title="File names",
            description="Files are named PREFIX_IMG_0001.jpg and PREFIX_VID_0001")
        pref = Adw.EntryRow(title="Prefix")
        pref.set_text(self.name_prefix)

        def prefix_done(*_):
            v = (pref.get_text() or "").strip().replace("/", "")
            if v:
                self.name_prefix = v
                self._save_settings()
        pref.connect("apply", prefix_done)
        pref.connect("notify::text", prefix_done)
        grp2.add(pref)
        page.add(grp2)

        grpa = Adw.PreferencesGroup(
            title="Microphone",
            description="Applied to the recording only. Nothing here touches "
                        "the system capture level, so other apps are unaffected")

        # A meter, because gain is not a number anyone can guess. Talk at it
        # and set the slider so the loud parts sit in the amber, not the red.
        self.set_meter = AudioMeter()
        self.set_meter.set_content_width(150)
        self.set_meter.set_content_height(16)
        meter_row = Adw.ActionRow(title="Input level")
        meter_row.set_subtitle("Speak normally and watch this while you set the gain")
        self.set_meter_label = Gtk.Label(label="--")
        self.set_meter_label.add_css_class("hud-mono")
        self.set_meter_label.set_width_chars(9)
        meter_row.add_suffix(self.set_meter_label)
        meter_row.add_suffix(self.set_meter)
        grpa.add(meter_row)

        gain_row = Adw.ActionRow(title="Gain")
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -12.0, 18.0, 0.5)
        scale.set_size_request(230, -1)
        scale.set_draw_value(False)
        scale.set_valign(Gtk.Align.CENTER)
        scale.add_mark(0.0, Gtk.PositionType.BOTTOM, None)
        # Slider in decibels, not multiples. Doubling from 1x to 2x is a big
        # step and 7x to 8x is barely audible, so a linear multiplier gives a
        # slider that does almost everything in its first third.
        scale.set_value(20.0 * math.log10(max(self.mic_gain, 0.01)))

        def show_gain():
            db = 20.0 * math.log10(max(self.mic_gain, 0.01))
            gain_row.set_subtitle(f"{db:+.1f} dB  ({self.mic_gain:.2f}x)")
        show_gain()

        def gain_moved(sc):
            self.mic_gain = 10.0 ** (sc.get_value() / 20.0)
            show_gain()
            for pipe in (self.pipeline, self._audio_mon):
                if pipe is None:
                    continue
                v = pipe.get_by_name("micvol")
                if v is not None:      # live, including on a running take
                    v.props.volume = self.mic_gain
            self._save_settings()
        scale.connect("value-changed", gain_moved)
        gain_row.add_suffix(scale)
        grpa.add(gain_row)

        RATES = [0, 16000, 22050, 32000, 44100, 48000]
        rate_row = Adw.ComboRow(title="Capture rate")
        rate_row.set_subtitle("Leave on automatic unless the mic sounds wrong. "
                              "Some cheap ones only behave at one rate")
        rate_row.set_model(Gtk.StringList.new(
            ["Automatic"] + [f"{r // 1000} kHz" if r % 1000 == 0 else f"{r} Hz"
                             for r in RATES[1:]]))
        rate_row.set_selected(RATES.index(self.mic_rate) if self.mic_rate in RATES else 0)

        def rate_done(row, *_):
            self.mic_rate = RATES[row.get_selected()]
            self._save_settings()
            if self._audio_mon:
                self._stop_audio_monitor()
                self._start_audio_monitor()
        rate_row.connect("notify::selected", rate_done)
        grpa.add(rate_row)

        reset_row = Adw.ActionRow(title="Start over")
        reset_row.set_subtitle("Unity gain, automatic rate")
        rbtn = Gtk.Button(label="Reset")
        rbtn.set_valign(Gtk.Align.CENTER)

        def do_reset(*_):
            self.mic_gain = 1.0
            self.mic_rate = 0
            scale.set_value(0.0)
            rate_row.set_selected(0)
            show_gain()
            self._save_settings()
        rbtn.connect("clicked", do_reset)
        reset_row.add_suffix(rbtn)
        grpa.add(reset_row)

        nr = Adw.SwitchRow(title="Noise suppression")
        nr.set_subtitle(
            "RNNoise. Measured on a recording from this machine it lifted "
            "speech-to-noise from 4.5 dB to 71 dB where the voice lives. "
            "Off records the microphone exactly as it is")
        nr.set_active(self.denoise)
        nr.set_sensitive(have(RNNOISE))
        if not have(RNNOISE):
            nr.set_subtitle("RNNoise plugin not installed, see the README")

        def nr_done(row, *_):
            self.denoise = row.get_active()
            self._save_settings()
            if self._audio_mon:
                self._stop_audio_monitor()
                self._start_audio_monitor()
        nr.connect("notify::active", nr_done)
        grpa.add(nr)

        mix_row = Adw.ActionRow(title="Suppression strength")
        mix = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 25.0, 1.0)
        mix.set_size_request(230, -1)
        mix.set_draw_value(False)
        mix.set_valign(Gtk.Align.CENTER)
        mix.add_mark(5.0, Gtk.PositionType.BOTTOM, None)
        mix.set_value(self.nr_mix * 100.0)
        mix.set_inverted(True)      # right is stronger, which is what people expect

        def show_mix():
            d = self.nr_mix * 100.0
            if d < 0.5:
                word = "maximum, and the voice may wobble"
            elif d <= 7:
                word = "strong, steady"
            elif d <= 15:
                word = "moderate"
            else:
                word = "light, some room noise stays"
            mix_row.set_subtitle(f"{100 - d:.0f} percent, {word}")
        show_mix()

        def mix_done(sc):
            self.nr_mix = float(sc.get_value()) / 100.0
            show_mix()
            self._save_settings()
            if self._audio_mon:
                self._stop_audio_monitor()
                self._start_audio_monitor()
        mix.connect("value-changed", mix_done)
        mix_row.add_suffix(mix)
        mix_row.set_sensitive(have(RNNOISE))
        grpa.add(mix_row)
        page.add(grpa)

        # Diagnostics. Not decoration: the hiss on this machine was a +30dB
        # analog preamp, and nothing anywhere showed the analog gain apart
        # from the software one, so every level change looked like it did
        # nothing. This is the panel that would have found it in a minute.
        grpd = Adw.PreferencesGroup(
            title="Audio diagnostics",
            description="What the capture path is actually doing")
        rows = {}
        for key, title in (("backend", "Audio server"),
                           ("device", "Input device"),
                           ("format", "Format"),
                           ("analog_gain", "Analog gain (hardware preamp)"),
                           ("sw_volume", "Software volume"),
                           ("processing", "System processing"),
                           ("levels", "Level now"),
                           ("verdict", "Assessment")):
            r = Adw.ActionRow(title=title)
            r.set_subtitle("...")
            rows[key] = r
            grpd.add(r)
        self._diag_rows = rows

        def refresh(*_):
            f = audio_facts()
            for k in ("backend", "device", "format", "analog_gain",
                      "sw_volume", "processing"):
                rows[k].set_subtitle(str(f.get(k, "unknown")))
            pct = f.get("analog_pct")
            notes = []
            if pct is not None and pct >= 90:
                notes.append(
                    "The hardware preamp is at maximum, which is where hiss "
                    "comes from. Turning the system input level down below "
                    "about 30 percent is what actually lowers it; above that "
                    "the level is only being changed in software and the "
                    "noise comes down with the voice.")
            if f.get("processing", "") != "none loaded":
                notes.append("The system is applying its own processing.")
            if f.get("muted") == "yes":
                notes.append("The system has this input muted.")
            rows["verdict"].set_subtitle(
                "  ".join(notes) if notes else
                "Nothing obviously wrong with the capture path.")
        refresh()
        rows["levels"].set_subtitle("open with the meter above running")
        btn = Gtk.Button(label="Refresh")
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", refresh)
        rows["backend"].add_suffix(btn)
        page.add(grpd)

        grpc = Adw.PreferencesGroup(
            title="Camera",
            description="Off is a choice, not only what happens when there "
                        "is nothing plugged in")
        cams = list_v4l2_cameras()
        cam_names = ["No camera"] + [c[1].split(":")[0].strip() or c[0] for c in cams]
        cam_row = Adw.ComboRow(title="Use")
        cam_row.set_model(Gtk.StringList.new(cam_names))
        cam_row.set_selected(0 if self.camera_off
                             else min(self.cam_idx + 1, len(cam_names) - 1))

        def cam_sub():
            if self.camera_off:
                cam_row.set_subtitle("The device is closed and nothing will reopen it")
            elif cams:
                cam_row.set_subtitle(cams[min(self.cam_idx, len(cams) - 1)][0])
            else:
                cam_row.set_subtitle("No cameras detected")
        cam_sub()

        def cam_done(row, *_):
            sel = row.get_selected()
            # Guarded: setting the model or the selection below fires this
            # again, and re-entering would restart the pipeline mid-restart.
            if getattr(self, "_cam_row_busy", False):
                return
            self._cam_row_busy = True
            try:
                self._select_camera(None if sel == 0 else sel - 1)
                cam_sub()
            finally:
                self._cam_row_busy = False
        cam_row.connect("notify::selected", cam_done)
        grpc.add(cam_row)

        rescan = Adw.ActionRow(title="Look for cameras again")
        rescan.set_subtitle("For one plugged in, or freed by another app, "
                            "since Lens started")
        rb = Gtk.Button(label="Rescan")
        rb.set_valign(Gtk.Align.CENTER)

        def do_rescan(*_):
            found = list_v4l2_cameras()
            names = ["No camera"] + [c[1].split(":")[0].strip() or c[0] for c in found]
            if found:
                self.cameras = found
                self.cam_idx = min(self.cam_idx, len(found) - 1)
            self._cam_row_busy = True
            try:
                cam_row.set_model(Gtk.StringList.new(names))
                cam_row.set_selected(0 if self.camera_off
                                     else min(self.cam_idx + 1, len(names) - 1))
            finally:
                self._cam_row_busy = False
            rescan.set_subtitle(f"Found {len(found)} camera(s)")
            self._refresh_flip_button()
        rb.connect("clicked", do_rescan)
        rescan.add_suffix(rb)
        grpc.add(rescan)

        smooth = Adw.SwitchRow(title="Smooth motion")
        smooth.set_subtitle(
            "Hold the camera to its full frame rate. Off lets it slow down "
            "in dim light for a brighter picture: measured here, 30fps "
            "against 7")
        smooth.set_active(self.smooth_motion)

        def smooth_done(row, *_):
            self.smooth_motion = row.get_active()
            self._save_settings()
            self._start_pipeline()      # the rate is fixed at stream start
        smooth.connect("notify::active", smooth_done)
        grpc.add(smooth)
        page.add(grpc)

        grpv = Adw.PreferencesGroup(title="Viewfinder")
        mir = Adw.SwitchRow(title="Mirror the preview")
        mir.set_subtitle("Move left and your image moves left, the way a "
                         "mirror behaves. Only changes what you see")
        mir.set_active(self.mirror_view)

        mir_save = Adw.SwitchRow(title="Mirror what gets saved too")
        mir_save.set_subtitle("Off keeps files the way the room really was. "
                              "On matches the preview, but writing in shot "
                              "will read backwards")
        mir_save.set_active(self.mirror_saved)
        mir_save.set_sensitive(self.mirror_view)

        def mir_done(row, *_):
            self.mirror_view = row.get_active()
            mir_save.set_sensitive(self.mirror_view)
            self._apply_mirror()
            self._save_settings()

        def mir_save_done(row, *_):
            self.mirror_saved = row.get_active()
            self._apply_mirror()
            self._save_settings()
        mir.connect("notify::active", mir_done)
        mir_save.connect("notify::active", mir_save_done)
        grpv.add(mir)
        grpv.add(mir_save)
        page.add(grpv)

        grp3 = Adw.PreferencesGroup(
            title="Recycle bin",
            description="Deleted files wait here before being removed for good")
        auto = Adw.SwitchRow(title="Delete old files automatically")
        auto.set_subtitle("Off means nothing leaves the bin unless you empty it")
        auto.set_active(self.trash_auto)

        spin = Adw.SpinRow.new_with_range(1, 365, 1)
        spin.set_title("Keep for (days)")
        spin.set_value(max(1, self.trash_days))
        spin.set_sensitive(self.trash_auto)

        def days_done(*_):
            self.trash_days = int(spin.get_value())
            self._save_settings()

        def auto_done(row, *_):
            self.trash_auto = row.get_active()
            spin.set_sensitive(self.trash_auto)
            self._save_settings()
        spin.connect("notify::value", days_done)
        auto.connect("notify::active", auto_done)
        grp3.add(auto)
        grp3.add(spin)

        where = Adw.ActionRow(title="Bin location", subtitle=str(trash_dir()))
        ob = Gtk.Button(label="Open")
        ob.set_valign(Gtk.Align.CENTER)
        ob.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(
            trash_dir().as_uri(), None))
        cb = Gtk.Button(label="Change")
        cb.set_valign(Gtk.Align.CENTER)

        def pick_bin(*_):
            dlg = Gtk.FileDialog()
            dlg.set_title("Where should deleted files go?")

            def done(d, res):
                try:
                    f = d.select_folder_finish(res)
                except Exception:
                    return
                if f is None:
                    return
                self.trash_path = f.get_path()
                set_trash_dir(self.trash_path)
                where.set_subtitle(str(trash_dir()))
                self._save_settings()
            dlg.select_folder(win, None, done)
        cb.connect("clicked", pick_bin)
        where.add_suffix(cb)
        where.add_suffix(ob)
        grp3.add(where)

        empty = Adw.ActionRow(title="Empty the bin now")
        eb = Gtk.Button(label="Empty")
        eb.add_css_class("destructive-action")
        eb.set_valign(Gtk.Align.CENTER)

        def do_empty(*_):
            n = 0
            for f in trash_dir().glob("*"):
                try:
                    if f.is_file():
                        f.unlink(); n += 1
                except OSError:
                    pass
            empty.set_subtitle(f"Removed {n} file(s)")
        eb.connect("clicked", do_empty)
        empty.add_suffix(eb)
        grp3.add(empty)
        page.add(grp3)

        # The meter is pointless without a live mic, so hold one open only
        # while this dialog is, and put it back exactly as it was on close.
        was_monitoring = bool(self._audio_mon)
        if not was_monitoring and self.mic_available and self.mic_enabled:
            self._start_audio_monitor()

        def settings_closed(*_):
            if not was_monitoring and not (self.video_mode and self.mic_enabled):
                self._stop_audio_monitor()
            return False
        win.connect("close-request", settings_closed)

        win.add(page)
        win.present()

    def _open_photo_viewer(self, path):
        """Open the gallery, positioned on whatever was tapped."""
        items = []
        for f in list(self.pic_dir.glob("*.jpg")) + [
                g for e in VIDEO_EXTS for g in self.vid_dir.glob("*" + e)]:
            try:
                if f.stat().st_size:
                    items.append((f.stat().st_mtime, f))
            except OSError:
                pass
        items.sort(key=lambda t: t[0])
        self.viewer.pic_dir = self.pic_dir
        self.viewer.vid_dir = self.vid_dir
        self.viewer.load([f for _, f in items], start=path)
        self.viewer.set_visible(True)

    def _close_photo_viewer(self):
        self.viewer.set_visible(False)

    def _refresh_deck(self):
        """Feed the newest photos on disk into the deck.

        Re-reads the directory every time rather than tracking additions, so
        deleting a photo promotes the next newest one instead of leaving a
        stale card behind.
        """
        items = []
        candidates = list(self.pic_dir.glob("*.jpg"))
        for ext in VIDEO_EXTS:
            candidates += list(self.vid_dir.glob("*" + ext))
        for f in candidates:
            try:
                st = f.stat()
            except OSError:
                continue          # deleted between the glob and the stat
            if st.st_size == 0:
                continue          # a failed capture, not a recording
            items.append((st.st_mtime, f))
        items.sort(key=lambda t: t[0])
        self.thumb.update_photos([f for _, f in items])

    def _watch_pictures(self):
        """Rebuild the deck when the photo folder changes underneath us, so
        deleting from Files updates the previews without a restart."""
        self._pic_monitors = []
        for d in (self.pic_dir, self.vid_dir):
            try:
                mon = Gio.File.new_for_path(str(d)).monitor_directory(
                    Gio.FileMonitorFlags.NONE, None)
                mon.connect("changed", self._on_pictures_changed)
                self._pic_monitors.append(mon)
            except Exception as e:
                print(f"Lens: cannot watch {d}: {e}", file=sys.stderr)

    def _on_pictures_changed(self, monitor, gfile, other, event):
        # A single save emits several events, so coalesce into one rebuild.
        if self._deck_refresh_id:
            GLib.source_remove(self._deck_refresh_id)
        self._deck_refresh_id = GLib.timeout_add(300, self._deck_refresh_now)

    def _deck_refresh_now(self):
        self._deck_refresh_id = None
        self._refresh_deck()
        return False


class ThumbnailDeck(Gtk.Overlay):
    """A stacked "deck of cards" thumbnail preview.

    Idle: shows just the top card (last photo).
    Hover: after 500ms delay, fans the deck out and starts a card-pull cycle
      where the bottom card animates up-and-out, holds briefly, then returns
      under the deck. Cycle continues until mouse leaves.
    """
    DECK_SIZE = 5     # up to N cards visible
    THUMB_PX  = 112   # collapsed edge (was 56 — user asked for 2x)
    # Wide enough for the fan-out swipe but not so wide it covers the
    # centered shutter button behind it.
    HIT_W     = 300
    HIT_H     = 160
    # Geometry of the expanded fan. These MUST match the
    # .state-expanded.deck-idx-N rules in the CSS, because the hit zones are
    # derived from them rather than guessed at.
    FAN_ANGLES  = (-30, -15, 0, 15, 30)     # rotate(Ndeg)
    # Shifted left from 80: the preview sat too close to the shutter.
    FAN_DX      = 44                        # base translate X
    FAN_OFFSETS = (-60, -30, 0, 30, 60)     # per-card slide, added to FAN_DX
    # Inside the fan the nearest card always wins, so there are no dead
    # zones between cards. Focus is only dropped once the finger is clearly
    # past either end, which needs to be a generous distance or overshooting
    # the last card feels like the deck snatches itself away.
    FAN_EDGE   = 110  # horizontal px past the outermost card centre
    FAN_SLOP_Y = 160  # vertical px above/below the widget

    def set_scale(self, thumb_px, hit_w, margin):
        """Resize the deck for a narrower window."""
        if thumb_px == self.THUMB_PX and hit_w == self.HIT_W:
            return
        self.THUMB_PX = thumb_px
        self.HIT_W = hit_w
        self.set_size_request(hit_w, self.HIT_H)
        self.set_margin_start(margin)
        # Force the next update_photos to rebuild, without throwing away
        # card_paths. Clearing that list was destroying the card-to-file
        # mapping, so releasing on a card looked up an index into an empty
        # list and opened nothing, and a plain tap did the same.
        self._needs_rebuild = True

    def __init__(self, on_click=None, on_card_click=None):
        super().__init__()
        self.set_size_request(self.HIT_W, self.HIT_H)
        self.set_hexpand(False); self.set_vexpand(False)
        # Centered vertically in the row, with breathing room from the left edge.
        self.set_valign(Gtk.Align.CENTER); self.set_halign(Gtk.Align.START)
        self.set_margin_start(28)
        self.on_click = on_click
        self.on_card_click = on_card_click
        self.card_paths = []
        self._pending_photos = None
        self._needs_rebuild = False
        self.cards = []        # bottom-to-top order
        self.hover_delay_id = None
        self.cycle_id = None
        self.expanded = False
        self.peeking = False
        self.current_pull = 0

        # Placeholder icon when no photos yet
        self.placeholder = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
        self.placeholder.set_pixel_size(28)
        self.placeholder.add_css_class("thumb-placeholder")
        self.set_child(self.placeholder)

        # New interaction: press-and-hold to fan out, release on a card to
        # open it. Short click opens the most recent. Double click opens the
        # gallery folder in the file manager.
        self._hold_timer = None
        self._click_timer = None
        self._press_is_hold = False
        self._pressed = False
        self._debug = bool(os.environ.get("LENS_DEBUG"))

        press = Gtk.GestureClick.new()
        press.set_button(1)   # left mouse
        press.connect("pressed",  self._on_press)
        press.connect("released", self._on_release)
        self.add_controller(press)

        # Touch scrubbing goes through a drag gesture, not the motion
        # controller. EventControllerMotion only sees pointer-emulation
        # events, so a finger held still stops producing them and the deck
        # went dead until you lifted off. A drag gesture keeps the sequence
        # for as long as the finger is down, including outside the widget.
        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin",  self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end",    self._on_drag_end)
        # A drag does not always finish with drag-end: releasing away from
        # the cards, or the sequence being taken over, ends it via cancel or
        # end instead, and the fan stayed open. Both are handled, but the
        # cleanup is deferred to an idle callback because 'end' can arrive
        # BEFORE 'drag-end'. Running it immediately cleared _press_is_hold
        # first, so _finish_hold bailed out and releasing on a card opened
        # nothing at all.
        drag.connect("cancel", self._abort_hold)
        drag.connect("end",    self._abort_hold)
        self.add_controller(drag)
        self._drag_start = (0.0, 0.0)

        # Motion controller — mouse hover only
        motion = Gtk.EventControllerMotion()
        motion.connect("enter",  lambda _c, x, y: self._on_enter())
        motion.connect("motion", lambda _c, x, y: self._on_motion(x, y))
        motion.connect("leave", lambda *_: self._on_leave())
        self.add_controller(motion)

    def _load_thumb(self, path, size=112):
        video = is_video(path)
        if video:
            path = video_thumbnail(path)
            if not path:
                return None
        try:
            full = GdkPixbuf.Pixbuf.new_from_file(str(path))
        except Exception:
            return None
        side = min(full.get_width(), full.get_height())
        x = (full.get_width()  - side) // 2
        y = (full.get_height() - side) // 2
        sq = full.new_subpixbuf(x, y, side, side)
        scaled = sq.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        if video:
            return self._with_play_badge(scaled, size)
        return Gdk.Texture.new_for_pixbuf(scaled)

    def _with_play_badge(self, pixbuf, size):
        """Stamp a play triangle on a thumbnail so clips read as clips."""
        try:
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
            cr = cairo.Context(surf)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint()
            r = size * 0.17
            cx = cy = size / 2.0
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(cx - r * 0.33, cy - r * 0.48)
            cr.line_to(cx + r * 0.52, cy)
            cr.line_to(cx - r * 0.33, cy + r * 0.48)
            cr.close_path()
            cr.fill()
            surf.flush()
            return Gdk.MemoryTexture.new(
                size, size, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                GLib.Bytes.new(bytes(surf.get_data())), surf.get_stride())
        except Exception as e:
            print(f"Lens: play badge failed: {e}", file=sys.stderr)
            return Gdk.Texture.new_for_pixbuf(pixbuf)

    def update_photos(self, paths):
        """Rebuild the deck from these paths (oldest → newest at top)."""
        latest = list(paths)[-self.DECK_SIZE:]
        new_paths = [str(p) for p in latest]
        if (new_paths == self.card_paths and self.cards
                and not self._needs_rebuild):
            return                      # nothing actually changed
        if self.expanded:
            # Rebuilding now would destroy the cards under the finger. Hold
            # it until the fan closes.
            self._pending_photos = list(paths)
            return
        self._pending_photos = None
        self._needs_rebuild = False
        # Clear existing
        while self.cards:
            self.remove_overlay(self.cards.pop())
        self.set_child(None)
        self.card_paths = new_paths
        if not latest:
            self.set_child(self.placeholder); return
        for i, p in enumerate(latest):
            tex = self._load_thumb(p, self.THUMB_PX * 2)
            if not tex: continue
            img = Gtk.Image.new_from_paintable(tex)
            img.set_pixel_size(self.THUMB_PX)
            img.add_css_class("deck-card")
            img.add_css_class(f"deck-idx-{i}")   # for stacking transforms
            # Clip the paintable to the widget's rounded corners so the image
            # doesn't bleed past the border.
            img.set_overflow(Gtk.Overflow.HIDDEN)

            # Anchor each card to the LEFT and vertically centered — this
            # keeps the thumbnail well inside the action area (avoids the
            # bottom-cutoff when the deck was anchored to END).
            img.set_halign(Gtk.Align.START); img.set_valign(Gtk.Align.CENTER)
            self.cards.append(img)
            self.add_overlay(img)
        self._focused_card = None
        self._refresh_transforms()

    def _refresh_transforms(self):
        """Apply CSS classes for the current stage: stacked, peeking or
        fully fanned."""
        if self.expanded:
            state = "expanded"
        elif self.peeking:
            state = "peek"
        else:
            state = "idle"
        for i, c in enumerate(self.cards):
            for cls in ("state-idle", "state-peek", "state-expanded", "pulled"):
                if c.has_css_class(cls): c.remove_css_class(cls)
            c.add_css_class(f"state-{state}")

    def _clear_focus(self):
        """Drop the focused card so nothing is raised."""
        if self._focused_card is None:
            return
        for c in self.cards:
            if c.has_css_class("card-focused"):
                c.remove_css_class("card-focused")
        self._focused_card = None

    def _fan_centers(self):
        """X of each expanded card, computed from the same numbers the CSS
        uses. Previously this was a hardcoded 20..240 band while the cards
        actually sat between 108 and 164, so where you pointed and which card
        lit up were unrelated."""
        n = len(self.cards)
        if not n:
            return []
        c0 = self.cards[0]
        w = c0.get_width()  or self.THUMB_PX
        h = c0.get_height() or self.THUMB_PX
        out = []
        for i in range(n):
            j = min(i, len(self.FAN_ANGLES) - 1)
            # Card rotates about its bottom centre, so its middle swings out
            # by (h/2)*sin(angle) from the pivot.
            pivot = w / 2 + self.FAN_DX + self.FAN_OFFSETS[j]
            out.append(pivot + (h / 2) * math.sin(math.radians(self.FAN_ANGLES[j])))
        return out

    def _on_motion(self, x, y):
        """Pointer moved over the deck. Touch scrubbing comes through
        _on_drag_update instead, because motion events are not reliable for a
        finger that is held still."""
        self._update_focus(x, y)

    def _update_focus(self, x, y):
        """Focus the card nearest to x, or nothing if clearly off the fan."""
        if not self.expanded or len(self.cards) < 2:
            return

        centers = self._fan_centers()
        if not centers:
            return

        if self._debug:
            print(f"[deck] x={x:7.1f} y={y:7.1f} centers="
                  + " ".join(f"{c:.0f}" for c in centers), file=sys.stderr)

        # Nearest card wins, so pointing at a card selects that card by
        # construction, and anywhere across the fan keeps a selection.
        idx = min(range(len(centers)), key=lambda i: abs(x - centers[i]))
        if (x < min(centers) - self.FAN_EDGE or x > max(centers) + self.FAN_EDGE
                or y < -self.FAN_SLOP_Y or y > self.HIT_H + self.FAN_SLOP_Y):
            self._clear_focus()
            return

        target = self.cards[idx]
        if self._focused_card is target:
            return
        for c in self.cards:
            if c.has_css_class("card-focused"):
                c.remove_css_class("card-focused")
        target.add_css_class("card-focused")
        self._focused_card = target
        # NOTE: don't reparent (remove/add_overlay) — that resets the CSS
        # transition state and makes the card teleport up instead of
        # animating. The 190px translate + 1.7x scale already lifts the
        # card visually above the fan, even without z-order changes.

    # ---- new press/hold interaction ----
    PEEK_MS   = 650   # ms of hover before the deck tips open a little
    HOLD_MS   = 220   # ms to hold before fan-out starts
    DBLCLK_MS = 280   # ms to wait for a possible second click before opening

    def _on_press(self, gesture, n_press, x, y):
        # If a single-click was pending from an earlier release, cancel it —
        # a second press means the user is going for a double-click OR a hold.
        if self._click_timer:
            GLib.source_remove(self._click_timer); self._click_timer = None
        # Start (or restart) hold timer
        if self._hold_timer:
            GLib.source_remove(self._hold_timer); self._hold_timer = None
        self._press_is_hold = False
        self._pressed = True
        def _trigger():
            self._hold_timer = None
            if not self._pressed:
                return False       # released before the hold threshold
            self._press_is_hold = True
            self._expand()
            return False
        self._hold_timer = GLib.timeout_add(self.HOLD_MS, _trigger)

    def _on_release(self, gesture, n_press, x, y):
        self._pressed = False
        # If we're mid-hold-timer, release before threshold = a click
        if self._hold_timer:
            GLib.source_remove(self._hold_timer); self._hold_timer = None
            if n_press >= 2:
                # Second click confirmed — open gallery
                if self.on_click: self.on_click()
            else:
                # First (short) click — defer to see if a second click follows
                def _fire_single():
                    self._click_timer = None
                    # A tap opens the gallery at the newest shot. It used to
                    # open a bare full-screen image, which looked like
                    # nothing had happened.
                    if self.card_paths and self.on_card_click:
                        self.on_card_click(self.card_paths[-1])
                    elif self.on_click:
                        self.on_click()
                    return False
                self._click_timer = GLib.timeout_add(self.DBLCLK_MS, _fire_single)
            return
        # Otherwise we were in hold/fan mode — open the focused card
        self._finish_hold()

    def _abort_hold(self, *_):
        """Close the fan once the gesture is over, whatever ended it.

        Deliberately does NOT touch _hold_timer. 'end' fires on a plain tap
        as well as a drag, and cancelling the timer here meant _on_release
        found it already None, skipped the click branch, and the tap was
        swallowed: that is why a single tap on the preview did nothing.
        The timer belongs to press/release.

        Deferred to idle for the same ordering reason: 'end' can arrive
        before 'drag-end' and before 'released'.
        """
        GLib.idle_add(self._abort_hold_now)

    def _abort_hold_now(self):
        self._pressed = False
        if self._press_is_hold:
            self._press_is_hold = False
            self._collapse()
        elif self.expanded:
            # Open with nothing held: only reachable if a gesture vanished.
            self._collapse()
        return False

    def _finish_hold(self):
        """End a hold-and-scrub: open whatever card is focused, then close
        the fan. Releasing with nothing focused opens nothing."""
        if not self._press_is_hold:
            return
        if self._focused_card and self._focused_card in self.cards:
            idx = self.cards.index(self._focused_card)
            if idx < len(self.card_paths) and self.on_card_click:
                self.on_card_click(self.card_paths[idx])
        self._press_is_hold = False
        self._collapse()

    # ---- touch drag scrubbing ----
    def _on_drag_begin(self, gesture, sx, sy):
        self._drag_start = (sx, sy)

    def _on_drag_update(self, gesture, ox, oy):
        if not self._press_is_hold:
            return
        sx, sy = self._drag_start
        self._update_focus(sx + ox, sy + oy)

    def _on_drag_end(self, gesture, ox, oy):
        # Once the drag threshold is passed this gesture claims the touch
        # sequence, which cancels the click gesture, so its "released" never
        # arrives and the hold has to be finished from here.
        if not self._press_is_hold:
            return
        sx, sy = self._drag_start
        self._update_focus(sx + ox, sy + oy)
        self._finish_hold()

    def _collapse(self):
        """Reverse of _expand: hide the fan, stop the cycle."""
        if self.cycle_id:
            GLib.source_remove(self.cycle_id); self.cycle_id = None
        self.expanded = False
        for c in self.cards:
            if c.has_css_class("card-focused"): c.remove_css_class("card-focused")
            if c.has_css_class("pulled"): c.remove_css_class("pulled")
        self._focused_card = None
        self._refresh_transforms()
        # Apply any photo-list change that arrived while the fan was open.
        if self._pending_photos is not None:
            pending, self._pending_photos = self._pending_photos, None
            GLib.idle_add(lambda: (self.update_photos(pending), False)[1])

    def _on_enter(self):
        """Hovering for a moment opens the deck a little. Touch has no hover,
        so on a tablet you go straight from stacked to press-and-hold."""
        if self.expanded or self.peeking:
            return
        if self.hover_delay_id:
            GLib.source_remove(self.hover_delay_id)
        def _peek():
            self.hover_delay_id = None
            if not self.expanded and self.cards:
                self.peeking = True
                self._refresh_transforms()
            return False
        self.hover_delay_id = GLib.timeout_add(self.PEEK_MS, _peek)

    def _on_leave(self):
        # Mid-drag, ignore leave entirely. The drag gesture keeps delivering
        # real coordinates outside the widget, so _update_focus is the one
        # that decides whether anything is selected. Clearing here as well
        # made it impossible to scrub back in after overshooting.
        if self._press_is_hold and self.expanded:
            return
        # If the mouse leaves the deck mid-hold, cancel the fan.
        if self.hover_delay_id:
            GLib.source_remove(self.hover_delay_id); self.hover_delay_id = None
        if self._hold_timer:
            GLib.source_remove(self._hold_timer); self._hold_timer = None
        if self.expanded:
            self._collapse()
        elif self.peeking:
            self.peeking = False
            self._refresh_transforms()

    def _expand(self):
        # Drop any pending peek timer properly. Clearing the id alone left the
        # timeout armed, so it could fire after a later collapse and tip the
        # deck open on its own.
        if self.hover_delay_id:
            GLib.source_remove(self.hover_delay_id)
        self.hover_delay_id = None
        if not self.cards: return False
        self.expanded = True
        self._refresh_transforms()
        self.current_pull = 0
        self.cycle_id = GLib.timeout_add(1200, self._cycle_pull)
        return False

    def _cycle_pull(self):
        if not self.expanded or not self.cards: return False
        # If user is focused on a specific card, don't disturb it
        if getattr(self, "_focused_card", None):
            return True
        # Un-pull previous card
        for c in self.cards:
            if c.has_css_class("pulled"): c.remove_css_class("pulled")
        # Pull next card (skip the top-most, i == len-1, since it's already visible)
        if len(self.cards) > 1:
            idx = self.current_pull % (len(self.cards) - 1)
            self.cards[idx].add_css_class("pulled")
            self.current_pull += 1
        return True


class _GridOverlay(Gtk.DrawingArea):
    """Rule-of-thirds grid drawn over the viewfinder."""
    def __init__(self):
        super().__init__()
        self.set_can_target(False)  # transparent to input
        self.divisions = 3
        self.set_draw_func(self._draw)

    def set_divisions(self, n):
        self.divisions = n
        self.queue_draw()

    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_line_width(1)
        n = getattr(self, "divisions", 3)
        for i in range(1, n):
            x, y = w * i / n, h * i / n
            cr.move_to(x, 0); cr.line_to(x, h); cr.stroke()
            cr.move_to(0, y); cr.line_to(w, y); cr.stroke()


class LensApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = LensWindow(self)
        win.present()


if __name__ == "__main__":
    sys.exit(LensApp().run(sys.argv))
