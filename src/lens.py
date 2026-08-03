#!/usr/bin/env python3
"""
Lens — a fast, mobile-first camera app for Linux tablets.
GTK4 + libadwaita + GStreamer.
"""

import gi, os, sys, math, json, datetime, pathlib
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GdkPixbuf", "2.0")
import cairo
from gi.repository import Gtk, Adw, Gst, GLib, Gdk, Gio, GObject, GdkPixbuf

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
PICTURES.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)


def list_v4l2_cameras():
    """Return [(path, human_name)] for real video capture devices.

    A single physical camera often exposes multiple /dev/videoN nodes
    (capture, metadata, subdev). We keep only nodes that report
    "Video Capture" in their capabilities.
    """
    import subprocess
    devs = []
    seen_cards = set()
    for d in sorted(pathlib.Path("/dev").glob("video*")):
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
        if card in seen_cards:  # one node per physical camera
            continue
        seen_cards.add(card)
        devs.append((str(d), card))
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


class LensWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Lens")
        # Landscape default — works well on laptops. Resizable to portrait for tablets.
        self.set_default_size(960, 640)
        self.set_size_request(320, 400)   # allow shrinking down to phone-ish sizes
        self.set_resizable(True)
        self.pipeline = None
        self.paintable = None
        self.cameras = list_v4l2_cameras() or [("/dev/video0", "Camera")]
        print(f"Lens: detected {len(self.cameras)} camera(s):")
        for c in self.cameras: print(f"   {c}")
        self.recording = False
        self.aspects = ["4:3", "16:9", "1:1"]

        # Restore what was set last time. Loaded before the pipeline starts,
        # since cam_idx decides which device it opens.
        cfg = load_settings()
        self.aspect_idx = cfg.get("aspect_idx", 0)
        if not 0 <= self.aspect_idx < len(self.aspects):
            self.aspect_idx = 0
        self.grid_visible = bool(cfg.get("grid_visible", False))
        self.timer_sec = cfg.get("timer_sec", 0)
        if self.timer_sec not in (0, 3, 10):
            self.timer_sec = 0
        self.video_mode = bool(cfg.get("video_mode", False))
        # Cameras can come and go between runs, so never trust the old index.
        self.cam_idx = cfg.get("cam_idx", 0)
        if not 0 <= self.cam_idx < len(self.cameras):
            self.cam_idx = 0
        self.last_photo = None
        self.countdown_val = 0

        self._build_ui()
        self._apply_settings()
        self._start_pipeline()

    # ---------- pipeline ----------
    def _start_pipeline(self, keep_old=False):
        old_pipeline = self.pipeline
        if self.pipeline and not keep_old:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
            self.pipeline = None
            self.paintable = None
            old_pipeline = None

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
        self.paintable = sink.props.paintable
        self.picture.set_paintable(self.paintable)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_err)
        self.pipeline.set_state(Gst.State.PLAYING)
        if old_pipeline is not None:
            # Only now that the replacement is running and its paintable is
            # attached. Tearing the old one down first left the Picture with
            # no content, which collapsed the layout into the grey band and
            # black gap seen during a flip.
            old_pipeline.set_state(Gst.State.NULL)

    def _source_for(self, dev):
        """v4l2src plus the caps needed to actually get a usable framerate."""
        mode = best_mode(dev)
        if not mode:
            return f"v4l2src device={dev} ! videoconvert"
        fourcc, w, h, fps = mode
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

        # The viewfinder and the things that genuinely belong over the image.
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
        frame_stack = self.frame_stack

        self.aspect_frame = Gtk.AspectFrame.new(0.5, 0.5, 4/3, False)
        self.aspect_frame.add_css_class("viewframe")
        self.aspect_frame.set_child(frame_stack)
        self.aspect_frame.set_hexpand(True); self.aspect_frame.set_vexpand(True)
        bg.append(self.aspect_frame)

        # ---- Top control row ----
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_valign(Gtk.Align.START); top.set_halign(Gtk.Align.CENTER)
        top.set_margin_top(12)
        overlay.add_overlay(top)

        self.btn_grid   = self._pill_button("#",   self._toggle_grid,   width=42, tint=False)
        self.btn_aspect = self._pill_button(self.aspects[0], self._toggle_aspect, width=58, tint=False)
        self.btn_timer  = self._pill_button("⏱",  self._toggle_timer,  width=58, tint=False)
        top.append(self.btn_grid); top.append(self.btn_aspect); top.append(self.btn_timer)

        # Fullscreen toggle — top-right corner
        self.btn_fullscreen = self._pill_button("⛶", self._toggle_fullscreen, width=42, tint=False)
        self.btn_fullscreen.set_halign(Gtk.Align.END)
        self.btn_fullscreen.set_valign(Gtk.Align.START)
        self.btn_fullscreen.set_margin_top(12)
        self.btn_fullscreen.set_margin_end(12)
        overlay.add_overlay(self.btn_fullscreen)

        # Exit — top-left corner
        self.btn_exit = self._pill_button("✕", lambda: self.close(), width=42, tint=False)
        self.btn_exit.set_halign(Gtk.Align.START)
        self.btn_exit.set_valign(Gtk.Align.START)
        self.btn_exit.set_margin_top(12)
        self.btn_exit.set_margin_start(12)
        overlay.add_overlay(self.btn_exit)

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
        self.btn_flip.set_sensitive(len(self.cameras) > 1)
        self.btn_flip.connect("clicked", lambda *_: self._flip_camera())
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
        # Sits over the viewfinder between the top pills and the bottom bar.
        hud = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        # Clear the pill row, which is centred over the top of the picture.
        # At 20px the STBY timer ran straight into the grid pill.
        hud.set_margin_top(58); hud.set_margin_bottom(18)
        hud.set_margin_start(16); hud.set_margin_end(16)
        hud.set_can_target(False)

        hud_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        hud_top.set_valign(Gtk.Align.START)

        # REC / STBY, left
        rec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rec_row.set_halign(Gtk.Align.START); rec_row.set_hexpand(True)
        rec_row.set_valign(Gtk.Align.CENTER)
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
        hud_top.append(rec_row)

        # Battery, right
        bat_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bat_row.set_halign(Gtk.Align.END); bat_row.set_valign(Gtk.Align.CENTER)
        self.bat_gauge = BatteryGauge()
        self.bat_label = Gtk.Label(label="--%")
        self.bat_label.add_css_class("hud-mono")
        self.bat_left_label = Gtk.Label(label="")
        self.bat_left_label.add_css_class("hud-dim")
        bat_row.append(self.bat_gauge)
        bat_row.append(self.bat_label)
        bat_row.append(self.bat_left_label)
        hud_top.append(bat_row)
        hud.append(hud_top)

        spacer = Gtk.Box(); spacer.set_vexpand(True)
        hud.append(spacer)

        hud_bot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        hud_bot.set_valign(Gtk.Align.END)
        self.hud_clock = Gtk.Label(label="")
        self.hud_clock.add_css_class("hud-mono")
        self.hud_clock.set_halign(Gtk.Align.START); self.hud_clock.set_hexpand(True)
        self.hud_format = Gtk.Label(label="MKV  H.264  4Mb/s")
        self.hud_format.add_css_class("hud-dim")
        self.hud_format.set_halign(Gtk.Align.END)
        hud_bot.append(self.hud_clock); hud_bot.append(self.hud_format)
        hud.append(hud_bot)

        self.rec_indicator = hud
        self.rec_indicator.set_visible(False)
        # On the picture, not the window: the readouts belong over the frame
        # you are shooting, not floating in the letterbox bars.
        self.frame_stack.add_overlay(self.rec_indicator)
        self.hud_timer = None
        self._power_samples = []

        # Grid overlay (rule of thirds)

        # Full-screen photo viewer (last overlay so it sits on top of everything)
        self.viewer = Gtk.Overlay()
        self.viewer.add_css_class("viewer")
        self.viewer.set_visible(False)
        viewer_bg = Gtk.Box(); viewer_bg.add_css_class("viewer-bg")
        viewer_bg.set_hexpand(True); viewer_bg.set_vexpand(True)
        self.viewer.set_child(viewer_bg)
        self.viewer_picture = Gtk.Picture()
        self.viewer_picture.set_can_shrink(True)
        self.viewer_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.viewer_picture.set_hexpand(True); self.viewer_picture.set_vexpand(True)
        viewer_bg.append(self.viewer_picture)
        # Back button — top-LEFT so it's clearly "back to camera" not "close app"
        # (the ✕ in the corner already means close-app)
        viewer_close = Gtk.Button(label="← Back")
        viewer_close.add_css_class("pill")
        viewer_close.set_size_request(-1, 42)
        viewer_close.set_halign(Gtk.Align.START); viewer_close.set_valign(Gtk.Align.START)
        viewer_close.set_margin_top(12); viewer_close.set_margin_start(12)
        viewer_close.connect("clicked", lambda *_: self._close_photo_viewer())
        self.viewer.add_overlay(viewer_close)
        # Also close on click anywhere in the viewer backdrop
        viewer_click = Gtk.GestureClick()
        viewer_click.connect("released", lambda *_: self._close_photo_viewer())
        viewer_bg.add_controller(viewer_click)
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
        .state-idle.deck-idx-4 { transform: translate(80px, -20px) rotate(0deg); }
        /* Peek: hovering for a moment tips the deck open a little, as a hint
           that there is a stack under there. Press and hold for the full fan. */
        .state-peek.deck-idx-0 { transform: translate(80px, -20px) rotate(-14deg); }
        .state-peek.deck-idx-1 { transform: translate(80px, -20px) rotate( -7deg); }
        .state-peek.deck-idx-2 { transform: translate(80px, -20px) rotate(  0deg); }
        .state-peek.deck-idx-3 { transform: translate(80px, -20px) rotate(  7deg); }
        .state-peek.deck-idx-4 { transform: translate(80px, -20px) rotate( 14deg); }
        /* Expanded: hand-of-cards fan. Rotation alone only spread the card
           centres over ~56px, about 14px per card, which is far too small to
           aim at with a finger, so each card also slides sideways. The X
           values here MUST stay in step with FAN_DX + FAN_OFFSETS in
           ThumbnailDeck or the hit zones drift away from what is drawn. */
        .state-expanded.deck-idx-0 { transform: translate( 20px, -20px) rotate(-30deg); }
        .state-expanded.deck-idx-1 { transform: translate( 50px, -20px) rotate(-15deg); }
        .state-expanded.deck-idx-2 { transform: translate( 80px, -20px) rotate(  0deg); }
        .state-expanded.deck-idx-3 { transform: translate(110px, -20px) rotate( 15deg); }
        .state-expanded.deck-idx-4 { transform: translate(140px, -20px) rotate( 30deg); }
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
        .state-expanded.deck-idx-0.card-focused { transform: translate( 20px, -190px) rotate(0deg) scale(1.7); }
        .state-expanded.deck-idx-1.card-focused { transform: translate( 50px, -190px) rotate(0deg) scale(1.7); }
        .state-expanded.deck-idx-2.card-focused { transform: translate( 80px, -190px) rotate(0deg) scale(1.7); }
        .state-expanded.deck-idx-3.card-focused { transform: translate(110px, -190px) rotate(0deg) scale(1.7); }
        .state-expanded.deck-idx-4.card-focused { transform: translate(140px, -190px) rotate(0deg) scale(1.7); }
        .state-idle.card-focused {
            transform: translate(80px, -190px) rotate(0deg) scale(1.7);
        }
        .card-focused {
            box-shadow: 0 18px 40px rgba(0,0,0,0.9);
        }
        /* Full-screen photo viewer */
        /* Camera flip: the viewfinder squeezes left-to-right to a vertical
           sliver, the pipeline is swapped while it is invisible, then it
           opens back out. This used to be perspective()+rotateY(), but GTK's
           3D projection over a GStreamer paintable rendered as a squashed
           band with a triangular artifact. A plain 2D scaleX reads as the
           same turn and composites cleanly. */
        .viewflip { transition: transform 140ms cubic-bezier(.45,0,.55,1); }
        .viewflip.flipped { transform: scaleX(0.02); }
        .viewer-bg { background: rgba(0,0,0,0.95); }
        .flip:active    { transform: scale(0.9); }
        .pill:active    { transform: scale(0.9); }
        /* Camcorder HUD. Monospace and a hard shadow so it stays legible
           over any scene, the way a viewfinder overlay has to be. */
        .hud-mono { color: white; font-family: monospace; font-size: 13px;
                    font-weight: bold; letter-spacing: 1px;
                    text-shadow: 0 1px 3px rgba(0,0,0,0.9); }
        .hud-dim  { color: rgba(255,255,255,0.72); font-family: monospace;
                    font-size: 11px; letter-spacing: 0px;
                    text-shadow: 0 1px 3px rgba(0,0,0,0.9); }
        .hud-rec  { color: #ff3b30; font-family: monospace; font-size: 13px;
                    font-weight: bold; letter-spacing: 2px;
                    text-shadow: 0 1px 3px rgba(0,0,0,0.9); }
        .hud-rec.standby { color: rgba(255,255,255,0.75); }
        .thumb { background: rgba(255,255,255,0.2); border-radius: 8px;
                 border: 1px solid white; }
        .flip { background: rgba(0,0,0,0.55); border-radius: 36px;
                color: white; border: none;
                box-shadow: 0 2px 12px rgba(0,0,0,0.55);
                transition: background 140ms ease-out, transform 120ms ease-out; }
        .flip:hover { background: rgba(0,0,0,0.72); }
        .flip:disabled { opacity: 0.35; }
        /* Mirrors horizontally and stays that way, so the icon shows which
           camera you are on rather than just animating and resetting. */
        .flip-icon { transition: transform 260ms cubic-bezier(.45,0,.55,1); }
        .flip-icon.mirrored { transform: scaleX(-1); }
        .pill { background: rgba(0,0,0,0.5); border-radius: 21px; color: white;
                font-weight: bold; font-size: 14px; padding: 4px 12px; border: none; }
        .pill-active { background: #62a0ea; color: white; }
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
    def _apply_settings(self):
        """Push the restored settings into the widgets, without animating."""
        ratio = {"4:3": 4 / 3, "16:9": 16 / 9, "1:1": 1.0}[self.aspects[self.aspect_idx]]
        self.aspect_frame.set_ratio(ratio)
        self.btn_aspect.set_label(self.aspects[self.aspect_idx])

        self.grid_widget.set_visible(self.grid_visible)
        if self.grid_visible:
            self.btn_grid.add_css_class("pill-active")

        if self.timer_sec > 0:
            self.btn_timer.set_label(f"{self.timer_sec}s")
            self.btn_timer.add_css_class("pill-active")

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
            "timer_sec":    self.timer_sec,
            "video_mode":   self.video_mode,
            "cam_idx":      self.cam_idx,
        })

    def _toggle_grid(self):
        self.grid_visible = not self.grid_visible
        self.grid_widget.set_visible(self.grid_visible)
        if self.grid_visible: self.btn_grid.add_css_class("pill-active")
        else: self.btn_grid.remove_css_class("pill-active")
        self._save_settings()

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
        self._save_settings()

    def _toggle_timer(self):
        seq = [0, 3, 10]
        cur = seq.index(self.timer_sec) if self.timer_sec in seq else 0
        self.timer_sec = seq[(cur + 1) % len(seq)]
        if self.timer_sec > 0:
            self.btn_timer.set_label(f"{self.timer_sec}s")
            self.btn_timer.add_css_class("pill-active")
        else:
            self.btn_timer.set_label("⏱")
            self.btn_timer.remove_css_class("pill-active")
        self._save_settings()

    def _set_video_mode(self, video):
        if self.recording: return
        self.video_mode = video
        if video:
            self.btn_video.add_css_class("mode-active")
            self.btn_photo.remove_css_class("mode-active")
            self.shutter_core.add_css_class("video")
            self.rec_indicator.set_visible(True)
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
        self._save_settings()

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

        self.hud_clock.set_label(
            datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

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

    def _toggle_fullscreen(self):
        if self.is_fullscreen(): self.unfullscreen()
        else: self.fullscreen()

    FLIP_MS = 140

    def _flip_camera(self):
        if len(self.cameras) < 2: return
        if self._flipping: return          # ignore taps during the turn
        self._flipping = True
        # Toggle the mirror and leave it there until the next press.
        if self.flip_icon.has_css_class("mirrored"):
            self.flip_icon.remove_css_class("mirrored")
        else:
            self.flip_icon.add_css_class("mirrored")

        # Turn the viewfinder edge-on first, swap behind it, then turn back.
        self.frame_stack.add_css_class("flipped")
        GLib.timeout_add(self.FLIP_MS, self._flip_swap)

    def _flip_swap(self):
        self.cam_idx = (self.cam_idx + 1) % len(self.cameras)
        self._save_settings()
        print(f"Lens: switching to camera {self.cam_idx}: {self.cameras[self.cam_idx]}")
        # The two cameras are separate devices, so the replacement can be
        # brought up while the old one is still feeding the viewfinder. The
        # picture never goes blank and the layout never jumps, which is what
        # was interrupting the transition half way through.
        self._start_pipeline(keep_old=True)
        GLib.timeout_add(40, self._flip_back)
        return False

    def _flip_back(self):
        self.frame_stack.remove_css_class("flipped")
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
            self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
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
            grab.get_state(Gst.CLOCK_TIME_NONE)
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
        path = VIDEOS / f"Lens-{ts}.mkv"
        # Rebuild pipeline for recording
        self.pipeline.set_state(Gst.State.NULL)
        dev = self.cameras[self.cam_idx][0]
        self.pipeline = Gst.parse_launch(
            f"{self._source_for(dev)} ! tee name=t "
            f"t. ! queue ! videoconvert ! gtk4paintablesink name=sink "
            f"t. ! queue ! videoconvert ! x264enc tune=zerolatency bitrate=4000 ! "
            f"matroskamux ! filesink location={path}"
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
        # Send EOS then tear down and restart preview
        self.pipeline.send_event(Gst.Event.new_eos())
        bus = self.pipeline.get_bus()
        bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS)
        self.pipeline.set_state(Gst.State.NULL)
        self.recording = False
        self.shutter_core.remove_css_class("recording")
        # Back to standby rather than hiding the HUD: still in video mode.
        self._hud_standby()
        self._start_pipeline()
        print(f"Lens: saved video ({self.rec_seconds}s)")

    def _open_gallery(self, *_):
        folder = VIDEOS if self.video_mode else PICTURES
        Gio.AppInfo.launch_default_for_uri("file://" + str(folder), None)

    def _open_photo_viewer(self, path):
        """Show a photo full-screen inside Lens. Clips go to the system
        player, since there is no video playback in here."""
        if is_video(path):
            Gio.AppInfo.launch_default_for_uri("file://" + str(path), None)
            return
        try:
            self.viewer_picture.set_filename(path)
        except Exception as e:
            print("viewer load:", e); return
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
                items.append((f.stat().st_mtime, f))
            except OSError:
                continue          # deleted between the glob and the stat
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
    FAN_DX      = 80                        # base translate X
    FAN_OFFSETS = (-60, -30, 0, 30, 60)     # per-card slide, added to FAN_DX
    # Inside the fan the nearest card always wins, so there are no dead
    # zones between cards. Focus is only dropped once the finger is clearly
    # past either end, which needs to be a generous distance or overshooting
    # the last card feels like the deck snatches itself away.
    FAN_EDGE   = 110  # horizontal px past the outermost card centre
    FAN_SLOP_Y = 160  # vertical px above/below the widget

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
        if new_paths == self.card_paths and self.cards:
            return                      # nothing actually changed
        if self.expanded:
            # Rebuilding now would destroy the cards under the finger. Hold
            # it until the fan closes.
            self._pending_photos = list(paths)
            return
        self._pending_photos = None
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
                    if self.card_paths and self.on_card_click:
                        self.on_card_click(self.card_paths[-1])
                    return False
                self._click_timer = GLib.timeout_add(self.DBLCLK_MS, _fire_single)
            return
        # Otherwise we were in hold/fan mode — open the focused card
        self._finish_hold()

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
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_line_width(1)
        for i in (1, 2):
            cr.move_to(w * i / 3, 0); cr.line_to(w * i / 3, h); cr.stroke()
            cr.move_to(0, h * i / 3); cr.line_to(w, h * i / 3); cr.stroke()


class LensApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = LensWindow(self)
        win.present()


if __name__ == "__main__":
    sys.exit(LensApp().run(sys.argv))
