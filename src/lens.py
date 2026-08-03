#!/usr/bin/env python3
"""
Lens — a fast, mobile-first camera app for Linux tablets.
GTK4 + libadwaita + GStreamer.
"""

import gi, os, sys, math, json, time, datetime, pathlib
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
import cairo
from gi.repository import Gtk, Adw, Gst, GLib, Gdk, Gio, GObject, GdkPixbuf, Graphene

Gst.init(None)

APP_ID = "org.cheapaz.Lens"
CONFIG_DIR = pathlib.Path(
    os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "lens"
SETTINGS = CONFIG_DIR / "settings.json"
PICTURES = pathlib.Path.home() / "Pictures" / "Lens"
VIDEOS   = pathlib.Path.home() / "Videos"  / "Lens"
THUMBS = pathlib.Path(
    os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "lens" / "thumbs"
VIDEO_EXTS = (".mkv", ".mp4", ".webm", ".mov")

# label -> (extension, muxer, video encoder, audio encoder)
CONTAINERS = {
    "MKV":  ("mkv",  "matroskamux", "x264enc tune=zerolatency bitrate=4000", "opusenc"),
    "MP4":  ("mp4",  "mp4mux",      "x264enc tune=zerolatency bitrate=4000", "voaacenc"),
    "WebM": ("webm", "webmmux",     "vp8enc deadline=1",                     "opusenc"),
}
PICTURES.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)


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
DENOISE_CHAIN = ("audiowsincband mode=band-pass lower-frequency=110 "
                 "upper-frequency=7500 length=101")


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
        self.set_content_width(46)
        self.set_content_height(11)
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


class GalleryView(Gtk.Box):
    """Browse and edit what has been shot: rotate, mirror, crop, save.

    Edits are non-destructive until saved, and saving writes a new file
    rather than overwriting: losing an original to a mis-drag would be a
    poor trade for the convenience.
    """

    THUMB = 74

    def __init__(self, on_close, on_open_external):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("gal")
        self.on_close = on_close
        self.on_open_external = on_open_external
        self.paths = []
        self.index = 0
        self.src = None            # untouched pixbuf
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

        self.title = Gtk.Label(label="")
        self.title.add_css_class("gal-title")
        self.title.set_ellipsize(3)
        self.title.set_hexpand(True)
        self.title.set_halign(Gtk.Align.START)
        self.title.set_margin_start(8)
        head.append(self.title)

        for icon, tip, cb in (
            ("object-rotate-left-symbolic",  "Rotate left",  lambda: self._rotate(-90)),
            ("object-rotate-right-symbolic", "Rotate right", lambda: self._rotate(90)),
            ("object-flip-horizontal-symbolic", "Mirror horizontally", lambda: self._flip(True)),
            ("object-flip-vertical-symbolic",   "Mirror vertically",   lambda: self._flip(False)),
        ):
            head.append(self._tool(icon, tip, cb))

        self.btn_crop = Gtk.ToggleButton()
        self.btn_crop.set_child(self._icon("edit-cut-symbolic"))
        self.btn_crop.add_css_class("gal-tool")
        self.btn_crop.set_tooltip_text("Crop")
        self.btn_crop.connect("toggled", lambda b: self._set_cropping(b.get_active()))
        head.append(self.btn_crop)

        self.ratio_btn = Gtk.MenuButton()
        self.ratio_btn.set_child(Gtk.Label(label="Free"))
        self.ratio_btn.add_css_class("gal-tool")
        self.ratio_btn.set_tooltip_text("Crop ratio")
        self.ratio_pop = Gtk.Popover()
        self.ratio_btn.set_popover(self.ratio_pop)
        self.ratio_btn.set_sensitive(False)
        self._build_ratio_menu()
        head.append(self.ratio_btn)

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
        stack.set_child(self.pic)
        self.crop = CropOverlay(on_change=lambda: None)
        stack.add_overlay(self.crop)

        # Click the left or right third to page through, like a photo viewer.
        page = Gtk.GestureClick.new()
        page.set_button(1)
        page.connect("released", self._on_page_click)
        self.pic.add_controller(page)
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        self.empty = Gtk.Label(label="Nothing here yet")
        self.empty.add_css_class("gal-empty")
        stack.add_overlay(self.empty)
        self.empty.set_visible(False)
        self.append(stack)

        # ---- filmstrip
        self.strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.strip.set_margin_start(10); self.strip.set_margin_end(10)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        sc.set_child(self.strip)
        sc.set_size_request(-1, self.THUMB + 26)
        sc.add_css_class("gal-strip")
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

    # ---- content ----
    def load(self, paths, start=None):
        """Photos only. Video editing is a different job, and a crop tool
        that silently did nothing to a clip would be worse than not offering
        it."""
        self.paths = [str(p) for p in paths if not is_video(p)]
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
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    p, self.THUMB, self.THUMB, True)
                img.set_from_paintable(Gdk.Texture.new_for_pixbuf(pb))
            except Exception:
                img.set_from_icon_name("image-missing-symbolic")
            img.set_pixel_size(self.THUMB)
            b.set_child(img)
            b.connect("clicked", lambda _b, i=i: self.go(i))
            self.strip.append(b)
            self._strip_buttons.append(b)

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
            self.title.set_label("")
            self.pic.set_paintable(None)
            return
        path = self.paths[self.index]
        try:
            self.src = GdkPixbuf.Pixbuf.new_from_file(path)
        except Exception as e:
            print(f"Lens: cannot open {path}: {e}", file=sys.stderr)
            self.src = None
        self.rotation = 0
        self.flip_h = self.flip_v = False
        self.btn_crop.set_active(False)
        self.btn_save.set_sensitive(False)
        self.title.set_label(
            f"{pathlib.Path(path).name}    {self.index + 1} of {len(self.paths)}")
        for i, b in enumerate(self._strip_buttons):
            if i == self.index:
                b.add_css_class("gal-thumb-active")
            else:
                b.remove_css_class("gal-thumb-active")
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
    def _rotate(self, deg):
        if self.src is None:
            return
        self.rotation = (self.rotation + deg) % 360
        self.btn_save.set_sensitive(True)
        self._render()

    def _flip(self, horizontal):
        if self.src is None:
            return
        if horizontal:
            self.flip_h = not self.flip_h
        else:
            self.flip_v = not self.flip_v
        self.btn_save.set_sensitive(True)
        self._render()

    def _set_cropping(self, on):
        self.crop.active = on
        self.ratio_btn.set_sensitive(on)
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
        self.btn_save.set_sensitive(False)

    # ---- navigation ----
    def _on_page_click(self, gesture, n_press, x, y):
        w = self.pic.get_width() or 1
        if x < w * 0.28:
            self.go(self.index - 1)
        elif x > w * 0.72:
            self.go(self.index + 1)

    def _on_scroll(self, ctrl, dx, dy):
        if dy:
            self.go(self.index + (1 if dy > 0 else -1))
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
        self.aspects = ["4:3", "16:9", "1:1"]

        # Restore what was set last time. Loaded before the pipeline starts,
        # since cam_idx decides which device it opens.
        cfg = load_settings()
        self.aspect_idx = cfg.get("aspect_idx", 0)
        if not 0 <= self.aspect_idx < len(self.aspects):
            self.aspect_idx = 0
        self.container = cfg.get("container", "MKV")
        if self.container not in CONTAINERS:
            self.container = "MKV"
        # (w, h, fps) the user pinned, or None to auto-pick
        self.mode_override = cfg.get("mode_override")
        self.mic_source = cfg.get("mic_source")     # None = system default
        self.denoise = bool(cfg.get("denoise", True))
        self.mic_available = has_microphone()
        self.mic_enabled = bool(cfg.get("mic_enabled", True))
        self.mic_active = False
        self._audio_mon = None
        self._audio_mon_handler = None
        self._hud_items = []
        self._hud_hidden = set()
        self.cur_res = None
        self._fps_count = 0
        self._fps_shown = 0
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
        self._start_pipeline()

    # ---------- pipeline ----------
    def _start_pipeline(self, defer_attach=False, on_ready=None):
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

        dev = self.cameras[self.cam_idx][0]
        src = self._source_for(dev)
        # Preview branch + photo-capture branch, both fed by the same v4l2src via tee.
        # The photo branch produces encoded JPEG buffers on the appsink,
        # and we pull the latest one when the shutter is pressed.
        # Let the camera pick its native resolution — forcing 1280x720 stretched
        # 4:3 sensors (like the Z13 rear camera at 2592x1944) into 16:9.
        pipe_str = (
            f"{src} ! tee name=t "
            f"t. ! queue max-size-buffers=2 leaky=downstream ! gtk4paintablesink name=sink "
            f"t. ! queue max-size-buffers=2 leaky=downstream ! jpegenc quality=92 ! "
            f"appsink name=photosink emit-signals=false max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(pipe_str)
        sink = self.pipeline.get_by_name("sink")
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
        if not self.cur_res:
            return None
        w, h = self.cur_res
        target = {"4:3": 4 / 3, "16:9": 16 / 9, "1:1": 1.0}[self.aspects[self.aspect_idx]]
        if w / h > target:
            return int(h * target), h
        return w, int(w / target)

    def _on_frame(self, *_):
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
        self.picture.set_hexpand(True); self.picture.set_vexpand(True)

        # Grid lives inside the aspect frame, over the picture only. On the
        # window overlay it drew across the letterbox bars too, where a
        # rule-of-thirds guide means nothing.
        self.grid_widget = _GridOverlay()
        self.grid_widget.set_visible(False)
        self.frame_stack = Gtk.Overlay()
        # The Picture is the measured child. Making it an unmeasured overlay
        # instead (to stop it re-measuring on a feed swap) left the aspect
        # frame with a zero-height child, which collapsed the viewfinder into
        # a permanent band. Not worth it.
        self.frame_stack.set_child(self.picture)
        self.frame_stack.add_overlay(self.grid_widget)
        self.frame_stack.add_css_class("viewflip")
        # Nothing in the overlay may paint outside the picture, whatever
        # its natural width says.
        self.frame_stack.set_overflow(Gtk.Overflow.HIDDEN)
        # Right-click the picture for the overlay checklist. It is a context
        # menu, so it belongs on the right button: left-click stays free for
        # anything that should act on the shot itself.
        self.view_popover = Gtk.Popover()
        self.view_popover.set_parent(self.frame_stack)
        view_click = Gtk.GestureClick.new()
        view_click.set_button(3)
        view_click.connect("released", self._on_view_clicked)
        self.frame_stack.add_controller(view_click)
        frame_stack = self.frame_stack

        self.aspect_frame = Gtk.AspectFrame.new(0.5, 0.5, 4/3, False)
        self.aspect_frame.add_css_class("viewframe")
        self.aspect_frame.set_child(frame_stack)
        self.aspect_frame.set_hexpand(True); self.aspect_frame.set_vexpand(True)
        bg.append(self.aspect_frame)

        # ---- Top bar ----
        # Its own strip above the viewfinder rather than floating over the
        # picture, so nothing covers the shot. Exit left, mode pills centred,
        # fullscreen right.
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
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.append(self.btn_grid); top.append(self.btn_aspect); top.append(self.btn_timer)

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
        self.btn_flip.set_sensitive(len(self.cameras) > 1)
        self.btn_flip.connect("clicked", lambda *_: self._flip_camera())
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
        hud.append(spacer)

        # --- bottom left: sound. Its own corner so the meter survives at
        # sizes where the top row has already had to shed things.
        audio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        audio_row.set_valign(Gtk.Align.CENTER)
        self.btn_mic = Gtk.Button()
        self.mic_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        self.mic_icon.set_pixel_size(18)
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
        self.hud_timer = None
        self._power_samples = []

        # Grid overlay (rule of thirds)

        # Gallery (last overlay so it sits on top of everything)
        self.viewer = GalleryView(
            on_close=self._close_photo_viewer,
            on_open_external=lambda p: Gio.AppInfo.launch_default_for_uri(
                "file://" + str(p), None))
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
        .hud-mic > button { min-width: 26px; min-height: 22px; }
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
        .cam-row { background: transparent; border: none; color: white;
                   padding: 6px 10px; border-radius: 8px; font-size: 13px; }
        .cam-row:hover { background: rgba(255,255,255,0.12); }
        .cam-row-active { color: #62a0ea; }
        .pill-active { background: #62a0ea; color: white; }
        .pill-off { color: rgba(255,255,255,0.35); }
        /* Off, not broken: dim enough to read as inactive but still
           legible, unlike the old 35% which vanished. */
        .pill-dim { color: rgba(255,255,255,0.5);
                    background: rgba(255,255,255,0.06); }
        .countdown { color: white; font-size: 96px; font-weight: bold;
                     background: rgba(0,0,0,0.5); padding: 30px 50px; border-radius: 60px; }
        .flash { background: black; }
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
        self._ballast.set_size_request(btn * 3 + 4, -1)
        self._ballast.set_visible(cls == "roomy")
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
            "container":    self.container,
            "mode_override": self.mode_override,
            "mic_source":   self.mic_source,
            "denoise":      self.denoise,
            "timer_sec":    self.timer_sec,
            "video_mode":   self.video_mode,
            "cam_id":       self.cameras[self.cam_idx][2],
            "cam_latency":  self._cam_latency,
        })

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
        """pulsesrc plus the denoise filter, as a pipeline fragment.

        The trailing audioconvert is required, not tidiness: audiowsincband
        emits float, the encoders want integer, and without it negotiation
        fails outright. The pipeline then sticks in PAUSED, the recording
        never starts, and the file is left at zero bytes.
        """
        src = f"pulsesrc device={self.mic_source}" if self.mic_source else "pulsesrc"
        chain = f" ! {DENOISE_CHAIN} ! audioconvert" if self.denoise else ""
        return f"{src} provide-clock=false ! audioconvert{chain}"

    # ---- live audio level ----
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
        box.append(self._menu_row("Noise suppression", self.denoise,
                                  self._toggle_denoise))
        box.append(Gtk.Separator())
        box.append(self._menu_head("AUDIO INPUT"))
        box.append(self._menu_row("System default", self.mic_source is None,
                                  lambda: self._set_mic_source(None)))
        for name, desc in list_audio_sources():
            box.append(self._menu_row(desc, self.mic_source == name,
                                      lambda n=name: self._set_mic_source(n)))
        self.mic_popover.set_child(box)
        self.mic_popover.popup()

    def _toggle_denoise(self):
        self.mic_popover.popdown()
        self.denoise = not self.denoise
        self._save_settings()
        # Rebuild the monitor so the change is audible on the meter at once.
        self._stop_audio_monitor()
        if self.video_mode:
            self._start_audio_monitor()

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
            self.mic_icon.set_pixel_size(18)
            self.btn_mic.set_tooltip_text("No microphone found")
            self.btn_mic.add_css_class("hud-btn-off")
        elif self.mic_enabled:
            self.mic_icon.set_from_icon_name("audio-input-microphone-symbolic")
            self.mic_icon.set_pixel_size(18)
            self.btn_mic.set_tooltip_text("Microphone on, click to mute")
        else:
            # Muted is a warning, not a disabled state: you are about to
            # record something silent. Grey made it nearly invisible.
            self.mic_icon.set_from_icon_name("microphone-disabled-symbolic")
            self.mic_icon.set_pixel_size(18)
            self.btn_mic.set_tooltip_text("Microphone MUTED, click to unmute")
            self.btn_mic.add_css_class("hud-btn-muted")
        self.btn_mic.set_sensitive(self.mic_available)
        self.audio_meter.set_muted(not (self.mic_available and self.mic_enabled))

    def _toggle_mic(self):
        if not self.mic_available or self.recording:
            return          # changing it mid-take would split the file
        self.mic_enabled = not self.mic_enabled
        if self.mic_enabled and self.video_mode:
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
        gap = 20
        clock_w = self.hud_clock.get_preferred_size()[1].width
        rec_w = w_of(self._rec_row)
        bat_w = self._bat_row.get_preferred_size()[1].width
        half = (inner - clock_w) / 2 - gap
        if rec_w > half or bat_w > half:
            self.hud_clock.set_visible(False)          # centre corner goes
            if rec_w + bat_w + gap > inner:
                self._bat_row.set_visible(False)       # then the right one
                if rec_w > inner:
                    self._hud_top.set_visible(False)

        # Bottom row: sound on the left, format on the right.
        if w_of(self._audio_row, self._info_row) > avail:
            self._info_row.set_visible(False)
            if w_of(self._audio_row) > avail:
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
        rate = f"{self._fps_shown:g} FPS" if self._fps_shown else ""
        self.hud_fps.set_label("  ".join(x for x in (res, rate) if x))
        self._refresh_mic_icon()

        ext, _mux, venc, aenc = CONTAINERS[self.container]
        vcodec = "VP8" if venc.startswith("vp8") else "H.264"
        acodec = ("AAC" if aenc.startswith("voaac") else "OPUS") \
            if (self.mic_available and self.mic_enabled) else "MUTE"
        self.hud_format.set_label(f"{self.container.upper()}  {vcodec}  {acodec}")

        pct, status, energy, watts = read_battery()
        charging = status.lower().startswith("charg")

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

        for i, cam in enumerate(self.cameras):
            # Card names carry a redundant "USB2.0 HD UVC WebCam: USB2.0 HD"
            # style repeat, so keep the part before the colon.
            name = cam[1].split(":")[0].strip() or cam[0]
            row = Gtk.Button()
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            tick = Gtk.Image.new_from_icon_name(
                "object-select-symbolic" if i == self.cam_idx else "camera-photo-symbolic")
            tick.set_pixel_size(14)
            lbl = Gtk.Label(label=name)
            lbl.set_halign(Gtk.Align.START); lbl.set_hexpand(True)
            lbl.set_ellipsize(3)          # PANGO_ELLIPSIZE_END
            lbl.set_max_width_chars(28)
            inner.append(tick); inner.append(lbl)
            row.set_child(inner)
            row.add_css_class("cam-row")
            if i == self.cam_idx:
                row.add_css_class("cam-row-active")
            row.connect("clicked", lambda _b, idx=i: self._select_camera(idx))
            box.append(row)

        self.cam_popover.set_child(box)
        self.cam_popover.popup()

    def _select_camera(self, idx):
        self.cam_popover.popdown()
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
        else:
            self._take_photo()

    def _tick_countdown(self):
        self.countdown_val -= 1
        if self.countdown_val <= 0:
            self.countdown_label.set_visible(False)
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
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = PICTURES / f"Lens-{ts}.jpg"
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
        if not self.recording: return False
        self.rec_seconds += 1
        h, rem = divmod(self.rec_seconds, 3600)
        m, s = divmod(rem, 60)
        self.rec_time_label.set_label(f"{h:02d}:{m:02d}:{s:02d}")
        # Blink dot every tick
        if self.rec_dot.has_css_class("blink"): self.rec_dot.remove_css_class("blink")
        else: self.rec_dot.add_css_class("blink")
        return True

    def _start_recording(self):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        ext, muxer, venc, aenc = CONTAINERS[self.container]
        path = VIDEOS / f"Lens-{ts}.{ext}"
        # Rebuild pipeline for recording
        self.pipeline.set_state(Gst.State.NULL)
        dev = self.cameras[self.cam_idx][0]
        # Clips were silent: there was no audio branch at all. Opus into the
        # same matroska container, dropped if there is no capture source so a
        # machine without a mic still records video.
        self.mic_active = self.mic_available and self.mic_enabled
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
        self.pipeline = Gst.parse_launch(
            f"{self._source_for(dev)} ! tee name=t "
            f"t. ! queue ! videoconvert ! gtk4paintablesink name=sink "
            f"t. ! queue max-size-buffers=8 ! videoconvert ! {venc} ! "
            f"queue ! mux. "
            f"{audio}"
            f"{muxer} name=mux ! filesink location={path}"
        )
        sink = self.pipeline.get_by_name("sink")
        self.paintable = sink.props.paintable
        self.picture.set_paintable(self.paintable)
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
        bus = self.pipeline.get_bus()
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
        if old:
            if self._eos_handler:
                try:
                    old.get_bus().disconnect(self._eos_handler)
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
        self._start_pipeline()
        print(f"Lens: saved video ({self.rec_seconds}s)")
        return False

    def _open_gallery(self, *_):
        folder = VIDEOS if self.video_mode else PICTURES
        Gio.AppInfo.launch_default_for_uri("file://" + str(folder), None)

    def _open_photo_viewer(self, path):
        """Open the gallery at this photo. Clips go to the system player,
        since there is no video playback in here."""
        if is_video(path):
            Gio.AppInfo.launch_default_for_uri("file://" + str(path), None)
            return
        shots = sorted(PICTURES.glob("*.jpg"), key=lambda f: f.stat().st_mtime)
        self.viewer.load(shots, start=path)
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
        candidates = list(PICTURES.glob("*.jpg"))
        for ext in VIDEO_EXTS:
            candidates += list(VIDEOS.glob("*" + ext))
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
        for d in (PICTURES, VIDEOS):
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
        def _trigger():
            self._hold_timer = None
            self._press_is_hold = True
            self._expand()
            return False
        self._hold_timer = GLib.timeout_add(self.HOLD_MS, _trigger)

    def _on_release(self, gesture, n_press, x, y):
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

        Deferred: 'end' can arrive before 'drag-end', so acting immediately
        would tear the hold down before the release had a chance to open the
        card under the finger.
        """
        if self._hold_timer:
            GLib.source_remove(self._hold_timer)
            self._hold_timer = None
        GLib.idle_add(self._abort_hold_now)

    def _abort_hold_now(self):
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
